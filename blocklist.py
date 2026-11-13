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

_COMPANY_SUFFIX_RE = re.compile(r'(technologies|technology|corporation|corp|inc|llc|labs|group|hq|ai)$')
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
        out.add(f"{canon_company(c)}|{t}" if sep else k)
    return out


def append_to_role_blocklist(keys) -> None:
    keys = {k for k in keys if k}
    if not keys:
        return
    merged = sorted(load_role_blocklist() | keys)
    atomic_write_json(ROLE_BLOCKLIST_PATH, merged, indent=4)
    print(f"Updated role blocklist with {len(keys)} key(s). Total: {len(merged)}")


def is_blocked(job: dict, blocked_urls: set[str], blocked_roles: set[str]) -> bool:
    """True when the job's canonical URL or company|title key is already blocklisted."""
    if normalize_url(job.get("url", "")) in blocked_urls:
        return True
    return role_key(job.get("company"), job.get("title")) in blocked_roles

