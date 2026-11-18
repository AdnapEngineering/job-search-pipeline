
# Job Search Pipeline

Finds job postings that fit you and sends the best ones to Notion, Obsidian, a text file or a web page, with an optional phone notification.

Each run pulls postings from about 1.4 million company job boards (through [job-board-aggregator](https://github.com/Riley-Dorrington/job-board-aggregator)), LinkedIn, Indeed, Remote OK, Jobicy, Himalayas, We Work Remotely and Hacker News's monthly "Who is hiring?" thread. It drops the ones that break your location, title, pay and experience rules, fetches the full description for the rest, scores them against your skills, and has an AI command-line tool (agy, Claude Code or Codex) grade the best of them against a profile you write.

> **Credit:** the ATS listings, come from Riley Dorrington's [job-board-aggregator](https://github.com/Riley-Dorrington/job-board-aggregator) (MIT). It indexes 1M+ active postings from 20,000+ companies' Greenhouse, Lever, Ashby, Workday and other boards, refreshed daily by GitHub Actions and published to [job-board-data](https://github.com/Riley-Dorrington/job-board-data). If this pipeline helps you, give that project a star.

Everything specific to your search lives in two files you own: `config.json` (titles, skills, scoring, pay and experience limits, locations, companies to skip, the AI rubric) and `candidate_profile.md` (your background). Both start as copies of tracked templates, [`config.example.json`](config.example.json) and [`candidate_profile.example.md`](candidate_profile.example.md). None of the Python code is tied to one career.

Every run also writes a page of matches you can open in a browser, with no account needed. This one uses made-up data:

![The HTML digest: stat tiles, search and filters, and match cards with Match %, AI verdict, reason, gap chips and badges](docs/digest.png)

---

## Quickstart

Get a first list of matches in your browser, with no Notion, AI reviewer or push notifications. Each can be added later.

```bash
git clone https://github.com/AdnapEngineering/job-search-pipeline.git
cd job-search-pipeline
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp config.example.json config.json   # then edit it (see below), or draft it with --init-config
python3 -m job_search_pipeline --check   # flags typos and settings still at their placeholders
python3 -m job_search_pipeline --no-notion --no-llm-review --no-notify
xdg-open data/digest.html            # macOS: open data/digest.html
```

In your copy, replace the placeholders in the settings you have to change:

- `sources.search_terms` / `searches`: the job titles to search LinkedIn and Indeed for, and where (your city, or `Remote`).
- `location`: your metro area's name and cities, and `home_states` / `home_timezones` if remote roles must match them.
- `titles`: regexes for titles to keep (`target_patterns`), titles that score highest (`strong_target_patterns`, `primary_role_groups`) and levels to skip.
- `skills_and_scoring`: your skills (`core_strengths`), weighted domain terms (`focus_keywords`) and skills that rule a job out (`hard_exclude_skills`).
- `compensation.min_salary` / `experience.max_min_yoe`: the pay floor and the most years of experience a posting may ask for.

Every other setting in the template shows its built-in default (from [`job_search_pipeline/defaults.py`](job_search_pipeline/defaults.py)), so you can delete the ones you leave alone; see [Configuration](docs/configuration.md). Next steps: write [`candidate_profile.md`](docs/configuration.md#candidate_profilemd) and [choose an LLM provider](docs/configuration.md#ai-reviewer) for the AI review, add [Notion](docs/outputs.md) or [Obsidian](docs/outputs.md#obsidian) and [ntfy](#environment-variables), and [schedule it with cron](#cron).

---

## Contents

- [Quickstart](#quickstart)
- [How it works](#how-it-works) and [how scores are calculated](#how-scores-are-calculated)
- [Setup](#setup): [updating](#updating), [install options](#install-options), [your config and profile](#your-config-and-profile), [environment variables](#environment-variables), [checking the setup](#checking-the-setup)
- [Running it](#running-it): [a search](#running-a-search), [cron](#cron), [tests](#tests)

More in [`docs/`](docs):

- [Configuration](docs/configuration.md): `candidate_profile.md`, every `config.json` setting, location rules, the AI reviewer and its spending caps
- [Outputs](docs/outputs.md): Notion, Obsidian, a text file instead of Notion, and the HTML digest
- [Usage](docs/usage.md): every flag, tracing why a posting was dropped, and troubleshooting
- [Data sources](docs/data-sources.md): where postings and job descriptions come from
- [Files](docs/files.md): what each file in the repository does

---

## How it works

```
┌────────────────────────────────────────────────────────────────────────────┐
│ 1. Fetch & filter (sources/: filter.py, linkedin_indeed.py, job_boards.py) │
│    • Streams gzipped ATS chunks (~1.4M rows), LinkedIn, Indeed, Remote OK, │
│      Jobicy, Himalayas, We Work Remotely, and HN Who's Hiring.             │
│    • Applies fast pre-filters (recency, company/recruiter exclusions,      │
│      location/remote eligibility, title matching, salary floor).           │
│    • Re-points aggregator reposts (LinkedIn/Indeed) to the company's own   │
│      direct ATS URL and deduplicates against blocklist.json.               │
└─────────────────────────────────────┬──────────────────────────────────────┘
                                      ▼
┌────────────────────────────────────────────────────────────────────────────┐
│ 2. Fetch job descriptions (scrape.py)                                      │
│    • Fetches each new posting's full description; ones that failed with    │
│      an error last time are tried again.                                   │
│    • Queries structured JSON APIs first (Ashby, Lever, Greenhouse,         │
│      BambooHR, Workday CXS) before HTML fallback.                          │
│    • Detects closed/expired postings, separates requirements sections      │
│      from company boilerplate, and extracts matched skills.                │
└─────────────────────────────────────┬──────────────────────────────────────┘
                                      ▼
┌────────────────────────────────────────────────────────────────────────────┐
│ 3. Keyword scoring (evaluate.py)                                           │
│    • Screens JD body for hidden onsite/hybrid/foreign-residency rules.     │
│    • Distinguishes hard requirements from "nice-to-have / preferred"       │
│      sections and multi-language alternatives ("Skill A or Skill B").      │
│    • Enforces YOE ceilings (ignoring degree-equivalency boilerplate).      │
│    • Computes a 0–100% keyword score and deduplicates by company|title.    │
└─────────────────────────────────────┬──────────────────────────────────────┘
                                      ▼
┌────────────────────────────────────────────────────────────────────────────┐
│ 4. AI review (review.py)                                                   │
│    • Reuses cached reviews (also for reposts), records token/USD spend,    │
│      and stops at the configured per-run / per-day caps.                   │
│    • Drops roles already in Notion blocklists, and remote roles limited to │
│      other states/time zones, before spending LLM quota.                   │
│    • Grades each match against candidate_profile.md on a 12-point rubric:  │
│      hard_reqs (0–4), core_work (0–3), evidence (0–3), level (0–2).        │
│    • Verifies verbatim evidence_quote against the JD to prevent            │
│      made-up evidence; Python computes the final Match % and verdict.      │
└─────────────────────────────────────┬──────────────────────────────────────┘
                                      ▼
┌────────────────────────────────────────────────────────────────────────────┐
│ 5. Outputs and notifications (outputs/: notion.py, obsidian.py,            │
│    text_export.py, html_digest.py)                                         │
│    • Rewrites data/digest.html, a self-contained page of matches.          │
│    • Syncs qualified matches (>= min_score) to a Notion database, writes   │
│      them as notes in an Obsidian vault, and/or appends them to a .txt log.│
│    • Appends output URLs and company|title keys to persistent blocklists.  │
│    • Updates the Notion Callout / Obsidian summary note, saves Markdown    │
│      summary, and sends a push notification with source-health warnings.   │
└────────────────────────────────────────────────────────────────────────────┘
```

### How scores are calculated

A posting gets two scores. Stage 3 computes a **keyword score** from the JD text; Stage 4's AI review then replaces it with the final **Match %**, which is what `--min-score` and the outputs use. Default values are shown; every one is a setting in `config.json`.

| | Stage 3: keyword score (`evaluate.py`) | Stage 4: Match % (`review.py`) |
| :--- | :--- | :--- |
| **Start** | `base_score` (20) | `round(100 × (hard_reqs + core_work + evidence + level) / 12)`, graded 0–4, 0–3, 0–3, 0–2 (`llm_review.components`) |
| **Add** | Title credit: a `primary_role_groups` match (its `points`, 20) or a `strong_target_patterns` match (20); a generic title with your core stack scores 10, or 20 when the JD shows enough target work (`generic_body_signal_min`) |  |
| | `core_strengths`: 5 each (`core_strength_points`, or a skill's own weight in `core_strength_weights`), up to `core_strengths_cap` (40) |  |
| | `focus_keywords`: their weights, up to `domain_score_cap` (30); synonyms listed together in `focus_keyword_groups` count once, at the group's highest weight |  |
| **Subtract** | `soft_skill_penalty` (5) for each excluded skill listed as optional, up to `soft_skill_penalty_cap` (15); each `penalized_keywords` weight (not when the term is only offered beside an accepted skill, as in "React, Vue or Angular"); each matching `hard_ask_penalties` entry; `top_yoe_penalty` (10) when the JD asks `max_min_yoe`+ years | `pay_midpoint_penalty` (10) when the pay band's midpoint is under `min_salary` (unless its top reaches `pay_midpoint_max_cap`); an evidence grade of 3 without a verbatim quote from the JD counts as 2 |
| **Cap** | 100 | `blocker_cap` (30) when hard_reqs is 0 or logistics fail; `level_mismatch_cap` when level is 0; `core_work_caps` per core_work grade (default 30 at 0, 65 at 1); the pay-floor cap when the band tops out under `min_salary` (see [`compensation`](docs/configuration.md)) |
| **Then** | ≥ `strong_floor` (75): Strong; ≥ `secondary_floor` (55): Secondary; below: dropped. Matches ≥ `--review-min-score` (55) go to the AI review | ≥ `apply_min_score` (follows `min_sync_score`, 80): **APPLY**; ≥ `stretch_min_score` (50): **STRETCH**; below: **SKIP** (listed as filtered by AI review). APPLY is what gets synced at the default `--min-score`. Equal Match % sort by keyword score |

For example, a posting whose title matches a primary role group, that names 6 of your core strengths (5 points each) and one 15-point focus keyword, and that asks for `max_min_yoe`+ years scores 20 + 20 + 30 + 15 − 10 = **75** (Strong), so it goes to review. The reviewer grades hard_reqs 3, core_work 3, evidence 3 (with a verified quote) and level 2: 11/12 = **92%**, APPLY, synced. Had its pay midpoint been under your floor, it would be 82%: still APPLY. Had the reviewer graded hard_reqs 0, it would be capped at 30%: SKIP.

With `--no-llm-review` (or no AI reviewer installed), the keyword score is the Match %. Cached reviews are rescored from their stored grades on every run, so changing these settings never requires new reviews.

---

## Setup

The [Quickstart](#quickstart) covers cloning, the virtualenv, dependencies and your first config. This section covers the rest: updates, install options, secrets and the setup check ([Notion](docs/outputs.md#notion) has its own page). Python 3.10+ is required.

### Updating

```bash
git pull origin main
```

Your settings live in `config.json` and your background in `candidate_profile.md`. Git ignores both, so pulling never touches them. New settings arrive in the tracked template `config.example.json`; after a pull, `git diff HEAD@{1} -- config.example.json` shows what was added so you can copy the keys you want into your own file. Any key you leave out keeps its built-in default.

### Install options

The Quickstart's `pip install -r requirements.txt` inside `.venv` is all a run needs; `run_job_search.sh` uses `.venv`'s Python automatically when it exists, even from cron. Two extras:

```bash
pip install -e .                      # adds a `job-search` command, same as `python3 -m job_search_pipeline`
pip install -r requirements-dev.txt   # ruff, for development (see Tests)
```

### Your config and profile

```bash
cp config.example.json config.json         # or draft it with --init-config (below)
cp candidate_profile.example.md candidate_profile.md
```

Run the pipeline (and cron) straight from your clone: `config.json`, `candidate_profile.md`, `.env` and `data/` (which also holds the log and the summary) are all ignored by git, so `git pull` updates the code and leaves them alone. To keep extra personal files in the clone (your own scripts or notes), list them in `.git/info/exclude`, which works like `.gitignore` but is never committed.

Edit the copies, not the templates: see [Configuration](docs/configuration.md).

To skip most of the hand-editing, write `candidate_profile.md` first and let the AI reviewer's CLI draft the career-specific settings from it, optionally reading up to three postings you would want:

```bash
python3 -m job_search_pipeline --init-config                       # from candidate_profile.md alone
python3 -m job_search_pipeline --init-config URL1 URL2 URL3        # also learn from sample postings
```

It fills in `sources.search_terms`, the title patterns, `core_strengths` and their weights, `focus_keywords`, `hard_exclude_skills`, `max_min_yoe`, the AI review's `calibration_examples` and, when the profile says where you live, the location settings, on top of the template's career settings. Each field is type-checked and every regex compiled before anything is written; invalid ones are dropped and listed. The draft goes to `config.draft.json` (ignored by git), never over your `config.json`: review it, copy it over or merge in the parts you want, and run `--check`, which flags anything still at a placeholder (`sources.searches` always needs your city). It makes one call to the [configured provider](docs/configuration.md#ai-reviewer), with up to 10 minutes to answer. Until `config.json` exists a search run stops and says how to create it. Until `candidate_profile.md` exists the AI review is skipped (the placeholder profile is never sent to the LLM) and matches are output on their keyword scores.

### Environment variables

Secrets and machine-specific values go in `.env`, never in git:

```bash
cp .env.example .env
chmod +x run_job_search.sh
```

```bash
NOTION_TOKEN="ntn_your_secret_token"
NOTION_DATABASE_ID="your-32-char-database-id"
NOTIFY_URL="https://ntfy.sh/your-topic"
```

Every variable is optional; without them the pipeline still runs end to end and writes the HTML digest.

| Variable | What it sets | Config equivalent |
| :--- | :--- | :--- |
| `NOTION_TOKEN` | Notion integration secret (`NOTION_API_KEY` also works). | `notion.notion_token` (prefer the variable) |
| `NOTION_DATABASE_ID` | The Notion database to sync matches to. | `notion.database_id` |
| `OBSIDIAN_VAULT` | Path of the Obsidian vault to write notes to (`obsidian.enabled` must still be true). | `obsidian.vault_path` |
| `NOTIFY_URL` | `ntfy` topic URL for push notifications. | `notifications.notify_url` |
| `JOB_PIPELINE_CONFIG` | Path of a different config file, e.g. one per career. |  |
| `JOB_SEARCH_ENV_FILE` | Path of an extra env file read before `.env`, for secrets kept outside the clone. Set it in the environment (e.g. the crontab line), not in `.env`. |  |
| `JOB_SEARCH_LOG_MAX_BYTES` | Size at which `data/cron_job_search.log` is trimmed (default 5 MB; `0` turns trimming off). Read by `run_job_search.sh` only, from the environment (not `.env`). |  |

Every way of starting the pipeline (`run_job_search.sh`, cron, `python3 -m job_search_pipeline`, `job-search`, `--check`) reads `$JOB_SEARCH_ENV_FILE` when set and then `.env`. A value in `.env` wins over the other file, and a value in either file wins over one already exported in your shell.

### Checking the setup

```bash
python3 -m job_search_pipeline --check
```

The check fetches no jobs, spends no AI quota and writes nothing. It reports, one line each: Python version and installed dependencies; which config file is read, unknown keys (with a "did you mean" for typos) and career settings still at the template's placeholder values; whether `candidate_profile.md` exists; whether the AI reviewer's CLI is found; when Notion is configured, whether the token works and the database has every property the sync writes (its only network call); and, when Obsidian is on, whether the vault folder exists and is writable. It reads `.env` and `~/.config/notion/job-search.env` the way the cron wrapper does. It exits 1 when something would make a run fail (✗), and 0 when there are only warnings (!).

---

## Running it

### Running a search

```bash
./run_job_search.sh
```

Or invoke the package directly with flags (`job-search` works the same after `pip install -e .`):

```bash
# Dry run: evaluate and generate Markdown without syncing to Notion or sending notifications
python3 -m job_search_pipeline --no-notion --no-notify --md-output data/latest_job_summary.md

# Re-evaluate already-scraped jobs in data/enriched_jobs.json after tweaking config.json
python3 -m job_search_pipeline --skip-fetch --skip-scrape --no-notion --no-notify --md-output data/latest_job_summary.md

# Skip the LLM review step (use Stage 3 keyword scores only)
python3 -m job_search_pipeline --no-llm-review --min-score 75
```

### Cron

To run at 7:10, 12:10 and 19:10:

```cron
10 7,12,19 * * * /path/to/job-search-pipeline/run_job_search.sh
```

The wrapper appends every run to `data/cron_job_search.log` and writes the latest summary table to `data/latest_job_summary.md`. When the log passes 5 MB, the wrapper keeps its newest half at the start of the next run. Set `JOB_SEARCH_LOG_MAX_BYTES` on the crontab line to change the limit, or to `0` to turn trimming off (for example, to manage the file with `logrotate` instead).

The ATS feed is refreshed once a day, usually between about 15:00 and 19:00 UTC; runs before that see the previous day's snapshot, and the pipeline warns when the feed is more than `sources.feed_stale_hours` (36) old.

### Tests

```bash
pip install -r requirements-dev.txt   # ruff, pinned to the version CI uses
ruff check .
python3 -m unittest discover -s tests
```

GitHub Actions runs the same lint and tests on Python 3.10 and 3.13 for every pull request and push to `main` ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)).

The tests load `tests/fixtures/search_config.json` (a made-up search) through `tests/_fixture_config.py` and write only to a temporary directory, so they pass whatever your own `config.json` says and never touch `data/`.

---

## License

[MIT](LICENSE)
