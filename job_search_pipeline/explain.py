"""`--explain <url>`: trace one posting through the Stage 1 pre-filters, the Stage 3 rules and the keyword score.

The posting's company, title and location come from data/enriched_jobs.json or data/filtered_jobs.json when
the pipeline has already seen it; otherwise the company is guessed from the URL and the title and location can
be given with --title / --location. The description is taken from the saved row, or scraped now.
"""
import asyncio
import re
from urllib.parse import urlparse

import aiohttp

from job_search_pipeline.blocklist import is_blocked, load_blocklist, load_role_blocklist
from job_search_pipeline.config import CFG, get_path, load_json
from job_search_pipeline.evaluate import (DROP_RULES, SECONDARY_FLOOR, STRONG_FLOOR, evaluate_job_traced,
                                          score_breakdown)
from job_search_pipeline.sources.filter import (is_recent, is_valid_company, is_valid_location, is_valid_salary,
                                                title_reject_reason)
from job_search_pipeline.urls import normalize_url

ENRICHED_JOBS = get_path("enriched_jobs")
FILTERED_JOBS = get_path("filtered_jobs")
REVIEW_MIN_SCORE = int(CFG["skills_and_scoring"]["min_review_score"])

# ATS hosts whose first path segment is the company's board slug
_SLUG_HOSTS = re.compile(r'(?:^|\.)(?:greenhouse\.io|lever\.co|ashbyhq\.com|workable\.com|bamboohr\.com|'
                         r'recruitee\.com|breezy\.hr|smartrecruiters\.com)$')
PASS, FAIL, SKIP = "✓", "✗", "·"
STAGE1_CHECKS = {
    "not_recent": "Recent (sources.lookback_days)",
    "company": "Company (companies.exclude_companies / exclude_recruiters)",
    "location": "Location (location.*)",
    "title": "Title (titles.*)",
    "salary": "Pay (compensation.min_salary)",
}


def find_saved(url):
    """A copy of the saved row for `url` (enriched first, since it holds the JD) and the file it came from."""
    key = normalize_url(url)
    for path in (ENRICHED_JOBS, FILTERED_JOBS):
        for job in load_json(path, default=[]) or []:
            if isinstance(job, dict) and job.get("url") and normalize_url(job["url"]) == key:
                return dict(job), path
    return None, None


def company_from_url(url):
    """A best-effort company name: the board slug on an ATS host, else the site's name."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    parts = [p for p in parsed.path.split("/") if p]
    if _SLUG_HOSTS.search(host) and parts and parts[0] not in ("embed", "job", "jobs", "j"):
        return parts[0].replace("-", " ")
    if host.endswith("myworkdayjobs.com"):
        return host.split(".")[0]
    labels = [h for h in host.split(".") if h not in ("www", "jobs", "careers", "boards", "apply")]
    return labels[-2] if len(labels) >= 2 else host


async def _scrape(job):
    from job_search_pipeline.scrape import detect_stack, extract_requirements, fetch_jd
    async with aiohttp.ClientSession() as session:
        desc = await fetch_jd(session, job)
    job["description_full"] = desc
    job["description_ai"] = extract_requirements(desc)
    job["stack_detected"] = detect_stack(desc)


def build_job(url, title=None, company=None, location=None, rescrape=False):
    """(job dict, where its fields came from). Saved rows are reused; the JD is scraped when missing."""
    job, path = find_saved(url)
    origin = f"saved row in {path}" if job else "not seen by the pipeline yet"
    job = job or {"url": url}
    if company or not job.get("company"):
        job["company"] = company or company_from_url(url)
    if title:
        job["title"] = title
    if location is not None:
        job["location"] = location
    if rescrape or not (job.get("description_full") or job.get("description_ai")):
        asyncio.run(_scrape(job))
        origin += "; description scraped now"
    return job, origin


def stage1_trace(job):
    """[(mark, label, detail)] for each Stage 1 pre-filter, in the order filter.passes_filters checks them."""
    first_seen = job.get("first_seen")
    loc, remote = job.get("location", ""), job.get("remote") is True
    title_reason = title_reject_reason(job.get("title", ""))
    checks = [
        ("not_recent", None if not first_seen else is_recent(first_seen),
         "no first_seen (not from a feed)" if not first_seen else f"first seen {first_seen[:10]}"),
        ("company", is_valid_company(job.get("company")), job.get("company") or "no company"),
        ("location", is_valid_location(loc, remote), f"{loc or 'no location'}" + (" (remote flag)" if remote else "")),
        ("title", not title_reason, title_reason or job.get("title") or "no title"),
        ("salary", is_valid_salary(job), _salary_text(job.get("salary"))),
    ]
    return [(SKIP if ok is None else PASS if ok else FAIL, STAGE1_CHECKS[rule], detail) for rule, ok, detail in checks]


def _salary_text(salary):
    if not isinstance(salary, dict) or not any(salary.get(k) for k in ("max", "p90", "p75")):
        return "no pay band in the feed"
    return f"top of band {salary.get('max') or salary.get('p90') or salary.get('p75')}"


def explain(url, title=None, company=None, location=None, rescrape=False, out=print):
    """Print the full trace for one posting. Returns 0, or 1 when the posting could not be built."""
    try:
        job, origin = build_job(url, title, company, location, rescrape)
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
        out(f"Could not fetch {url}: {type(e).__name__}: {e}")
        return 1

    jd = job.get("description_full") or job.get("description_ai") or ""
    out(f"Explaining {url}")
    out(f"  Company:  {job.get('company') or '?'}")
    out(f"  Title:    {job.get('title') or '? (pass --title to set it)'}")
    out(f"  Location: {job.get('location') or 'not specified'}" + (" · remote flag set" if job.get("remote") is True else ""))
    out(f"  Source:   {origin}")
    out(f"  JD:       {len(jd):,} characters" + (f" ({jd[:60]!r}...)" if len(jd) < 200 else ""))

    out("\nStage 1 · pre-filters (feed rules; LinkedIn/Indeed rows use a similar title/location gate)")
    for mark, label, detail in stage1_trace(job):
        out(f"  {mark} {label}: {detail}")

    out("\nStage 3 · deterministic rules")
    trace, parts = [], []
    result, rejection = evaluate_job_traced(job, trace, parts)
    for step in trace:
        mark = PASS if step["passed"] else FAIL
        out(f"  {mark} {step['rule']}" + (f": {step['detail']}" if step["detail"] else ""))
    if rejection:
        out(f"  → DROPPED by {rejection['rejected_by']} ({DROP_RULES.get(rejection['rejected_by'], '')})")

    if result and result["category"] == "Manual Review":
        out(f"  → MANUAL REVIEW: {result['reason']}")
        return 0

    if not parts and rejection and rejection["rejected_by"] != "expired" and len(jd) >= 100:
        out("\nScore breakdown (what it would have scored without the rule that dropped it)")
        score, parts = score_breakdown(job)
    elif parts:
        out("\nScore breakdown")
        score = result["score"] if result else min(100, sum(p for _, p in parts))
    for label, points in parts:
        out(f"  {points:+4d}  {label}")
    if parts:
        raw = sum(p for _, p in parts)
        tier = ("Strong" if score >= STRONG_FLOOR else "Secondary" if score >= SECONDARY_FLOOR
                else "below the Secondary floor")
        out(f"  = {raw}" + (f" (capped at {score})" if raw != score else "")
            + f" → {tier} (Strong ≥ {STRONG_FLOOR}, Secondary ≥ {SECONDARY_FLOOR})")

    if not result:
        return 0

    blocked = is_blocked(job, load_blocklist(), load_role_blocklist())
    out(f"\nBlocklist: {'already synced, exported or dismissed' if blocked else 'not blocked'}")
    if blocked:
        return 0
    if result["score"] >= REVIEW_MIN_SCORE:
        out(f"\nResult: {result['category']} match at {result['score']}%; goes to the AI review "
            f"(min_review_score {REVIEW_MIN_SCORE}). Reason: {result['reason']}")
    else:
        out(f"\nResult: {result['category']} match at {result['score']}%, under min_review_score {REVIEW_MIN_SCORE}, "
            f"so it is kept on its keyword score without an AI review. Reason: {result['reason']}")
    return 0
