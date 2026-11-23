"""Dynamic Antigravity Workflow: Two-Stage Native Agent Fleet for Stage 4 Job Review.

Replaces single-turn CLI subprocess calls with a schema-validated two-pass agent fleet:
- Pass 1 (Triage Fleet, `flash` model): Screens uncached matches in parallel for hard blockers
  (`hard_reqs == 0`, `logistics_ok == False`, pay below floor, or hidden third-party posters).
- Pass 2 (Deep Rubric Fleet, `inherit` model): Grades survivors on the full 12-point rubric with
  verbatim `evidence_quote` verification, updates `data/review_cache.json` and `data/evaluated_jobs.json`,
  and refreshes `data/digest.html`.

Run via Antigravity `run_workflow`:
    run_workflow(
        ScriptPath="/home/justin/projects/job-search-pipeline/workflows/tiered_review_workflow.py",
        Cwd="/home/justin/projects/job-search-pipeline",
        Description="Run two-stage parallel agent fleet (Flash triage -> Deep rubric review) on evaluated matches",
    )

Optional `Args`:
    - "min_review_score" (int): minimum keyword score to review (defaults to config skills_and_scoring.min_review_score)
    - "max_jobs" (int): cap number of uncached jobs reviewed in this workflow run (default 25)
    - "triage_model" (str): model tier for Pass 1 ("flash_lite", "flash", "inherit"; default "flash")
    - "deep_model" (str): model tier for Pass 2 ("inherit", "pro", "flash"; default "inherit")
"""
import json
import sys
from datetime import datetime

ROOT = "/home/justin/projects/job-search-pipeline"
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from job_search_pipeline.config import CFG, atomic_write_json, get_path, load_json  # noqa: E402
from job_search_pipeline.outputs.html_digest import HTML_DIGEST_ENABLED, HTML_DIGEST_PATH, write_digest  # noqa: E402
from job_search_pipeline.review import (  # noqa: E402
    CACHE_PATH,
    DEFAULT_MODEL,
    PROMPT,
    PROVIDER,
    TRIAGE_PROMPT,
    _apply_results,
    _load_review_inputs,
    _parse_triage,
    _parse_verdict,
    _profile_hash,
    _profile_text,
    _reuse_reposts,
    _salary_text,
    _save_cache,
    archive_postings,
    cache_entry,
    _split_cached,
    _url_key,
    reason_for_notion,
)

WORKFLOW_ARGS = globals().get("args") or {}
phase = globals().get("phase")
log = globals().get("log")
agent = globals().get("agent")
parallel = globals().get("parallel")
EVALUATED_PATH = get_path("evaluated_jobs")
ENRICHED_PATH = get_path("enriched_jobs")
MIN_REVIEW_SCORE = int(WORKFLOW_ARGS.get("min_review_score") or CFG["skills_and_scoring"]["min_review_score"])
MIN_SYNC_SCORE = int(CFG["skills_and_scoring"]["min_sync_score"])
MAX_JOBS = int(WORKFLOW_ARGS.get("max_jobs") or 25)
TRIAGE_TIER = WORKFLOW_ARGS.get("triage_model") or "flash"
DEEP_TIER = WORKFLOW_ARGS.get("deep_model") or "inherit"

TRIAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "hard_reqs": {"type": "integer", "minimum": 0, "maximum": 4},
        "level": {"type": "integer", "minimum": 0, "maximum": 2},
        "logistics_ok": {"type": "boolean"},
        "third_party": {"type": "boolean"},
        "pay_min": {"type": ["number", "null"]},
        "pay_max": {"type": ["number", "null"]},
        "reason": {"type": "string"},
        "blockers": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["hard_reqs", "level", "logistics_ok", "third_party", "reason", "blockers"],
}

DEEP_RUBRIC_SCHEMA = {
    "type": "object",
    "properties": {
        "hard_reqs": {"type": "integer", "minimum": 0, "maximum": 4},
        "core_work": {"type": "integer", "minimum": 0, "maximum": 3},
        "evidence": {"type": "integer", "minimum": 0, "maximum": 3},
        "evidence_quote": {"type": "string"},
        "level": {"type": "integer", "minimum": 0, "maximum": 2},
        "logistics_ok": {"type": "boolean"},
        "third_party": {"type": "boolean"},
        "pay_min": {"type": ["number", "null"]},
        "pay_max": {"type": ["number", "null"]},
        "verdict": {"type": "string", "enum": ["apply", "stretch", "skip"]},
        "reason": {"type": "string"},
        "blockers": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "hard_reqs", "core_work", "evidence", "evidence_quote", "level",
        "logistics_ok", "third_party", "verdict", "reason", "blockers",
    ],
}

async def run_tiered_review():
    phase("1. Load Candidate Profile & Deduplicate Cache")
    eval_results = load_json(EVALUATED_PATH, default={}) or {}
    matches = eval_results.get("top_matches") or []
    profile = _profile_text()
    phash = _profile_hash(profile, DEFAULT_MODEL, PROVIDER)
    cache = load_json(CACHE_PATH, default={})
    if not isinstance(cache, dict):
        cache = {}

    due = sorted((j for j in matches if j.get("score", 0) >= MIN_REVIEW_SCORE),
                 key=lambda j: j.get("score", 0), reverse=True)
    results, misses = _split_cached(due, cache, phash, TRIAGE_TIER)
    cache_hits = len(results)
    jd_by_url, salary_by_url = _load_review_inputs(misses, ENRICHED_PATH)
    todo = _reuse_reposts(misses, jd_by_url, cache, phash, results, TRIAGE_TIER)
    repost_hits = len(misses) - len(todo)

    todo_valid = [j for j in todo if len(jd_by_url.get(_url_key(j)) or "") >= 200]
    deferred = {_url_key(j) for j in todo_valid[MAX_JOBS:]}
    batch = todo_valid[:MAX_JOBS]

    log(f"Matches due: {len(due)} | Cache hits: {cache_hits} | Repost hits: {repost_hits} | "
        f"To review now: {len(batch)} (deferred: {len(deferred)})")

    fresh = {}

    if batch:
        phase(f"2. Pass-1 Triage Fleet ({TRIAGE_TIER})")
        triage_replies = await parallel(
            batch,
            lambda job: agent(
                TRIAGE_PROMPT.format(
                    profile=profile,
                    company=job.get("company") or "Unknown",
                    title=job.get("title") or "",
                    location=job.get("location") or "",
                    salary=_salary_text(salary_by_url.get(_url_key(job))),
                    jd=jd_by_url.get(_url_key(job)) or "",
                ),
                schema=TRIAGE_SCHEMA,
                role=f"Triage · {job.get('company', 'Unknown')} — {job.get('title', 'Role')[:35]}",
                model=TRIAGE_TIER,
            ),
        )

        survivors = []
        triaged_out_count = 0
        for job, reply_obj in zip(batch, triage_replies, strict=True):
            key = _url_key(job)
            early_skip, _ = _parse_triage(json.dumps(reply_obj), salary_by_url.get(key))
            if early_skip is not None:
                fresh[key] = early_skip
                triaged_out_count += 1
            else:
                survivors.append(job)

        log(f"Pass-1 Triage complete: {triaged_out_count} filtered out early, {len(survivors)} advancing to Pass-2.")

        if survivors:
            phase(f"3. Pass-2 Deep Rubric Fleet ({DEEP_TIER})")
            deep_replies = await parallel(
                survivors,
                lambda job: agent(
                    PROMPT.format(
                        profile=profile,
                        company=job.get("company") or "Unknown",
                        title=job.get("title") or "",
                        location=job.get("location") or "",
                        salary=_salary_text(salary_by_url.get(_url_key(job))),
                        jd=jd_by_url.get(_url_key(job)) or "",
                    ),
                    schema=DEEP_RUBRIC_SCHEMA,
                    role=f"Deep Review · {job.get('company', 'Unknown')} — {job.get('title', 'Role')[:35]}",
                    model=DEEP_TIER,
                ),
            )

            for job, reply_obj in zip(survivors, deep_replies, strict=True):
                key = _url_key(job)
                jd = jd_by_url.get(key) or ""
                parsed = _parse_verdict(json.dumps(reply_obj), jd, salary_by_url.get(key))
                if parsed:
                    fresh[key] = parsed

    phase("4. Persist Cache & Update Evaluated Matches")
    for job in batch:
        key = _url_key(job)
        if key in fresh:
            cache[key] = cache_entry(phash, fresh[key], job, jd_by_url.get(key) or "", TRIAGE_TIER)

    results.update(fresh)
    _save_cache(cache, phash, changed=bool(fresh or repost_hits), triage_model=TRIAGE_TIER)
    archive_postings(cache, ENRICHED_PATH)

    kept, skipped, failed, hidden = _apply_results(matches, results, deferred, MIN_REVIEW_SCORE)
    for job in kept:
        if job.get("verdict") or job.get("review_failed"):
            job["keyword_reason"] = job.get("reason", "")
            job["reason"] = reason_for_notion(job)

    eval_results["top_matches"] = kept
    eval_results["ai_skipped"] = skipped
    atomic_write_json(EVALUATED_PATH, eval_results)

    if HTML_DIGEST_ENABLED:
        write_digest(eval_results, [], [], HTML_DIGEST_PATH, min_score=MIN_SYNC_SCORE)

    apply_matches = [j for j in kept if j.get("verdict") == "apply"]
    stretch_matches = [j for j in kept if j.get("verdict") == "stretch"]
    log(
        f"Fleet review complete ({datetime.now():%Y-%m-%d %H:%M}): "
        f"{len(apply_matches)} APPLY, {len(stretch_matches)} STRETCH, {len(skipped)} SKIP."
    )
    print(json.dumps({
        "reviewed": len(fresh),
        "cache_hits": cache_hits,
        "repost_hits": repost_hits,
        "apply_count": len(apply_matches),
        "stretch_count": len(stretch_matches),
        "skip_count": len(skipped),
        "top_apply": [
            {"company": j.get("company"), "title": j.get("title"), "score": j.get("score"), "url": j.get("url")}
            for j in apply_matches[:10]
        ],
    }, indent=2))


await run_tiered_review()  # noqa: F704
