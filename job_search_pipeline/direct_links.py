"""Re-point job-board links (Glassdoor by default) at the employer's own posting before a role is synced.

Glassdoor never hands out the employer's link: its apply button is a Glassdoor redirect, and Easy Apply
listings have no outside link at all. So the role is looked up on the company's public Greenhouse, Lever
or Ashby board instead. Boards come from direct links the pipeline has already seen for that company
(the GitHub feed and ATS rows), then from slugs guessed from its name and website. A role matches when
its title, or the heading line of its JD, equals a posting's title.

When a board is found but holds no such role, the job keeps its board link and gets a note, since a
listing missing from the employer's own board is often stale or not a real opening.
"""
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

from job_search_pipeline.blocklist import canon_company, load_blocklist
from job_search_pipeline.config import CFG, get_path, load_json
from job_search_pipeline.scrape import HEADERS
from job_search_pipeline.urls import normalize_url

RESOLVE_HOSTS = tuple(h.lower() for h in CFG["url_normalization"]["resolve_direct_hosts"])
ATS_NAMES = {"greenhouse": "Greenhouse", "lever": "Lever", "ashby": "Ashby"}

_NON_ALNUM_RE = re.compile(r'[^a-z0-9]+')
_LEGAL_SUFFIX_RE = re.compile(r'[,\s]+(?:inc|llc|ltd|corp|corporation|co|company|plc|gmbh|lp)\.?$', re.IGNORECASE)
# Glassdoor titles often carry a location or remote tag the employer's title lacks
_TITLE_TAG_RE = re.compile(r'\s*[\(\[][^)\]]*(?:remote|hybrid|onsite|on-site|us|usa)[^)\]]*[\)\]]\s*$', re.IGNORECASE)
_TITLE_TAIL_RE = re.compile(r'\s+[-–—|]\s+(?:remote|hybrid|us|usa|united states)\b.*$', re.IGNORECASE)


def _norm_title(title):
    t = _TITLE_TAIL_RE.sub('', _TITLE_TAG_RE.sub('', title or ''))
    t = re.sub(r'\bsr\b\.?', 'senior', t.lower()).replace('&', 'and')
    return _NON_ALNUM_RE.sub(' ', t).strip()


def needs_resolving(url):
    return bool(url) and any(h in url.lower() for h in RESOLVE_HOSTS)


def board_from_url(url):
    """(ats, slug) for a Greenhouse, Lever or Ashby posting URL, else None."""
    parsed = urllib.parse.urlparse(url or '')
    host, parts = parsed.netloc.lower(), [p for p in parsed.path.split('/') if p]
    if 'greenhouse.io' in host:
        board = urllib.parse.parse_qs(parsed.query).get('for', [None])[0]
        if board:
            return ('greenhouse', board.lower())
        if parts and parts[0] not in ('embed', 'v1'):
            return ('greenhouse', parts[0].lower())
    elif host.endswith('lever.co') and parts:
        return ('lever', parts[0].lower())
    elif host.endswith('ashbyhq.com') and parts:
        return ('ashby', parts[0].lower())
    return None


def _company_key(name):
    return canon_company(_NON_ALNUM_RE.sub('', (name or '').lower().replace('&', 'and')))


def known_boards(jobs, urls=()):
    """company key -> boards seen in direct links, from saved jobs (with their company names)."""
    boards = {}
    for job in jobs:
        board = board_from_url(job.get('url'))
        if board:
            boards.setdefault(_company_key(job.get('company')), []).append(board)
    for url in urls:  # blocklisted links carry no company name, only the board slug
        board = board_from_url(url)
        if board:
            boards.setdefault(_company_key(board[1]), []).append(board)
    return boards


def slug_guesses(company, site=None):
    """[(slug, trusted)] a company's board is likely to use. Trusted slugs (its full name squashed, its
    website's domain label; Glassdoor gives the website) are taken to be its own board, so a board there
    without the role earns a note. Its first word, alone or with inc/hq, is only good for an exact match."""
    name = (company or '').strip()
    bare = _LEGAL_SUFFIX_RE.sub('', name)
    words = [w for w in _NON_ALNUM_RE.split(bare.lower()) if w]
    guesses = [(_NON_ALNUM_RE.sub('', bare.lower()), True), ('-'.join(words), True),
               (_NON_ALNUM_RE.sub('', name.lower()), True)]
    if site:
        host = urllib.parse.urlparse(site if '//' in site else f'//{site}').netloc.lower()
        guesses.append((host.removeprefix('www.').split('.')[0], True))
    if words:
        guesses += [(words[0], False), (words[0] + 'inc', False), (words[0] + 'hq', False)]
    out = {}
    for slug, trusted in guesses:
        if slug and len(slug) > 1:
            out[slug] = out.get(slug, False) or trusted
    return list(out.items())


def _get_json(url, timeout=15):
    req = urllib.request.Request(url, headers={'User-Agent': HEADERS['User-Agent'], 'Accept': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return None


def fetch_board(ats, slug):
    """[(title, url)] on one public board, or None when the board does not exist or can't be read."""
    q = urllib.parse.quote(slug)
    if ats == 'greenhouse':
        data = _get_json(f'https://boards-api.greenhouse.io/v1/boards/{q}/jobs')
        jobs = data.get('jobs') if isinstance(data, dict) else None
        return None if jobs is None else [(j.get('title', ''), j.get('absolute_url', '')) for j in jobs]
    if ats == 'lever':
        data = _get_json(f'https://api.lever.co/v0/postings/{q}?mode=json')
        return None if not isinstance(data, list) else [(j.get('text', ''), j.get('hostedUrl', '')) for j in data]
    if ats == 'ashby':
        data = _get_json(f'https://api.ashbyhq.com/posting-api/job-board/{q}')
        jobs = data.get('jobs') if isinstance(data, dict) else None
        return None if jobs is None else [(j.get('title', ''), j.get('jobUrl', '')) for j in jobs]
    return None


def _jd_heading(job):
    for line in (job.get('description_full') or job.get('description_ai') or '').splitlines():
        line = line.strip().strip('*#_ ').strip()
        if line:
            return line if len(line) <= 120 else ''
    return ''


def match_posting(job, postings):
    """The one posting URL whose title equals the job's title (or its JD heading), else None."""
    wanted = {t for t in (_norm_title(job.get('title')), _norm_title(_jd_heading(job))) if t}
    urls = {url for title, url in postings if url and _norm_title(title) in wanted}
    return urls.pop() if len(urls) == 1 else None


class Resolver:
    """Looks roles up on employer boards, reading each board once per run."""

    def __init__(self, boards=None, fetch=fetch_board):
        self.boards = boards or {}
        self.fetch = fetch
        self._cache = {}

    def _board(self, ats, slug):
        if (ats, slug) not in self._cache:
            self._cache[(ats, slug)] = self.fetch(ats, slug)
        return self._cache[(ats, slug)]

    def candidates(self, job):
        """[((ats, slug), trusted)]: boards already seen for the company first, then guessed slugs."""
        out = {board: True for board in self.boards.get(_company_key(job.get('company')), [])}
        for slug, trusted in slug_guesses(job.get('company'), job.get('company_site')):
            for ats in ATS_NAMES:
                out[(ats, slug)] = out.get((ats, slug), False) or trusted
        return list(out.items())

    def resolve(self, job):
        """(direct url, None) on a match, (None, note) when the company's own board lacks the role,
        else (None, None)."""
        found = []
        for (ats, slug), trusted in self.candidates(job):
            postings = self._board(ats, slug)
            if postings is None:
                continue
            url = match_posting(job, postings)
            if url:
                return url, None
            if trusted:
                found.append(ATS_NAMES[ats])
        if found:
            boards = '/'.join(dict.fromkeys(found))
            return None, f"No posting with this title on the company's {boards} board; check it is still open."
        return None, None


def resolve_direct_links(jobs, min_score, resolver=None, enriched=None):
    """Swap job-board links for the employer's posting on roles at or above min_score; returns how many.
    Match entries carry no JD or company website, so those are read from the enriched jobs by URL."""
    targets = [j for j in jobs if j.get('score', 0) >= min_score and needs_resolving(j.get('url'))]
    if not targets:
        return 0
    if enriched is None:
        enriched = load_json(get_path('enriched_jobs'), default=[])
    enriched = enriched if isinstance(enriched, list) else []
    by_url = {normalize_url(j.get('url') or ''): j for j in enriched if j.get('url')}
    if resolver is None:
        resolver = Resolver(known_boards(enriched, load_blocklist()))
    swapped = 0
    for job in targets:
        probe = {**by_url.get(normalize_url(job['url']), {}), **job}
        try:
            url, note = resolver.resolve(probe)
        except Exception as e:  # a lookup failure must never block the sync
            print(f"[direct links] {job.get('company')}: {e}", file=sys.stderr)
            continue
        if url:
            job['board_url'], job['url'] = job['url'], url
            swapped += 1
            print(f"[direct links] {job.get('company')} - {job.get('title')}: {url}")
        elif note:
            job['direct_link_note'] = note
            print(f"[direct links] {job.get('company')} - {job.get('title')}: {note}")
    return swapped
