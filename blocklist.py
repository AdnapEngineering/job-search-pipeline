"""Persistent dedup state shared by every pipeline stage.

- URL blocklist: canonical URLs already synced to (or removed from) Notion.
- Role blocklist: normalized `company|title` keys, so reposts of the same role
  on another board or under a new URL are also skipped.
"""
import re

from pipeline_config import CFG, atomic_write_json, get_path, load_json
from url_normalizer import normalize_url, normalize_url_list

BLOCKLIST_PATH = get_path("blocklist", "data/blocklist.json")
ROLE_BLOCKLIST_PATH = get_path("role_blocklist", "data/blocklist_roles.json")

_COMPANY_SUFFIX_RE = re.compile(r'(technologies|technology|corporation|software|corp|inc|llc|labs|group|hq|ai)$')
_COMPANY_PAREN_RE = re.compile(r'^(.*?)\s*\(([^)]+)\)\s*$')
_TITLE_NOISE_RE = re.compile(
    r'\([^)]*\)|\[[^\]]*\]|\b(remote|hybrid|united states|usa|us|anywhere)\b|\bonly\b'
)
_SENIOR_ABBREV_RE = re.compile(r'\bsr\b\.?')
_NON_ALNUM_RE = re.compile(r'[^a-z0-9]')
_COMPANY_ALIASES = {
    k.lower(): v.lower()
    for k, v in ((CFG.get("companies") or {}).get("company_aliases") or {}).items()
}


def canon_company(squashed: str) -> str:
    """Suffix-stripped, alias-resolved form of an already-squashed company name."""
    c = squashed
    while c and _COMPANY_SUFFIX_RE.search(c) and len(c) > 6:
        c = _COMPANY_SUFFIX_RE.sub('', c)
    return _COMPANY_ALIASES.get(c, c)


def role_key(company: str | None, title: str | None) -> str:
    """Normalized company|title key so reposts across job boards match the same role ('' if either is blank)."""
    company = company or ''
    paren = _COMPANY_PAREN_RE.match(company)
    if paren:
        company = min(paren.group(1), paren.group(2), key=len) if paren.group(1) else paren.group(2)
    c = canon_company(_NON_ALNUM_RE.sub('', company.lower().replace('&', 'and')))
    t = (title or '').lower().replace('&', 'and')
    t = _SENIOR_ABBREV_RE.sub('senior', t)
    t = _NON_ALNUM_RE.sub('', _TITLE_NOISE_RE.sub(' ', t))
    return f"{c}|{t}" if c and t else ""


def load_blocklist() -> set[str]:
    raw = load_json(BLOCKLIST_PATH, default=[])
    return {normalize_url(u) for u in raw if u}


def append_to_blocklist(new_urls: list[str]) -> None:
    """Atomically append new URLs to the blocklist, deduplicating via normalize_url_list."""
    if not new_urls:
        return
    clean = normalize_url_list(load_json(BLOCKLIST_PATH, default=[]) + list(new_urls))
    atomic_write_json(BLOCKLIST_PATH, clean, indent=4)
    print(f"Updated blocklist with {len(new_urls)} new URLs. Total: {len(clean)}")


def load_role_blocklist() -> set[str]:
    """Stored role keys, re-canonicalized so alias/suffix rule changes apply to old entries."""
    out = set()
    for k in load_json(ROLE_BLOCKLIST_PATH, default=[]):
        c, sep, t = k.partition("|")
        out.add(f"{canon_company(c)}|{t}" if sep and not k.startswith("jd:") else k)
    return out


def append_to_role_blocklist(keys) -> None:
    keys = {k for k in keys if k}
    if not keys:
        return
    merged = sorted(load_role_blocklist() | keys)
    atomic_write_json(ROLE_BLOCKLIST_PATH, merged, indent=4)
    print(f"Updated role blocklist with {len(keys)} key(s). Total: {len(merged)}")


def job_keys(job: dict) -> set[str]:
    """Every key a role is known by: company|title, its JD-text key, and those of merged duplicates."""
    keys = {role_key(job.get("company"), job.get("title")), job.get("jd_key") or "", *(job.get("dup_keys") or [])}
    return keys - {""}


def select_new_matches(jobs: list[dict], min_score: int, verbose: bool = True) -> list[dict]:
    """Jobs scoring >= min_score that are not blocklisted, keeping only the first of any same-role reposts.
    Jobs whose AI review failed (review_failed) are held back so they are reviewed before they are output."""
    blocked_urls = load_blocklist()
    blocked_roles = load_role_blocklist()
    selected = []
    for job in jobs:
        if job.get("score", 0) < min_score or not job.get("url") or job.get("review_failed"):
            continue
        if normalize_url(job["url"]) in blocked_urls:
            continue
        keys = job_keys(job)
        if keys & blocked_roles:
            if verbose:
                print(f"Skipping repost of a synced/removed role: {job.get('company')} - {job.get('title')}")
            continue
        blocked_roles |= keys
        selected.append(job)
    return selected


def record_exported(jobs: list[dict]) -> None:
    """Blocklist the URLs and role keys of jobs written to Notion or an export, so they are not output again."""
    if not jobs:
        return
    append_to_blocklist([j["url"] for j in jobs])
    append_to_role_blocklist(k for j in jobs for k in job_keys(j))


def is_blocked(job: dict, blocked_urls: set[str], blocked_roles: set[str]) -> bool:
    """True when the job's canonical URL or any of its role keys is already blocklisted."""
    if normalize_url(job.get("url", "")) in blocked_urls:
        return True
    return bool(job_keys(job) & blocked_roles)

