"""Plain-text alternative to Notion: append each run's new qualified matches to a .txt log.

Useful for piping results into another documentation platform (Obsidian, Confluence,
a wiki, a spreadsheet import, ...) without a Notion account.
"""
import os
from datetime import datetime

from job_search_pipeline.blocklist import record_exported, select_new_matches
from job_search_pipeline.config import CFG, get_path

TEXT_EXPORT_ENABLED = bool(CFG["text_export"]["enabled"])
TEXT_EXPORT_PATH = get_path("text_export")


def format_job(job: dict) -> str:
    """One match as a headline plus indented detail lines (empty fields are omitted)."""
    lines = [f"[{job.get('score', 0)}%] {(job.get('company') or 'Unknown').title()} — {job.get('title') or 'Unknown Role'}"]
    details = (
        ("Location", job.get("location")),
        ("Posted", job.get("posted")),
        ("Why", job.get("reason")),
        ("Link", job.get("url")),
    )
    lines.extend(f"    {label}: {value}" for label, value in details if value)
    lines.extend(f"    Also open: {e['title']} ({e['score']}%) {e['url']}" for e in job.get("also_open") or [])
    return "\n".join(lines)


def export_jobs_to_text(jobs, path=TEXT_EXPORT_PATH, min_score=75, record_in_blocklist=True):
    """Append new qualified jobs (score >= min_score, not blocklisted) to `path`; returns the exported URLs.

    With record_in_blocklist, exported roles are blocklisted so later runs only append new ones.
    """
    new_jobs = select_new_matches(jobs, min_score, verbose=False)
    if not new_jobs:
        print(f"No new matches to export to {path}.")
        return []

    header = (f"===== Job Search Run {datetime.now():%Y-%m-%d %H:%M} — "
              f"{len(new_jobs)} new match(es) (min score {min_score}%) =====")
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(header + "\n\n" + "\n\n".join(format_job(j) for j in new_jobs) + "\n\n")

    if record_in_blocklist:
        record_exported(new_jobs)
    print(f"Exported {len(new_jobs)} new match(es) to {path}.")
    return [j["url"] for j in new_jobs]
