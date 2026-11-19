"""Persistent dedup state shared by every pipeline stage.

- URL blocklist: canonical URLs already synced to (or removed from) Notion.
- Role blocklist: normalized `company|title` keys, so reposts of the same role
  on another board or under a new URL are also skipped.
"""
import re

from job_search_pipeline.config import CFG, atomic_write_json, get_path, load_json
from job_search_pipeline.urls import normalize_url, normalize_url_list

BLOCKLIST_PATH = get_path("blocklist")
ROLE_BLOCKLIST_PATH = get_path("role_blocklist")
# Most roles from one company output per run (0 = no limit); see select_new_matches
MAX_ROLES_PER_RUN = int(CFG["companies"]["max_roles_per_run"] or 0)

_COMPANY_SUFFIX_RE = re.compile(r'(technologies|technology|corporation|software|corp|inc|llc|labs|group|hq|ai)$')
_COMPANY_PAREN_RE = re.compile(r'^(.*?)\s*\(([^)]+)\)\s*$')
_TITLE_NOISE_RE = re.compile(
    r'\([^)]*\)|\[[^\]]*\]|\b(remote|hybrid|united states|usa|us|anywhere)\b|\bonly\b'
)
_SENIOR_ABBREV_RE = re.compile(r'\bsr\b\.?')
_NON_ALNUM_RE = re.compile(r'[^a-z0-9]')
_COMPANY_ALIASES = {
    k.lower(): v.lower()
    for k, v in (CFG["companies"]["company_aliases"] or {}).items()
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


def company_key(company: str | None) -> str:
    """The canonical company part of role_key ('' when blank), so a brand's roles group however it is written."""
    return role_key(company, "x").partition("|")[0]


def select_new_matches(jobs: list[dict], min_score: int, verbose: bool = True,
                       max_per_company: int | None = None) -> list[dict]:
    """Jobs scoring >= min_score that are not blocklisted, keeping only the first of any same-role reposts.
    Jobs whose AI review failed (review_failed) are held back so they are reviewed before they are output.

    With max_per_company (default companies.max_roles_per_run; 0 = no limit), only that many of one company's
    roles are kept, the highest scored. The rest ride along on the company's top kept job as `also_open` (a
    copy of that job is returned; the input is not changed), so outputs name them and record_exported
    blocklists them with it instead of letting them through one per run."""
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
    cap = MAX_ROLES_PER_RUN if max_per_company is None else max_per_company
    return _cap_per_company(selected, cap, verbose) if cap > 0 else selected


def _cap_per_company(jobs: list[dict], cap: int, verbose: bool) -> list[dict]:
    """`jobs` (order kept) with at most `cap` per company; the rest become `also_open` on the company's top job."""
    by_company = {}
    for job in jobs:
        key = company_key(job.get("company"))
        if key:
            by_company.setdefault(key, []).append(job)
    held, extras_for = set(), {}
    for roles in by_company.values():
        if len(roles) <= cap:
            continue
        ranked = sorted(roles, key=lambda j: j.get("score", 0), reverse=True)  # stable: ties keep their order
        extras = ranked[cap:]
        held |= {id(j) for j in extras}
        extras_for[id(ranked[0])] = [{"title": j.get("title") or "Unknown Role", "score": j.get("score", 0),
                                      "url": j["url"], "keys": sorted(job_keys(j))} for j in extras]
        if verbose:
            print(f"Holding back {len(extras)} more role(s) at {ranked[0].get('company')} "
                  f"(companies.max_roles_per_run = {cap}); they are listed on the kept one.")
    return [{**job, "also_open": extras_for[id(job)]} if id(job) in extras_for else job
            for job in jobs if id(job) not in held]


def also_open_text(job: dict) -> str:
    """'Also open at this company: Title (92%); ...' for a job carrying held-back roles, else ''."""
    extras = job.get("also_open") or []
    if not extras:
        return ""
    return "Also open at this company: " + "; ".join(f"{e['title']} ({e['score']}%)" for e in extras)


def record_exported(jobs: list[dict]) -> None:
    """Blocklist the URLs and role keys of jobs written to Notion or an export, so they are not output again.
    Roles held back by companies.max_roles_per_run (`also_open`) are blocklisted with the job they ride on."""
    if not jobs:
        return
    extras = [e for j in jobs for e in j.get("also_open") or []]
    append_to_blocklist([j["url"] for j in jobs] + [e["url"] for e in extras])
    append_to_role_blocklist([k for j in jobs for k in job_keys(j)] + [k for e in extras for k in e["keys"]])


def is_blocked(job: dict, blocked_urls: set[str], blocked_roles: set[str]) -> bool:
    """True when the job's canonical URL or any of its role keys is already blocklisted."""
    if normalize_url(job.get("url", "")) in blocked_urls:
        return True
    return bool(job_keys(job) & blocked_roles)

