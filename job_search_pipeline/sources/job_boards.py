"""Fetch postings from public job-board APIs: Remote OK, Jobicy, Himalayas,
We Work Remotely, and the monthly Hacker News "Who is hiring?" thread.

Jobs are filtered with the same title/company/salary rules as sources/filter.py
and returned in the same shape, with description_ai already filled so
scrape.py skips them.
"""
import html
import json
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from job_search_pipeline.scrape import HEADERS, clean_html
from job_search_pipeline.sources.filter import (
    FOREIGN_RE, LOOKBACK_DAYS, US_PATTERN, is_local_metro, is_valid_company, is_valid_salary, is_valid_title,
)
from job_search_pipeline.config import CFG

_SOURCES_CFG = CFG["sources"]
_TITLES_CFG = CFG["titles"]
_LOC_CFG = CFG["location"]

REMOTEOK_URL = _SOURCES_CFG["remoteok_url"]
JOBICY_URL = _SOURCES_CFG["jobicy_url"]
HN_SEARCH_URL = _SOURCES_CFG["hn_search_url"]
HN_ITEM_URL = _SOURCES_CFG["hn_item_url"]

HN_THREAD_MAX_AGE_DAYS = int(_SOURCES_CFG["hn_thread_max_age_days"])
HIMALAYAS_SEARCH_URL = _SOURCES_CFG["himalayas_search_url"]
HIMALAYAS_MAX_PAGES = int(_SOURCES_CFG["himalayas_max_pages"])
HIMALAYAS_PAUSE_SECONDS = 1
WWR_FEEDS = list(_SOURCES_CFG["wwr_feeds"])
SEARCH_TERMS = list(_SOURCES_CFG["search_terms"])

_US_ELIGIBLE_RE = re.compile(
    _LOC_CFG["global_eligible_pattern"],
    re.I
)

_SALARY_RE = re.compile(
    r'\$\s?(\d[\d,.]*)\s*(k)?\s*(?:-|–|—|to)\s*\$?\s?(\d[\d,.]*)\s*(k)?'
    r'|\$\s?(\d[\d,.]*)\s*(k)\b',
    re.I,
)
_HOURLY_RE = re.compile(r'/\s*(hr|hour)\b|per\s+hour|hourly', re.I)

_HN_ROLE_RE = re.compile(
    _TITLES_CFG["hn_role_pattern"],
    re.I,
)
_HN_NO_REMOTE_RE = re.compile(r'\b(no remote|not remote|remote not|part(?:ial|ially)? remote)\b')
_HN_EXTRA_FOREIGN_RE = re.compile(r'\b(eu|european|cet|germany|netherlands|india|brazil)\b')
_HN_MAX_TITLE_LEN = 80
_HN_US_RE = re.compile(r'\b(US|USA|U\.S\.A?\.?|[Uu]nited [Ss]tates)(?![a-z])')
_HN_NON_FULLTIME_RE = re.compile(r'\b(part[- ]time|contract|freelance)\b')
_HN_FULLTIME_RE = re.compile(r'\bfull[- ]?time\b')
_LOCAL_DISPLAY_NAME = _LOC_CFG["local_display_name"]
# WWR regions are a fixed list ("USA Only", "Europe Only", "Anywhere in the World", ...); "X Only" is a restriction
_WWR_US_REGION_RE = re.compile(r'\b(usa|us|united states|north america|americas)\b', re.I)


def _get_json(url, timeout=30):
    req = urllib.request.Request(url, headers={'User-Agent': HEADERS['User-Agent'],
                                               'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _get_text(url, timeout=30):
    req = urllib.request.Request(url, headers={'User-Agent': HEADERS['User-Agent'],
                                               'Accept': 'application/rss+xml, application/xml'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode('utf-8', errors='replace')


def _parse_date(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _is_recent(dt, days=LOOKBACK_DAYS):
    return dt is not None and datetime.now(timezone.utc) - dt <= timedelta(days=days)


def _to_number(num, k_suffix):
    try:
        v = float(num.replace(',', ''))
    except ValueError:
        return None
    return v * 1000 if k_suffix else v


def parse_usd_salary(text):
    """Pull a USD range like '$150 - 210K' or '$120-160/hr' out of free text.
    Hourly rates are annualized at 2080 hours. Returns {'min','max'} or None."""
    if not text:
        return None
    m = _SALARY_RE.search(text)
    if not m:
        return None
    if m.group(5):
        lo = hi = _to_number(m.group(5), m.group(6))
    else:
        lo_k, hi_k = m.group(2), m.group(4)
        lo = _to_number(m.group(1), lo_k or hi_k)
        hi = _to_number(m.group(3), hi_k)
    if lo is None or hi is None:
        return None
    if _HOURLY_RE.search(text[m.end():m.end() + 12]) or hi < 1000:
        lo, hi = lo * 2080, hi * 2080
    return {"min": int(lo), "max": int(hi)}


def _is_us_eligible(loc_lower):
    """For remote-only boards: reject locations that name only non-allowed regions."""
    if FOREIGN_RE.search(loc_lower):
        return bool(US_PATTERN.search(loc_lower) or _US_ELIGIBLE_RE.search(loc_lower))
    return True


def _passes_filters(job):
    return (is_valid_company(job['company'])
            and is_valid_title(job['title'])
            and is_valid_salary(job))


def fetch_remoteok():
    data = _get_json(REMOTEOK_URL)
    jobs = []
    for item in data:
        if not isinstance(item, dict) or 'position' not in item:
            continue
        posted = _parse_date(item.get('date'))
        if not _is_recent(posted):
            continue
        loc = (item.get('location') or '').strip()
        if not _is_us_eligible(loc.lower()):
            continue
        sal_min, sal_max = int(item.get('salary_min') or 0), int(item.get('salary_max') or 0)
        desc = clean_html(item.get('description', ''))
        job = {
            "title": (item.get('position') or '').strip(),
            "company": (item.get('company') or '').strip(),
            "location": f"Remote - {loc}" if loc else "Remote",
            "remote": True,
            "salary": {"min": sal_min, "max": sal_max} if sal_max else None,
            "url": item.get('url') or item.get('apply_url'),
            "first_seen": posted.isoformat(),
            "description_ai": desc,
            "description_full": desc,
            "ats": "RemoteOK",
        }
        if job['url'] and _passes_filters(job):
            jobs.append(job)
    return jobs


def fetch_jobicy():
    data = _get_json(JOBICY_URL)
    jobs = []
    for item in data.get('jobs', []):
        posted = _parse_date(item.get('pubDate'))
        if not _is_recent(posted):
            continue
        if not any('full' in str(t).lower() for t in (item.get('jobType') or ['Full-Time'])):
            continue
        loc = (item.get('jobGeo') or '').strip()
        if not _is_us_eligible(loc.lower()):
            continue
        salary = None
        if (item.get('salaryCurrency') or 'USD') == 'USD' and item.get('salaryMax'):
            try:
                lo, hi = float(item.get('salaryMin') or item['salaryMax']), float(item['salaryMax'])
                if str(item.get('salaryPeriod') or 'yearly').lower().startswith('hour'):
                    lo, hi = lo * 2080, hi * 2080
                salary = {"min": int(lo), "max": int(hi)}
            except (TypeError, ValueError):
                pass
        desc = clean_html(html.unescape(item.get('jobDescription') or ''))
        job = {
            "title": html.unescape(item.get('jobTitle') or '').strip(),
            "company": html.unescape(item.get('companyName') or '').strip(),
            "location": f"Remote - {loc}" if loc else "Remote",
            "remote": True,
            "salary": salary,
            "url": item.get('url'),
            "first_seen": posted.isoformat(),
            "description_ai": desc,
            "description_full": desc,
            "ats": "Jobicy",
        }
        if job['url'] and _passes_filters(job):
            jobs.append(job)
    return jobs


def _himalayas_job(item):
    """One Himalayas search hit as a pipeline job, or None when it is stale, not full time or not US-eligible."""
    try:
        posted = datetime.fromtimestamp(int(item.get('pubDate')), timezone.utc)
    except (TypeError, ValueError):
        return None
    if not _is_recent(posted):
        return None
    if 'full' not in str(item.get('employmentType') or 'Full Time').lower():
        return None
    places = [str(p) for p in item.get('locationRestrictions') or []]
    loc = ", ".join(places)
    if places and not _is_us_eligible(loc.lower()):
        return None
    salary = None
    if (item.get('currency') or 'USD') == 'USD' and item.get('maxSalary'):
        try:
            lo, hi = float(item.get('minSalary') or item['maxSalary']), float(item['maxSalary'])
            if str(item.get('salaryPeriod') or 'annual').lower().startswith('hour'):
                lo, hi = lo * 2080, hi * 2080
            salary = {"min": int(lo), "max": int(hi)}
        except (TypeError, ValueError):
            pass
    desc = clean_html(item.get('description') or '')
    return {
        "title": (item.get('title') or '').strip(),
        "company": (item.get('companyName') or '').strip(),
        "location": f"Remote - {loc}" if loc else "Remote",
        "remote": True,
        "salary": salary,
        "url": item.get('guid') or (item.get('applicationLink') or '').split('?')[0],
        "first_seen": posted.isoformat(),
        "description_ai": desc,
        "description_full": desc,
        "ats": "Himalayas",
    }


def fetch_himalayas(terms=None):
    """Search Himalayas once per search term, newest first, paging until postings fall out of the lookback."""
    jobs, seen = [], set()
    for term in terms or SEARCH_TERMS:
        for page in range(1, HIMALAYAS_MAX_PAGES + 1):
            query = urllib.parse.urlencode({"q": term, "country": "United States",
                                            "employment_type": "Full Time", "sort": "recent", "page": page})
            items = _get_json(f"{HIMALAYAS_SEARCH_URL}?{query}").get('jobs') or []
            time.sleep(HIMALAYAS_PAUSE_SECONDS)
            for item in items:
                job = _himalayas_job(item)
                if job and job['url'] and job['url'] not in seen and _passes_filters(job):
                    seen.add(job['url'])
                    jobs.append(job)
            oldest = min((int(i.get('pubDate') or 0) for i in items), default=0)
            if len(items) < 20 or not _is_recent(datetime.fromtimestamp(oldest, timezone.utc)):
                break
    return jobs


def _wwr_job(item):
    """One We Work Remotely RSS <item> as a pipeline job, or None when it fails the board-level checks."""
    def text(tag):
        return (item.findtext(tag) or '').strip()
    try:
        posted = parsedate_to_datetime(text('pubDate'))
    except (TypeError, ValueError):
        return None
    if not _is_recent(posted):
        return None
    if text('type') and 'full' not in text('type').lower():
        return None
    company, _, title = text('title').partition(': ')
    if not title:
        return None
    loc = ", ".join(v for v in (text('region'), text('country')) if v)
    if not _is_us_eligible(loc.lower()):
        return None
    if re.search(r'\bonly\b', text('region'), re.I) and not _WWR_US_REGION_RE.search(text('region')):
        return None
    desc = clean_html(html.unescape(text('description')))
    return {
        "title": html.unescape(title),
        "company": html.unescape(company),
        "location": f"Remote - {loc}" if loc else "Remote",
        "remote": True,
        "salary": None,
        "url": text('link') or text('guid'),
        "first_seen": posted.isoformat(),
        "description_ai": desc,
        "description_full": desc,
        "ats": "We Work Remotely",
    }


def fetch_wwr():
    jobs, seen = [], set()
    for feed in WWR_FEEDS:
        root = ET.fromstring(_get_text(feed))
        for item in root.iter('item'):
            job = _wwr_job(item)
            if job and job['url'] and job['url'] not in seen and _passes_filters(job):
                seen.add(job['url'])
                jobs.append(job)
    return jobs


def _hn_title(segments, body):
    """Pick the first role in the header (or body) that passes the title filter."""
    candidates = []
    for seg in segments[1:]:
        seg = re.sub(r'https?://\S+', '', seg)
        candidates += [r.strip() for r in re.split(r'[,;]', seg)]
    candidates += [m.group(1) for m in _HN_ROLE_RE.finditer(body)]
    for role in candidates:
        if role and len(role) <= _HN_MAX_TITLE_LEN and is_valid_title(role):
            return role
    return None


def _hn_location(header, segments):
    """Return a location label if the header says remote (eligible) or local metro."""
    header_lower = header.lower()
    if is_local_metro(header_lower):
        return next((s for s in segments if is_local_metro(s.lower())), _LOCAL_DISPLAY_NAME)
    if 'remote' in header_lower and not _HN_NO_REMOTE_RE.search(header_lower):
        names_foreign = FOREIGN_RE.search(header_lower) or _HN_EXTRA_FOREIGN_RE.search(header_lower)
        if not names_foreign or _HN_US_RE.search(header) or _US_ELIGIBLE_RE.search(header_lower):
            return next((s for s in segments if 'remote' in s.lower()), "Remote")[:_HN_MAX_TITLE_LEN]
    return None


def fetch_hn_whos_hiring():
    hits = _get_json(HN_SEARCH_URL).get('hits', [])
    thread = next((h for h in hits if h.get('title', '').lower().startswith('ask hn: who is hiring')), None)
    if not thread or not _is_recent(_parse_date(thread.get('created_at')), HN_THREAD_MAX_AGE_DAYS):
        return []

    item = _get_json(HN_ITEM_URL.format(thread['objectID']), timeout=60)
    jobs = []
    for c in item.get('children', []):
        text = c.get('text')
        if not text:
            continue
        posted = _parse_date(c.get('created_at'))
        if not _is_recent(posted):
            continue
        body = clean_html(html.unescape(text))
        header = clean_html(html.unescape(text.split('<p>', 1)[0])).strip()
        segments = [s.strip() for s in header.split('|') if s.strip()]
        if len(segments) < 2:
            continue

        header_lower = header.lower()
        if _HN_NON_FULLTIME_RE.search(header_lower) and not _HN_FULLTIME_RE.search(header_lower):
            continue
        location = _hn_location(header, segments)
        if not location:
            continue
        title = _hn_title(segments, body)
        if not title:
            continue

        company = re.sub(r'\(?https?://\S+\)?', '', segments[0]).strip(' -–(')
        job = {
            "title": title,
            "company": company,
            "location": location,
            "remote": 'remote' in location.lower(),
            "salary": parse_usd_salary(header),
            "url": f"https://news.ycombinator.com/item?id={c['id']}",
            "first_seen": posted.isoformat() if posted else thread['created_at'],
            "description_ai": body,
            "description_full": body,
            "ats": "HN Who's Hiring",
        }
        if company and _passes_filters(job):
            jobs.append(job)
    return jobs


def fetch_board_jobs(stats=None):
    """All board postings that pass the filters. Per-board counts (or the error) go into stats."""
    all_jobs = []
    boards = []
    if _SOURCES_CFG["remoteok_enabled"]:
        boards.append(("remoteok", "Remote OK", fetch_remoteok))
    if _SOURCES_CFG["jobicy_enabled"]:
        boards.append(("jobicy", "Jobicy", fetch_jobicy))
    if _SOURCES_CFG["himalayas_enabled"]:
        boards.append(("himalayas", "Himalayas", fetch_himalayas))
    if _SOURCES_CFG["wwr_enabled"]:
        boards.append(("wwr", "We Work Remotely", fetch_wwr))
    if _SOURCES_CFG["hn_enabled"]:
        boards.append(("hn", "HN Who's Hiring", fetch_hn_whos_hiring))

    for key, name, fn in boards:
        try:
            jobs = fn()
            print(f"  {name}: {len(jobs)} postings passed filters")
            all_jobs.extend(jobs)
            if stats is not None:
                stats[key] = {"kept": len(jobs)}
        except Exception as e:
            print(f"  {name}: fetch failed: {e}", file=sys.stderr)
            if stats is not None:
                stats[key] = {"kept": 0, "error": str(e)[:120]}
    return all_jobs


if __name__ == "__main__":
    for j in fetch_board_jobs():
        print(f"[{j['ats']}] {j['company']} | {j['title']} | {j['location']} | {j['salary']} | {j['url']}")
