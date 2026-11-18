"""Stage 5: sync qualified matches to a Notion database and maintain the run-summary Callout block."""
import json
import os
import sys
import time
from datetime import datetime, timezone

import requests

from job_search_pipeline.blocklist import record_exported, select_new_matches
from job_search_pipeline.evaluate import skip_reason
from job_search_pipeline.config import CFG, CONFIG_PATH, atomic_write_json, get_path, load_json

_NOTION_CFG = CFG["notion"]

NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = _NOTION_CFG["notion_version"]
DEFAULT_DB_ID = os.environ.get("NOTION_DATABASE_ID") or _NOTION_CFG.get("database_id") or ""
RUN_HISTORY_PATH = get_path("run_history")
# Properties the Job Prospects database must have (name -> Notion type); "Originally Posted" is optional
NOTION_PROPERTIES = {
    "Company & Stage": "title",
    "Role Title & Compensation": "rich_text",
    "Location": "rich_text",
    "Why It Matches": "rich_text",
    "Match %": "rich_text",
    "Link": "url",
    "Date Posted": "date",
}
OPTIONAL_NOTION_PROPERTIES = {"Originally Posted": "date"}
REQUEST_TIMEOUT = 15
SYNC_DELAY_SECONDS = 0.35  # stays under Notion's ~3 requests/second rate limit
RUN_HISTORY_KEEP = 10
RATE_LIMIT_RETRIES = 3
RATE_LIMIT_MAX_WAIT = 30  # seconds; caps a Retry-After header

_session = requests.Session()
_session.mount("https://", requests.adapters.HTTPAdapter(
    pool_connections=4,
    pool_maxsize=8,
    max_retries=requests.adapters.Retry(total=3, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503])
))


def get_notion_token() -> str:
    """Bearer token from the environment (NOTION_TOKEN / NOTION_API_KEY) or config.json."""
    token = (os.environ.get("NOTION_TOKEN") or os.environ.get("NOTION_API_KEY")
             or _NOTION_CFG.get("notion_token"))
    if not token:
        raise RuntimeError(f"Missing NOTION_TOKEN in environment or at {CONFIG_PATH}")
    return token if token.startswith("Bearer ") else f"Bearer {token}"


def _notion_headers() -> dict:
    return {
        "Authorization": get_notion_token(),
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def _rich_text(content: str) -> dict:
    return {"rich_text": [{"text": {"content": content}}]}


def _build_notion_payload(job: dict, database_id: str) -> dict:
    """Build the Notion page-create payload from a job dict."""
    first_seen = job.get("first_seen")
    date_str = first_seen[:10] if first_seen else datetime.now(timezone.utc).strftime("%Y-%m-%d")

    properties = {
        "Company & Stage": {"title": [{"text": {"content": (job.get("company") or "Unknown").title()}}]},
        "Role Title & Compensation": _rich_text(job.get("title") or "Unknown Role"),
        "Location": _rich_text(job.get("location") or "Remote"),
        "Why It Matches": _rich_text(job.get("reason") or "Matches target criteria"),
        "Match %": _rich_text(f"{job.get('score', 0)}%"),
        "Link": {"url": job.get("url", "")},
        "Date Posted": {"date": {"start": date_str}},
    }
    if job.get("posted"):
        properties["Originally Posted"] = {"date": {"start": job["posted"][:10]}}
    return {"parent": {"database_id": database_id}, "properties": properties}


def _retry_after_seconds(res, attempt):
    try:
        wait = float(res.headers.get("Retry-After", ""))
    except (TypeError, ValueError):
        wait = 2 ** attempt
    return max(0.0, min(wait, RATE_LIMIT_MAX_WAIT))


def _create_page(payload, headers):
    """POST one page, waiting out HTTP 429. The session's own retries skip POST (not idempotent), but a
    rate-limited request was never processed, so repeating it cannot create a duplicate page."""
    for attempt in range(RATE_LIMIT_RETRIES + 1):
        res = _session.post(f"{NOTION_API}/pages", headers=headers, json=payload, timeout=REQUEST_TIMEOUT)
        if res.status_code != 429 or attempt == RATE_LIMIT_RETRIES:
            return res
        wait = _retry_after_seconds(res, attempt)
        print(f"Notion rate limit (429); retrying in {wait:.0f}s ({attempt + 1}/{RATE_LIMIT_RETRIES}).", file=sys.stderr)
        time.sleep(wait)
    return res


def sync_jobs_to_notion(evaluated_jobs, database_id=DEFAULT_DB_ID, min_score=75):
    """Create a Notion page for each qualified job (score >= min_score); returns the URLs that were created.
    Each page is blocklisted as soon as it exists, so an interrupted run cannot create it again."""
    if not database_id:
        print("Skipping Notion sync: no Notion database_id configured.", file=sys.stderr)
        return []
    headers = _notion_headers()

    candidates = select_new_matches(evaluated_jobs, min_score)
    print(f"Found {len(candidates)} jobs eligible for Notion sync (Score >= {min_score}%, not blocked).")
    if not candidates:
        return []

    created = []
    total = len(candidates)

    for idx, job in enumerate(candidates, 1):
        company = (job.get("company") or "Unknown").title()
        title = job.get("title") or "Unknown Role"
        prefix = f"[{idx}/{total}]"
        try:
            res = _create_page(_build_notion_payload(job, database_id), headers)
            if res.status_code in (200, 201):
                created.append(job)
                record_exported([job])
                print(f"{prefix} Synced: {company} - {title} ({job.get('score', 0)}%)")
            elif res.status_code == 400:
                print(f"{prefix} Bad payload for {company}: {res.text}", file=sys.stderr)
            else:
                print(f"{prefix} Failed to sync {company} ({res.status_code}): {res.text}", file=sys.stderr)
        except requests.Timeout:
            print(f"{prefix} Timeout syncing {company}; skipping.", file=sys.stderr)
        except requests.RequestException as e:
            print(f"{prefix} Request error on {company}: {e}", file=sys.stderr)

        time.sleep(SYNC_DELAY_SECONDS)

    print(f"Notion sync complete: {len(created)}/{total} jobs successfully created.")
    return [job["url"] for job in created]


def match_tally(top_matches, min_score):
    """Counts behind the run summary. top_matches holds every open match the AI did not skip (50%+ by
    default), so the number at or above the sync threshold is reported separately from the total."""
    held = sum(1 for j in top_matches if j.get("review_failed"))
    at_threshold = sum(1 for j in top_matches if j.get("score", 0) >= min_score and not j.get("review_failed"))
    return {"at_threshold": at_threshold, "held": held,
            "third_party": sum(1 for j in top_matches if j.get("third_party"))}


def _truncate_text(text, max_len=120):
    if not text:
        return ""
    text = text.strip().replace("\n", " ")
    return (text[:max_len] + "...") if len(text) > max_len else text


def _save_run_history(history):
    try:
        atomic_write_json(RUN_HISTORY_PATH, history[-RUN_HISTORY_KEEP:], indent=4)
    except OSError as e:
        print(f"Error saving run history: {e}", file=sys.stderr)


def _text(content: str, **annotations) -> dict:
    """One Notion rich-text segment, with annotations (bold, color, ...) only when given."""
    segment = {"type": "text", "text": {"content": content}}
    if annotations:
        segment["annotations"] = annotations
    return segment


def _role_line(entry: dict, suffix: str) -> list[dict]:
    """'• Company — Title' (linked when the entry has an http URL) followed by `suffix`."""
    url = entry.get("url", "")
    link = {"url": url} if url and url.startswith("http") else None
    company = (entry.get("company") or "Unknown").title()
    label = _text(f"{company} — {entry.get('title') or 'Role'}", bold=True, underline=bool(link))
    label["text"]["link"] = link
    return [_text("• "), label, _text(suffix)]


def _build_summary_rich_text(eval_results, synced_urls, min_score=65, history=None):
    top_matches = eval_results.get("top_matches", [])
    manual_reviews = eval_results.get("manual_reviews", [])
    ai_skipped = eval_results.get("ai_skipped", [])
    location_filtered = eval_results.get("location_filtered", [])
    total_eval = eval_results.get("total_evaluated", len(top_matches) + len(manual_reviews) + len(ai_skipped))
    tally = match_tally(top_matches, min_score)

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rich_text = [_text(f"📋 Job Search Run Summary — {now_str}\n", bold=True)]

    counts = [
        ("Evaluated: ", total_eval, {}),
        ("  |  Open matches: ", len(top_matches), {}),
        (f"  |  At {min_score}%+: ", tally["at_threshold"], {}),
        ("  |  Synced: ", len(synced_urls), {}),
        ("  |  Held (AI review failed): ", tally["held"], {"color": "orange" if tally["held"] else "default"}),
        ("  |  Manual Reviews: ", len(manual_reviews), {"color": "orange" if manual_reviews else "default"}),
        ("  |  AI Skipped: ", len(ai_skipped), {}),
        ("  |  Location/Onsite Dropped: ", len(location_filtered), {}),
    ]
    for i, (label, value, extra) in enumerate(counts):
        line_end = "\n" if i == len(counts) - 1 else ""
        rich_text.append(_text(label, color="gray"))
        rich_text.append(_text(f"{value}{line_end}", bold=True, **extra))

    health = eval_results.get("source_health")
    if health:
        rich_text.append(_text(f"{health['line']}\n", color="gray"))
        rich_text.extend(_text(f"⚠️ {w}\n", color="red") for w in health["warnings"])
    rich_text.append(_text("\n"))

    if manual_reviews:
        rich_text.append(_text(f"⚠️ Manual Reviews Required ({len(manual_reviews)} failed scrapes):\n",
                               bold=True, color="orange"))
        for mr in manual_reviews[:5]:
            reason = _truncate_text(mr.get("reason", "Failed scrape / missing description"), 80)
            rich_text.extend(_role_line(mr, f" ({reason})\n"))
        rich_text.append(_text("\n"))

    if top_matches:
        shown = top_matches[:5]
        rich_text.append(_text(f"🎯 Open Matches, best first (showing {len(shown)} of {len(top_matches)}):\n", bold=True))
        for tm in shown:
            reason = _truncate_text(tm.get("reason", ""), 85)
            rich_text.extend(_role_line(tm, f" ({tm.get('score', 0)}%) — {reason}\n"))
        rich_text.append(_text("\n"))

    if ai_skipped:
        shown = ai_skipped[:4]
        rich_text.append(_text(f"🚫 Filtered by AI Review ({len(ai_skipped)}):\n", bold=True, color="gray"))
        for sk in shown:
            company = (sk.get("company") or "Unknown").title()
            why = _truncate_text(skip_reason(sk), 110)
            rich_text.append(_text(f"• {company} ({sk.get('title') or 'Role'}): {why}\n", color="gray"))
        if len(ai_skipped) > len(shown):
            rich_text.append(_text(f"• ...and {len(ai_skipped) - len(shown)} more filtered roles\n",
                                   color="gray", italic=True))

    if history:
        rich_text.append(_text("\n🕒 Recent Runs:\n", bold=True, color="gray"))
        rich_text.extend(_text(f"• {h['time']}: {h['summary']}\n", color="gray") for h in history[-4:])

    return rich_text


def record_run(eval_results, synced_urls, min_score=80) -> list[dict]:
    """Append this run to the persisted history and return the previous runs (call once per run)."""
    top_matches = eval_results.get("top_matches", [])
    manual_reviews = eval_results.get("manual_reviews", [])
    ai_skipped = eval_results.get("ai_skipped", [])
    total_eval = eval_results.get("total_evaluated", len(top_matches) + len(manual_reviews) + len(ai_skipped))

    history = load_json(RUN_HISTORY_PATH, default=[])
    previous = list(history)
    history.append({
        "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "summary": f"{total_eval} eval, {len(top_matches)} open matches "
                   f"({match_tally(top_matches, min_score)['at_threshold']} at {min_score}%+), "
                   f"{len(synced_urls)} synced, {len(manual_reviews)} reviews",
    })
    _save_run_history(history)
    return previous


def _update_summary_block(block_id, rich_text, headers) -> bool:
    try:
        res = _session.patch(f"{NOTION_API}/blocks/{block_id}", headers=headers,
                             json={"callout": {"rich_text": rich_text}}, timeout=REQUEST_TIMEOUT)
        if res.status_code in (200, 201):
            print(f"[Notion Log] Run summary updated in Notion (block {block_id}).")
            return True
        print(f"[Notion Log] Existing summary block returned {res.status_code}. Re-creating block...", file=sys.stderr)
    except requests.RequestException as e:
        print(f"[Notion Log] Failed to update block {block_id}: {e}", file=sys.stderr)
    return False


def _database_parent_page(database_id, headers):
    try:
        res = _session.get(f"{NOTION_API}/databases/{database_id}", headers=headers, timeout=REQUEST_TIMEOUT)
        if res.status_code == 200:
            return res.json().get("parent", {}).get("page_id")
    except requests.RequestException as e:
        print(f"[Notion Log] Could not fetch parent page for database {database_id}: {e}", file=sys.stderr)
    return None


def _create_summary_block(parent_page_id, database_id, rich_text, headers):
    """Insert a new Callout right after the database on its parent page; returns the new block id or None."""
    payload = {
        "children": [{
            "object": "block",
            "type": "callout",
            "callout": {
                "rich_text": rich_text,
                "icon": {"type": "emoji", "emoji": "📋"},
                "color": "gray_background",
            },
        }],
        "after": database_id,
    }
    try:
        res = _session.patch(f"{NOTION_API}/blocks/{parent_page_id}/children", headers=headers,
                             json=payload, timeout=REQUEST_TIMEOUT)
        if res.status_code in (200, 201):
            results = res.json().get("results", [])
            if results and results[0].get("id"):
                return results[0]["id"]
        print(f"[Notion Log] Failed to create summary block ({res.status_code}): {res.text}", file=sys.stderr)
    except requests.RequestException as e:
        print(f"[Notion Log] Error creating summary block: {e}", file=sys.stderr)
    return None


def _remember_summary_block(block_id):
    """Persist the new block id in config.json so later runs update it in place."""
    if os.path.exists(CONFIG_PATH) and load_json(CONFIG_PATH) is None:
        print(f"[Notion Log] {CONFIG_PATH} is unreadable; not saving summary_block_id {block_id}.", file=sys.stderr)
        return
    cfg = load_json(CONFIG_PATH, default={})
    cfg.setdefault("notion", {})["summary_block_id"] = block_id
    try:
        atomic_write_json(CONFIG_PATH, cfg)
    except OSError as e:
        print(f"[Notion Log] Could not save summary_block_id to {CONFIG_PATH}: {e}", file=sys.stderr)


def log_run_summary_to_notion(eval_results, synced_urls, min_score=65, database_id=DEFAULT_DB_ID, history=None):
    """Creates or updates the Callout run summary block on the parent page of the Job Prospects DB.
    `history` is the previous runs from record_run; when None, this run is recorded here."""
    if not database_id:
        return None
    headers = _notion_headers()

    # Read config.json fresh rather than CFG: _remember_summary_block rewrites it between runs
    cfg = load_json(CONFIG_PATH, default={}) or {}
    notion_cfg = cfg.get("notion") or {}
    summary_block_id = notion_cfg.get("summary_block_id")

    previous_runs = record_run(eval_results, synced_urls, min_score) if history is None else history
    rich_text = _build_summary_rich_text(eval_results, synced_urls, min_score=min_score, history=previous_runs)

    if summary_block_id and _update_summary_block(summary_block_id, rich_text, headers):
        return summary_block_id

    parent_page_id = notion_cfg.get("summary_page_id") or _database_parent_page(database_id, headers)
    if not parent_page_id:
        print("[Notion Log] Unable to determine parent page for run summary.", file=sys.stderr)
        return None

    new_block_id = _create_summary_block(parent_page_id, database_id, rich_text, headers)
    if new_block_id:
        _remember_summary_block(new_block_id)
        print(f"[Notion Log] Run summary created in Notion (block {new_block_id}).")
    return new_block_id


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 -m job_search_pipeline.outputs.notion <evaluated_jobs.json> [min_score]")
        sys.exit(1)

    with open(sys.argv[1], encoding="utf-8") as f:
        data = json.load(f)

    score_thresh = int(sys.argv[2]) if len(sys.argv) > 2 else 75
    jobs = data.get("top_matches", data) if isinstance(data, dict) else data
    created = sync_jobs_to_notion(jobs, min_score=score_thresh)
    if isinstance(data, dict):
        log_run_summary_to_notion(data, created, min_score=score_thresh)
