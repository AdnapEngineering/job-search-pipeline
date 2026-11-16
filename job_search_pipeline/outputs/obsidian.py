"""Stage 5 alternative to Notion: write each qualified match as a note in an Obsidian vault.

Each match becomes `<vault>/<folder>/<Company> - <Title>.md` with YAML frontmatter that Obsidian shows as
Properties (and that Dataview or Bases can query like the Notion database). Each run also rewrites one
run-summary note in the same folder. Nothing needs Obsidian to be running: the vault is a plain folder.
"""
import os
import re
import sys
from datetime import datetime, timezone

from job_search_pipeline.blocklist import record_exported, select_new_matches
from job_search_pipeline.config import CFG
from job_search_pipeline.evaluate import skip_reason

_OBSIDIAN_CFG = CFG.get("obsidian") or {}

OBSIDIAN_ENABLED = bool(_OBSIDIAN_CFG.get("enabled", False))
VAULT_PATH = os.path.expanduser(os.environ.get("OBSIDIAN_VAULT") or _OBSIDIAN_CFG.get("vault_path") or "")
FOLDER = _OBSIDIAN_CFG.get("folder") or "Job Prospects"
SUMMARY_NOTE = _OBSIDIAN_CFG.get("summary_note") or "Job Search Run Summary"
TAG = _OBSIDIAN_CFG.get("tag") or "job-prospect"
MAX_NAME_LEN = 120

# Characters Obsidian (or Windows/macOS sync) does not allow in note names, plus link syntax
_UNSAFE_NAME_RE = re.compile(r'[\\/:*?"<>|#^\[\]]+')


def notes_dir(vault_path=VAULT_PATH, folder=FOLDER) -> str:
    return os.path.join(vault_path, folder) if folder else vault_path


def note_name(job: dict) -> str:
    """'Company - Title', stripped of characters a note name cannot hold."""
    company = (job.get("company") or "Unknown").title()
    name = f"{company} - {job.get('title') or 'Unknown Role'}"
    name = re.sub(r"\s+", " ", _UNSAFE_NAME_RE.sub(" ", name)).strip(" .")
    return name[:MAX_NAME_LEN].rstrip(" .") or "Untitled Role"


def _yaml_str(value) -> str:
    """A double-quoted YAML scalar, safe for any text (colons, quotes, leading dashes, ...)."""
    text = str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ").replace("\r", " ")
    return f'"{text}"'


def build_note(job: dict) -> str:
    """Frontmatter mirroring the Notion database's properties, then the match reasoning as the body."""
    first_seen = job.get("first_seen")
    added = first_seen[:10] if first_seen else datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines = [
        "---",
        f"company: {_yaml_str((job.get('company') or 'Unknown').title())}",
        f"role: {_yaml_str(job.get('title') or 'Unknown Role')}",
        f"location: {_yaml_str(job.get('location') or 'Remote')}",
        f"match: {int(job.get('score', 0))}",
        f"link: {_yaml_str(job.get('url', ''))}",
        f"date_added: {added}",
    ]
    if job.get("posted"):
        lines.append(f"originally_posted: {job['posted'][:10]}")
    if job.get("third_party"):
        lines.append("third_party: true")
    lines += ["status: Prospect", "tags:", f"  - {TAG}", "---", ""]
    lines += ["## Why It Matches", "", job.get("reason") or "Matches target criteria", ""]
    if job.get("url"):
        lines += [f"[Open the posting]({job['url']})", ""]
    return "\n".join(lines)


def _write_new(path: str, content: str) -> bool:
    """Create `path` with `content`; False when it already exists (a note is never overwritten)."""
    try:
        with open(path, "x", encoding="utf-8") as f:
            f.write(content)
        return True
    except FileExistsError:
        return False


def sync_jobs_to_obsidian(evaluated_jobs, vault_path=VAULT_PATH, folder=FOLDER, min_score=75,
                          record_in_blocklist=True):
    """Write a note for each qualified job (score >= min_score, not blocklisted); returns their URLs.

    A note that already exists (from an interrupted run, or a same-named role) is left untouched, so
    your edits to it (status, notes) are never lost. With record_in_blocklist, each written role is
    blocklisted at once, so later runs only add new ones."""
    if not vault_path:
        print("Skipping Obsidian export: no obsidian.vault_path (or OBSIDIAN_VAULT) configured.", file=sys.stderr)
        return []
    if not os.path.isdir(vault_path):
        print(f"Skipping Obsidian export: vault folder {vault_path} does not exist.", file=sys.stderr)
        return []

    candidates = select_new_matches(evaluated_jobs, min_score)
    print(f"Found {len(candidates)} jobs eligible for Obsidian export (Score >= {min_score}%, not blocked).")
    if not candidates:
        return []

    target = notes_dir(vault_path, folder)
    try:
        os.makedirs(target, exist_ok=True)
    except OSError as e:
        print(f"Skipping Obsidian export: cannot create {target}: {e}", file=sys.stderr)
        return []

    written = []
    for idx, job in enumerate(candidates, 1):
        prefix = f"[{idx}/{len(candidates)}]"
        name = note_name(job)
        try:
            created = _write_new(os.path.join(target, f"{name}.md"), build_note(job))
        except OSError as e:
            print(f"{prefix} Could not write note {name!r}: {e}", file=sys.stderr)
            continue
        written.append(job)
        if record_in_blocklist:
            record_exported([job])
        print(f"{prefix} {'Wrote' if created else 'Note already exists, kept'}: {name} ({job.get('score', 0)}%)")

    print(f"Obsidian export complete: {len(written)}/{len(candidates)} notes in {target}.")
    return [job["url"] for job in written]


def _link(entry: dict) -> str:
    """'**Company — Title**', linked to the posting when it has an http URL."""
    label = f"{(entry.get('company') or 'Unknown').title()} — {entry.get('title') or 'Role'}"
    label = label.replace("[", "(").replace("]", ")")
    url = entry.get("url", "")
    return f"**[{label}]({url})**" if url.startswith("http") else f"**{label}**"


def _one_line(text, max_len=120) -> str:
    text = (text or "").strip().replace("\n", " ")
    return (text[:max_len] + "...") if len(text) > max_len else text


def build_summary(eval_results, written_urls, min_score=65, history=None) -> str:
    """The run summary as Markdown: the same counts and lists as the Notion callout."""
    from job_search_pipeline.outputs.notion import match_tally

    top_matches = eval_results.get("top_matches", [])
    manual_reviews = eval_results.get("manual_reviews", [])
    ai_skipped = eval_results.get("ai_skipped", [])
    location_filtered = eval_results.get("location_filtered", [])
    total_eval = eval_results.get("total_evaluated", len(top_matches) + len(manual_reviews) + len(ai_skipped))
    tally = match_tally(top_matches, min_score)

    out = [
        "---", f"updated: {datetime.now():%Y-%m-%dT%H:%M:%S}", "tags:", f"  - {TAG}-summary", "---", "",
        f"# 📋 Job Search Run Summary — {datetime.now():%Y-%m-%d %H:%M}", "",
        "| Evaluated | Open matches | At {0}%+ | Added to vault | Held (AI review failed) | Manual reviews "
        "| AI skipped | Location/onsite dropped |".format(min_score),
        "|---|---|---|---|---|---|---|---|",
        f"| {total_eval} | {len(top_matches)} | {tally['at_threshold']} | {len(written_urls)} | {tally['held']} "
        f"| {len(manual_reviews)} | {len(ai_skipped)} | {len(location_filtered)} |", "",
    ]

    health = eval_results.get("source_health")
    if health:
        out.append(health["line"])
        out += [f"> [!warning] {w}" for w in health["warnings"]]
        out.append("")

    if manual_reviews:
        out += [f"## ⚠️ Manual Reviews Required ({len(manual_reviews)} failed scrapes)", ""]
        out += [f"- {_link(mr)} ({_one_line(mr.get('reason', 'Failed scrape / missing description'), 80)})"
                for mr in manual_reviews[:5]]
        out.append("")

    if top_matches:
        shown = top_matches[:5]
        out += [f"## 🎯 Open Matches, best first (showing {len(shown)} of {len(top_matches)})", ""]
        out += [f"- {_link(tm)} ({tm.get('score', 0)}%) — {_one_line(tm.get('reason', ''), 85)}" for tm in shown]
        out.append("")

    if ai_skipped:
        out += [f"## 🚫 Filtered by AI Review ({len(ai_skipped)})", ""]
        out += [f"- {(sk.get('company') or 'Unknown').title()} ({sk.get('title') or 'Role'}): "
                f"{_one_line(skip_reason(sk), 110)}" for sk in ai_skipped[:4]]
        if len(ai_skipped) > 4:
            out.append(f"- *...and {len(ai_skipped) - 4} more filtered roles*")
        out.append("")

    if history:
        out += ["## 🕒 Recent Runs", ""]
        out += [f"- {h['time']}: {h['summary']}" for h in history[-4:]]
        out.append("")

    return "\n".join(out)


def log_run_summary_to_obsidian(eval_results, written_urls, min_score=65, history=None,
                                vault_path=VAULT_PATH, folder=FOLDER, summary_note=SUMMARY_NOTE):
    """Rewrite the run-summary note in the vault; returns its path, or None when it could not be written."""
    if not vault_path or not os.path.isdir(vault_path):
        return None
    path = os.path.join(notes_dir(vault_path, folder), f"{summary_note}.md")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(build_summary(eval_results, written_urls, min_score=min_score, history=history))
        os.replace(tmp, path)
    except OSError as e:
        print(f"[Obsidian Log] Could not write {path}: {e}", file=sys.stderr)
        return None
    print(f"[Obsidian Log] Run summary written to {path}.")
    return path
