#!/usr/bin/env python3
"""End-to-end orchestrator: fetch, scrape, evaluate, AI review, Notion sync, Markdown summary and ntfy notification."""
import argparse
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

import requests

from blocklist import is_blocked, load_blocklist, load_role_blocklist
from evaluate_jobs import evaluate_jobs, generate_markdown
from llm_review import reason_for_notion, review_matches
from pipeline_config import CFG, SCRIPT_DIR, get_path, load_json
from sync_notion_db import log_run_summary_to_notion, sync_jobs_to_notion

FILTERED_JOBS = get_path("filtered_jobs", "data/filtered_jobs.json")
ENRICHED_JOBS = get_path("enriched_jobs", "data/enriched_jobs.json")
EVALUATED_JOBS = get_path("evaluated_jobs", "data/evaluated_jobs.json")
SOURCE_STATS_PATH = get_path("source_stats", "data/source_stats.json")

_SOURCES_CFG = CFG.get("sources") or {}
_SKILLS_CFG = CFG.get("skills_and_scoring") or {}
_NOTIF_CFG = CFG.get("notifications") or {}

FEED_STALE_HOURS = int(_SOURCES_CFG.get("feed_stale_hours", 36))
_SOURCE_LABELS = [
    ("linkedin", "LinkedIn"),
    ("indeed", "Indeed"),
    ("jobicy", "Jobicy"),
    ("hn", "HN"),
    ("remoteok", "Remote OK"),
]
PRIORITY_DEFAULT = 3
PRIORITY_HIGH = 4
NOTIFY_TOP_MATCHES = 5


def _feed_warnings(feed):
    warnings = []
    if feed.get("failed_chunks"):
        warnings.append(f"GitHub feed: {feed['failed_chunks']} of {feed.get('chunks')} chunks failed to download")
    try:
        updated = datetime.fromisoformat(feed["last_updated"].replace("Z", "+00:00"))
        age_hours = (datetime.now(timezone.utc) - updated).total_seconds() / 3600
        if age_hours > FEED_STALE_HOURS:
            warnings.append(f"GitHub feed last updated {age_hours:.0f}h ago (stale upstream?)")
    except (KeyError, TypeError, ValueError, AttributeError):
        pass
    return warnings


def source_health(path=SOURCE_STATS_PATH):
    """One line of per-source counts from the last fetch, plus warnings for sources that look broken or stale."""
    stats = load_json(path)
    if not isinstance(stats, dict):
        return None
    parts, warnings = [], []
    feed = stats.get("github_feed") or {}
    if feed:
        parts.append(f"GitHub feed {feed.get('kept', 0)}")
        warnings.extend(_feed_warnings(feed))
    for key, label in _SOURCE_LABELS:
        s = stats.get(key)
        if s is None:
            continue
        part = f"{label} {s.get('kept', 0)}"
        if s.get("rate_limited"):
            part += f" ({s['rate_limited']} rate-limited)"
        parts.append(part)
        if s.get("error"):
            warnings.append(f"{label} fetch failed: {s['error']}")
        elif key in ("linkedin", "indeed") and s.get("raw", 0) == 0:
            warnings.append(f"{label} returned no rows at all (blocked, or jobspy needs an update?)")
    return {"line": "Sources (postings that passed filters): " + " · ".join(parts), "warnings": warnings}


def send_notification(title: str, message: str, priority: int = PRIORITY_DEFAULT):
    notify_url = os.getenv("NOTIFY_URL") or _NOTIF_CFG.get("notify_url") or CFG.get("notify_url")
    if not notify_url:
        return

    try:
        res = requests.post(
            notify_url,
            data=message.encode("utf-8"),
            headers={"Title": title, "Priority": str(priority)},
            timeout=10,
        )
        if res.status_code == 200:
            print(f"[Notification] Sent: {title}")
        else:
            print(f"[Notification] HTTP {res.status_code} from {notify_url}")
    except requests.RequestException as e:
        print(f"[Notification] Failed to send notification: {e}", file=sys.stderr)


def print_banner(step_name, detail=""):
    print("\n==========================================")
    print(f"--> [Step: {step_name}]" + (f" {detail}" if detail else ""))
    print("==========================================")


def run_step(step_name, cmd, cwd=SCRIPT_DIR):
    """Run a stage script as a subprocess; returns True on exit code 0."""
    print_banner(step_name, f"Running: {' '.join(cmd)}")
    started = time.monotonic()
    res = subprocess.run(cmd, cwd=cwd)
    print(f"--> [Step: {step_name}] took {time.monotonic() - started:.0f}s")
    if res.returncode != 0:
        print(f"Error during step '{step_name}' (exit code {res.returncode})", file=sys.stderr)
        return False
    return True


def run_stage_script(args, step_name, cmd):
    """Run a subprocess stage, notifying and exiting the pipeline when it fails."""
    if run_step(step_name, cmd):
        return
    if not args.no_notify:
        send_notification("Job Search Failed", f"{step_name} step failed. Check pipeline logs.", priority=PRIORITY_HIGH)
    sys.exit(1)


def parse_args(argv=None):
    default_min_score = int(_SKILLS_CFG.get("min_sync_score", 80))
    default_review_score = int(_SKILLS_CFG.get("min_review_score", 55))

    parser = argparse.ArgumentParser(description="End-to-End Automated Job Search Pipeline")
    parser.add_argument("--skip-fetch", action="store_true", help="Skip fetching and filtering step")
    parser.add_argument("--skip-scrape", action="store_true", help="Skip batch scraping JD step")
    parser.add_argument("--no-notion", action="store_true", help="Skip syncing to Notion")
    parser.add_argument("--no-notion-log", action="store_true", help="Skip logging run summary to Notion")
    parser.add_argument("--no-notify", action="store_true", help="Skip sending push notification")
    parser.add_argument("--min-score", type=int, default=default_min_score,
                        help=f"Minimum Match %% to sync to Notion (default: {default_min_score})")
    parser.add_argument("--review-min-score", type=int, default=default_review_score,
                        help=f"Minimum keyword score for the AI review (default: {default_review_score})")
    parser.add_argument("--no-llm-review", action="store_true",
                        help="Skip the AI review of matches against candidate_profile.md")
    parser.add_argument("--md-output", type=str, default=None, help="Optional path to save Markdown summary")
    return parser.parse_args(argv)


def drop_blocklisted(eval_results):
    """Drop already-synced or dismissed roles before they cost an AI review."""
    blocked_urls, blocked_roles = load_blocklist(), load_role_blocklist()
    top_matches = eval_results.get("top_matches", [])
    kept = [j for j in top_matches if not is_blocked(j, blocked_urls, blocked_roles)]
    if len(top_matches) > len(kept):
        print(f"Dropped {len(top_matches) - len(kept)} match(es) already synced to or removed from Notion.")
    eval_results["top_matches"] = kept


def ai_review(eval_results, min_score):
    """Re-grade top matches against candidate_profile.md; AI-skipped roles move to eval_results['ai_skipped']."""
    print_banner("AI Review (agy)")
    started = time.monotonic()
    top_matches, ai_skipped = review_matches(eval_results["top_matches"], ENRICHED_JOBS, min_score=min_score)
    print(f"--> [Step: AI Review] took {time.monotonic() - started:.0f}s")
    for job in top_matches:
        if job.get("verdict"):
            job["keyword_reason"] = job.get("reason", "")
            job["reason"] = reason_for_notion(job)
    eval_results["top_matches"] = top_matches
    eval_results["ai_skipped"] = ai_skipped


def write_markdown(eval_results, path):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(generate_markdown(eval_results))
    print(f"\nSaved Markdown evaluation table to: {path}")


def notify_results(eval_results, synced_urls, health, notion_enabled):
    top_matches = eval_results["top_matches"]
    manual_reviews = eval_results.get("manual_reviews", [])
    ai_skipped = eval_results.get("ai_skipped", [])
    location_filtered = eval_results.get("location_filtered", [])
    warnings = health["warnings"] if health else []

    if notion_enabled:
        lines = [f"{len(top_matches)} matches found ({len(synced_urls)} synced to Notion)."]
    else:
        lines = [f"{len(top_matches)} matches found."]
    if manual_reviews:
        lines.append(f"{len(manual_reviews)} manual review(s) needed.")
    if ai_skipped:
        lines.append(f"{len(ai_skipped)} filtered out by AI review.")
    if location_filtered:
        lines.append(f"{len(location_filtered)} dropped for onsite/hybrid/ineligible location wording.")
    if warnings:
        lines.append("")
        lines.extend(f"⚠️ {w}" for w in warnings)
    if top_matches:
        lines.extend(["", "Top Matches:"])
        for job in top_matches[:NOTIFY_TOP_MATCHES]:
            lines.append(f"• {job.get('company', 'Unknown')} — {job.get('title', 'Role')} ({job.get('score', 0)}%)")

    title = f"Job Search: {len(top_matches)} Qualified Matches" if top_matches else "Job Search Complete: No Matches"
    if warnings:
        title += " (source warning)"
    send_notification(title, "\n".join(lines), priority=PRIORITY_HIGH if warnings else PRIORITY_DEFAULT)


def run(args):
    # 1. Fetch and filter
    if args.skip_fetch:
        print("\n--> [Step: Fetch & Filter] Skipped.")
    else:
        run_stage_script(args, "Fetch & Filter", [sys.executable, "fetch_and_filter.py"])

    # 2. Batch scrape JDs
    if args.skip_scrape:
        print("\n--> [Step: Batch Scrape JDs] Skipped.")
    else:
        run_stage_script(args, "Batch Scrape JDs", [sys.executable, "batch_scraper.py", FILTERED_JOBS, ENRICHED_JOBS])

    # 3. Evaluate jobs
    print_banner("Evaluate Matches")
    eval_results = evaluate_jobs(ENRICHED_JOBS, EVALUATED_JOBS)
    eval_results.setdefault("top_matches", [])
    health = None if args.skip_fetch else source_health()
    if health:
        eval_results["source_health"] = health
        print(health["line"])
        for w in health["warnings"]:
            print(f"WARNING: {w}", file=sys.stderr)
    drop_blocklisted(eval_results)

    # 4. AI review against candidate_profile.md
    if args.no_llm_review:
        print("\n--> [Step: AI Review] Skipped (--no-llm-review flag passed).")
    else:
        ai_review(eval_results, args.review_min_score)

    print(f"Total qualified matches: {len(eval_results['top_matches'])}")
    print(f"Total manual reviews needed: {len(eval_results.get('manual_reviews', []))}")

    # 5. Sync to Notion
    synced_urls = []
    if args.no_notion:
        print("\n--> [Step: Batch Sync to Notion] Skipped (--no-notion flag passed).")
    else:
        print_banner(f"Batch Sync to Notion (Min Score: {args.min_score}%)")
        synced_urls = sync_jobs_to_notion(eval_results["top_matches"], min_score=args.min_score)
        print(f"Successfully synced {len(synced_urls)} new prospect(s) to Notion.")

    # 6. Markdown summary and Notion run log
    if args.md_output:
        write_markdown(eval_results, args.md_output)

    if args.no_notion or args.no_notion_log:
        print("\n--> [Step: Log Run Summary to Notion] Skipped.")
    else:
        print_banner("Log Run Summary to Notion")
        log_run_summary_to_notion(eval_results, synced_urls, min_score=args.min_score)

    # 7. Push notification
    if not args.no_notify:
        notify_results(eval_results, synced_urls, health, notion_enabled=not args.no_notion)

    print("\n[Pipeline Complete] Done.")


def main():
    args = parse_args()
    try:
        run(args)
    except Exception as e:
        if not args.no_notify:
            send_notification("Job Search Failed", f"Pipeline crashed: {e}. Check pipeline logs.",
                              priority=PRIORITY_HIGH)
        raise


if __name__ == "__main__":
    main()
