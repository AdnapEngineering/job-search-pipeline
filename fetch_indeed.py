"""Stage 1 source: LinkedIn and Indeed via python-jobspy.

Both sites are scraped in parallel threads. LinkedIn job-detail pages are cached on
disk between runs to avoid HTTP 429s, and rows without a verifiable remote/local
location are kept only when the JD text supports it.
"""
import logging
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pandas as pd
from jobspy import scrape_jobs

from evaluate_jobs import JD_HYBRID_ONSITE_RE, JD_US_REMOTE_RE
from fetch_and_filter import (
    FILTERED_JOBS_PATH, FOREIGN_PATTERNS, LOOKBACK_DAYS, MIN_SALARY, US_PATTERN, is_local_metro, is_valid_company,
)
from pipeline_config import CFG, atomic_write_json, compile_alternation, compile_regex_list, get_path, load_json
from url_normalizer import is_aggregator_url, normalize_url

_TITLES_CFG = CFG.get("titles") or {}
_SKILLS_CFG = CFG.get("skills_and_scoring") or {}
_SOURCES_CFG = CFG.get("sources") or {}

_EXCLUDE_TECH_PATTERNS = compile_regex_list(_TITLES_CFG.get("exclude_tech_patterns", []))
_EXCLUDE_SENIORITY = compile_alternation(
    _TITLES_CFG.get("exclude_seniority_patterns", ["staff", "principal", "director", "manager", "intern", "lead", "architect", "contract", "entry level"]),
    word_bounded=True
)

_JD_STRONG_REMOTE_RE = re.compile(
    r'\b(?:fully|100%|completely|entirely) remote\b|\bremote[- ](?:first|friendly)\b'
    r'|\bremote\s*[-–(]\s*(?:us|usa|united states)\b|\((?:fully )?remote\)|\blocation:\s*remote\b'
    r'|\bwork(?:ing)? from home\b|\bwork(?:ing)? remotely\b'
    r'|\bremote (?:work|onboarding|position|role|opportunity|employees?)\b', re.I
)
_JD_WEAK_REMOTE_RE = re.compile(r'\bremote(?:ly)?\b', re.I)
_JD_OFFICE_RE = re.compile(
    r'\bin[- ]office perks\b|\brelocation (?:expense|assistance|package|support|coverage)'
    r'|\bfree (?:meals|lunch)\b[^.\n]{0,30}\b(?:hub|office)\b|\bteam (?:is )?based in\b'
    r'|\b(?:\d+|three|four|five) days? (?:a|per) week\b', re.I
)

_HEAVY_EXCLUSIONS = [
    re.compile(r'(?<![\w.#+])' + re.escape(t.lower()) + r'(?![\w#+])')
    for t in (_SKILLS_CFG.get("hard_exclude_skills") or [])
]

_CORE_STRENGTHS = [s.lower() for s in (_SKILLS_CFG.get("core_strengths") or [])]
_FOCUS_AREAS = [k.lower() for k in (_SKILLS_CFG.get("focus_keywords") or {})]
_PRIMARY_ROLE_TERMS = [t.lower() for t in (_TITLES_CFG.get("primary_role_patterns") or [])]

SEARCH_TERMS = _SOURCES_CFG.get("search_terms") or ["Software Engineer"]
SEARCHES = [tuple(s) for s in (_SOURCES_CFG.get("searches") or [
    ["linkedin", "United States", " remote", 40],
    ["indeed", "Remote", "", 40],
])]
LINKEDIN_PAUSE_SECONDS = int(_SOURCES_CFG.get("linkedin_pause_seconds", 3))
COUNTRY_INDEED = _SOURCES_CFG.get("country_indeed", "USA")

LI_DETAILS_CACHE_PATH = get_path("linkedin_details_cache", "data/linkedin_details_cache.json")
LI_DETAILS_CACHE_DAYS = LOOKBACK_DAYS + 1


def is_valid_jobspy_title(title):
    """Search results are already on-target by query, so only seniority and tech exclusions apply."""
    t = (title or "").lower()
    if _EXCLUDE_SENIORITY and _EXCLUDE_SENIORITY.search(t):
        return False
    return not any(p.search(t) for p in _EXCLUDE_TECH_PATTERNS)


def remote_evidence(loc, desc="", remote_flag=False):
    """Return 'onsite', 'remote', 'unverified' or 'none' for a non-local row."""
    loc_lower = (loc or "").lower()
    text = (desc or "").replace("\\", "")
    if JD_HYBRID_ONSITE_RE.search(text) or _JD_OFFICE_RE.search(text):
        return "onsite"
    if ("remote" in loc_lower or "work from home" in loc_lower
            or JD_US_REMOTE_RE.search(text) or _JD_STRONG_REMOTE_RE.search(text)):
        return "remote"
    if remote_flag or _JD_WEAK_REMOTE_RE.search(text):
        return "unverified"
    return "none"


def is_valid_jobspy_location(loc, desc="", remote_flag=False):
    """Local metro, or a non-foreign location whose JD shows (or at least hints at) remote work."""
    loc_lower = (loc or "").lower()

    if any(p.search(loc_lower) for p in FOREIGN_PATTERNS) and not US_PATTERN.search(loc_lower):
        return False

    if is_local_metro(loc_lower):
        return True

    return remote_evidence(loc, desc, remote_flag) in ("remote", "unverified")


def score_job(title, desc):
    desc_lower = (desc or "").lower()
    t_lower = (title or "").lower()

    heavy_count = sum(1 for p in _HEAVY_EXCLUSIONS if p.search(desc_lower))
    if heavy_count >= 2:
        return 0, []

    matched = set()
    score = 0

    for s in _CORE_STRENGTHS:
        if s in desc_lower:
            score += 12
            matched.add(s.capitalize())

    for f in _FOCUS_AREAS:
        if f in desc_lower or f in t_lower:
            score += 15
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
    """Wrap jobspy's LinkedIn._get_job_details with a disk cache keyed by job id."""
    try:
        from jobspy.linkedin import LinkedIn
        from jobspy.model import JobType
    except ImportError:
        return lambda: 0
    original = getattr(LinkedIn, "_get_job_details", None)
    if original is None:
        return lambda: 0

    cutoff = datetime.now(timezone.utc).timestamp() - LI_DETAILS_CACHE_DAYS * 86400
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
            return details
        details = original(self, job_id)
        if details.get("description"):
            stored = dict(details)
            stored["job_type"] = [t.name for t in details.get("job_type") or []]
            cache[job_id] = {"fetched": datetime.now(timezone.utc).timestamp(), "details": stored}
        return details

    LinkedIn._get_job_details = cached_details

    def finish():
        LinkedIn._get_job_details = original
        try:
            atomic_write_json(LI_DETAILS_CACHE_PATH, cache)
        except (OSError, TypeError) as e:
            print(f"Could not save LinkedIn details cache: {e}", file=sys.stderr)
        return hits
    return finish


class _ErrorCounter(logging.Handler):
    """Counts jobspy ERROR records (LinkedIn 429s are logged, not raised)."""
    def __init__(self):
        super().__init__(level=logging.ERROR)
        self.count = 0
        self.rate_limited = 0

    def emit(self, record):
        self.count += 1
        if "429" in record.getMessage():
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


def _row_to_job(row):
    """Convert one jobspy row to a pipeline job dict, or None when it fails filters."""
    direct = row.get("job_url_direct")
    url = (normalize_url(direct) if isinstance(direct, str) and direct and not is_aggregator_url(direct)
           else row.get("job_url"))
    if not url:
        return None

    title = str(row.get("title") or "")
    desc = str(row.get("description") or "")

    if not is_valid_jobspy_title(title):
        return None
    company = str(row.get("company")) if pd.notna(row.get("company")) else ""
    if company and not is_valid_company(company):
        return None

    raw_loc = str(row.get("location") or "")
    row_remote = row.get("is_remote")
    remote_flag = pd.notna(row_remote) and bool(row_remote)
    if not is_valid_jobspy_location(raw_loc, desc, remote_flag):
        return None
    evidence = "local" if is_local_metro(raw_loc) else remote_evidence(raw_loc, desc, remote_flag)

    sal_min = row.get("min_amount")
    sal_max = row.get("max_amount")
    if MIN_SALARY > 0 and pd.notna(sal_max):
        try:
            if float(sal_max) < MIN_SALARY:
                return None
        except (TypeError, ValueError):
            pass

    score, reasons = score_job(title, desc)
    if score == 0:
        return None

    return {
        "title": title,
        "company": company,
        "location": _display_location(raw_loc, evidence),
        "remote": evidence == "remote",
        "salary": _salary_from_row(sal_min, sal_max),
        "url": url,
        "first_seen": datetime.now(timezone.utc).isoformat(),
        "posted": str(row["date_posted"])[:10] if pd.notna(row.get("date_posted")) else None,
        "description_ai": desc,
        "description_full": desc,
        "stack_detected": reasons,
        "ats": str(row.get("site") or "Indeed").capitalize(),
        "score": score,
    }


def _scrape_site(site, lookback_hours, terms):
    logger = logging.getLogger("JobSpy:LinkedIn" if site == "linkedin" else "JobSpy:Indeed")
    counter = _ErrorCounter()
    logger.addHandler(counter)
    finish_cache = _install_linkedin_details_cache() if site == "linkedin" else (lambda: 0)
    jobs, raw, failed = [], 0, 0
    try:
        for location, suffix, wanted in [(loc, sfx, n) for s, loc, sfx, n in SEARCHES if s == site]:
            for term in terms:
                try:
                    df = scrape_jobs(
                        site_name=[site],
                        search_term=term + suffix,
                        location=location,
                        results_wanted=wanted,
                        hours_old=lookback_hours,
                        country_indeed=COUNTRY_INDEED,
                        linkedin_fetch_description=True,
                        enforce_annual_salary=True,
                    )
                except Exception as e:
                    failed += 1
                    print(f"Error querying {site} for {term + suffix} ({location}): {e}", file=sys.stderr)
                    continue
                raw += len(df)
                for _, row in df.iterrows():
                    job = _row_to_job(row)
                    if job:
                        jobs.append(job)
                if site == "linkedin":
                    time.sleep(LINKEDIN_PAUSE_SECONDS)
    finally:
        logger.removeHandler(counter)
        cache_hits = finish_cache()
    return jobs, {"raw": raw, "kept": len(jobs), "failed_queries": failed,
                  "errors": counter.count, "rate_limited": counter.rate_limited, "cached_details": cache_hits}


def fetch_indeed_jobs(lookback_hours=LOOKBACK_DAYS * 24, stats=None, terms=None):
    terms = terms or SEARCH_TERMS
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {site: pool.submit(_scrape_site, site, lookback_hours, terms) for site in ("linkedin", "indeed")}
        results = {site: f.result() for site, f in futures.items()}

    scraped_jobs, seen = [], set()
    for site, (jobs, site_stats) in results.items():
        if stats is not None:
            stats[site] = site_stats
        for job in jobs:
            if job["url"] not in seen:
                seen.add(job["url"])
                scraped_jobs.append(job)
        print(f"  {site}: {site_stats['raw']} rows, {site_stats['kept']} passed filters"
              + (f", {site_stats['cached_details']} job pages from cache" if site_stats['cached_details'] else "")
              + (f", {site_stats['rate_limited']} rate-limited (429)" if site_stats['rate_limited'] else "")
              + (f", {site_stats['failed_queries']} failed queries" if site_stats['failed_queries'] else ""))

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
