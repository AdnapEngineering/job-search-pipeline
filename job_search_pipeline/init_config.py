"""`--init-config [SAMPLE_URL ...]`: draft a tailored config from candidate_profile.md with the AI reviewer's CLI.

The LLM fills in the career-specific settings (search terms, title patterns, skills, domain terms, excluded
skills, calibration examples) on top of the career settings from config.example.json. Its answer is checked (types, regexes) before it
is written, and the draft goes to config.draft.json, never over an existing config.json unless asked.
"""
import json
import os
import re
import subprocess
import sys

from job_search_pipeline.check import PLACEHOLDER_KEYS
from job_search_pipeline.config import ROOT_DIR, load_json
from job_search_pipeline.review import (
    CLI_NAMES,
    DEFAULT_MODEL,
    PROFILE_PATH,
    PROVIDER,
    _cli_path,
    _cli_response,
    priced_usd,
)

EXAMPLE_PATH = os.path.join(ROOT_DIR, "config.example.json")
DEFAULT_OUTPUT = os.path.join(ROOT_DIR, "config.draft.json")
TIMEOUT_SECONDS = 600
SAMPLE_JD_CHARS = 3500
MAX_SAMPLES = 3

# Template settings the draft starts from: what every search must set; everything else keeps its default
STARTER_KEYS = {
    "sources": ["search_terms", "searches"],
    "location": ["local_display_name", "local_metro_patterns", "jd_local_metro_pattern", "home_states",
                 "home_timezones"],
    "titles": ["target_patterns", "strong_target_patterns", "primary_role_groups", "exclude_seniority_patterns"],
    "skills_and_scoring": ["core_strengths", "detect_stack_keywords", "focus_keywords", "hard_exclude_skills"],
    "compensation": ["min_salary"],
    "experience": ["max_min_yoe"],
}

# Settings the LLM may fill: (section, key) -> expected shape. Anything else in its reply is ignored.
STR_LIST, REGEX_LIST, INT_MAP, GROUPS, ROLE_GROUPS, INT, STR = (
    "list of strings", "list of regexes", "map of string to integer", "list of lists of strings",
    "list of {label, pattern, points}", "integer", "string")
FIELDS = {
    ("sources", "search_terms"): STR_LIST,
    ("location", "local_display_name"): STR,
    ("location", "local_metro_patterns"): REGEX_LIST,
    ("location", "home_states"): STR_LIST,
    ("location", "home_timezones"): STR_LIST,
    ("titles", "target_patterns"): REGEX_LIST,
    ("titles", "strong_target_patterns"): REGEX_LIST,
    ("titles", "primary_role_groups"): ROLE_GROUPS,
    ("titles", "exclude_seniority_patterns"): REGEX_LIST,
    ("titles", "exclude_role_patterns"): REGEX_LIST,
    ("skills_and_scoring", "core_strengths"): STR_LIST,
    ("skills_and_scoring", "core_strength_weights"): INT_MAP,
    ("skills_and_scoring", "detect_stack_keywords"): STR_LIST,
    ("skills_and_scoring", "focus_keywords"): INT_MAP,
    ("skills_and_scoring", "focus_keyword_groups"): GROUPS,
    ("skills_and_scoring", "hard_exclude_skills"): STR_LIST,
    ("skills_and_scoring", "avoid_keywords"): STR_LIST,
    ("skills_and_scoring", "penalized_keywords"): INT_MAP,
    ("compensation", "min_salary"): INT,
    ("experience", "max_min_yoe"): INT,
    ("llm_review", "calibration_examples"): STR_LIST,
}

PROMPT = """You are drafting the configuration for a job-search pipeline that filters and scores job postings
for one candidate. Read the candidate profile (and any sample postings the candidate would want) and reply with
one JSON object holding the settings below, tailored to this candidate. Do not use tools, browse, or read files.

How the pipeline uses the settings:
- sources.search_terms: 3-8 job titles to search LinkedIn and Indeed for.
- location.*: only if the profile states where the candidate lives; otherwise leave these keys out.
  local_display_name ("Raleigh-Durham, NC"), local_metro_patterns (lower-case city/suburb names as regexes),
  home_states (two-letter codes), home_timezones ("eastern", "central", "mountain", "pacific").
- titles.target_patterns: broad lower-case regexes a title must match to be kept at all (e.g. "\\\\bengineer\\\\b").
- titles.strong_target_patterns: lower-case phrases (regex) naming the candidate's exact target roles.
- titles.primary_role_groups: 1-3 groups of the very best titles, each {{"label", "pattern", "points"}} with
  points 15-25.
- titles.exclude_seniority_patterns: seniority words to reject (regex), e.g. "staff", "principal", "director",
  "vp", "head", and "junior"/"intern" when the candidate is past that level. Do not list the candidate's own level.
- titles.exclude_role_patterns: title words for adjacent roles the candidate does not want (regex, word-bounded
  like "\\\\bdata\\\\b").
- skills_and_scoring.core_strengths: 8-20 lower-case skills/tools the candidate is strong in, as they appear in
  postings ("react", "typescript", "sql"). Each one found in a posting adds points.
- skills_and_scoring.core_strength_weights: optional per-skill points (3-10) so the primary stack outweighs
  peripheral skills; skills left out score 5.
- skills_and_scoring.detect_stack_keywords: the same skills in display case ("React", "TypeScript").
- skills_and_scoring.focus_keywords: 8-20 lower-case domain terms or competencies from the candidate's best work,
  each with a weight 5-15 (15 = the heart of what they want to do).
- skills_and_scoring.focus_keyword_groups: optional lists of synonyms in focus_keywords that are one concept
  (["accessibility", "wcag"]).
- skills_and_scoring.hard_exclude_skills: lower-case skills that rule a posting out when it requires them (a
  different primary language or specialty the candidate lacks). Keep this short and certain.
- skills_and_scoring.avoid_keywords: lower-case technologies to reject when asked for outright (optional).
- skills_and_scoring.penalized_keywords: lower-case terms that lower the score (map to 5-15) without rejecting.
- compensation.min_salary: annual base pay floor in dollars, only if the profile states one.
- experience.max_min_yoe: the most minimum years of experience a posting may ask for (the candidate's years plus
  about 2).
- llm_review.calibration_examples: 3-4 one-line examples that anchor the AI reviewer, in exactly this format:
  "Company, Role: what the posting asks and why it fits or not. hard H, core C, evidence E, level L [NN%]."
  where hard is 0-4, core 0-3, evidence 0-3, level 0-2. Include one strong fit (about 95%), one partial fit
  (about 70%) and one poor fit with a blocker (about 30%, end with "[30%, blocker]"). Base them on the sample
  postings when given, otherwise on realistic archetypes for this candidate.

All regexes are Python regexes matched against lower-case text; escape backslashes for JSON.

CANDIDATE PROFILE
{profile}
{samples}
Reply with the JSON object only, shaped like:
{{"sources": {{"search_terms": [...]}}, "titles": {{...}}, "skills_and_scoring": {{...}}, "experience": {{...}},
"llm_review": {{"calibration_examples": [...]}}}}
"""


def _samples_block(urls, out=print):
    """The sample postings as prompt text; postings that cannot be fetched are skipped with a note."""
    if not urls:
        return ""
    from job_search_pipeline.explain import build_job
    blocks = []
    for url in urls[:MAX_SAMPLES]:
        try:
            job, _ = build_job(url)
        except Exception as e:  # a bad sample must not cost the whole draft
            out(f"  Skipping sample {url}: {type(e).__name__}: {e}")
            continue
        jd = (job.get("description_full") or job.get("description_ai") or "").strip()
        if len(jd) < 200:
            out(f"  Skipping sample {url}: no description could be fetched")
            continue
        out(f"  Sample: {job.get('company')} · {job.get('title') or url} ({len(jd):,} characters)")
        blocks.append(f"Company: {job.get('company')}\nTitle: {job.get('title') or 'unknown'}\n"
                      f"Posting text:\n{jd[:SAMPLE_JD_CHARS]}")
    if len(urls) > MAX_SAMPLES:
        out(f"  Only the first {MAX_SAMPLES} sample URLs are used.")
    if not blocks:
        return ""
    return ("\nSAMPLE POSTINGS THE CANDIDATE WOULD WANT\n"
            + "\n\n".join(f"--- Sample {i + 1} ---\n{b}" for i, b in enumerate(blocks)) + "\n")


def parse_reply(text):
    """The JSON object in an LLM reply (code fences and surrounding prose allowed), or None."""
    text = re.sub(r"```(?:json)?", "", text or "")
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _regex_ok(pattern):
    try:
        re.compile(pattern)
        return True
    except re.error:
        return False


def _clean(value, shape):
    """(cleaned value or None, list of problems) for one field against its expected shape."""
    problems = []
    if shape in (STR_LIST, REGEX_LIST):
        if not isinstance(value, list):
            return None, [f"expected a {shape}"]
        items = [v.strip() for v in value if isinstance(v, str) and v.strip()]
        if shape == REGEX_LIST:
            bad = [p for p in items if not _regex_ok(p)]
            problems += [f"dropped invalid regex {p!r}" for p in bad]
            items = [p for p in items if p not in bad]
        return (list(dict.fromkeys(items)) or None), problems
    if shape == INT_MAP:
        if not isinstance(value, dict):
            return None, [f"expected a {shape}"]
        out = {}
        for k, v in value.items():
            if isinstance(k, str) and k.strip() and isinstance(v, (int, float)) and not isinstance(v, bool):
                out[k.strip().lower()] = int(v)
            else:
                problems.append(f"dropped {k!r}: {v!r}")
        return (out or None), problems
    if shape == GROUPS:
        if not isinstance(value, list):
            return None, [f"expected a {shape}"]
        groups = [[s.strip().lower() for s in g if isinstance(s, str) and s.strip()]
                  for g in value if isinstance(g, list)]
        return ([g for g in groups if len(g) > 1] or None), problems
    if shape == ROLE_GROUPS:
        if not isinstance(value, list):
            return None, [f"expected a {shape}"]
        groups = []
        for g in value:
            if not (isinstance(g, dict) and isinstance(g.get("pattern"), str) and g["pattern"].strip()):
                problems.append(f"dropped role group {g!r}")
                continue
            if not _regex_ok(g["pattern"]):
                problems.append(f"dropped role group with invalid regex {g['pattern']!r}")
                continue
            points = g.get("points", 20)
            groups.append({"label": str(g.get("label") or "Target Role"), "pattern": g["pattern"],
                           "points": int(points) if isinstance(points, (int, float)) else 20})
        return (groups or None), problems
    if shape == INT:
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            return int(value), problems
        return None, [f"expected a positive {shape}"]
    if shape == STR:
        return (value.strip() or None, problems) if isinstance(value, str) else (None, [f"expected a {shape}"])
    raise ValueError(shape)


def build_draft(reply, template):
    """(config draft, fields filled, problems): the template with every valid field from the LLM's reply laid
    over it."""
    draft = json.loads(json.dumps(template))
    filled, problems = [], []
    for (section, key), shape in FIELDS.items():
        value = (reply.get(section) or {}).get(key) if isinstance(reply.get(section), dict) else None
        if value is None:
            continue
        cleaned, issues = _clean(value, shape)
        problems += [f"{section}.{key}: {p}" for p in issues]
        if cleaned is not None:
            draft.setdefault(section, {})[key] = cleaned
            filled.append(f"{section}.{key}")
    # Keep the stack display list in step with core_strengths when the LLM left it out
    skills = draft.get("skills_and_scoring") or {}
    if (reply.get("skills_and_scoring") or {}).get("core_strengths") and not (
            reply.get("skills_and_scoring") or {}).get("detect_stack_keywords"):
        skills["detect_stack_keywords"] = [s.title() if s.islower() else s for s in skills.get("core_strengths", [])]
    return draft, filled, problems


def starter_config(example):
    """The STARTER_KEYS settings of the template, as the base of a new draft."""
    return {section: {k: example[section][k] for k in keys if k in example.get(section, {})}
            for section, keys in STARTER_KEYS.items()}


def remaining_placeholders(draft, templates):
    """Career-specific settings the draft still holds at a template's placeholder value."""
    left = []
    for section, key in PLACEHOLDER_KEYS:
        value = (draft.get(section) or {}).get(key)
        if value is not None and any(value == (t.get(section) or {}).get(key) for t in templates):
            left.append(f"{section}.{key}")
    return left


def init_config(sample_urls=(), output=None, force=False, out=print):
    """Draft a config with the AI reviewer's CLI and write it. Returns a process exit code."""
    output = os.path.abspath(output or DEFAULT_OUTPUT)
    if os.path.exists(output) and output != DEFAULT_OUTPUT and not force:
        out(f"{output} already exists; pass --force to overwrite it, or leave out --init-output to write "
            f"{os.path.basename(DEFAULT_OUTPUT)}.")
        return 1
    try:
        with open(PROFILE_PATH, encoding="utf-8") as f:
            profile = f.read().strip()
    except OSError:
        out(f"{PROFILE_PATH} not found. Copy candidate_profile.example.md to it and describe your background first.")
        return 1
    executable = _cli_path(PROVIDER)
    if not executable:
        out(f"The {CLI_NAMES[PROVIDER]} CLI for llm_review.provider {PROVIDER!r} was not found. Install it or set "
            "llm_review.provider; `uv run python -m job_search_pipeline --check` shows what is missing.")
        return 1
    template = starter_config(load_json(EXAMPLE_PATH, default={}) or {})

    out(f"Drafting a config from {os.path.basename(PROFILE_PATH)} with {PROVIDER}"
        + (f" ({DEFAULT_MODEL})" if DEFAULT_MODEL else "") + "...")
    prompt = PROMPT.format(profile=profile, samples=_samples_block(list(sample_urls), out))
    try:
        reply_text, usage = _cli_response(executable, PROVIDER, DEFAULT_MODEL, prompt, timeout=TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        out(f"The {CLI_NAMES[PROVIDER]} CLI did not answer within {TIMEOUT_SECONDS}s.")
        return 1
    except (RuntimeError, OSError, ValueError) as e:
        out(f"The {CLI_NAMES[PROVIDER]} CLI failed ({type(e).__name__}: {e}); check its login and model settings.")
        return 1
    reply = parse_reply(reply_text)
    if reply is None:
        out("The reply held no JSON object, so nothing was written. Try again, or set llm_review.model to a "
            "stronger model.")
        return 1

    draft, filled, problems = build_draft(reply, template)
    with open(output, "w", encoding="utf-8") as f:
        json.dump(draft, f, indent=2, ensure_ascii=False)
        f.write("\n")

    out(f"\nWrote {output}")
    out(f"  Filled in: {', '.join(filled) or 'nothing'}")
    for p in problems:
        out(f"  ! {p}")
    left = remaining_placeholders(draft, [template])
    if left:
        out(f"  Still at placeholder values, edit these by hand: {', '.join(left)}")
    if usage:
        usd = usage.get("usd") if usage.get("usd") is not None else priced_usd(usage)
        tokens = usage["input"] + usage["cached"] + usage["output"]
        out(f"  Used {tokens:,} tokens" + (f" (${usd:.3f})" if usd is not None else ""))
    if os.path.basename(output) != "config.json":
        out(f"\nReview it, then `cp {os.path.relpath(output)} config.json` (or merge the parts you want into your "
            "existing config.json), and run `uv run python -m job_search_pipeline --check`.")
    return 0


if __name__ == "__main__":
    sys.exit(init_config(sys.argv[1:]))
