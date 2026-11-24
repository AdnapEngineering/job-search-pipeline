"""Stage 2: enrich filtered jobs with full job descriptions.

Already-enriched jobs are skipped. Each posting is fetched from its ATS's public
JSON API when one is recognized (Ashby, Lever, Greenhouse, BambooHR, Workday CXS),
falling back to scraping the HTML page. Closed or expired postings are marked so
later stages can drop them.
"""
import asyncio
import html as html_lib
import json
import re
import sys
from urllib.parse import parse_qs, urlparse

import aiohttp
from bs4 import BeautifulSoup

from job_search_pipeline.config import CFG, atomic_write_json, get_path, load_json

_SKILLS_CFG = CFG["skills_and_scoring"]
_SCRAPER_CFG = CFG["scraper"]

SCRAPE_CONCURRENCY = int(_SCRAPER_CFG["concurrency"])
DETECT_STACK_KEYWORDS = (
    _SKILLS_CFG.get("detect_stack_keywords")
    or _SKILLS_CFG.get("core_strengths")
    or []
)

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,application/json,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9'
}
JSON_HEADERS = {
    'User-Agent': HEADERS['User-Agent'],
    'Accept': 'application/json'
}

_TIMEOUT_SHORT = aiohttp.ClientTimeout(total=10)
_TIMEOUT_WORKDAY = aiohttp.ClientTimeout(total=8)
_TIMEOUT_FALLBACK = aiohttp.ClientTimeout(total=15)
_MIN_API_DESC_LEN = 50
_NO_DESCRIPTION = "No description extracted"

_MULTI_NEWLINE_RE = re.compile(r'\n\s*\n+')
_APOS = r'[\x27\u2019]'  # career pages often use a curly apostrophe ("What you’ll bring")
_REQUIREMENTS_RE = re.compile(
    r'(?i)(?:requirements|qualifications|what you(?:' + _APOS + r'll|' + _APOS + r're| will| need to)? bring'
    r'|what we(?:' + _APOS + r're| are)? looking for|who you are|basic qualifications'
    r'|minimum qualifications)[\s\S]{100,2500}'
)
# LinkedIn/Indeed descriptions are markdown, so a heading is often bold ("**Required Qualifications**")
_REQUIREMENTS_HEADING_RE = re.compile(
    r'(?im)^[ \t]*(?:#+\s*)?(?:\*\*|__)?[ \t]*(?:job |minimum |basic |key |all )?'
    r'(?:requirements|qualifications|required (?:skills|qualifications)'
    r'|what you(?:' + _APOS + r'll|' + _APOS + r're| will| need to)? bring'
    r'|wh(?:at|o) we(?:' + _APOS + r're| are)? looking for|who you are|about you|you (?:have|bring)|what you have'
    r'|you might be a fit|(?:this role|you) (?:could|would|might) be a (?:great |good )?fit if you have'
    r'|this role requires|must\\?[- ]haves?(?: skills)?'
    r'|what you(?:' + _APOS + r'll| will| should)? (?:need|have))\b[^\n]{0,40}$'
)
# Headings that follow the requirements: the window stops there, so perks ("grow into distributed systems")
# are not read as asks
_AFTER_REQUIREMENTS_HEADING_RE = re.compile(
    r'(?im)^[ \t]*(?:#+\s*)?(?:\*\*|__)?[ \t]*(?:why (?:join|work)|(?:our |the )?(?:benefits|perks)|what we offer'
    r'|compensation|salary|pay (?:range|transparency)|base pay|about us|equal (?:employment )?opportunity'
    r'|how to apply|life at)\b[^\n]{0,60}$'
)
_EEO_BOILERPLATE_RE = re.compile(
    r'(?i)without regard to|race, color|equal (?:employment )?opportunity|reasonable accommodation'
)
_JOB_DESC_CLASS_RE = re.compile(r'job-description|description', re.IGNORECASE)
_CLOSED_POSTING_RE = re.compile(
    r'current openings at|no longer (?:accepting applications|available|open)'
    r'|job (?:not found|has been filled|is closed)|position has been filled', re.IGNORECASE
)
_FOR_PARAM_RE = re.compile(r'for=([^&]+)')
_TOKEN_PARAM_RE = re.compile(r'token=([^&]+)')


def clean_html(html_str):
    if not html_str:
        return ''
    if '&lt;' in html_str:
        html_str = html_lib.unescape(html_str)
    try:
        soup = BeautifulSoup(html_str, 'html.parser')
        for s in soup(['script', 'style', 'nav', 'header', 'footer', 'noscript', 'svg']):
            s.extract()
        for tag in soup.find_all(['br', 'p', 'div', 'li', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6']):
            tag.insert_after('\n')
        text = soup.get_text()
        text = _MULTI_NEWLINE_RE.sub('\n\n', text)
        return text.strip()
    except Exception:
        return ''


def extract_requirements(full_text):
    if not full_text:
        return ''
    if len(full_text) <= 3500:
        return full_text
    m = _REQUIREMENTS_HEADING_RE.search(full_text)
    if m:
        window = full_text[m.start():m.start() + 2500]
        end = _AFTER_REQUIREMENTS_HEADING_RE.search(window, 200)
        return (window[:end.start()] if end else window).strip()
    for m in _REQUIREMENTS_RE.finditer(full_text):
        if not _EEO_BOILERPLATE_RE.search(m.group(0)[:300]):
            return m.group(0).strip()
    return full_text[:3000].strip()


def detect_stack(text):
    text_lower = (text or "").lower()
    return [
        kw for kw in DETECT_STACK_KEYWORDS
        if re.search(r'(?<![\w.+#])' + re.escape(kw.lower()) + r'(?![\w+#])', text_lower)
    ]


_api_failures = {}  # host -> count of ATS API calls that failed for a reason other than "no such posting"


def _note_api_failure(url, why):
    host = urlparse(url).netloc
    _api_failures[host] = _api_failures.get(host, 0) + 1
    print(f"[scrape] ATS API call failed ({why}): {url}", file=sys.stderr)


async def _get_json(session, url, headers=None, timeout=None):
    """GET that returns a parsed JSON object, or None when the API has no such posting (404/410) or the call
    fails. Network errors, bad JSON and unexpected statuses are logged and counted, since the caller then
    falls back to HTML scraping; anything else (a bug) is raised."""
    try:
        async with session.get(url, headers=headers or HEADERS, timeout=timeout or _TIMEOUT_SHORT) as r:
            if r.status == 200:
                data = await r.json(content_type=None)
                return data if isinstance(data, dict) else None
            if r.status not in (404, 410):
                _note_api_failure(url, f"HTTP {r.status}")
    except TimeoutError:
        _note_api_failure(url, "timeout")
    except (aiohttp.ClientError, ValueError) as e:
        _note_api_failure(url, type(e).__name__)
    return None


def _path_parts(path):
    return path.strip('/').split('/')


async def _fetch_ashby(session, url, parsed, company):
    if 'ashbyhq.com' not in parsed.netloc.lower():
        return None
    parts = _path_parts(parsed.path)
    if len(parts) < 2:
        return None
    org, job_id = parts[0], parts[1].lower()
    data = await _get_json(session, f'https://api.ashbyhq.com/posting-api/job-board/{org}')
    for item in (data or {}).get('jobs', []):
        if job_id in item.get('id', '').lower() or job_id in item.get('jobUrl', '').lower():
            desc = clean_html(item.get('descriptionHtml') or item.get('descriptionPlain') or '')
            if desc:
                return desc
    return None


async def _fetch_lever(session, url, parsed, company):
    if 'lever.co' not in parsed.netloc.lower():
        return None
    parts = _path_parts(parsed.path)
    if len(parts) < 2:
        return None
    data = await _get_json(session, f'https://api.lever.co/v0/postings/{parts[0]}/{parts[1]}')
    if not data:
        return None
    lists_text = ''.join(f"\n\n{lst.get('text', '')}:\n" + clean_html(lst.get('content', ''))
                         for lst in data.get('lists', []))
    full_desc = clean_html(data.get('description', '')) + lists_text + '\n\n' + clean_html(data.get('additional', ''))
    return full_desc if len(full_desc.strip()) > _MIN_API_DESC_LEN else None


def _greenhouse_board_and_job(url, parsed, company):
    """(board token, job id) from a Greenhouse URL, or from a gh_jid param on a company career site."""
    gh_jid = parse_qs(parsed.query).get('gh_jid', [None])[0]
    if 'greenhouse.io' not in parsed.netloc.lower():
        return (company, gh_jid) if gh_jid else (None, None)
    parts = _path_parts(parsed.path)
    if len(parts) >= 3 and parts[1] == 'jobs':
        return parts[0], parts[2]
    board = None
    if 'for=' in url:
        for_m = _FOR_PARAM_RE.search(url)
        if for_m:
            board = for_m.group(1)
        token_m = _TOKEN_PARAM_RE.search(url)
        if token_m:
            gh_jid = token_m.group(1)
    return board, gh_jid


async def _fetch_greenhouse(session, url, parsed, company):
    board, job_id = _greenhouse_board_and_job(url, parsed, company)
    if not (board and job_id):
        return None
    data = await _get_json(session, f'https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{job_id}')
    desc = clean_html((data or {}).get('content', ''))
    return desc if len(desc) > _MIN_API_DESC_LEN else None


async def _fetch_bamboohr(session, url, parsed, company):
    netloc = parsed.netloc.lower()
    if 'bamboohr.com' not in netloc:
        return None
    job_id = _path_parts(parsed.path)[-1]
    data = await _get_json(session, f'https://{netloc}/careers/{job_id}/detail', headers=JSON_HEADERS)
    desc = clean_html((data or {}).get('result', {}).get('jobOpening', {}).get('description', ''))
    return desc if len(desc) > _MIN_API_DESC_LEN else None


async def _fetch_workday(session, url, parsed, company):
    """Workday CXS API; the site segment is not always in the public URL, so try up to 3 patterns."""
    netloc = parsed.netloc.lower()
    if 'myworkdayjobs.com' not in netloc:
        return None
    parts = _path_parts(parsed.path)
    tenant = netloc.split('.')[0]
    if parts[0] not in ('en-US', 'en'):
        site = parts[0]
    else:
        site = parts[1] if len(parts) > 1 else tenant
    job_slug = parts[-1]
    for cxs_site in (site, tenant, 'careers'):
        cxs_url = f'https://{netloc}/wday/cxs/{tenant}/{cxs_site}/job/{job_slug}'
        data = await _get_json(session, cxs_url, headers=JSON_HEADERS, timeout=_TIMEOUT_WORKDAY)
        desc = clean_html((data or {}).get('jobPostingInfo', {}).get('jobDescription', ''))
        if len(desc) > _MIN_API_DESC_LEN:
            return desc
    return None


def _description_from_html(html):
    """JD text from a career page, a closed-posting marker, or None when nothing useful was found."""
    try:
        soup = BeautifulSoup(html, 'html.parser')
        page_text = soup.get_text(' ', strip=True)[:3000]
        if _CLOSED_POSTING_RE.search(page_text):
            return "Posting expired (closed-posting page)"
        job_desc_div = soup.find('div', class_=_JOB_DESC_CLASS_RE)
        if job_desc_div:
            desc = clean_html(str(job_desc_div))
            if len(desc) > 100:
                return desc
        desc = clean_html(html)
        if len(desc) > 200:
            return desc
    except Exception:  # noqa: S110 -- any parse failure falls through to the next description source
        pass
    return None


def _redirected_to_board(path, final_path):
    """True when a posting URL was redirected up to its job board (a closed posting). A redirect to another
    posting-level page is not: some boards re-issue a live posting under a new id and forward the old link."""
    if final_path is None:
        return False
    job_id = path.rstrip('/').split('/')[-1]
    if not job_id or job_id in final_path:
        return False
    depth = len([p for p in path.split('/') if p])
    return len([p for p in final_path.split('/') if p]) < depth


async def _fetch_html(session, url, path):
    """Scrape the posting page itself, detecting expired postings by status code, redirect, or page text."""
    try:
        async with session.get(url, headers=HEADERS, timeout=_TIMEOUT_FALLBACK) as r:
            if r.status in (404, 410):
                return f"Posting expired (HTTP {r.status})"
            if r.status != 200:
                return _NO_DESCRIPTION
            if _redirected_to_board(path, r.url.path if r.history else None):
                return "Posting expired (redirected to job board)"
            return _description_from_html(await r.text(errors='replace')) or _NO_DESCRIPTION
    except TimeoutError:
        return "Error: timeout on direct scrape"
    except Exception as e:
        return f"Error: {e}"


# Structured ATS JSON APIs, tried in order before falling back to HTML scraping
_ATS_FETCHERS = (_fetch_ashby, _fetch_lever, _fetch_greenhouse, _fetch_bamboohr, _fetch_workday)


async def fetch_jd(session, job):
    """Full job description text, or a 'Posting expired ...' / 'Error: ...' / 'No description extracted' marker."""
    url = job.get('url', '')
    if not url:
        return _NO_DESCRIPTION
    parsed = urlparse(url)
    company = (job.get('company') or '').lower()
    for fetcher in _ATS_FETCHERS:
        desc = await fetcher(session, url, parsed, company)
        if desc:
            return desc
    return await _fetch_html(session, url, parsed.path)


_PRESCRAPED_ATS = {'indeed', 'linkedin', 'remoteok', 'remotive', 'jobicy', 'himalayas', 'we work remotely',
                   "hn who's hiring"}


def _needs_full_jd(job):
    """Older entries saved only the trimmed requirements text; the evaluator needs the full JD.
    Pre-scraped sources already hold the full text, so it is backfilled from description_ai."""
    if job.get('description_full'):
        return False
    if (job.get('ats') or '').lower() in _PRESCRAPED_ATS:
        job['description_full'] = job.get('description_ai', '')
        return False
    return True


def _has_complete_jd(job):
    """True when the job already holds a usable, open, full description and needs no re-scrape."""
    needs_full_jd = _needs_full_jd(job)  # always run: it backfills description_full for pre-scraped sources
    desc = job.get('description_ai') or ''
    return (not needs_full_jd and len(desc) > 100 and not desc.startswith('Error')
            and not _CLOSED_POSTING_RE.search(desc[:3000]))


async def process_job(semaphore, session, job):
    if _has_complete_jd(job):
        if not job.get('stack_detected'):
            job['stack_detected'] = detect_stack(job.get('description_ai', ''))
        return job

    async with semaphore:
        desc = await fetch_jd(session, job)

    job['description_ai'] = extract_requirements(desc)
    job['description_full'] = desc
    job['stack_detected'] = detect_stack(desc)
    return job


def _load_enriched(output_file):
    """Previously enriched jobs worth keeping (complete or known-expired), keyed by URL."""
    keep = {}
    for j in load_json(output_file, default=[]):
        if not j.get('url'):
            continue
        if _has_complete_jd(j) or (j.get('description_ai') or '').startswith('Posting expired'):
            keep[j['url']] = j
    return keep


async def main():
    input_file = sys.argv[1] if len(sys.argv) > 1 else get_path("filtered_jobs")
    output_file = sys.argv[2] if len(sys.argv) > 2 else get_path("enriched_jobs")

    with open(input_file, encoding='utf-8') as f:  # noqa: ASYNC230 -- one small read before any fetches start
        jobs = json.load(f)

    existing_by_url = _load_enriched(output_file)
    jobs_to_scrape = [j for j in jobs if j.get('url') not in existing_by_url]

    print(f"Skipping {len(jobs) - len(jobs_to_scrape)} already enriched jobs.")
    print(f"Fetching descriptions for {len(jobs_to_scrape)} new jobs (concurrency={SCRAPE_CONCURRENCY})...")

    semaphore = asyncio.Semaphore(SCRAPE_CONCURRENCY)
    connector = aiohttp.TCPConnector(limit=50, limit_per_host=10, ttl_dns_cache=300)

    async with aiohttp.ClientSession(connector=connector) as session:
        tasks = [process_job(semaphore, session, j) for j in jobs_to_scrape]
        new_enriched = await asyncio.gather(*tasks)

    success_count = sum(
        1 for j in new_enriched
        if len(j.get('description_ai', '')) > 100 and not j.get('description_ai', '').startswith('Posting expired')
    )
    print(f"Completed: {success_count}/{len(jobs_to_scrape)} new descriptions successfully extracted.")
    if _api_failures:
        detail = ", ".join(f"{host} x{n}" for host, n in sorted(_api_failures.items()))
        print(f"WARNING: {sum(_api_failures.values())} ATS API call(s) failed and fell back to HTML scraping: {detail}",
              file=sys.stderr)

    merged_by_url = {**existing_by_url, **{j['url']: j for j in new_enriched if j.get('url')}}
    seen = set()
    all_enriched = []
    for j in jobs:
        url = j.get('url')
        if url and url not in seen and url in merged_by_url:
            all_enriched.append(merged_by_url[url])
            seen.add(url)

    atomic_write_json(output_file, all_enriched)
    print(f"Saved {len(all_enriched)} total enriched jobs to {output_file}")


if __name__ == "__main__":
    asyncio.run(main())
