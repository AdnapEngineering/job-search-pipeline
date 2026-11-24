"""Stage 3: deterministic evaluation of enriched jobs.

Rejects postings whose title, location, JD logistics (onsite/hybrid/residency),
requirements (excluded skills, clearance, language, years of experience) or
focus rule them out, then scores the rest from config-driven keyword weights.
"""
import hashlib
import json
import os
import re
import sys
from typing import NamedTuple
from urllib.parse import urlparse

from job_search_pipeline.blocklist import role_key
from job_search_pipeline.config import (
    CFG,
    atomic_write_json,
    compile_alternation,
    get_path,
    term_regex,
)
from job_search_pipeline.sources.filter import (
    FOREIGN_CITY_RE,
    FOREIGN_PLACES,
    FOREIGN_RE,
    STRONG_TARGET_RE,
    UNSPECIFIED_LOCATIONS,
    US_PATTERN,
    is_local_metro,
    is_recent,
    names_foreign_place,
    title_reject_reason,
)
from job_search_pipeline.urls import is_aggregator_url

_SKILLS_CFG = CFG["skills_and_scoring"]
_TITLES_CFG = CFG["titles"]
_EXP_CFG = CFG["experience"]
_LOC_CFG = CFG["location"]

CORE_STRENGTHS = [s.lower() for s in (_SKILLS_CFG.get("core_strengths") or [])]
HARD_EXCLUDE_SKILLS = [s.lower() for s in (_SKILLS_CFG.get("hard_exclude_skills") or [])]
AVOID_TECH = [s.lower() for s in (_SKILLS_CFG.get("avoid_keywords") or [])]
PENALIZED_KEYWORDS = {k.lower(): int(v) for k, v in (_SKILLS_CFG.get("penalized_keywords") or {}).items()}
FOCUS_KEYWORDS = {k.lower(): int(v) for k, v in (_SKILLS_CFG.get("focus_keywords") or {}).items()}
# Synonyms that name one concept ("accessibility" / "wcag"): a group earns its best member's weight once
_FOCUS_GROUP_OF = {
    kw.lower(): i
    for i, group in enumerate(_SKILLS_CFG.get("focus_keyword_groups") or [])
    for kw in group
}
CONDITIONAL_REQUIRE = _SKILLS_CFG.get("conditional_require_any") or {}
_CONDITIONAL_IF_PRESENT = [s.lower() for s in (CONDITIONAL_REQUIRE.get("if_present") or [])]
_CONDITIONAL_REQUIRE_ANY = [s.lower() for s in (CONDITIONAL_REQUIRE.get("require_any_of") or [])]

_MISSING_COMPANY_VALUES = {'', 'nan', 'none', 'null', 'unknown'}
_MARKDOWN_NOISE_RE = re.compile(r'[\\*_`]')


def strip_markdown(text):
    """Drop markdown emphasis and escape characters that split phrases the regexes look for."""
    return _MARKDOWN_NOISE_RE.sub('', text)


_term_re = term_regex


_ALL_TERMS = set(
    CORE_STRENGTHS
    + HARD_EXCLUDE_SKILLS
    + AVOID_TECH
    + list(PENALIZED_KEYWORDS.keys())
    + _CONDITIONAL_IF_PRESENT
    + _CONDITIONAL_REQUIRE_ANY
    + [s.lower() for s in (_TITLES_CFG.get("generic_role_required_skills") or [])]
)
_TERM_RES = {t: _term_re(t) for t in _ALL_TERMS}
_TERM_RES.update({kw: _term_re(kw, allow_suffix=True) for kw in FOCUS_KEYWORDS})


def _term(term):
    pat = _TERM_RES.get(term)
    if pat is None:
        pat = _TERM_RES[term] = _term_re(term)
    return pat


def _has(term, text):
    return bool(_term(term).search(text))


def _mentions(term, stack, text):
    """True when the term was detected in the scraped stack or appears as a whole term in text."""
    return term in stack or _has(term, text)


_SENIOR_PATTERN = compile_alternation(
    _TITLES_CFG["exclude_seniority_patterns"],
    word_bounded=True
)

_JD_LOCAL_METRO_PATTERN = _LOC_CFG["jd_local_metro_pattern"]
_JD_LOCAL_METRO_RE = re.compile(_JD_LOCAL_METRO_PATTERN, re.IGNORECASE) if _JD_LOCAL_METRO_PATTERN else None

_US_TOKEN = _LOC_CFG["country_token_pattern"]
JD_US_REMOTE_RE = re.compile(
    r'(?i:remote(?:ly)?)[^.\n]{0,50}\b' + _US_TOKEN + r'\b'
    r'|\b' + _US_TOKEN + r'\b[- ,(\]]{0,3}(?i:(?:based[- ])?remote)'
    r'|(?i:anywhere in the (?:u\.?s\.?|united states))'
)
_JD_GENERIC_REMOTE_RE = re.compile(
    r'\b(?:fully|100%|completely|entirely) remote\b|\bremote[- ]first\b|\bremote(?:ly)?\b', re.IGNORECASE
)
# Foreign residency wording ("remote within Canada", "based in Australia"): places from location.foreign_places
_FOREIGN_PLACES = '|'.join(FOREIGN_PLACES)
_JD_FOREIGN_REMOTE_RE = re.compile(
    r'\b(?:remote(?:ly)?|based)\s+(?:(?:with)?in|from)\s+(?:the\s+)?(?:' + _FOREIGN_PLACES + r')\b'
    r'|\banywhere (?:with)?in\s+(?:the\s+)?(?:' + _FOREIGN_PLACES + r')\b'
    r'|\b(?:located|based|reside|residing|living)\s+in\s+(?:[\w .]{0,30},\s*)?(?:the\s+)?(?:'
    + _FOREIGN_PLACES + r')\b', re.IGNORECASE
)
JD_HYBRID_ONSITE_RE = re.compile(
    r'\bhybrid (?:work(?:place)? )?(?:schedule|role|position|model|setting|environment|flexibility|arrangement)'
    r'|\blocal to (?:our|the) [\w .,]{1,40} office'
    r'|\b\d+ days? (?:a |per )?(?:week )?(?:in|on)[- ]?(?:site|office|person)'
    r'|\bon[- ]?site (?:role|position|requirement|required)|\bmust (?:be able to )?(?:commute|relocate)'
    r'|\bin[- ]office (?:role|position)', re.IGNORECASE
)


# "unable to consider applications from candidates based in France": an exclusion, not a requirement
_FOREIGN_EXCLUSION_BEFORE_RE = re.compile(
    r"\b(?:unable to|cannot|can't|can not|won't|will not|do not|don't|not able to)\b[^.\n]{0,60}$"
    r"|\b(?:except|excluding|other than|outside(?: of)?)\b[^.\n]{0,30}$", re.IGNORECASE
)


def foreign_remote_match(text):
    """First foreign-only remote/residency phrase that is not stated as an exclusion, or None."""
    for m in _JD_FOREIGN_REMOTE_RE.finditer(text):
        if not _FOREIGN_EXCLUSION_BEFORE_RE.search(text[max(0, m.start() - 90):m.start()]):
            return m
    return None


def jd_location_ok(text):
    """For jobs with no feed location: accept only if the JD itself says eligible remote or local metro."""
    if (_JD_LOCAL_METRO_RE and _JD_LOCAL_METRO_RE.search(text)) or JD_US_REMOTE_RE.search(text):
        return True
    if foreign_remote_match(text) or JD_HYBRID_ONSITE_RE.search(text):
        return False
    return bool(_JD_GENERIC_REMOTE_RE.search(text))


_N = r'(?:\d|one|two|three|four|five)'
_DAYS_A_WEEK = _N + r'(?:\s*(?:-|to)\s*' + _N + r')?\s+(?:full\s+)?days?\s+(?:a|per|each|every)\s+week'
_IN_PLACE = r'(?:on[- ]?site|in[- ]office|in[- ]person|in the office|at (?:the|our) office)'
_JD_HARD_ONSITE_RE = re.compile(
    r'\b(?:100\s?%|fully|entirely|exclusively)\s+' + _IN_PLACE +
    r'|\brequires? (?:you |employees )?to (?:be|work) ' + _IN_PLACE +
    r"|\b(?:remote|hybrid)(?: (?:and|or|and/or) (?:remote|hybrid))? (?:work )?(?:will not|won't|cannot|can't|is not|isn't)"
    r' (?:be )?(?:considered|available|an option|offered|possible|supported)'
    r'|\bno remote\b|\bnot (?:a remote|eligible for remote|open to remote|remote[- ]eligible)\b'
    r'|\bmust (?:be able to )?(?:commute|relocate)\b'
    r'|\bmust (?:reside|live) within (?:approximately |about |roughly )?(?:an? )?(?:\d|commut|driving|reasonable)'
    r'|\brelocation (?:to [\w ,]{2,30} )?is required\b', re.IGNORECASE
)
_JD_SOFT_ONSITE_RE = re.compile(
    r'\b' + _IN_PLACE + r'\s+(?:role|position|job|requirement|only|presence is required)\b'
    r'|\b(?:this|the) (?:role|position|job) (?:is|will be) (?:an? )?(?:fully )?(?:on[- ]?site|in[- ]office|in[- ]person|hybrid)\b'
    r'|\b' + _DAYS_A_WEEK + r'\s+(?:in|at|from|on)[- ](?:the |our |an? )?(?:office|site|hq|headquarters|studio|campus)'
    r'|\b' + _IN_PLACE + r',?\s+(?:at least\s+)?' + _DAYS_A_WEEK +
    r'|\bhybrid (?:work(?:place)? |office |in-office )?(?:schedule|role|position|model|arrangement|approach|policy|requirement)\b',
    re.IGNORECASE
)
_JD_OPTIONS_RE = re.compile(
    r'\bor (?:fully |100% )?remote(?:ly)?\b|\bremote(?:ly)?,? (?:or|and) (?:hybrid|on-?site)'
    r'|\bremote,\s*hybrid\b', re.IGNORECASE
)
_JD_NEGATED_BEFORE_RE = re.compile(r"\b(?:not|no|never|isn't|without)\s+(?:an?\s+|the\s+)?$", re.IGNORECASE)
_JD_OPTIONAL_RE = re.compile(r'\boptional\b', re.IGNORECASE)
_INDEED_WORK_LOCATION_RE = re.compile(r'\bwork location:\s*((?:in person|hybrid remote in|on the road)[^\n]{0,50})', re.IGNORECASE)
_JD_LOCATION_LINE_RE = re.compile(r'\blocations?\s*:\s*([^\n|]{2,60})', re.IGNORECASE)
_OFFICE_LOCATIONS_RE = re.compile(r'\b(?:office|offices|other|our|hub|headquarters)\s*$', re.IGNORECASE)
_LOCATION_LINE_END_RE = re.compile(r'\s{2,}|\s+(?:type|job type|department|team|employment|salary|compensation|about|reports)\b', re.IGNORECASE)
# A location line naming hybrid/onsite work ("Location: Durham, NC (Hybrid - 2 weeks onsite)"), unless remote is
# offered first or as an alternative ("Remote (US) or hybrid in NYC")
_LOCATION_ARRANGEMENT_RE = re.compile(r'\b(?:hybrid|on[- ]?site|in[- ]office|in[- ]person)\b')


def _onsite_passage(text, patterns):
    """First passage matching one of `patterns` that is not negated, optional, or one of several options."""
    for pattern in patterns:
        for m in pattern.finditer(text):
            window = text[max(0, m.start() - 80):m.end() + 80]
            if (_JD_OPTIONS_RE.search(window) or _JD_OPTIONAL_RE.search(window)
                    or _JD_NEGATED_BEFORE_RE.search(text[max(0, m.start() - 15):m.start()])):
                continue
            return text[max(0, m.start() - 40):m.end() + 40].replace('\n', ' ').strip()
    return ''


def hard_onsite_passage(text):
    """JD passage that flatly requires onsite work or relocation ('' when there is none). Safe to drop on
    without reading further, unlike hybrid-policy or office-perk wording, which needs the full stage-3 check."""
    return _onsite_passage(strip_markdown(text or ''), (_JD_HARD_ONSITE_RE,))


_US_STATES = {
    'al': 'alabama', 'ak': 'alaska', 'az': 'arizona', 'ar': 'arkansas', 'ca': 'california', 'co': 'colorado',
    'ct': 'connecticut', 'de': 'delaware', 'fl': 'florida', 'ga': 'georgia', 'hi': 'hawaii', 'id': 'idaho',
    'il': 'illinois', 'in': 'indiana', 'ia': 'iowa', 'ks': 'kansas', 'ky': 'kentucky', 'la': 'louisiana',
    'me': 'maine', 'md': 'maryland', 'ma': 'massachusetts', 'mi': 'michigan', 'mn': 'minnesota',
    'ms': 'mississippi', 'mo': 'missouri', 'mt': 'montana', 'ne': 'nebraska', 'nv': 'nevada',
    'nh': 'new hampshire', 'nj': 'new jersey', 'nm': 'new mexico', 'ny': 'new york', 'nc': 'north carolina',
    'nd': 'north dakota', 'oh': 'ohio', 'ok': 'oklahoma', 'or': 'oregon', 'pa': 'pennsylvania',
    'ri': 'rhode island', 'sc': 'south carolina', 'sd': 'south dakota', 'tn': 'tennessee', 'tx': 'texas',
    'ut': 'utah', 'vt': 'vermont', 'va': 'virginia', 'wa': 'washington', 'wv': 'west virginia',
    'wi': 'wisconsin', 'wy': 'wyoming', 'dc': 'district of columbia',
}
_STATE_NAME_TO_ABBR = {name: abbr for abbr, name in _US_STATES.items()}
_STATE_NAMES = '|'.join(sorted(_STATE_NAME_TO_ABBR, key=len, reverse=True))
# Abbreviations are matched upper-case only: "in", "or", "me", "ok" and "hi" are words too
_STATE_TOKEN = r'(?:(?i:' + _STATE_NAMES + r')|' + '|'.join(a.upper() for a in _US_STATES) + r')(?![\w.])'
_STATE_LIST = _STATE_TOKEN + r'(?:\s*(?:,|/|&|,?\s*(?i:or|and))\s*' + _STATE_TOKEN + r')*'
_STATE_TOKEN_RE = re.compile(_STATE_TOKEN)
# The candidate's own state(s) and time zone(s). Both rules below are off until these are configured.
_HOME_STATES = {_STATE_NAME_TO_ABBR.get(s.lower(), s.lower()) for s in (_LOC_CFG.get("home_states") or [])}
_HOME_TIMEZONES = {t.lower() for t in (_LOC_CFG.get("home_timezones") or [])}
# "Remote - CA", "Remote in Texas", "(Remote, NC only)" in a title or a feed location
_REMOTE_STATE_RE = re.compile(r'(?i:\bremote)\s*(?:(?i:in|within)\s+|[-–—:,]\s*)(' + _STATE_LIST + r')\s*(?:(?i:only)\b|[)\]]|$)')
# "must reside in MA, IN, or TX", "candidates must be located in one of the following states: ..."
_RESIDENCY_STATES_RE = re.compile(
    r'(?i:\b(?:must|required to|need to|needs to|have to|only (?:hire|consider)s? candidates (?:who|that)))'
    r'[^.\n]{0,40}?(?i:\b(?:resid\w+|live|living|located|based)\s+(?:with)?in)\s+'
    r'(?i:(?:one of )?(?:the )?(?:following )?(?:states?|locations?)\s*(?:of\s+)?:?\s*)?(' + _STATE_LIST + r')'
)
_TZ_ABBR_TO_NAME = {
    'est': 'eastern', 'edt': 'eastern', 'et': 'eastern',
    'cst': 'central', 'cdt': 'central', 'ct': 'central',
    'mst': 'mountain', 'mdt': 'mountain', 'mt': 'mountain',
    'pst': 'pacific', 'pdt': 'pacific', 'pt': 'pacific',
}
# Abbreviations match upper case only, so "pt" or "mt" inside ordinary text is not read as a zone
_TZ_ABBRS = r'(?-i:(?:' + '|'.join(sorted((a.upper() for a in _TZ_ABBR_TO_NAME), key=len, reverse=True)) + r')\b)'
_TZ_NAMES = r'(?:eastern|central|mountain|pacific|' + _TZ_ABBRS + r')'
_TZ_LIST = _TZ_NAMES + r'(?:\s*(?:,|/|,?\s*(?:or|and))\s*' + _TZ_NAMES + r')*'
# Residency only: "must be located in the Eastern or Central time zone", "remote restricted to Central or
# Eastern time", "must be based in EST", "restricted to the ET or CT time zones". Working-hours wording
# ("candidates from the US only (EST time zone)", "work EST hours") is left alone: it sets hours, not residence.
_RESIDENCY_TZ_RE = re.compile(
    r'\b(?:(?:located|based|resid\w+|live|living)\s+(?:with)?in|restricted to|limited to)\s+(?:the\s+)?'
    r'(?:(' + _TZ_LIST + r')\s+(?:standard\s+)?time(?:\s*zones?)?\b'
    r'|(' + _TZ_ABBRS + r'(?:\s*(?:,|/|,?\s*(?:or|and))\s*' + _TZ_ABBRS + r')*)(?!\s+(?:hours|business))\b)', re.IGNORECASE
)
_TZ_NAME_RE = re.compile(_TZ_NAMES, re.IGNORECASE)


def _tz_zones(listed):
    """Canonical zone names ('eastern', ...) from a matched list of zone names and abbreviations."""
    return {_TZ_ABBR_TO_NAME.get(z.lower(), z.lower()) for z in _TZ_NAME_RE.findall(listed)}


_TZ_CONNECTOR = r'\s*(?:,|/|&|,?\s*(?:or|and))\s*'
# MT and CT are also Montana and Connecticut. A state-code match followed by "time (zone)" or listed with a
# zone-only abbreviation ("MT or PT") is a time zone list, so the state rule leaves it to the time zone rule.
_TZ_ONLY_ABBRS = r'(?-i:(?:' + '|'.join(sorted((a.upper() for a in _TZ_ABBR_TO_NAME if a not in _US_STATES),
                                               key=len, reverse=True)) + r')\b)'
_TZ_AFTER_STATES_RE = re.compile(
    r'(?:\s+(?:standard\s+)?time\b|(?:' + _TZ_CONNECTOR + r'[A-Z]{2}\b)*' + _TZ_CONNECTOR + _TZ_ONLY_ABBRS + r')', re.IGNORECASE
)
# Further zones after a time zone match: "in MT time zone or PT time zone", "in the Eastern time zone or Pacific"
_TZ_CONTINUATION_RE = re.compile(
    _TZ_CONNECTOR + r'(?:the\s+)?(' + _TZ_NAMES + r')(?:\s+(?:standard\s+)?time(?:\s*zones?)?\b)?', re.IGNORECASE
)


def _residency_tz_zones(text, m):
    """Zones named by a time zone residency match, plus any listed right after it."""
    zones, end = _tz_zones(m.group(1) or m.group(2)), m.end()
    while (more := _TZ_CONTINUATION_RE.match(text, end)):
        zones |= _tz_zones(more.group(1))
        end = more.end()
    return zones
_REMOTE_ALTERNATIVE_RE = re.compile(r'\bremote(?:ly)?\b|\b(?:anywhere|elsewhere)\b', re.IGNORECASE)


def _other_states_only(state_list):
    """True when a matched list of states names none of the candidate's home states."""
    states = {_STATE_NAME_TO_ABBR.get(s.lower(), s.lower()) for s in _STATE_TOKEN_RE.findall(state_list)}
    return bool(states) and not (states & _HOME_STATES)


def remote_restriction(job, text):
    """Wording that limits a remote role to states or time zones the candidate is not in, or ''.
    These are stated outright in the title, feed location or JD, so they need no AI review."""
    if _HOME_STATES:
        fields = [job.get('title') or '']
        # LinkedIn/Indeed locations are rewritten by sources/linkedin_indeed.py ("Remote (City, ST)"), not a restriction
        if (job.get('ats') or '').lower() not in ('linkedin', 'indeed'):
            fields.append(job.get('location') or '')
        for field in fields:
            m = _REMOTE_STATE_RE.search(field)
            if m and _other_states_only(m.group(1)):
                return m.group(0).strip(' )]')
        for m in _RESIDENCY_STATES_RE.finditer(text):
            # "...located in Texas or work remotely" offers a way out; leave those to the AI review
            if _TZ_AFTER_STATES_RE.match(text, m.end()):
                continue
            if _other_states_only(m.group(1)) and not _REMOTE_ALTERNATIVE_RE.search(text[m.end():m.end() + 60]):
                return m.group(0).replace('\n', ' ').strip()[:140]
    if _HOME_TIMEZONES:
        for m in _RESIDENCY_TZ_RE.finditer(text):
            zones = _residency_tz_zones(text, m)
            if zones and not (zones & _HOME_TIMEZONES):
                return m.group(0).replace('\n', ' ').strip()
    return ''


def jd_logistics_block(job):
    """Return the JD passage that rules the job out on location/work-arrangement, or '' when eligible."""
    loc = job.get('location') or ''
    if is_local_metro(loc):
        return ''
    text = strip_markdown(job.get('description_full') or job.get('description_ai') or '')

    m = _INDEED_WORK_LOCATION_RE.search(text)
    if m and not is_local_metro(m.group(1)):
        return m.group(0).strip()

    us_remote_stated = bool(JD_US_REMOTE_RE.search(text))
    for m in _JD_LOCATION_LINE_RE.finditer(text):
        if us_remote_stated or _OFFICE_LOCATIONS_RE.search(text[max(0, m.start() - 12):m.start()]):
            continue
        value = _LOCATION_LINE_END_RE.split(m.group(1))[0].lower()
        foreign = FOREIGN_RE.search(value) or FOREIGN_CITY_RE.search(value)
        if foreign and not (US_PATTERN.search(value) or 'anywhere' in value or is_local_metro(value)):
            return m.group(0).strip()[:120]
        arrangement = _LOCATION_ARRANGEMENT_RE.search(value)
        if (arrangement and not is_local_metro(value) and 'remote' not in value[:arrangement.start()]
                and not _JD_OPTIONS_RE.search(value)):
            return m.group(0).strip()[:120]

    if _JD_LOCAL_METRO_RE and _JD_LOCAL_METRO_RE.search(text):
        return ''

    inferred = (job.get('ats') or '').lower() in ('linkedin', 'indeed')
    trusted_remote = not inferred and (job.get('remote') is True or 'remote' in loc.lower())
    patterns = (_JD_HARD_ONSITE_RE,) if trusted_remote else (_JD_HARD_ONSITE_RE, _JD_SOFT_ONSITE_RE)
    passage = _onsite_passage(text, patterns)
    if passage:
        return passage

    m = foreign_remote_match(text)
    if m and not JD_US_REMOTE_RE.search(text):
        return m.group(0)
    return remote_restriction(job, text)


_PRIMARY_ROLE_GROUPS = [
    {
        "label": g.get("label", "Target Role"),
        "re": re.compile(g["pattern"], re.IGNORECASE),
        "points": int(g.get("points", 20)),
    }
    for g in (_TITLES_CFG.get("primary_role_groups") or [])
    if g.get("pattern")
]

_FAILED_SCRAPE_PATTERN = re.compile(r'^(no description extracted|error\b)', re.IGNORECASE)
GENERIC_TITLE_BODY_SIGNALS = int(_TITLES_CFG["generic_body_signal_min"])
_GENERIC_ROLE_PATTERN = compile_alternation(
    _TITLES_CFG["generic_role_patterns"],
    word_bounded=True
)
_GENERIC_REQUIRED_SKILLS = [s.lower() for s in (_TITLES_CFG.get("generic_role_required_skills") or [])]
_GENERIC_BODY_SIGNAL_RES = [
    re.compile(p, re.IGNORECASE) for p in (_TITLES_CFG.get("generic_body_signal_patterns") or []) if p
]

_REJECT_CLEARANCE = bool(_EXP_CFG["reject_clearance"])
_AVOID_KEYWORDS_PATTERN = re.compile(
    r'\b(public trust|security clearance|clearance required|active clearance|ts/sci|polygraph'
    r'|(?:top )?secret clearance|(?:dod|government|federal) clearance'
    r'|(?:obtain|maintain|hold)(?: and maintain)? (?:an? )?(?:active )?(?:u\.?s\.? )?(?:government |security |secret )?clearance)\b'
)
_CLEARANCE_NEGATION_RE = re.compile(r"\b(?:no|not|without|doesn't|does not)\b[^.\n]{0,25}$")
# Matched clearance wording to let through, e.g. ["public trust"] (a background check, not a clearance)
_CLEARANCE_IGNORE_TERMS = {t.lower() for t in (_EXP_CFG.get("clearance_ignore_terms") or [])}


def _clearance_hit(text):
    """The clearance wording that rules the job out, or ''."""
    if not _REJECT_CLEARANCE:
        return ''
    for m in _AVOID_KEYWORDS_PATTERN.finditer(text):
        if (m.group(0) not in _CLEARANCE_IGNORE_TERMS
                and not _CLEARANCE_NEGATION_RE.search(text[max(0, m.start() - 40):m.start()])
                and not _in_soft_section(text, m.start())):  # "Ways to stand out: active clearance"
            return m.group(0)
    return ''


def _requires_clearance(text):
    return bool(_clearance_hit(text))


_REJECT_FOREIGN_LANG = bool(_EXP_CFG["reject_foreign_language_req"])
_LANGS = r'(?:spanish|portuguese|french|german|polish|italian|dutch|mandarin|cantonese|japanese|korean|hebrew|arabic)'
_LANGUAGE_REQ_RE = re.compile(
    r'\b' + _LANGS + r'\b[^.\n]{0,40}\b(?:required|native|fluent|fluency|conversational|proficien)'
    r'|\b(?:fluent|fluency|native|bilingual|proficien\w*)\b[^.\n]{0,30}\b' + _LANGS + r'\b'
)

_SPECIALTY_REJECT_RULES = []
for rule in (_SKILLS_CFG.get("specialty_reject_rules") or []):
    _SPECIALTY_REJECT_RULES.append({
        "title_re": re.compile(rule["title_pattern"], re.IGNORECASE) if rule.get("title_pattern") else None,
        "yoe_re": re.compile(rule["jd_yoe_pattern"], re.IGNORECASE) if rule.get("jd_yoe_pattern") else None,
        "term_re": re.compile(rule["jd_term_pattern"], re.IGNORECASE) if rule.get("jd_term_pattern") else None,
        "min_mentions": int(rule.get("min_mentions", 3)),
    })

_SENIOR_ROLE_YOE_RE = re.compile(r'\b(\d{1,2})\s*\+?\s*(?:years|yrs)\b[^.\n]{0,25}\b(?:senior|lead|staff)[- ]level\b')
MAX_SENIOR_ROLE_YEARS = int(_EXP_CFG["max_senior_role_years"])

_ALT_LANG_RE = re.compile(r'\b(or|and/or)\b|/')
_ACCEPTED_ALT_SKILLS = _SKILLS_CFG.get("accepted_alternative_skills") or CORE_STRENGTHS
_ACCEPTED_LANG_RE = compile_alternation([re.escape(s) for s in _ACCEPTED_ALT_SKILLS], word_bounded=True)
_SOFT_ASK_RE = re.compile(
    r'\b(familiarity|exposure|nice to have|nice-to-have|bonus|a plus|preferred|is a plus|desirable|desired|ideally|even better|good to have|optional'
    r'|(?:desire|willingness|willing|eager|interest|open) (?:to learn|in learning))\b'
)
_SOFT_SECTION_RE = re.compile(
    r'(?:nice[- ]to[- ]have|preferred|bonus|even better|desirable|desired|good to have|optional|additional qualifications'
    r'|what will set you apart|stand out)[^\n]{0,40}\n'
)
_REQ_SECTION_RE = re.compile(
    r'(?:required|must[- ]have|minimum|basic qualifications|what you(?:\x27ll|\u2019ll| will) need)[^\n]{0,40}\n'
)
SOFT_SKILL_PENALTY = int(_SKILLS_CFG["soft_skill_penalty"])
SOFT_SKILL_PENALTY_CAP = int(_SKILLS_CFG["soft_skill_penalty_cap"])


def _in_soft_section(desc, pos):
    preceding = desc[max(0, pos - 350):pos]
    soft_matches = list(_SOFT_SECTION_RE.finditer(preceding))
    if not soft_matches:
        return False
    last_soft_end = soft_matches[-1].end()
    return not _REQ_SECTION_RE.search(preceding[last_soft_end:])


def _match_window(text, m):
    return text[max(0, m.start() - 80):m.end() + 60]


def _is_soft_ask(text, m):
    """True when the match sits near 'nice to have'-style wording or inside a preferred section."""
    return bool(_SOFT_ASK_RE.search(_match_window(text, m))) or _in_soft_section(text, m.start())


def _has_hard_ask(pattern, text):
    """True when pattern matches at least once outside a soft-ask window or preferred section."""
    return any(not _is_soft_ask(text, m) for m in pattern.finditer(text))


_NUM = r'(\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen)'
_YEARS = (_NUM + r'\s*(?:\+|plus|or more)?\s*(?:(?:-|–|—|to)\s*\d{1,2}\s*\+?\s*)?(?:years|yrs)\b')
_YOE_AFTER_RE = re.compile(r'\b' + _YEARS + r'(?!\s+ago)(?=[^.;\n]{0,60}experience)')
_YOE_BEFORE_RE = re.compile(r'(?:experience[^.\n]{0,25}?|at least\s+|minimum(?: of)?\s+)' + r'\b' + _YEARS)
_YOE_FIELD_TERMS = _EXP_CFG["yoe_field_terms_pattern"]
_YOE_IN_FIELD_RE = re.compile(
    r'\b' + _NUM + r'\s*(?:\+|plus)\s*(?:years|yrs)\s+(?:in|of|as an?|building|writing)\s+(?:[\w/-]+\s+){0,2}?'
    + _YOE_FIELD_TERMS
)
_WORD_NUMBERS = {
    'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5, 'six': 6, 'seven': 7, 'eight': 8,
    'nine': 9, 'ten': 10, 'eleven': 11, 'twelve': 12, 'fifteen': 15
}

MAX_MIN_YOE = int(_EXP_CFG["max_min_yoe"])
MAX_MIN_SECONDARY_YOE = int(_EXP_CFG["max_min_secondary_yoe"])
TOP_YOE_PENALTY = int(_EXP_CFG["top_yoe_penalty"])

BASE_SCORE = int(_SKILLS_CFG["base_score"])
CORE_STRENGTH_POINTS = int(_SKILLS_CFG["core_strength_points"])
# Per-skill overrides of core_strength_points, so the primary stack outweighs peripheral skills
CORE_STRENGTH_WEIGHTS = {k.lower(): int(v) for k, v in (_SKILLS_CFG.get("core_strength_weights") or {}).items()}
CORE_STRENGTHS_CAP = int(_SKILLS_CFG["core_strengths_cap"])
DOMAIN_SCORE_CAP = int(_SKILLS_CFG["domain_score_cap"])
STRONG_FLOOR = int(_SKILLS_CFG["strong_floor"])
SECONDARY_FLOOR = int(_SKILLS_CFG["secondary_floor"])

_SECONDARY_YOE_CTX = _EXP_CFG["secondary_yoe_context_pattern"]
_SECONDARY_YOE_CONTEXT_RE = re.compile(_SECONDARY_YOE_CTX, re.IGNORECASE) if _SECONDARY_YOE_CTX else None
_PRIMARY_YOE_CTX = _EXP_CFG["primary_yoe_context_pattern"]
_PRIMARY_YOE_CONTEXT_RE = re.compile(_PRIMARY_YOE_CTX, re.IGNORECASE) if _PRIMARY_YOE_CTX else None

_DEGREE_EQUIV_RE = re.compile(r'degree[^\n]{0,45}\b(?:or|equivalent)\b')


# "5-8+ years": the number a pattern captured may be the top of a range; the minimum is the bottom
_RANGE_LOW_BEFORE_RE = re.compile(r'(\d{1,2})\s*\+?\s*(?:-|–|—|to)\s*$')
# "5+ years for senior, 8+ years for staff" / "5+ years (lead consultant)": a figure for another level
_OTHER_LEVEL_AFTER_RE = re.compile(
    r'^\s*(?:of (?:[\w-]+ ){0,3}experience\s*)?(?:for|\(|as|at)\s*(?:an? |the )?'
    r'(?:staff|principal|lead|distinguished|architect|manager|director)\b'
)
# "(ideally 8+ years)", "preferably 10 years": a wish, not a minimum
_SOFT_YEARS_BEFORE_RE = re.compile(r'\b(?:ideally|preferably|preferred|bonus|a plus|nice to have)\b[\s:,(]*(?:with\s+)?$')


def _match_years(m, text=None):
    """Minimum years a YoE match asks for. Given the text, a range ("5-8+ years") counts as its bottom."""
    n = m.group(1)
    years = int(n) if n.isdigit() else _WORD_NUMBERS[n]
    if text is not None:
        low = _RANGE_LOW_BEFORE_RE.search(text[max(0, m.start(1) - 8):m.start(1)])
        if low:
            years = min(years, int(low.group(1)))
    return years


def _yoe_matches(text):
    """YoE asks that bind this role: skips degree-equivalency boilerplate, figures quoted for another
    level, and soft asks ("ideally 8+ years", or anything under a nice-to-have heading)."""
    raw = (list(_YOE_AFTER_RE.finditer(text)) + list(_YOE_BEFORE_RE.finditer(text))
           + list(_YOE_IN_FIELD_RE.finditer(text)))
    kept = []
    for m in raw:
        start = m.start(1)  # the number itself; _YOE_BEFORE_RE matches start earlier, at "experience"
        if _DEGREE_EQUIV_RE.search(text[max(0, m.start() - 60):m.start()]):
            continue
        if _OTHER_LEVEL_AFTER_RE.search(text[m.end():m.end() + 50]):
            continue
        if _SOFT_YEARS_BEFORE_RE.search(text[max(0, start - 30):start]) or _in_soft_section(text, start):
            continue
        kept.append(m)
    return kept


def _high_yoe(text):
    """('max_min_yoe' | 'max_min_secondary_yoe', detail) for the first years-of-experience ask over its limit,
    or None."""
    text = strip_markdown(text)
    for m in _yoe_matches(text):
        years = _match_years(m, text)
        phrase = m.group(0).strip()
        if years > MAX_MIN_YOE:
            return "max_min_yoe", f'"{phrase}": {years} years > max {MAX_MIN_YOE}'
        if _SECONDARY_YOE_CONTEXT_RE:
            context = re.split(r'[\n.;•]', text[m.end():m.end() + 100])[0]
            if (years > MAX_MIN_SECONDARY_YOE
                    and _SECONDARY_YOE_CONTEXT_RE.search(context)
                    and not (_PRIMARY_YOE_CONTEXT_RE and _PRIMARY_YOE_CONTEXT_RE.search(context))):
                return ("max_min_secondary_yoe",
                        f'"{phrase}{context[:60].rstrip()}": {years} years > max {MAX_MIN_SECONDARY_YOE}')
    return None


def _requires_high_yoe(text):
    return _high_yoe(text) is not None


_HARD_ASK_PENALTIES = [
    {
        "label": item["label"],
        "re": re.compile(item["pattern"], re.IGNORECASE),
        "penalty": int(item.get("penalty", 10)),
        "ignore_multi_level": bool(item.get("ignore_multi_level", False)),
    }
    for item in (_SKILLS_CFG.get("hard_ask_penalties") or [])
    if item.get("pattern") and item.get("label")
]
_MULTI_LEVEL_RE = re.compile(r'\ball levels\b|\b(?:through|to) (?:tech )?lead\b', re.IGNORECASE)

_OFF_CENTER_CFG = _SKILLS_CFG.get("off_center_role_check") or {}
_OFF_CENTER_ROLE_RE = (
    re.compile(_OFF_CENTER_CFG["role_statement_pattern"], re.IGNORECASE)
    if _OFF_CENTER_CFG.get("role_statement_pattern") else None
)
_OFF_CENTER_WORD_RE = (
    re.compile(_OFF_CENTER_CFG["off_center_words_pattern"], re.IGNORECASE)
    if _OFF_CENTER_CFG.get("off_center_words_pattern") else None
)
_TARGET_WORD_RE = (
    re.compile(_OFF_CENTER_CFG["target_words_pattern"], re.IGNORECASE)
    if _OFF_CENTER_CFG.get("target_words_pattern") else None
)
_OFF_CENTER_MIN_MENTIONS = int(_OFF_CENTER_CFG.get("min_mentions", 6))
_OFF_CENTER_RATIO = float(_OFF_CENTER_CFG.get("ratio", 1.5))


def _max_min_yoe(text):
    text = strip_markdown(text)
    return max((_match_years(m, text) for m in _yoe_matches(text)), default=0)


STRONG_TITLE_POINTS = 20
GENERIC_TITLE_POINTS = 10
_REST_RE = re.compile(r'\brest(?:ful)?\b')
_REST_ALTERNATIVE_TERMS = ('soap', 'xml-rpc')


class _TitleCredit(NamedTuple):
    points: int = 0
    reason: str = ''
    primary_group: bool = False
    term: str = ''


def _seniority_hit(title):
    m = _SENIOR_PATTERN.search(title) if _SENIOR_PATTERN else None
    return f'title word "{m.group(0)}"' if m else ''


def _location_reject_reason(job, title, loc):
    """Non-allowed country in location/title, or a named location that is neither remote nor local metro."""
    foreign = FOREIGN_RE.search(loc) or FOREIGN_RE.search(title)
    if foreign and not US_PATTERN.search(loc):
        return f'names "{foreign.group(0)}" (location.foreign_places) and not the home country'
    remote_or_local = (job.get('remote') is True or 'remote' in loc or 'anywhere' in loc or is_local_metro(loc))
    if loc.strip() not in UNSPECIFIED_LOCATIONS and not remote_or_local:
        return f'"{loc}" is neither remote nor in location.local_metro_patterns'
    return ''


def _location_rejected(job, title, loc):
    return bool(_location_reject_reason(job, title, loc))


def _offered_as_alternative(window, alt_re=_ALT_LANG_RE):
    return bool(alt_re.search(window) and _ACCEPTED_LANG_RE and _ACCEPTED_LANG_RE.search(window))


def _excluded_skill_mentions(desc):
    """Yield (skill, optional) for each hard-exclude skill mention. A mention is optional when it is a
    soft ask or offered as an alternative to an accepted skill ("Skill A or Skill B")."""
    for skill in HARD_EXCLUDE_SKILLS:
        for m in _TERM_RES[skill].finditer(desc):
            yield skill, _offered_as_alternative(_match_window(desc, m)) or _is_soft_ask(desc, m)


def distinct_skills(skills):
    """Skills with aliases folded: "ruby" is dropped when "ruby on rails" is listed, ".net" for "asp.net"."""
    skills = list(dict.fromkeys(skills))
    return [s for s in skills if not any(s != other and s in other for other in skills)]


# A framework list ("React, Vue, Angular, etc.") names alternatives even without an "or"
_LIST_ALT_RE = re.compile(r'\b(or|and/or|etc|such as|similar)\b|/')


def _penalized_mention(kw, stack, text):
    """True when kw is asked for itself, not only offered beside an accepted skill or as a soft ask."""
    if kw in stack:
        return True
    return any(not (_offered_as_alternative(_match_window(text, m), _LIST_ALT_RE) or _is_soft_ask(text, m))
               for m in _term(kw).finditer(text))


def _avoided_tech_hit(desc):
    """The first avoid_keywords term asked for outright (not a soft ask), or ''."""
    for term in AVOID_TECH:
        for m in _TERM_RES[term].finditer(desc):
            rest_offered = term in _REST_ALTERNATIVE_TERMS and _REST_RE.search(_match_window(desc, m))
            if not (_is_soft_ask(desc, m) or rest_offered):
                return term
    return ''


def _conditional_require_miss(stack, full_desc):
    """Detail when a trigger skill is present but none of the skills it must come with are, else ''."""
    if not (_CONDITIONAL_IF_PRESENT and _CONDITIONAL_REQUIRE_ANY):
        return ''
    trigger = next((s for s in _CONDITIONAL_IF_PRESENT if _mentions(s, stack, full_desc)), None)
    if trigger and not any(_mentions(s, stack, full_desc) for s in _CONDITIONAL_REQUIRE_ANY):
        return f'mentions "{trigger}" but none of {", ".join(_CONDITIONAL_REQUIRE_ANY)}'
    return ''


def _specialty_hit(title, desc, clean_desc):
    """Which specialty_reject_rules entry matched, or ''."""
    for i, rule in enumerate(_SPECIALTY_REJECT_RULES):
        if rule["title_re"] and (m := rule["title_re"].search(title)):
            return f'rule {i + 1} title_pattern matched "{m.group(0)}"'
        if rule["yoe_re"] and (m := rule["yoe_re"].search(desc)):
            return f'rule {i + 1} jd_yoe_pattern matched "{m.group(0)}"'
        if rule["term_re"]:
            n = len(rule["term_re"].findall(clean_desc))
            if n >= rule["min_mentions"]:
                return f'rule {i + 1} jd_term_pattern found {n} times (min_mentions {rule["min_mentions"]})'
    return ''


def _off_center_hit(clean_desc):
    """Detail when the JD describes an adjacent role (by statement or by word counts) rather than the target."""
    if _OFF_CENTER_ROLE_RE and (m := _OFF_CENTER_ROLE_RE.search(clean_desc)):
        return f'role statement "{m.group(0)}"'
    if _OFF_CENTER_WORD_RE and _TARGET_WORD_RE:
        n_off = len(_OFF_CENTER_WORD_RE.findall(clean_desc))
        n_target = len(_TARGET_WORD_RE.findall(clean_desc))
        if n_off >= _OFF_CENTER_MIN_MENTIONS and n_off >= _OFF_CENTER_RATIO * n_target:
            return f'{n_off} off-center words vs {n_target} target words (min {_OFF_CENTER_MIN_MENTIONS}, ratio {_OFF_CENTER_RATIO})'
    return ''


def _is_off_center(clean_desc):
    return bool(_off_center_hit(clean_desc))


def _senior_years_hit(full_desc):
    m = next((m for m in _SENIOR_ROLE_YOE_RE.finditer(full_desc) if int(m.group(1)) > MAX_SENIOR_ROLE_YEARS), None)
    return f'"{m.group(0)}" > max {MAX_SENIOR_ROLE_YEARS}' if m else ''


def _language_hit(full_desc, title):
    if not _REJECT_FOREIGN_LANG:
        return ''
    m = _LANGUAGE_REQ_RE.search(full_desc) or _LANGUAGE_REQ_RE.search(title)
    return f'"{m.group(0)}"' if m else ''


def _requirements_gates(title, desc, full_desc, stack, strong_title):
    """(rule, detail thunk) for each hard reject read from the JD, in the order they are checked. A thunk
    returns '' when the job passes, a detail string, or a (rule, detail) pair that names a narrower rule."""
    clean_desc = strip_markdown(full_desc)
    return (
        ("avoid_keywords", lambda: (t := _avoided_tech_hit(desc)) and f'"{t}" asked as a requirement'),
        ("conditional_require_any", lambda: _conditional_require_miss(stack, full_desc)),
        ("clearance", lambda: (c := _clearance_hit(desc) or _clearance_hit(title)) and f'"{c}"'),
        ("foreign_language", lambda: _language_hit(full_desc, title)),
        ("specialty_reject_rules", lambda: _specialty_hit(title, desc, clean_desc)),
        ("max_min_yoe", lambda: _high_yoe(full_desc) or ''),
        ("off_center_role_check", lambda: '' if strong_title else _off_center_hit(clean_desc)),
        ("max_senior_role_years", lambda: _senior_years_hit(full_desc)),
    )


def _title_credit(title, strong_title, stack, full_desc):
    """Points for how directly the title (or, for generic titles, the JD body) names the target role."""
    for grp in _PRIMARY_ROLE_GROUPS:
        if grp["re"].search(title):
            return _TitleCredit(grp["points"], grp["label"], primary_group=True)
    if strong_title:
        term = STRONG_TARGET_RE.search(title).group(0)
        return _TitleCredit(STRONG_TITLE_POINTS, term.title(), term=term)
    if (_GENERIC_ROLE_PATTERN and _GENERIC_ROLE_PATTERN.search(title)
            and (not _GENERIC_REQUIRED_SKILLS or any(_mentions(s, stack, full_desc) for s in _GENERIC_REQUIRED_SKILLS))):
        body_signals = sum(len(r.findall(full_desc)) for r in _GENERIC_BODY_SIGNAL_RES)
        if body_signals >= GENERIC_TITLE_BODY_SIGNALS:
            return _TitleCredit(STRONG_TITLE_POINTS, "Target Role (per JD)")
        return _TitleCredit(GENERIC_TITLE_POINTS, "Generic Title w/ Core Stack")
    return _TitleCredit()


def _keyword_score(title, desc, full_desc, stack, strong_title, optional_skills, breakdown=None):
    """(score, reasons, matched core strengths) from title credit, skills, domain terms and penalties.
    With a `breakdown` list, each part of the score is appended to it as (label, points)."""
    parts = breakdown if breakdown is not None else []
    credit = _title_credit(title, strong_title, stack, full_desc)
    score = BASE_SCORE + credit.points
    reasons = [credit.reason] if credit.reason else []
    parts.append(("Base score (skills_and_scoring.base_score)", BASE_SCORE))
    parts.append((f"Title credit: {credit.reason}" if credit.reason else "Title credit: none", credit.points))

    matched_strengths = [s for s in CORE_STRENGTHS if _mentions(s, stack, full_desc)]
    weights = {s: CORE_STRENGTH_WEIGHTS.get(s, CORE_STRENGTH_POINTS) for s in matched_strengths}
    strength_points = min(CORE_STRENGTHS_CAP, sum(weights.values()))
    score += strength_points
    parts.append((f"Core strengths (cap {CORE_STRENGTHS_CAP}): "
                  + (", ".join(f"{s} +{w}" for s, w in weights.items()) or "none"), strength_points))

    group_points, group_terms = {}, {}
    for kw, pts in FOCUS_KEYWORDS.items():
        in_title = _has(kw, title) and not (credit.term and kw in credit.term)
        if _has(kw, full_desc) or in_title:
            group = _FOCUS_GROUP_OF.get(kw, kw)
            if group not in group_points:
                reasons.append(kw.title())
            group_terms.setdefault(group, []).append(kw)
            group_points[group] = max(pts, group_points.get(group, 0))
    domain_points = min(DOMAIN_SCORE_CAP, sum(group_points.values()))
    score += domain_points
    parts.append((f"Domain terms (cap {DOMAIN_SCORE_CAP}): "
                  + (", ".join(f"{'/'.join(group_terms[g])} +{p}" for g, p in group_points.items()) or "none"),
                  domain_points))

    optional_skills = distinct_skills(optional_skills)
    if optional_skills:
        penalty = min(SOFT_SKILL_PENALTY_CAP, SOFT_SKILL_PENALTY * len(optional_skills))
        score -= penalty
        reasons.append(f"{', '.join(s.upper() for s in optional_skills)} optional")
        parts.append((f"Excluded skills offered as optional: {', '.join(optional_skills)}", -penalty))

    for kw, pen in PENALIZED_KEYWORDS.items():
        if _penalized_mention(kw, stack, full_desc):
            score -= pen
            reasons.append(f"{kw.title()} mentioned")
            parts.append((f"penalized_keywords: {kw}", -pen))

    clean_desc = strip_markdown(full_desc)
    clean_req_desc = strip_markdown(desc)
    for item in _HARD_ASK_PENALTIES:
        if not _has_hard_ask(item["re"], clean_req_desc):
            continue
        if item["ignore_multi_level"] and (_MULTI_LEVEL_RE.search(title) or _MULTI_LEVEL_RE.search(clean_desc)):
            continue
        score -= item["penalty"]
        reasons.append(item["label"])
        parts.append((f"hard_ask_penalties: {item['label']}", -item["penalty"]))

    if _max_min_yoe(clean_desc) >= MAX_MIN_YOE:
        score -= TOP_YOE_PENALTY
        reasons.append(f"{MAX_MIN_YOE}+ yrs asked")
        parts.append((f"Asks {MAX_MIN_YOE}+ years (experience.top_yoe_penalty)", -TOP_YOE_PENALTY))

    return score, reasons, matched_strengths


# Why evaluate_single_job drops a posting: rule key -> what it means and the config behind it
DROP_RULES = {
    "expired": "Posting expired",
    "missing_company": "No company name (blind listing)",
    "seniority": "Seniority word in title (titles.exclude_seniority_patterns)",
    "title": "Title rules (titles.*)",
    "location": "Feed location neither remote nor local metro (location.*)",
    "foreign_url": "Description not fetched and the URL names a foreign place",
    "jd_location": "No feed location and the JD names no eligible remote or local work",
    "hard_exclude_skills": "Required skill in skills_and_scoring.hard_exclude_skills",
    "avoid_keywords": "Required tech in skills_and_scoring.avoid_keywords",
    "conditional_require_any": "skills_and_scoring.conditional_require_any",
    "clearance": "Security clearance (experience.reject_clearance)",
    "foreign_language": "Foreign language (experience.reject_foreign_language_req)",
    "specialty_reject_rules": "skills_and_scoring.specialty_reject_rules",
    "max_min_yoe": "Years asked over experience.max_min_yoe",
    "max_min_secondary_yoe": "Secondary-skill years over experience.max_min_secondary_yoe",
    "off_center_role_check": "Adjacent role (skills_and_scoring.off_center_role_check)",
    "max_senior_role_years": "Senior-level years over experience.max_senior_role_years",
    "secondary_floor": "Score under skills_and_scoring.secondary_floor",
    "jd_logistics": "Onsite / hybrid / residency wording in the JD (location.*)",
}


class _Rejected(Exception):
    def __init__(self, rule, detail, score=None):
        super().__init__(rule)
        self.rule, self.detail, self.score = rule, detail, score


def evaluate_job_traced(job, trace=None, breakdown=None):
    """(result, rejection). result is what evaluate_single_job returns; when it is None, rejection is
    {"rejected_by": a DROP_RULES key, "detail": what matched}, plus "score" for a gate checked after scoring.
    With a `trace` list, every gate checked is appended as {"rule", "passed", "detail"}; with a `breakdown`
    list, the score's parts as (label, points)."""
    def gate(rule, detail, score=None):
        if isinstance(detail, tuple):  # a gate that names a narrower rule than the one it is listed under
            rule, detail = detail
        if trace is not None:
            trace.append({"rule": rule, "passed": not detail, "detail": detail or ""})
        if detail:
            raise _Rejected(rule, detail, score)

    try:
        return _evaluate(job, gate, breakdown), None
    except _Rejected as r:
        rejection = {"rejected_by": r.rule, "detail": r.detail}
        if r.score is not None:
            rejection["score"] = r.score
        return None, rejection


def evaluate_single_job(job):
    """{'category': 'Strong' | 'Secondary' | 'Manual Review', 'score', 'reason'}, or None when rejected."""
    return evaluate_job_traced(job)[0]


def _job_texts(job):
    """(title, requirements text, full JD, stack fallback, location), lower-cased, as the rules read them."""
    title = (job.get('title') or '').lower()
    desc = (job.get('description_ai') or '').lower()
    full_desc = (job.get('description_full') or desc).lower()
    # stack_detected is only a fallback for old rows saved without the full JD: skills are matched as whole
    # terms in the text itself, so a stack list built another way cannot add credit the JD does not support
    stack = [] if job.get('description_full') else [s.lower() for s in job.get('stack_detected', [])]
    return title, desc, full_desc, stack, (job.get('location') or '').lower()


def score_breakdown(job):
    """(keyword score, [(label, points)]) for a job whatever the hard rules say, e.g. to show what a dropped
    posting would have scored."""
    title, desc, full_desc, stack, _ = _job_texts(job)
    parts = []
    score, _, _ = _keyword_score(title, desc, full_desc, stack, bool(STRONG_TARGET_RE.search(title)),
                                 [skill for skill, _ in _excluded_skill_mentions(desc)], parts)
    return min(100, score), parts


def _evaluate(job, gate, breakdown):
    title, desc, full_desc, stack, loc = _job_texts(job)

    # 1. Dead postings and blind company listings
    gate("expired", 'description starts "Posting expired"' if desc.startswith('posting expired') else '')
    gate("missing_company", f"company is {job.get('company')!r}"
         if str(job.get('company') or '').strip().lower() in _MISSING_COMPANY_VALUES else '')

    # 2. Undesired seniority, roles, and locations. Excluded title words (soft ones only on a non-strong
    # title) are applied by title_reject_reason.
    strong_title = bool(STRONG_TARGET_RE.search(title))
    gate("seniority", _seniority_hit(title))
    gate("title", title_reject_reason(title))
    gate("location", _location_reject_reason(job, title, loc))

    if not desc or len(desc) < 100 or _FAILED_SCRAPE_PATTERN.search(desc):
        # The URL path only: hosts such as job-boards.eu.greenhouse.io also list US roles
        url_words = re.sub(r'[-_/.]+', ' ', urlparse(job.get('url') or '').path.lower())
        gate("foreign_url", f"URL path names a foreign place: {url_words.strip()[:80]!r}"
             if names_foreign_place(url_words) else '')
        return {"category": "Manual Review", "score": 0, "reason": "Failed scrape / missing description"}

    # No feed location: keep only when the JD itself says eligible remote or local metro
    location_unknown = loc.strip() in UNSPECIFIED_LOCATIONS and job.get('remote') is not True
    gate("jd_location", "no feed location, and the JD states no US-remote or local-metro work"
         if location_unknown and not jd_location_ok(job.get('description_full') or job.get('description_ai') or '')
         else '')

    # 3. Hard rejects in requirements text
    excluded = list(_excluded_skill_mentions(desc))
    required = distinct_skills([skill for skill, optional in excluded if not optional])
    gate("hard_exclude_skills", f"asks for {', '.join(required)}" if required else '')
    for rule, detail in _requirements_gates(title, desc, full_desc, stack, strong_title):
        gate(rule, detail())

    # 4. Score calculation
    score, reasons, matched_strengths = _keyword_score(
        title, desc, full_desc, stack, strong_title, [skill for skill, _ in excluded], breakdown)
    match_pct = min(100, score)
    gate("secondary_floor", f"score {match_pct}% < {SECONDARY_FLOOR}%" if match_pct < SECONDARY_FLOOR else '')
    category = "Strong" if match_pct >= STRONG_FLOOR else "Secondary"

    # 5. Onsite, hybrid or residency wording in the JD (checked last: it reads the most text)
    gate("jd_logistics", jd_logistics_block(job), score=match_pct)

    unique_reasons = list(dict.fromkeys(reasons))
    reason_str = ", ".join(unique_reasons) if unique_reasons else f"Matches skills: {', '.join(matched_strengths[:3])}"
    return {"category": category, "score": match_pct, "reason": reason_str}


def _dedupe_key(entry):
    """Group key for duplicate roles: ('role', company|title), or ('url', url) when the company is unknown."""
    company = re.sub(r'[^a-z0-9]', '', str(entry.get("company") or "").lower())
    key = role_key(entry.get("company"), entry.get("title"))
    return ('role', key) if key and company != 'unknown' else ('url', entry["url"])


_JD_KEY_SKIP, _JD_KEY_LEN, _JD_KEY_MIN = 200, 800, 300


def jd_key(job):
    """'jd:<hash>|<title key>' identifying a posting by its JD text and title, so the same role posted under
    another company name (a brand vs its parent, an ATS slug, a reposter) still matches. '' for short JDs."""
    text = re.sub(r'[^a-z0-9]', '', (job.get('description_full') or job.get('description_ai') or '').lower())
    title = role_key('x', job.get('title')).partition('|')[2]
    if len(text) < _JD_KEY_MIN or not title:
        return ''
    # Skip the opening, which is often a per-source header, when the JD is long enough to spare it
    body = text[_JD_KEY_SKIP:_JD_KEY_SKIP + _JD_KEY_LEN] if len(text) >= _JD_KEY_SKIP + _JD_KEY_MIN else text
    return f"jd:{hashlib.sha1(body.encode()).hexdigest()[:16]}|{title}"


def _same_posting(a, b):
    """Same JD text, and the same title or one title extending the other ("Core Engineer" / "Core Engineer New")."""
    (fp_a, _, title_a), (fp_b, _, title_b) = a.partition('|'), b.partition('|')
    return fp_a == fp_b and (title_a.startswith(title_b) or title_b.startswith(title_a))


def _dedupe_by_role(entries):
    """Keep one entry per role with the highest score, linking to the company's own ATS posting when any
    duplicate has one instead of an aggregator link. Entries are the same role when they share a
    company+title key, or the same JD text and title under different company names."""
    groups = {}
    for e in entries:
        groups.setdefault(_dedupe_key(e), []).append(e)
    merged = []  # (entries, their jd keys) per distinct role
    for dupes in groups.values():
        keys = {e["jd_key"] for e in dupes if e.get("jd_key")}
        home = next((g for g in merged if any(_same_posting(a, b) for a in keys for b in g[1])), None)
        if home is None:
            merged.append((list(dupes), keys))
        else:
            home[0].extend(dupes)
            home[1].update(keys)
    deduped = []
    for dupes, jd_keys in merged:
        best = dict(max(dupes, key=lambda e: e["score"]))
        # Every name this role was posted under, so blocklisting it also covers the other spellings
        role_keys = {role_key(e.get("company"), e.get("title")) for e in dupes}
        best["dup_keys"] = sorted((role_keys | jd_keys) - {""})
        direct = [e for e in dupes if not is_aggregator_url(e["url"])]
        if direct:
            best["url"] = max(direct, key=lambda e: e["score"])["url"]
        posted = [e["posted"] for e in dupes if e.get("posted")]
        if posted:
            best["posted"] = min(posted)
        deduped.append(best)
    return deduped


DROP_EXAMPLES = 3


def record_drop(drops, job, rejection, stage="Stage 3"):
    """Tally one rejection in `drops` (rule key -> {rule, label, stage, count, examples})."""
    rule = rejection["rejected_by"]
    entry = drops.setdefault(rule, {"rule": rule, "label": DROP_RULES.get(rule, rule), "stage": stage,
                                    "count": 0, "examples": []})
    entry["count"] += 1
    if len(entry["examples"]) < DROP_EXAMPLES:
        entry["examples"].append({"company": str(job.get("company") or "Unknown").strip(),
                                  "title": job.get("title") or "Unknown Role",
                                  "url": job.get("url", "#"), "detail": rejection["detail"]})


def drop_funnel(drops):
    """The tallied drops as a list, largest first."""
    return sorted(drops.values(), key=lambda d: d["count"], reverse=True)


def evaluate_jobs(input_path, output_path=None):
    if not os.path.exists(input_path):
        raise FileNotFoundError(f"Input file not found: {input_path}")

    with open(input_path, encoding="utf-8") as f:
        jobs = json.load(f)

    jobs = [j for j in jobs if is_recent(j.get('first_seen'))]
    jobs.sort(key=lambda x: x.get('first_seen', ''), reverse=True)

    evaluated_matches = []
    manual_reviews = []
    location_filtered = []
    title_near_misses = []
    drops = {}

    for j in jobs:
        res, rejection = evaluate_job_traced(j)
        if not res:
            record_drop(drops, j, rejection)
            if rejection["rejected_by"] == "jd_logistics":
                location_filtered.append({
                    "company": str(j.get("company") or "Unknown").strip().title(),
                    "title": j.get("title") or "Unknown Role",
                    "score": rejection["score"],
                    "url": j.get("url", "#"),
                    "phrase": rejection["detail"],
                })
            elif rejection["rejected_by"] == "title":
                # Title rules run before scoring, so a pattern that is too broad drops strong roles silently
                score, _ = score_breakdown(j)
                if score >= STRONG_FLOOR:
                    title_near_misses.append({
                        "company": str(j.get("company") or "Unknown").strip().title(),
                        "title": j.get("title") or "Unknown Role",
                        "score": score,
                        "url": j.get("url", "#"),
                        "detail": rejection["detail"],
                    })
            continue

        company = str(j.get("company") or "").strip()
        if company.lower() in _MISSING_COMPANY_VALUES:
            company = "Unknown"
        entry = {
            "company": company.title(),
            "title": j.get("title") or "Unknown Role",
            "location": j.get("location") or "Not specified",
            "stack": ", ".join(j.get("stack_detected", []))[:40],
            "reason": res["reason"],
            "score": res["score"],
            "category": res["category"],
            "url": j.get("url", "#"),
            "first_seen": j.get("first_seen", ""),
            "posted": j.get("posted") or (j.get("first_seen") or "")[:10],
            "jd_key": jd_key(j),
        }

        if res["category"] == "Manual Review":
            manual_reviews.append(entry)
        else:
            evaluated_matches.append(entry)

    evaluated_matches = _dedupe_by_role(evaluated_matches)
    seen_roles = {_dedupe_key(e) for e in evaluated_matches}
    manual_reviews = [e for e in _dedupe_by_role(manual_reviews) if _dedupe_key(e) not in seen_roles]

    evaluated_matches.sort(key=lambda x: x["score"], reverse=True)

    unique_filtered = {}
    for e in location_filtered:
        unique_filtered.setdefault(_dedupe_key(e), e)
    location_filtered = [e for key, e in unique_filtered.items() if key not in seen_roles]

    unique_near = {}
    for e in sorted(title_near_misses, key=lambda e: e["score"], reverse=True):
        unique_near.setdefault(_dedupe_key(e), e)
    title_near_misses = [e for key, e in unique_near.items() if key not in seen_roles]

    result = {
        "total_evaluated": len(jobs),
        "top_matches": evaluated_matches,
        "manual_reviews": manual_reviews,
        "location_filtered": location_filtered,
        "title_near_misses": title_near_misses,
        "dropped_by_rule": drop_funnel(drops),
    }

    if output_path:
        atomic_write_json(output_path, result)
        print(f"Evaluated {len(jobs)} jobs. Top matches: {len(evaluated_matches)}, Manual reviews: {len(manual_reviews)}, "
              f"dropped for onsite/hybrid/ineligible location wording: {len(location_filtered)}, "
              f"dropped by other deterministic rules: "
              f"{sum(d['count'] for rule, d in drops.items() if rule != 'jd_logistics')}.")

    return result


def _cell(value):
    return str(value).replace("|", "/").replace("\n", " ")


def generate_markdown(result, limit=25):
    top_matches = result.get("top_matches", [])[:limit]
    manual_reviews = result.get("manual_reviews", [])[:10]

    lines = [
        "## Evaluated Job Matches",
        "",
        "| Company | Title | Location | Skills | Alignment Reason | Match % | Link |",
        "|---|---|---|---|---|---|---|"
    ]

    for r in top_matches:
        stack = r['stack'] if r['stack'] else 'None'
        lines.append(
            f"| {_cell(r['company'])} | {_cell(r['title'])} | {_cell(r['location'])} | {_cell(stack)} "
            f"| {_cell(r['reason'])} | {r['score']}% | [Apply]({r['url']}) |"
        )

    if manual_reviews:
        lines.extend([
            "",
            "### Manual Review Required (Failed Scrapes)",
            "",
            "| Company | Title | Location | Reason | Link |",
            "|---|---|---|---|---|"
        ])
        for r in manual_reviews:
            lines.append(
                f"| {_cell(r['company'])} | {_cell(r['title'])} | {_cell(r['location'])} | {_cell(r['reason'])} | [Apply]({r['url']}) |"
            )

    ai_skipped = result.get("ai_skipped", [])
    if ai_skipped:
        lines.extend([
            "",
            "### Filtered Out by AI Review",
            "",
            "| Company | Title | Score | Why skipped | Link |",
            "|---|---|---|---|---|"
        ])
        for r in ai_skipped:
            lines.append(
                f"| {_cell(r['company'])} | {_cell(r['title'])} | {r['score']}% | {_cell(skip_reason(r))} | [Apply]({r['url']}) |"
            )

    location_filtered = result.get("location_filtered", [])
    if location_filtered:
        lines.extend([
            "",
            "### Dropped Before AI Review (onsite / hybrid / location wording)",
            "",
            "| Company | Title | Keyword % | Phrase | Link |",
            "|---|---|---|---|---|"
        ])
        for r in location_filtered:
            phrase = r['phrase'].replace('|', '/')[:140]
            lines.append(f"| {_cell(r['company'])} | {_cell(r['title'])} | {r['score']}% | \"{phrase}\" | [Apply]({r['url']}) |")

    near_misses = result.get("title_near_misses", [])
    if near_misses:
        lines.extend([
            "",
            (f"<details><summary>Dropped by a Title Rule Despite a Keyword Score of {STRONG_FLOOR}%+ "
             f"({len(near_misses)})</summary>"),
            "",
            "| Company | Title | Keyword % | Rule | Link |",
            "|---|---|---|---|---|"
        ])
        for r in near_misses:
            lines.append(f"| {_cell(r['company'])} | {_cell(r['title'])} | {r['score']}% | {_cell(r['detail'])} "
                         f"| [Apply]({r['url']}) |")
        lines.extend(["", "</details>"])

    funnel =[*result.get("prefilter_drops", []), *result.get("dropped_by_rule", [])]
    if funnel:
        lines.extend([
            "",
            ("<details><summary>Dropped by Deterministic Rules "
             f"({sum(d['count'] for d in funnel)})</summary>"),
            "",
            "| Stage | Rule | Count | Example |",
            "|---|---|---|---|"
        ])
        for d in funnel:
            ex = (d.get("examples") or [None])[0]
            example = (f"[{_cell(ex['company'])} · {_cell(ex['title'])}]({ex['url']}): {_cell(ex['detail'])[:120]}"
                       if ex else "")
            lines.append(f"| {_cell(d.get('stage', ''))} | {_cell(d['label'])} | {d['count']} | {example} |")
        lines.extend(["", "</details>"])

    health = result.get("source_health")
    if health:
        lines.extend(["", "### Sources", "", health["line"]])
        lines.extend(f"- ⚠️ {w}" for w in health["warnings"])

    if result.get("ai_spend"):
        from job_search_pipeline.review import (
            spend_line,  # deferred: evaluate.py runs standalone without the review stage
        )
        lines.extend(["", "### AI Review Spend", "", spend_line(result["ai_spend"])])

    return "\n".join(lines)


def skip_reason(job):
    """The AI's reason plus any blocker that capped the score."""
    reason = job.get('ai_reason', '')
    blockers = job.get('blockers') or []
    blockers = sorted(blockers, key=lambda b: not b.startswith('pay '))
    return f"{reason} [{'; '.join(blockers[:3])}]" if blockers else reason


if __name__ == "__main__":
    in_file = sys.argv[1] if len(sys.argv) > 1 else get_path("enriched_jobs")
    out_file = sys.argv[2] if len(sys.argv) > 2 else get_path("evaluated_jobs")
    evaluate_jobs(in_file, out_file)
