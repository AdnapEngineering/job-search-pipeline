"""Stage 1: fetch postings from every source, apply cheap pre-filters, and merge them into filtered_jobs.json.

The GitHub ATS feed (~1.4M rows) is streamed chunk by chunk; LinkedIn/Indeed and the
public job boards are fetched by linkedin_indeed.py and job_boards.py. Aggregator
reposts are re-pointed at the company's own ATS posting when the feed has it.
"""
import asyncio
import gzip
import json
import re
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta

import aiohttp

from job_search_pipeline.blocklist import load_blocklist, role_key
from job_search_pipeline.config import (
    CFG,
    atomic_write_json,
    compile_alternation,
    compile_regex_list,
    get_path,
    load_json,
)
from job_search_pipeline.urls import is_aggregator_url, normalize_url

_SOURCES_CFG = CFG["sources"]
_LOC_CFG = CFG["location"]
_TITLES_CFG = CFG["titles"]
_COMP_CFG = CFG["compensation"]
_COMPANIES_CFG = CFG["companies"]

MANIFEST_URL = _SOURCES_CFG["github_feed_manifest_url"]
BASE_CHUNK_URL = _SOURCES_CFG["github_feed_base_chunk_url"]
CHUNK_CONCURRENCY = int(_SOURCES_CFG["chunk_concurrency"])
LOOKBACK_DAYS = int(_SOURCES_CFG["lookback_days"])
MIN_SALARY = float(_COMP_CFG["min_salary"])

def _unique(items):
    return list(dict.fromkeys(i for i in items if i))


# Places outside the home country: see location.foreign_places / foreign_cities in defaults.py
FOREIGN_PLACES = _unique(_LOC_CFG["foreign_places"])
FOREIGN_CITIES = _unique(_LOC_CFG["foreign_cities"])
FOREIGN_RE = compile_alternation(FOREIGN_PLACES, word_bounded=True) or re.compile(r'(?!x)x')
FOREIGN_CITY_RE = compile_alternation(FOREIGN_CITIES, word_bounded=True) or re.compile(r'(?!x)x')
US_PATTERN = re.compile(_LOC_CFG["allowed_country_pattern"])


def names_foreign_place(text):
    """True when lower-case `text` names a foreign place and not the home country."""
    return bool(FOREIGN_RE.search(text)) and not US_PATTERN.search(text)

_EXCLUDE_TECH_PATTERNS = compile_regex_list(_TITLES_CFG["exclude_tech_patterns"])

_STRONG_TARGET_LIST = _TITLES_CFG["strong_target_patterns"]
STRONG_TARGET_RE = (
    compile_alternation(_STRONG_TARGET_LIST, word_bounded=True)
    if _STRONG_TARGET_LIST
    else re.compile(r"(?!x)x")
)
# Title words that reject a role. soft_exclude_role_patterns reject it too, unless the title is a strong target;
# they need not also be listed in exclude_role_patterns.
_SOFT_ROLE_EXCLUDES = set(_unique(_TITLES_CFG["soft_exclude_role_patterns"] or []))
_EXCLUDE_ROLE_PATTERNS = compile_regex_list(_unique(
    (_TITLES_CFG["exclude_role_patterns"] or []) + sorted(_SOFT_ROLE_EXCLUDES)))

_TARGET_PATTERNS = compile_regex_list(_TITLES_CFG["target_patterns"])
# One combined pass to cheaply skip non-target titles before the full title rules run
_TARGET_PREFILTER_RE = (
    re.compile('|'.join(p.pattern for p in _TARGET_PATTERNS))
    if _TARGET_PATTERNS
    else re.compile(r'.')
)

_COMPANY_EXCLUDE = compile_alternation(_COMPANIES_CFG["exclude_companies"], word_bounded=True)
_RECRUITER_EXCLUDE = compile_alternation(_COMPANIES_CFG["exclude_recruiters"], word_bounded=True)
_RECRUITER_ALLOW = compile_alternation(_COMPANIES_CFG["allow_recruiters"], word_bounded=True)

_LOCAL_METRO_RE = compile_alternation(_LOC_CFG["local_metro_patterns"], word_bounded=True)
_LOCAL_METRO_EXCLUDE_RE = compile_alternation(_LOC_CFG["local_metro_exclude_patterns"], word_bounded=True)
_LOCAL_STATE_ONLY_PATTERN = _LOC_CFG["local_state_only_pattern"]
_LOCAL_STATE_ONLY_RE = re.compile(_LOCAL_STATE_ONLY_PATTERN) if _LOCAL_STATE_ONLY_PATTERN else None
ALLOW_REMOTE = bool(_LOC_CFG["allow_remote"])

SOURCE_STATS_PATH = get_path("source_stats")
FILTERED_JOBS_PATH = get_path("filtered_jobs")
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=30)

UNSPECIFIED_LOCATIONS = {'', 'not specified', 'unspecified', 'n/a', 'none'}
_TITLE_ONSITE_RE = re.compile(r'\b(hybrid|on-?site|in[- ]office)\b')


def is_recent(first_seen_str):
    if not first_seen_str:
        return False
    try:
        fs_date = datetime.fromisoformat(first_seen_str)
        if fs_date.tzinfo is None:
            fs_date = fs_date.replace(tzinfo=UTC)
    except (AttributeError, TypeError, ValueError):
        return False
    return datetime.now(UTC) - fs_date <= timedelta(days=LOOKBACK_DAYS)


def is_remote(loc):
    loc = (loc or "").lower()
    return "remote" in loc or "anywhere" in loc


def is_local_metro(loc):
    loc = (loc or '').lower()
    if not _LOCAL_METRO_RE and not _LOCAL_STATE_ONLY_RE:
        return False
    if _LOCAL_METRO_EXCLUDE_RE and _LOCAL_METRO_EXCLUDE_RE.search(loc):
        return False
    return bool(
        (_LOCAL_METRO_RE and _LOCAL_METRO_RE.search(loc))
        or (_LOCAL_STATE_ONLY_RE and _LOCAL_STATE_ONLY_RE.match(loc))
    )


def is_valid_location(loc, remote_flag=False):
    loc_lower = (loc or '').strip().lower()

    # Some ATS feeds omit location entirely. Trust the feed's remote flag when set;
    # otherwise let the scraper and evaluator decide from the JD text.
    if loc_lower in UNSPECIFIED_LOCATIONS:
        return True

    if names_foreign_place(loc_lower):
        return False

    if ALLOW_REMOTE and (remote_flag or is_remote(loc_lower)):
        return True

    return is_local_metro(loc_lower)


def title_reject_reason(title):
    """Why the title rules drop a title (naming the config key), or '' when it passes."""
    t = (title or "").lower()

    tech = next((p for p in _EXCLUDE_TECH_PATTERNS if p.search(t)), None)
    if tech:
        return f'matches titles.exclude_tech_patterns "{tech.pattern}"'

    if _TITLE_ONSITE_RE.search(t) and 'remote' not in t and not is_local_metro(t):
        return 'title says hybrid/onsite/in-office and names neither remote nor the local metro'

    strong = STRONG_TARGET_RE.search(t)
    role = next((p for p in _EXCLUDE_ROLE_PATTERNS
                 if p.search(t) and not (strong and p.pattern in _SOFT_ROLE_EXCLUDES)), None)
    if role:
        key = 'soft_exclude_role_patterns' if role.pattern in _SOFT_ROLE_EXCLUDES else 'exclude_role_patterns'
        return f'matches titles.{key} "{role.pattern}"'

    if strong or any(p.search(t) for p in _TARGET_PATTERNS):
        return ''
    return 'matches none of titles.target_patterns / strong_target_patterns'


def is_valid_title(title):
    return not title_reject_reason(title)


def is_valid_salary(job):
    if MIN_SALARY <= 0:
        return True
    sal = job.get('salary')
    if not sal:
        return True  # Unverified is okay at ingestion stage

    top_band = sal.get('max') or sal.get('p90') or sal.get('p75')
    if top_band is None:
        return True

    try:
        top_band = float(top_band)
    except (TypeError, ValueError):
        return True

    return top_band >= MIN_SALARY


def is_valid_company(company):
    if not company:
        return False
    company = company.lower()
    if _COMPANY_EXCLUDE and _COMPANY_EXCLUDE.search(company):
        return False
    if _RECRUITER_EXCLUDE and _RECRUITER_EXCLUDE.search(company):
        return bool(_RECRUITER_ALLOW and _RECRUITER_ALLOW.search(company))
    return True


# Stage 1 drop reasons, in the order passes_filters checks them, with the config they come from
PREFILTER_RULES = {
    "no_url": "No posting URL",
    "not_recent": "Older than sources.lookback_days",
    "company": "Company excluded (companies.exclude_companies / exclude_recruiters)",
    "location": "Location not remote or local metro (location.*)",
    "title": "Title rules (titles.*)",
    "salary": "Top of pay band under compensation.min_salary",
    "keyword_score": "No target-title or skill signal (LinkedIn/Indeed keyword gate)",
}


def filter_reason(job):
    """The PREFILTER_RULES key of the first Stage 1 filter that drops `job`, or None when it passes."""
    # Cheapest checks first: the recency window alone drops most of the ~1.4M feed rows
    if not job.get('url'):
        return "no_url"
    if not is_recent(job.get('first_seen')):
        return "not_recent"
    if not is_valid_company(job.get('company')):
        return "company"
    if not is_valid_location(job.get('location', ''), job.get('remote') is True):
        return "location"
    if not is_valid_title(job.get('title', '')):
        return "title"
    if not is_valid_salary(job):
        return "salary"
    return None


def passes_filters(job):
    return filter_reason(job) is None


def _ats_index_rows(jobs):
    """(role key, URL, first_seen) for every ATS posting with a target title, at any age.
    Promoted aggregator reposts look new while the feed first saw the posting weeks ago,
    so the recency filter would otherwise drop the original ATS posting they should link to."""
    rows = []
    for j in jobs:
        url = j.get('url')
        title = j.get('title') or ''
        if (url and j.get('first_seen') and _TARGET_PREFILTER_RE.search(title.lower())
                and not is_aggregator_url(url) and is_valid_title(title) and is_valid_company(j.get('company'))):
            key = role_key(j.get('company'), j.get('title'))
            if key:
                rows.append((key, url, j['first_seen']))
    return rows


def build_ats_index(rows):
    """Role key -> {'url': newest posting's URL, 'posted': earliest first_seen date}."""
    index = {}
    for key, url, first_seen in rows:
        entry = index.get(key)
        if entry is None:
            index[key] = {'url': url, 'latest': first_seen, 'posted': first_seen[:10]}
            continue
        if first_seen > entry['latest']:
            entry['url'], entry['latest'] = url, first_seen
        entry['posted'] = min(entry['posted'], first_seen[:10])
    return index


def link_ats_postings(jobs, ats_index):
    """Point LinkedIn/Indeed/job-board rows at the company's own ATS posting when the feed has it,
    and keep the earlier of the two posted dates. Returns how many rows were re-pointed."""
    linked = 0
    for j in jobs:
        if not is_aggregator_url(j.get('url')):
            continue
        entry = ats_index.get(role_key(j.get('company'), j.get('title')))
        if not entry:
            continue
        j['url'] = entry['url']
        j['posted'] = min(filter(None, (j.get('posted'), entry['posted'])))
        linked += 1
    return linked


def _filter_chunk(data):
    jobs = json.loads(gzip.decompress(data))
    kept, dropped = [], Counter()
    for j in jobs:
        reason = filter_reason(j)
        if reason is None:
            kept.append(j)
        else:
            dropped[reason] += 1
    return len(jobs), kept, _ats_index_rows(jobs), dropped


async def fetch_chunk(session, semaphore, url):
    """Download one gzipped chunk and return (rows fetched, rows that pass filters, ATS index rows,
    Counter of drop reasons), or None when the download fails, so the full feed is never held in memory at once."""
    async with semaphore:
        try:
            async with session.get(url, timeout=HTTP_TIMEOUT) as response:
                response.raise_for_status()
                data = await response.read()
            return await asyncio.to_thread(_filter_chunk, data)
        except Exception as e:
            print(f"Failed to fetch {url}: {e}", file=sys.stderr)
            return None


def prefer_ats_postings(jobs):
    """Drop aggregator copies of a role whose direct ATS posting is also in the list."""
    ats_roles = {role_key(j.get('company'), j.get('title')) for j in jobs
                 if not is_aggregator_url(j.get('url'))}
    ats_roles.discard('')
    return [j for j in jobs
            if not (is_aggregator_url(j.get('url')) and role_key(j.get('company'), j.get('title')) in ats_roles)]


async def fetch_github_feed(stats):
    """Stream every feed chunk; returns (rows passing filters, ATS role index) and records feed stats."""
    print("Fetching GitHub job-board manifest...")
    connector = aiohttp.TCPConnector(limit=50, limit_per_host=20)
    semaphore = asyncio.Semaphore(CHUNK_CONCURRENCY)

    async with aiohttp.ClientSession(connector=connector) as session:
        async with session.get(MANIFEST_URL, timeout=HTTP_TIMEOUT) as response:
            response.raise_for_status()
            manifest = await response.json(content_type=None)

        chunks = manifest.get('chunks', [])
        print(f"Found {len(chunks)} chunks. Fetching (concurrency={CHUNK_CONCURRENCY})...")
        results = await asyncio.gather(*(fetch_chunk(session, semaphore, BASE_CHUNK_URL + c) for c in chunks))

    failed_chunks = sum(1 for r in results if r is None)
    results = [r for r in results if r is not None]
    fetched = sum(r[0] for r in results)
    print(f"Total jobs fetched: {fetched}" + (f" ({failed_chunks} chunks failed)" if failed_chunks else ""))

    filtered = [job for _, kept, _, _ in results for job in kept]
    ats_index = build_ats_index(row for _, _, rows, _ in results for row in rows)
    dropped = sum((d for _, _, _, d in results), Counter())
    print(f"Total jobs after filtering: {len(filtered)}")
    stats["github_feed"] = {
        "fetched": fetched,
        "kept": len(filtered),
        "chunks": len(chunks),
        "failed_chunks": failed_chunks,
        "dropped": dict(dropped.most_common()),
        "last_updated": manifest.get("last_updated"),
    }
    return filtered, ats_index


def fetch_jobspy(stats):
    """LinkedIn/Indeed rows; a failure is recorded in stats instead of aborting the other sources."""
    try:
        # Imported lazily: linkedin_indeed imports this module, and jobspy is slow to import
        from job_search_pipeline.sources.linkedin_indeed import fetch_indeed_jobs
        print("Fetching Indeed/LinkedIn postings...")
        return fetch_indeed_jobs(stats=stats)
    except Exception as e:
        print(f"Notice: Indeed/LinkedIn fetch skipped or encountered an error: {e}")
        searched = {s[0] for s in _SOURCES_CFG["searches"]} & {"linkedin", "indeed", "glassdoor"}
        for site in sorted(searched) or ["linkedin", "indeed"]:
            stats[site] = {"raw": 0, "kept": 0, "error": str(e)[:120]}
        return []


def fetch_boards(stats):
    """Remote OK / Jobicy / Himalayas / WWR / HN rows; fetch_board_jobs records per-board errors in stats itself."""
    try:
        from job_search_pipeline.sources.job_boards import fetch_board_jobs
        print("Fetching public job boards (Remote OK, Jobicy, Himalayas, We Work Remotely, HN Who's Hiring)...")
        return fetch_board_jobs(stats=stats)
    except Exception as e:
        print(f"Notice: job board fetch skipped or encountered an error: {e}")
        return []


def merge_with_existing(fresh_jobs, ats_index, blocked_urls):
    """Previously saved jobs plus unseen fresh ones, minus blocked, stale, excluded and duplicate-aggregator rows."""
    existing_jobs = load_json(FILTERED_JOBS_PATH, default=[])
    link_ats_postings(existing_jobs, ats_index)
    seen_urls = {normalize_url(j.get("url", "")) for j in existing_jobs} - {""}

    new_jobs = []
    for j in fresh_jobs:
        u = normalize_url(j.get("url", ""))
        if u and u not in seen_urls and u not in blocked_urls:
            new_jobs.append(j)
            seen_urls.add(u)
    print(f"New jobs after deduplication: {len(new_jobs)}")

    merged = [j for j in existing_jobs + new_jobs
              if normalize_url(j.get("url", "")) not in blocked_urls
              and is_recent(j.get("first_seen"))
              and is_valid_company(j.get("company"))]
    before = len(merged)
    merged = prefer_ats_postings(merged)
    if before > len(merged):
        print(f"Dropped {before - len(merged)} aggregator copies of roles that have an ATS posting.")
    merged.sort(key=lambda x: x.get('first_seen', ''), reverse=True)
    return merged


async def main():
    filtered, ats_index, stats = [], {}, {}

    if _SOURCES_CFG["github_feed_enabled"]:
        try:
            filtered, ats_index = await fetch_github_feed(stats)
        except (TimeoutError, aiohttp.ClientError, ValueError, KeyError) as e:
            # A down or malformed feed manifest must not cost the run its other sources
            print(f"GitHub feed fetch failed, continuing with the other sources: {type(e).__name__}: {e}",
                  file=sys.stderr)
            stats["github_feed"] = {"fetched": 0, "kept": 0, "error": f"{type(e).__name__}: {e}"[:120]}
    if _SOURCES_CFG["jobspy_enabled"]:
        filtered.extend(fetch_jobspy(stats))
        print(f"Combined total (GitHub + Indeed/LinkedIn): {len(filtered)}")
    if _SOURCES_CFG["job_boards_enabled"]:
        filtered.extend(fetch_boards(stats))
        print(f"Combined total (GitHub + Indeed/LinkedIn + boards): {len(filtered)}")

    stats["fetched_at"] = datetime.now(UTC).isoformat()
    try:
        atomic_write_json(SOURCE_STATS_PATH, stats)
    except OSError as e:
        print(f"Could not save source stats: {e}", file=sys.stderr)

    linked = link_ats_postings(filtered, ats_index)
    if linked:
        print(f"Re-pointed {linked} LinkedIn/Indeed/board rows at the company's ATS posting.")

    blocked_urls = load_blocklist()
    print(f"Loaded {len(blocked_urls)} blocked URLs (canonical).")

    merged = merge_with_existing(filtered, ats_index, blocked_urls)
    atomic_write_json(FILTERED_JOBS_PATH, merged)
    print(f"Saved {len(merged)} jobs to {FILTERED_JOBS_PATH}")


if __name__ == "__main__":
    asyncio.run(main())
