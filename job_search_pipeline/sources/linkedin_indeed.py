"""Stage 1 source: LinkedIn, Indeed and (when sources.searches lists it) Glassdoor via python-jobspy.

The sites are scraped in parallel threads. LinkedIn job-detail pages are cached on
disk between runs to avoid HTTP 429s, and rows without a verifiable remote/local
location are kept only when the JD text supports it.
"""
import logging
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pandas as pd
from jobspy import scrape_jobs

from job_search_pipeline.config import (
    CFG,
    atomic_write_json,
    compile_alternation,
    compile_regex_list,
    get_path,
    load_json,
    term_regex,
)
from job_search_pipeline.evaluate import (
    JD_HYBRID_ONSITE_RE,
    JD_US_REMOTE_RE,
    _excluded_skill_mentions,
    distinct_skills,
    hard_onsite_passage,
)
from job_search_pipeline.sources.filter import (
    FILTERED_JOBS_PATH,
    LOOKBACK_DAYS,
    MIN_SALARY,
    is_local_metro,
    is_valid_company,
    names_foreign_place,
)
from job_search_pipeline.urls import is_aggregator_url, normalize_url

_TITLES_CFG = CFG["titles"]
_SKILLS_CFG = CFG["skills_and_scoring"]
_SOURCES_CFG = CFG["sources"]

_EXCLUDE_TECH_PATTERNS = compile_regex_list(_TITLES_CFG["exclude_tech_patterns"])
_EXCLUDE_SENIORITY = compile_alternation(
    _TITLES_CFG["exclude_seniority_patterns"],
    word_bounded=True
)

_JD_STRONG_REMOTE_RE = re.compile(
    r'\b(?:fully|100%|completely|entirely) remote\b|\bremote[- ](?:first|friendly)\b'
    r'|\bremote\s*[-–(]\s*(?:us|usa|united states)\b|\((?:fully )?remote\)|\blocation:\s*remote\b'
    r'|\bwork(?:ing)? from home\b|\bwork(?:ing)? remotely\b'
    r'|\bremote (?:work|onboarding|position|role|opportunity|employees?)\b', re.IGNORECASE
)
_JD_WEAK_REMOTE_RE = re.compile(r'\bremote(?:ly)?\b', re.IGNORECASE)
_JD_OFFICE_RE = re.compile(
    r'\bin[- ]office perks\b|\brelocation (?:expense|assistance|package|support|coverage)'
    r'|\bfree (?:meals|lunch)\b[^.\n]{0,30}\b(?:hub|office)\b|\bteam (?:is )?based in\b'
    r'|\b(?:\d+|three|four|five) days? (?:a|per) week\b', re.IGNORECASE
)


_CORE_STRENGTHS = [s.lower() for s in (_SKILLS_CFG.get("core_strengths") or [])]
_FOCUS_AREAS = [k.lower() for k in (_SKILLS_CFG.get("focus_keywords") or {})]
_PRIMARY_ROLE_TERMS = [t.lower() for t in (_TITLES_CFG.get("primary_role_patterns") or [])]
_CORE_STRENGTH_RES = {s: term_regex(s) for s in _CORE_STRENGTHS}
_FOCUS_AREA_RES = {f: term_regex(f, allow_suffix=True) for f in _FOCUS_AREAS}

SEARCH_TERMS = _SOURCES_CFG["search_terms"]
SEARCHES = [tuple(s) for s in _SOURCES_CFG["searches"]]
# jobspy sites this module scrapes; ZipRecruiter and Google Jobs answer 403 / nothing without proxies
JOBSPY_SITES = ("linkedin", "indeed", "glassdoor")
# Sites that block bursts: pause between their queries, stop asking after the first block, then cool down
THROTTLED_SITES = ("linkedin", "glassdoor")
LINKEDIN_PAUSE_SECONDS = int(_SOURCES_CFG["linkedin_pause_seconds"])
COUNTRY_INDEED = _SOURCES_CFG["country_indeed"]

LI_DETAILS_CACHE_PATH = get_path("linkedin_details_cache")
LI_DETAILS_CACHE_DAYS = LOOKBACK_DAYS + 1

# After a block, a throttled site is left alone for a while: asking again on every run keeps the block alive.
# Each block in a row doubles the wait, up to BLOCK_COOLDOWN_MAX_FACTOR times the base.
THROTTLE_STATE_PATH = get_path("throttle_state")
BLOCK_COOLDOWN_HOURS = float(_SOURCES_CFG["block_cooldown_hours"])
BLOCK_COOLDOWN_MAX_FACTOR = 4


def cooldown_until(site, state, now=None):
    """The time a throttled site's cooldown ends, or None when it may be queried now."""
    now = now or datetime.now(UTC)
    try:
        until = datetime.fromisoformat((state.get(site) or {})["until"])
    except (KeyError, TypeError, ValueError):
        return None
    return until if until > now else None


def record_block_outcome(site, blocked, state, now=None):
    """Start or extend a site's cooldown after a block; clear it after a clean run. Returns the new end time."""
    if not blocked:
        state.pop(site, None)
        return None
    now = now or datetime.now(UTC)
    strikes = int((state.get(site) or {}).get("strikes", 0)) + 1
    hours = BLOCK_COOLDOWN_HOURS * min(2 ** (strikes - 1), BLOCK_COOLDOWN_MAX_FACTOR)
    until = now + timedelta(hours=hours)
    state[site] = {"blocked_at": now.isoformat(), "strikes": strikes, "until": until.isoformat()}
    return until


def is_valid_jobspy_title(title):
    """Search results are already on-target by query, so only seniority and tech exclusions apply."""
    t = (title or "").lower()
    if _EXCLUDE_SENIORITY and _EXCLUDE_SENIORITY.search(t):
        return False
    return not any(p.search(t) for p in _EXCLUDE_TECH_PATTERNS)


def remote_evidence(loc, desc="", remote_flag=False):
    """Return 'onsite', 'remote', 'unverified' or 'none' for a non-local row.

    Only a flat onsite/relocation requirement is 'onsite' here. Hybrid-policy and office-perk wording
    ("three days a week for employees near an office", "we do not offer relocation assistance") is often
    conditional or negated, so those rows are kept as 'unverified' and judged by the stage-3 logistics
    check, which reads the context and lists what it drops in the run summary."""
    loc_lower = (loc or "").lower()
    text = (desc or "").replace("\\", "")
    if hard_onsite_passage(text):
        return "onsite"
    office_hint = bool(JD_HYBRID_ONSITE_RE.search(text) or _JD_OFFICE_RE.search(text))
    stated_remote = ("remote" in loc_lower or "work from home" in loc_lower
                     or JD_US_REMOTE_RE.search(text) or _JD_STRONG_REMOTE_RE.search(text))
    if stated_remote:
        return "unverified" if office_hint else "remote"
    if remote_flag or _JD_WEAK_REMOTE_RE.search(text):
        return "unverified"
    return "none"


def is_valid_jobspy_location(loc, desc="", remote_flag=False):
    """Local metro, or a non-foreign location whose JD shows (or at least hints at) remote work."""
    loc_lower = (loc or "").lower()

    if names_foreign_place(loc_lower):
        return False

    if is_local_metro(loc_lower):
        return True

    return remote_evidence(loc, desc, remote_flag) in ("remote", "unverified")


def score_job(title, desc):
    desc_lower = (desc or "").lower()
    t_lower = (title or "").lower()

    # Required (not "a plus" or "X or React") excluded skills, aliases counted once: "Ruby on Rails is a plus"
    # alone must not drop a posting as two excluded skills
    heavy = distinct_skills(skill for skill, optional in _excluded_skill_mentions(desc_lower) if not optional)
    if len(heavy) >= 2:
        return 0, []

    # The score is only a loose keep/drop gate, so it counts substrings ("reactjs" is a signal for "react").
    # The matched list becomes stack_detected, so it holds whole-term matches only: "javascript" is not "java".
    matched = set()
    score = 0

    for s in _CORE_STRENGTHS:
        if s in desc_lower:
            score += 12
            if _CORE_STRENGTH_RES[s].search(desc_lower):
                matched.add(s.capitalize())

    for f in _FOCUS_AREAS:
        if f in desc_lower or f in t_lower:
            score += 15
            if _FOCUS_AREA_RES[f].search(desc_lower) or _FOCUS_AREA_RES[f].search(t_lower):
                matched.add(f.capitalize())

    if any(term in t_lower for term in _PRIMARY_ROLE_TERMS):
        score += 20
        matched.add("Primary Target")

    # If no specific strengths/focuses are configured, pass all valid titles
    if not _CORE_STRENGTHS and not _FOCUS_AREAS and not _PRIMARY_ROLE_TERMS:
        return 50, ["Matched Title"]

    normalized = min(100, int((score / 140) * 100))
    return normalized, list(matched)


def _install_linkedin_details_cache():
    """Wrap jobspy's LinkedIn job-page fetch with a disk cache keyed by job id."""
    try:
        from jobspy.linkedin import LinkedIn
        from jobspy.model import CompensationInterval, JobType
    except ImportError:
        return lambda: 0
    # Named _fetch_details since jobspy 1.2 (_get_job_details before); a rename would silently drop the cache
    method = next((m for m in ("_fetch_details", "_get_job_details") if hasattr(LinkedIn, m)), None)
    if method is None:
        print("WARNING: jobspy's LinkedIn job-page fetch was not found; the details cache is off", file=sys.stderr)
        return lambda: 0
    original = getattr(LinkedIn, method)

    cutoff = datetime.now(UTC).timestamp() - LI_DETAILS_CACHE_DAYS * 86400
    stored_cache = load_json(LI_DETAILS_CACHE_PATH, default={})
    cache = {k: v for k, v in stored_cache.items() if v.get("fetched", 0) >= cutoff}
    hits = 0

    def cached_details(self, job_id):
        nonlocal hits
        entry = cache.get(job_id)
        if entry:
            hits += 1
            details = dict(entry["details"])
            types = [JobType[n] for n in details.get("job_type") or [] if n in JobType.__members__]
            details["job_type"] = types or None
            # jobspy copies these onto the JobPost without validation, so restore the enum it expects
            if details.get("interval") in CompensationInterval.__members__:
                details["interval"] = CompensationInterval[details["interval"]]
            return details
        details = original(self, job_id)
        if details.get("description"):
            stored = dict(details)
            stored["job_type"] = [t.name for t in details.get("job_type") or []]
            if isinstance(details.get("interval"), CompensationInterval):
                stored["interval"] = details["interval"].name
            cache[job_id] = {"fetched": datetime.now(UTC).timestamp(), "details": stored}
        return details

    setattr(LinkedIn, method, cached_details)

    def finish():
        setattr(LinkedIn, method, original)
        try:
            atomic_write_json(LI_DETAILS_CACHE_PATH, cache)
        except (OSError, TypeError) as e:
            print(f"Could not save LinkedIn details cache: {e}", file=sys.stderr)
        return hits
    return finish


class _ErrorCounter(logging.Handler):
    """Counts jobspy ERROR records (429s and 403 blocks are logged, not raised)."""
    def __init__(self):
        super().__init__(level=logging.ERROR)
        self.count = 0
        self.rate_limited = 0

    def emit(self, record):
        self.count += 1
        message = record.getMessage()
        if "429" in message or "blocked" in message:
            self.rate_limited += 1


def _display_location(raw_loc, evidence):
    """Location label that shows how much the remote claim was verified."""
    if evidence == "local":
        return raw_loc
    if evidence == "unverified":
        return f"Remote? (unverified) ({raw_loc})" if raw_loc else "Remote? (unverified)"
    if raw_loc and "remote" not in raw_loc.lower():
        return f"Remote ({raw_loc})"
    return raw_loc or "Remote"


def _salary_from_row(sal_min, sal_max):
    try:
        if pd.notna(sal_min) and pd.notna(sal_max):
            return {"min": int(float(sal_min)), "max": int(float(sal_max))}
        if pd.notna(sal_min):
            v = int(float(sal_min))
            return {"min": v, "max": v}
    except (TypeError, ValueError):
        pass
    return None


def _dropped(counts, reason):
    """Tally a Stage 1 drop reason (a filter.PREFILTER_RULES key, or "keyword_score") in counts["dropped"]."""
    if counts is not None:
        dropped = counts.setdefault("dropped", {})
        dropped[reason] = dropped.get(reason, 0) + 1


def _row_to_job(row, counts=None):
    """Convert one jobspy row to a pipeline job dict, or None when it fails filters.
    `counts` (optional dict) tallies rows dropped for a flat onsite requirement, and every drop by reason."""
    direct = row.get("job_url_direct")
    url = (normalize_url(direct) if isinstance(direct, str) and direct and not is_aggregator_url(direct)
           else row.get("job_url"))
    if not url:
        return _dropped(counts, "no_url")

    title = str(row.get("title") or "")
    desc = str(row.get("description") or "")

    if not is_valid_jobspy_title(title):
        return _dropped(counts, "title")
    company = str(row.get("company")) if pd.notna(row.get("company")) else ""
    if company and not is_valid_company(company):
        return _dropped(counts, "company")

    raw_loc = str(row.get("location") or "")
    row_remote = row.get("is_remote")
    remote_flag = pd.notna(row_remote) and bool(row_remote)
    if not is_valid_jobspy_location(raw_loc, desc, remote_flag):
        if counts is not None and not is_local_metro(raw_loc) and remote_evidence(raw_loc, desc, remote_flag) == "onsite":
            counts["onsite_dropped"] = counts.get("onsite_dropped", 0) + 1
        return _dropped(counts, "location")
    evidence = "local" if is_local_metro(raw_loc) else remote_evidence(raw_loc, desc, remote_flag)

    sal_min = row.get("min_amount")
    sal_max = row.get("max_amount")
    if MIN_SALARY > 0 and pd.notna(sal_max):
        try:
            if float(sal_max) < MIN_SALARY:
                return _dropped(counts, "salary")
        except (TypeError, ValueError):
            pass

    score, reasons = score_job(title, desc)
    if score == 0:
        return _dropped(counts, "keyword_score")

    return {
        "title": title,
        "company": company,
        "location": _display_location(raw_loc, evidence),
        "remote": evidence == "remote",
        "salary": _salary_from_row(sal_min, sal_max),
        "url": url,
        "first_seen": datetime.now(UTC).isoformat(),
        "posted": str(row["date_posted"])[:10] if pd.notna(row.get("date_posted")) else None,
        "description_ai": desc,
        "description_full": desc,
        "stack_detected": reasons,
        "ats": str(row.get("site") or "Indeed").capitalize(),
        "score": score,
        **({"company_site": site} if isinstance(site := row.get("company_url_direct"), str) and site else {}),
    }


def _scrape_site(site, lookback_hours, terms):
    # jobspy logs as "jobspy.<site>" since 1.2 ("JobSpy:LinkedIn" before); a rename hides 429s from the counter
    logger = logging.getLogger(f"jobspy.{site}")
    counter = _ErrorCounter()
    logger.addHandler(counter)
    finish_cache = _install_linkedin_details_cache() if site == "linkedin" else (lambda: 0)
    jobs, raw, failed, skipped, counts = [], 0, 0, 0, {}
    queries = [(loc, sfx, n, term) for s, loc, sfx, n in SEARCHES if s == site for term in terms]
    try:
        for i, (location, suffix, wanted, term) in enumerate(queries):
            try:
                df = scrape_jobs(
                    site_name=[site],
                    search_term=term + suffix,
                    location=location,
                    results_wanted=wanted,
                    hours_old=lookback_hours,
                    country_indeed=COUNTRY_INDEED,
                    fetch_description=True,
                    enforce_annual_salary=True,
                )
            except Exception as e:
                failed += 1
                print(f"Error querying {site} for {term + suffix} ({location}): {e}", file=sys.stderr)
                df = None
            if df is not None:
                raw += len(df)
                for _, row in df.iterrows():
                    job = _row_to_job(row, counts)
                    if job:
                        jobs.append(job)
            if site in THROTTLED_SITES and counter.rate_limited:
                # A block lasts well past this run, and every later query would spend ~2 minutes in
                # jobspy's retries before coming back empty, so stop asking.
                skipped = len(queries) - i - 1
                if skipped:
                    print(f"  {site}: rate-limited or blocked; skipping the remaining {skipped} queries this run",
                          file=sys.stderr)
                break
            if site in THROTTLED_SITES:
                time.sleep(LINKEDIN_PAUSE_SECONDS)
    finally:
        logger.removeHandler(counter)
        cache_hits = finish_cache()
    return jobs, {"raw": raw, "kept": len(jobs), "failed_queries": failed,
                  "onsite_dropped": counts.get("onsite_dropped", 0), "errors": counter.count, "rate_limited": counter.rate_limited, "skipped_queries": skipped, "cached_details": cache_hits,
                  "dropped": counts.get("dropped", {})}


def searched_sites(searches=None):
    """The jobspy sites sources.searches names, in JOBSPY_SITES order; unknown names are reported and skipped."""
    named = {s[0] for s in (SEARCHES if searches is None else searches)}
    for unknown in sorted(named - set(JOBSPY_SITES)):
        print(f"WARNING: sources.searches names {unknown!r}, which is not one of {', '.join(JOBSPY_SITES)}; "
              "skipping it", file=sys.stderr)
    return [site for site in JOBSPY_SITES if site in named]


def _cooling_stats(site, until, terms):
    queries = sum(1 for s in SEARCHES if s[0] == site) * len(terms)
    print(f"  {site}: blocked recently; skipping all {queries} queries until {until:%Y-%m-%d %H:%M} UTC "
          f"(delete {THROTTLE_STATE_PATH} to try sooner)", file=sys.stderr)
    return [], {"raw": 0, "kept": 0, "cooldown_until": until.isoformat(), "cooldown_queries": queries}


def fetch_indeed_jobs(lookback_hours=LOOKBACK_DAYS * 24, stats=None, terms=None):
    terms = terms or SEARCH_TERMS
    sites = searched_sites()
    throttle_state = load_json(THROTTLE_STATE_PATH, default={})
    if not isinstance(throttle_state, dict):
        throttle_state = {}
    cooling = {site: until for site in sites if site in THROTTLED_SITES
               and (until := cooldown_until(site, throttle_state))}
    active = [site for site in sites if site not in cooling]
    with ThreadPoolExecutor(max_workers=max(1, len(active))) as pool:
        futures = {site: pool.submit(_scrape_site, site, lookback_hours, terms) for site in active}
        results = {site: f.result() for site, f in futures.items()}

    state_before = dict(throttle_state)
    for site in active:
        if site in THROTTLED_SITES:
            until = record_block_outcome(site, bool(results[site][1]["rate_limited"]), throttle_state)
            if until:
                results[site][1]["cooldown_until"] = until.isoformat()
                print(f"  {site}: pausing this site until {until:%Y-%m-%d %H:%M} UTC so the block can expire",
                      file=sys.stderr)
    if throttle_state != state_before:
        try:
            atomic_write_json(THROTTLE_STATE_PATH, throttle_state)
        except OSError as e:
            print(f"Could not save the rate-limit cooldown state: {e}", file=sys.stderr)
    for site, until in cooling.items():
        results[site] = _cooling_stats(site, until, terms)

    scraped_jobs, seen = [], set()
    for site, (jobs, site_stats) in results.items():
        if stats is not None:
            stats[site] = site_stats
        for job in jobs:
            if job["url"] not in seen:
                seen.add(job["url"])
                scraped_jobs.append(job)
        if site in cooling:
            continue
        print(f"  {site}: {site_stats['raw']} rows, {site_stats['kept']} passed filters"
              + (f", {site_stats['onsite_dropped']} dropped for a stated onsite requirement"
                 if site_stats.get('onsite_dropped') else "")
              + (f", {site_stats['cached_details']} job pages from cache" if site_stats['cached_details'] else "")
              + (f", {site_stats['rate_limited']} rate-limited (429)" if site_stats['rate_limited'] else "")
              + (f", {site_stats['failed_queries']} failed queries" if site_stats['failed_queries'] else "")
              + (f", {site_stats['skipped_queries']} queries skipped" if site_stats.get('skipped_queries') else ""))

    print(f"Scraped and filtered {len(scraped_jobs)} relevant Indeed/LinkedIn postings.")
    return scraped_jobs


if __name__ == "__main__":
    jobs = fetch_indeed_jobs()
    existing = load_json(FILTERED_JOBS_PATH, default=[])
    existing_urls = {j.get("url") for j in existing if j.get("url")}
    new_jobs = [j for j in jobs if j.get("url") not in existing_urls]
    combined = existing + new_jobs
    atomic_write_json(FILTERED_JOBS_PATH, combined)
    print(f"Added {len(new_jobs)} new Indeed/LinkedIn jobs to {FILTERED_JOBS_PATH}")
