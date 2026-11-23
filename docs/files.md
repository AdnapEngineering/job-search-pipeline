# Files

[← README](../README.md)

```
job-search-pipeline/
├── job_search_pipeline/      the code (run with `python3 -m job_search_pipeline`)
│   ├── sources/              Stage 1: where postings come from              │
│   └── outputs/              Stage 5: where matches go                      │
├── workflows/                Antigravity multi-agent workflow scripts
├── tests/
├── docs/                     the rest of the documentation, and README images
├── run_job_search.sh         cron wrapper
├── config.example.json       every setting, with defaults; copy to config.json to start
├── candidate_profile.example.md
├── pyproject.toml            package metadata, dependency ranges, ruff settings
└── uv.lock                   the exact dependency versions `uv sync`, cron and CI install
```

Your own files sit next to these, ignored by git: `config.json`, `candidate_profile.md`, `.env` and `data/` (run data, the cron log and the latest summary).

**At the repository root**

| File | Purpose |
| :--- | :--- |
| [`run_job_search.sh`](../run_job_search.sh) | Shell wrapper that runs `uv sync --locked` when uv is installed and then `.venv`'s Python when present, with a concurrency lock (`flock`, or a `mkdir` lock on macOS); appends each run to `data/cron_job_search.log`, trimmed to its newest half past 5 MB. |
| [`config.example.json`](../config.example.json) | Tracked template for the career-agnostic configuration (sources, locations, pay, titles, skills, LLM rubric). |
| `config.json` | Your own copy of the template, ignored by git; this is the file the pipeline reads and the one you edit. |
| `config.draft.json` | Written by `--init-config` for you to review, ignored by git; the pipeline never reads it. |
| `data/reviewed_postings.json` | Copy of every posting the AI review graded, kept after it ages out of `enriched_jobs.json` so `--calibrate` can replay it; follows the review cache, capped by `llm_review.posting_archive_max`. |
| `data/throttle_state.json` | When LinkedIn or Glassdoor last blocked the pipeline and when its cooldown ends (`sources.block_cooldown_hours`); delete it to query a cooling site again on the next run. |
| `config.calibrated.json` | Written by `--auto-calibrate` after replay verification against cached AI reviews, ignored by git; copy to `config.json` or pass `--auto-calibrate --apply`. |
| [`workflows/`](../workflows) | Antigravity `run_workflow` scripts (`auto_calibrate_workflow.py` for parallel specialist rule tuning and `tiered_review_workflow.py` for two-stage agent fleet review). |
| [`candidate_profile.example.md`](../candidate_profile.example.md) | Tracked template for the candidate profile. |
| `candidate_profile.md` | Your own copy of the template, ignored by git; read by `review.py` during Stage 4 rubric grading. |
| [`pyproject.toml`](../pyproject.toml) | Package metadata, the runtime dependency ranges and the `dev` group (ruff), the optional `job-search` command, and ruff settings. |
| [`uv.lock`](../uv.lock) | The exact versions of every dependency, written by `uv lock`; `uv sync`, `run_job_search.sh` and CI install from it. |
| [`tests/`](../tests) | Unit and regression tests, run against the generic `tests/fixtures/search_config.json`. |
| [`docs/`](../docs) | These pages, and the README's image (`digest.png` is rendered from made-up data). |

**In [`job_search_pipeline/`](../job_search_pipeline)**

| File | Purpose |
| :--- | :--- |
| [`__main__.py`](../job_search_pipeline/__main__.py) | Entry point for `python3 -m job_search_pipeline` and `job-search`: loads the env files, then runs `pipeline.py`. |
| [`env.py`](../job_search_pipeline/env.py) | Reads `$JOB_SEARCH_ENV_FILE` and `.env` into the environment. |
| [`pipeline.py`](../job_search_pipeline/pipeline.py) | End-to-end orchestrator and CLI flags, source-health monitor, pre-LLM blocklist filter, and notifier. |
| [`calibrate.py`](../job_search_pipeline/calibrate.py) | `--calibrate` and `--auto-calibrate`: replays the Stage 3 rules and keyword score over cached AI reviews, reports discrepancies, and optionally runs a 3-stage agent workflow (Mine → Propose → Replay & Verify) to tune `config.json`. |
| [`explain.py`](../job_search_pipeline/explain.py) | `--explain URL`: traces one posting through the Stage 1 filters, the Stage 3 rules and the score breakdown. |
| [`init_config.py`](../job_search_pipeline/init_config.py) | `--init-config`: drafts the career-specific settings from `candidate_profile.md` with the AI reviewer's CLI and validates them into `config.draft.json`. |
| [`check.py`](../job_search_pipeline/check.py) | Setup check (`--check`): dependencies, config typos and placeholders, profile, AI reviewer CLI, Notion database properties, Obsidian vault, outputs. |
| [`defaults.py`](../job_search_pipeline/defaults.py) | The built-in default of every setting, which `config.json` is laid over, and `PERSONAL_KEYS`, the career-specific settings whose template value is a placeholder. |
| [`config.py`](../job_search_pipeline/config.py) | Central loader for `config.json` (laid over `defaults.py`), path resolution (relative `paths` entries resolve from the repository root), shared JSON read/atomic-write helpers, and regex compilation helpers. |
| [`blocklist.py`](../job_search_pipeline/blocklist.py) | Shared dedup state: `company\|title` role keys plus the URL and role blocklists used by every stage. |
| [`urls.py`](../job_search_pipeline/urls.py) | Canonical URL normalizer (strips tracking parameters, redundant `gh_jid`, and fragments). |
| [`sources/filter.py`](../job_search_pipeline/sources/filter.py) | Stage 1 async downloader for the GitHub ATS chunk feed, the location/title/salary filters, and orchestrator for the other sources. |
| [`sources/linkedin_indeed.py`](../job_search_pipeline/sources/linkedin_indeed.py) | Stage 1 LinkedIn & Indeed scraper (`python-jobspy`) with detail caching and remote evidence checks. |
| [`sources/job_boards.py`](../job_search_pipeline/sources/job_boards.py) | Stage 1 connector for Remote OK, Jobicy, Himalayas, We Work Remotely, and Hacker News "Who is hiring?". |
| [`scrape.py`](../job_search_pipeline/scrape.py) | Stage 2 async JD enricher supporting Ashby, Lever, Greenhouse, BambooHR, Workday CXS, and HTML fallback. |
| [`evaluate.py`](../job_search_pipeline/evaluate.py) | Stage 3 deterministic evaluator, JD logistics/onsite blocker, context-aware requirement parser, and scorer. |
| [`review.py`](../job_search_pipeline/review.py) | Stage 4 LLM reviewer with SHA-256 caching, verbatim quote verification, and deterministic score calculation. |
| [`outputs/notion.py`](../job_search_pipeline/outputs/notion.py) | Stage 5 Notion database syncer (records synced roles in the blocklists) and Callout dashboard logger. |
| [`outputs/obsidian.py`](../job_search_pipeline/outputs/obsidian.py) | Stage 5 Obsidian alternative to Notion: one note per new match with Notion-like Properties, plus a run-summary note; records written roles in the blocklists. |
| [`outputs/html_digest.py`](../job_search_pipeline/outputs/html_digest.py) | Stage 5 self-contained HTML page of matches (`data/digest.html`), with a store that keeps earlier runs' matches for `keep_days`. |
| [`outputs/text_export.py`](../job_search_pipeline/outputs/text_export.py) | Stage 5 plain-text alternative to Notion: appends new qualified matches to a `.txt` log and records them in the blocklists. |

---
