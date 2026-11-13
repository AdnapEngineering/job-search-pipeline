"""Canonical URL helpers used to match the same posting across sources and runs."""
import re
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from pipeline_config import CFG

_URL_CFG = CFG.get("url_normalization") or {}

DEFAULT_TRACKING_PARAMS = {
    'utm_source', 'utm_medium', 'utm_campaign', 'utm_term', 'utm_content',
    'ref', 'source', 'gh_src', 'fbclid', 'gclid', 'mc_cid', 'mc_eid',
    '_ga', '_gl', 'referer', 'sp_ref', 'trk', 'trackingid'
}

TRACKING_PARAMS = DEFAULT_TRACKING_PARAMS | set(_URL_CFG.get("extra_tracking_params") or [])

DEFAULT_AGGREGATOR_HOSTS = (
    'indeed.com', 'linkedin.com', 'news.ycombinator.com',
    'remoteok.com', 'remotive.com', 'jobicy.com'
)

AGGREGATOR_HOSTS = tuple(_URL_CFG.get("aggregator_hosts") or DEFAULT_AGGREGATOR_HOSTS)

# A path ending in a numeric or uuid/hash job ID makes a gh_jid query param redundant tracking
_TRAILING_JOB_ID_RE = re.compile(r'/(?:\d+|[0-9a-fA-F-]{8,})$')


def is_aggregator_url(url: str | None) -> bool:
    """True for job-board/aggregator links (LinkedIn, Indeed, ...) rather than a company's own ATS."""
    return any(h in (url or '').lower() for h in AGGREGATOR_HOSTS)


def normalize_url(url: str) -> str:
    """
    Normalizes a job posting URL for canonical matching:
    - Lowercase scheme and domain
    - Strip default ports (80, 443)
    - Strip trailing slash from paths (except root '/')
    - Strip marketing/tracking query parameters
    - Strip redundant gh_jid query params if path already ends in a job ID
    - Deterministically sort remaining query parameters
    - Drop URL fragments
    """
    if not url or not isinstance(url, str):
        return ""

    url = url.strip()
    try:
        parsed = urlparse(url)
    except ValueError:
        return url

    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()

    if netloc.endswith(':80') and scheme == 'http':
        netloc = netloc[:-3]
    elif netloc.endswith(':443') and scheme == 'https':
        netloc = netloc[:-4]

    path = parsed.path
    if len(path) > 1 and path.endswith('/'):
        path = path.rstrip('/')
    elif not path:
        path = '/'

    redundant_gh_jid = bool(_TRAILING_JOB_ID_RE.search(path))
    filtered_pairs = sorted(
        (k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=False)
        if k.lower() not in TRACKING_PARAMS and not (k.lower() == 'gh_jid' and redundant_gh_jid)
    )
    norm_query = urlencode(filtered_pairs)

    return urlunparse((scheme, netloc, path, '', norm_query, ''))


def normalize_url_list(urls: list[str]) -> list[str]:
    """Sorted, deduplicated canonical forms of `urls`, dropping any that normalize to empty."""
    return sorted({norm for norm in map(normalize_url, urls) if norm})
