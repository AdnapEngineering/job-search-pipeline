# Automated End-to-End Job Search Pipeline

An automated, idempotent, two-stage job search and screening pipeline that aggregates postings from **~1.4M+ direct ATS listings**, **LinkedIn**, **Indeed**, **Remote OK**, **Jobicy**, and **Hacker News ("Who is hiring?")**, enriches them with full job descriptions via direct ATS JSON APIs, scores them deterministically, performs a calibrated LLM rubric review against a candidate profile, and syncs top matches to **Notion** with push notifications via **ntfy**.

All career-specific rules—target titles, skills, scoring weights, experience ceilings, compensation floors, location rules, company/recruiter blocklists, and LLM rubric calibration—are externalized into [`config.json`](config.json) and [`candidate_profile.md`](candidate_profile.md). You can adapt the entire pipeline to **any career or industry** without editing Python code.

---

## Table of Contents

1. [What It Does & Pipeline Architecture](#1-what-it-does--pipeline-architecture)
2. [Pulling the GitHub Repository & Installation](#2-pulling-the-github-repository--installation)
3. [Data Sources, Links & `curl` Commands](#3-data-sources-links--curl-commands)
   - [GitHub ATS Job-Board Feed (`riley-dorrington/job-board-data`)](#31-github-ats-job-board-feed-riley-dorringtonjob-board-data)
   - [Remote OK API](#32-remote-ok-api)
   - [Jobicy Remote Jobs API](#33-jobicy-remote-jobs-api)
   - [Hacker News "Who Is Hiring?" (Algolia API)](#34-hacker-news-who-is-hiring-algolia-api)
   - [LinkedIn & Indeed (`python-jobspy`)](#35-linkedin--indeed-python-jobspy)
   - [Direct ATS JSON Endpoints (`batch_scraper.py`)](#36-direct-ats-json-endpoints-batch_scraperpy)
   - [Notion API & `ntfy` Push Notifications](#37-notion-api--ntfy-push-notifications)
4. [Configuring for Another Career (`config.json` & `candidate_profile.md`)](#4-configuring-for-another-career-configjson--candidate_profilemd)
5. [Usage, CLI Flags & Cron Automation](#5-usage-cli-flags--cron-automation)
6. [Repository File Reference](#6-repository-file-reference)

---

## 1. What It Does & Pipeline Architecture

```
┌────────────────────────────────────────────────────────────────────────────┐
│ 1. Fetch & Filter (fetch_and_filter.py + fetch_indeed.py + fetch_job_boards)│
│    • Streams gzipped ATS chunks (~1.4M rows), LinkedIn, Indeed, Remote OK, │
│      Jobicy, and HN Who's Hiring.                                          │
│    • Applies fast pre-filters (recency, company/recruiter exclusions,      │
│      location/remote eligibility, title matching, salary floor).           │
│    • Re-points aggregator reposts (LinkedIn/Indeed) to the company's own   │
│      direct ATS URL and deduplicates against blocklist.json.               │
└─────────────────────────────────────┬──────────────────────────────────────┘
                                      ▼
┌────────────────────────────────────────────────────────────────────────────┐
│ 2. Batch Scrape JDs (batch_scraper.py)                                     │
│    • Idempotently fetches full Job Descriptions (skips already-enriched).  │
│      Descriptions that previously failed with an error are retried.        │
│    • Queries structured JSON APIs first (Ashby, Lever, Greenhouse,         │
│      BambooHR, Workday CXS) before HTML fallback.                          │
│    • Detects closed/expired postings, separates requirements sections      │
│      from company boilerplate, and extracts matched skills.                │
└─────────────────────────────────────┬──────────────────────────────────────┘
                                      ▼
┌────────────────────────────────────────────────────────────────────────────┐
│ 3. Deterministic Evaluation (evaluate_jobs.py)                             │
│    • Screens JD body for hidden onsite/hybrid/foreign-residency rules.     │
│    • Distinguishes hard requirements from "nice-to-have / preferred"       │
│      sections and multi-language alternatives ("Skill A or Skill B").      │
│    • Enforces YOE ceilings (ignoring degree-equivalency boilerplate).      │
│    • Computes a 0–100% keyword score and deduplicates by company|title.    │
└─────────────────────────────────────┬──────────────────────────────────────┘
                                      ▼
┌────────────────────────────────────────────────────────────────────────────┐
│ 4. Calibrated LLM Rubric Review (llm_review.py)                            │
│    • Drops roles already in Notion blocklists before spending LLM quota.   │
│    • Grades each match against candidate_profile.md on a 12-point rubric:  │
│      hard_reqs (0–4), core_work (0–3), evidence (0–3), level (0–2).        │
│    • Verifies verbatim evidence_quote against the JD to prevent            │
│      hallucinated top scores; computes final Match % and verdict in Python.│
└─────────────────────────────────────┬──────────────────────────────────────┘
                                      ▼
┌────────────────────────────────────────────────────────────────────────────┐
│ 5. Notion Sync, Summary & Notifications (sync_notion_db.py + ntfy)         │
│    • Syncs qualified matches (>= min_score) to a Notion database.          │
│    • Appends synced URLs and company|title keys to persistent blocklists.  │
│    • Updates a Notion Callout dashboard block, saves Markdown summary,     │
│      and sends a push notification with source-health warnings.            │
└────────────────────────────────────────────────────────────────────────────┘
```

---

## 2. Pulling the GitHub Repository & Installation

### Clone and Pull Updates

```bash
# Clone the repository
git clone https://github.com/<your-username>/job-search-pipeline.git
cd job-search-pipeline

# Pull the latest changes at any time
git pull origin main
```

### Install Python Dependencies

Python 3.10+ is required:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

### Set Up Environment Variables

```bash
cp .env.example .env
chmod +x run_job_search.sh
```

Edit `.env` (or `~/.config/notion/job-search.env`) with your Notion token, Notion database ID, and optional `ntfy` URL:

```bash
NOTION_TOKEN="ntn_your_secret_token"
NOTION_DATABASE_ID="your-32-char-database-id"
NOTIFY_URL="https://ntfy.sh/your-topic"
```

---

## 3. Data Sources, Links & `curl` Commands

Below are the exact upstream endpoints queried by the pipeline and `curl` commands you can run to inspect their live responses.

### 3.1 GitHub ATS Job-Board Feed (`riley-dorrington/job-board-data`)

- **Manifest URL:** `https://riley-dorrington.github.io/job-board-data/data/chunks/jobs_manifest.json`
- **Chunk Base URL:** `https://riley-dorrington.github.io/job-board-data/data/chunks/`
- **What it provides:** Daily-updated gzipped JSON chunks (`jobs_0.json.gz`, `jobs_1.json.gz`, ...) containing ~1.4M+ job postings scraped directly from company ATS boards (Greenhouse, Lever, Ashby, Workday, BambooHR, iCIMS, SmartRecruiters, etc.).

**Inspect the manifest (chunk list & `last_updated` timestamp):**
```bash
curl -sL "https://riley-dorrington.github.io/job-board-data/data/chunks/jobs_manifest.json" | jq .
```

**Download and inspect the first 3 job records from chunk 0:**
```bash
curl -sL "https://riley-dorrington.github.io/job-board-data/data/chunks/jobs_0.json.gz" \
  | gunzip \
  | jq '.[0:3]'
```

**Count how many jobs are in a single chunk:**
```bash
curl -sL "https://riley-dorrington.github.io/job-board-data/data/chunks/jobs_0.json.gz" \
  | gunzip \
  | jq 'length'
```

---

### 3.2 Remote OK API

- **Endpoint:** `https://remoteok.com/api`
- **Used in:** [`fetch_job_boards.py`](fetch_job_boards.py) (`fetch_remoteok()`)
- **Note:** Element `[0]` of the array is Remote OK's legal notice; job objects start at index `[1]`.

**Fetch and inspect the newest 2 listings on Remote OK:**
```bash
curl -sL -A "Mozilla/5.0" "https://remoteok.com/api" | jq '.[1:3]'
```

---

### 3.3 Jobicy Remote Jobs API

- **Endpoint:** `https://jobicy.com/api/v2/remote-jobs?count=100&geo=usa&industry=engineering`
- **Used in:** [`fetch_job_boards.py`](fetch_job_boards.py) (`fetch_jobicy()`)
- **Adapting for other careers:** Change `industry=` in `config.json` (`sources.jobicy_url`) to match your field (e.g., `marketing`, `design-multimedia`, `data-science`, `business`, `hr`, `Customer-Support`, `finance-legal`, `management`, `copywriting`).

**Fetch and inspect the latest Jobicy remote listings:**
```bash
curl -sL -A "Mozilla/5.0" \
  "https://jobicy.com/api/v2/remote-jobs?count=5&geo=usa&industry=engineering" \
  | jq '.jobs[] | {companyName, jobTitle, jobGeo, salaryMin, salaryMax, pubDate, url}'
```

---

### 3.4 Hacker News "Who Is Hiring?" (Algolia API)

- **Search Endpoint:** `https://hn.algolia.com/api/v1/search_by_date?tags=story,author_whoishiring&query=who%20is%20hiring&hitsPerPage=5`
- **Thread Item Endpoint:** `https://hn.algolia.com/api/v1/items/{objectID}`
- **Used in:** [`fetch_job_boards.py`](fetch_job_boards.py) (`fetch_hn_whos_hiring()`)

**Step 1 — Find the latest monthly "Ask HN: Who is hiring?" thread `objectID`:**
```bash
curl -sL "https://hn.algolia.com/api/v1/search_by_date?tags=story,author_whoishiring&query=who%20is%20hiring&hitsPerPage=5" \
  | jq '.hits[] | select(.title | test("^Ask HN: Who is hiring"; "i")) | {objectID, title, created_at}'
```

**Step 2 — Fetch the thread comments (replace `<OBJECT_ID>` with the ID from Step 1):**
```bash
curl -sL "https://hn.algolia.com/api/v1/items/<OBJECT_ID>" \
  | jq '.children[0:3] | map({id, created_at, text})'
```

---

### 3.5 LinkedIn & Indeed (`python-jobspy`)

- **Used in:** [`fetch_indeed.py`](fetch_indeed.py)
- Queries LinkedIn and Indeed in parallel threads via [`python-jobspy`](https://github.com/Bunsly/JobSpy) using the `sources.search_terms` and `sources.searches` arrays in [`config.json`](config.json).
- Automatically caches LinkedIn job detail pages in `data/linkedin_details_cache.json` across runs to avoid `HTTP 429` rate limits and extracts `job_url_direct` when an offsite company ATS link is available.

---

### 3.6 Direct ATS JSON Endpoints (`batch_scraper.py`)

Instead of fragile HTML scraping, [`batch_scraper.py`](batch_scraper.py) queries each ATS's public JSON API first and falls back to `BeautifulSoup` HTML extraction only for custom career sites:

1. **Ashby Posting API**
   - URL pattern: `https://api.ashbyhq.com/posting-api/job-board/{org}`
   ```bash
   curl -sL "https://api.ashbyhq.com/posting-api/job-board/<org_slug>" \
     | jq '.jobs[0] | {id, title, location, jobUrl}'
   ```

2. **Lever Postings API**
   - URL pattern: `https://api.lever.co/v0/postings/{org}/{job_id}`
   ```bash
   curl -sL "https://api.lever.co/v0/postings/<org_slug>/<job_uuid>" \
     | jq '{text, categories, descriptionPlain}'
   ```

3. **Greenhouse Boards API**
   - URL pattern: `https://boards-api.greenhouse.io/v1/boards/{org}/jobs/{gh_jid}`
   ```bash
   curl -sL "https://boards-api.greenhouse.io/v1/boards/<org_slug>/jobs/<job_id>" \
     | jq '{title, location, updated_at, content}'
   ```

4. **BambooHR Career Detail JSON**
   - URL pattern: `https://{company}.bamboohr.com/careers/{job_id}/detail`
   ```bash
   curl -sL -H "Accept: application/json" \
     "https://<company>.bamboohr.com/careers/<job_id>/detail" \
     | jq '.result.jobOpening | {jobOpeningName, location, description}'
   ```

5. **Workday CXS API**
   - URL pattern: `https://{tenant}.wd1.myworkdayjobs.com/wday/cxs/{tenant}/{site}/job/{job_slug}`
   ```bash
   curl -sL -H "Accept: application/json" \
     "https://<tenant>.wd1.myworkdayjobs.com/wday/cxs/<tenant>/<site>/job/<job_slug>" \
     | jq '.jobPostingInfo | {title, location, timeType, jobDescription}'
   ```

---

### 3.7 Notion API & `ntfy` Push Notifications

**Test creating a page in your Notion Job Prospects database:**
```bash
curl -X POST "https://api.notion.com/v1/pages" \
  -H "Authorization: Bearer ${NOTION_TOKEN}" \
  -H "Notion-Version: 2022-06-28" \
  -H "Content-Type: application/json" \
  -d '{
    "parent": { "database_id": "'"${NOTION_DATABASE_ID}"'" },
    "properties": {
      "Company & Stage": { "title": [{ "text": { "content": "Example Corp" } }] },
      "Role Title & Compensation": { "rich_text": [{ "text": { "content": "Senior Specialist" } }] },
      "Location": { "rich_text": [{ "text": { "content": "Remote" } }] },
      "Why It Matches": { "rich_text": [{ "text": { "content": "APPLY: Strong skill alignment" } }] },
      "Match %": { "rich_text": [{ "text": { "content": "92%" } }] },
      "Link": { "url": "https://example.com/jobs/123" },
      "Date Posted": { "date": { "start": "2026-11-13" } }
    }
  }'
```

**Test sending a push notification via `ntfy`:**
```bash
curl -X POST "${NOTIFY_URL}" \
  -H "Title: Job Search: 3 Qualified Matches" \
  -H "Priority: 3" \
  -d "3 matches found (3 synced to Notion)."
```

---

## 4. Configuring for Another Career (`config.json` & `candidate_profile.md`)

To adapt the pipeline for a new career (e.g., Data Science, Product Management, DevOps, Design, Finance, Marketing, Healthcare):

### Step 1: Edit `candidate_profile.md`
Write an honest summary of the candidate's background. [`llm_review.py`](llm_review.py) hashes `candidate_profile.md`—any edit automatically invalidates stale cached LLM reviews:
- Total years of experience and years per core tool/skill.
- Explicit list of tools/certifications/specialties the candidate **does not** have.
- Specific accomplishments ("Strongest Evidence") that a top-matching job description should mirror.
- Target seniority levels and hard blockers.

### Step 2: Edit `config.json`
| Section | What to Customize |
| :--- | :--- |
| `paths` | Override where data files (`filtered_jobs`, `enriched_jobs`, `blocklist`, `review_cache`, ...) and `candidate_profile` live. Relative paths resolve from the repository directory, not the directory you run from; `~` is expanded. |
| `sources` | Set `lookback_days`, LinkedIn/Indeed `search_terms` and `searches` (`[site, location, suffix, count]`), and `jobicy_url` industry parameter. |
| `location` | Set `allow_remote`, `local_metro_patterns` (cities/suburbs in your metro area), `local_metro_exclude_patterns` (same-named cities in other states), and `allowed_country_pattern`. |
| `compensation` | Set `min_salary` (annual USD floor), `pay_midpoint_max_cap`, and `pay_midpoint_penalty`. Set `min_salary: 0` to disable salary filtering. |
| `experience` | Set `max_min_yoe` (rejects JDs asking for more than N years), `max_min_secondary_yoe` (ceiling for a secondary skill area), `reject_clearance`, and `reject_foreign_language_req`. |
| `titles` | Define `target_patterns` (Stage 1 pre-filter), `strong_target_patterns` (titles that override soft exclusions), `primary_role_groups` (+20 pt title bonus), `exclude_seniority_patterns`, `exclude_tech_patterns`, and `exclude_role_patterns`. |
| `companies` | List `exclude_companies`, `exclude_recruiters` (staffing agencies/aggregators), `allow_recruiters`, and `company_aliases` (e.g. parent company -> brand name). |
| `skills_and_scoring` | Define `core_strengths` (+5 pts each, up to `core_strengths_cap`), `focus_keywords` (weighted +5/+10/+15 domain terms, up to `domain_score_cap`), `hard_exclude_skills` (rejected in requirements unless listed as an optional/alternative skill), `hard_ask_penalties`, and `off_center_role_check`. |
| `llm_review` | Customize `rubric_descriptions` (`hard_reqs`, `core_work`, `evidence`, `level`) and provide 3–8 `calibration_examples` for your career. |
| `notion` | Set `database_id` (or use `NOTION_DATABASE_ID` in `.env`). Optionally set `summary_page_id` (the page that hosts the run-summary Callout; defaults to the database's parent page). `summary_block_id` is filled in automatically the first time the Callout is created. Expects Notion database properties: `Company & Stage` (Title), `Role Title & Compensation` (Text), `Location` (Text), `Why It Matches` (Text), `Match %` (Text), `Link` (URL), `Date Posted` (Date), and `Originally Posted` (Date). |

**How the config is loaded:**
- The pipeline reads `config.json` from the repository directory. Set `JOB_PIPELINE_CONFIG=/path/to/other.json` to use a different file (e.g. one per career).
- If the file is missing, every setting falls back to its built-in default. If it is not valid JSON, a `Could not read ...` warning is printed to stderr and defaults are used, so check the logs after editing.
- The Notion logger only writes `summary_block_id` back when `config.json` parsed cleanly, so a typo in the file is never overwritten.

---

## 5. Usage, CLI Flags & Cron Automation

### Run the Full Pipeline

```bash
./run_job_search.sh
```

Or invoke `run_pipeline.py` directly with flags:

```bash
# Dry run: evaluate and generate Markdown without syncing to Notion or sending notifications
python3 run_pipeline.py --no-notion --no-notify --md-output latest_job_summary.md

# Re-evaluate already-scraped jobs in data/enriched_jobs.json after tweaking config.json
python3 run_pipeline.py --skip-fetch --skip-scrape --no-notion --no-notify --md-output latest_job_summary.md

# Skip the LLM review step (use Stage 3 keyword scores only)
python3 run_pipeline.py --no-llm-review --min-score 75
```

### All CLI Flags (`run_pipeline.py`)

| Flag | Description |
| :--- | :--- |
| `--skip-fetch` | Skip Stage 1 (`fetch_and_filter.py`) and reuse `data/filtered_jobs.json`. |
| `--skip-scrape` | Skip Stage 2 (`batch_scraper.py`) and reuse `data/enriched_jobs.json`. |
| `--no-llm-review` | Skip Stage 4 (`llm_review.py`) and keep Stage 3 keyword scores. |
| `--no-notion` | Skip syncing matches to Notion. |
| `--no-notion-log` | Skip updating the Notion parent-page Callout summary block. |
| `--no-notify` | Skip sending the `ntfy` push notification. |
| `--min-score INT` | Minimum Match % to sync to Notion (defaults to `skills_and_scoring.min_sync_score`, `80`). |
| `--review-min-score INT` | Minimum Stage 3 keyword score to send to LLM review (defaults to `55`). |
| `--md-output PATH` | Write a Markdown summary table of matches, manual reviews, and skips to `PATH`. |

### Automate with Cron

Run 3 times daily (e.g., 7:10 AM, 12:10 PM, 5:10 PM):

```cron
10 7,12,17 * * * /path/to/job-search-pipeline/run_job_search.sh
```

---

## 6. Repository File Reference

| File | Purpose |
| :--- | :--- |
| [`run_job_search.sh`](run_job_search.sh) | Shell wrapper with `flock` concurrency lock, `.env` loading, and log rotation. |
| [`run_pipeline.py`](run_pipeline.py) | End-to-end orchestrator, source-health monitor, pre-LLM blocklist filter, and notifier. |
| [`pipeline_config.py`](pipeline_config.py) | Central loader for `config.json`, path resolution (relative `paths` entries resolve from the repo directory), shared JSON read/atomic-write helpers, and regex compilation helpers. |
| [`blocklist.py`](blocklist.py) | Shared dedup state: `company\|title` role keys plus the URL and role blocklists used by every stage. |
| [`config.json`](config.json) | Career-agnostic configuration file (sources, locations, pay, titles, skills, LLM rubric). |
| [`candidate_profile.md`](candidate_profile.md) | Candidate profile read by `llm_review.py` during Stage 4 rubric grading. |
| [`fetch_and_filter.py`](fetch_and_filter.py) | Stage 1 async downloader for the GitHub ATS chunk feed + orchestrator for board scrapers. |
| [`fetch_indeed.py`](fetch_indeed.py) | Stage 1 LinkedIn & Indeed scraper (`python-jobspy`) with detail caching and remote evidence checks. |
| [`fetch_job_boards.py`](fetch_job_boards.py) | Stage 1 connector for Remote OK, Jobicy, and Hacker News "Who is hiring?". |
| [`batch_scraper.py`](batch_scraper.py) | Stage 2 async JD enricher supporting Ashby, Lever, Greenhouse, BambooHR, Workday CXS, and HTML fallback. |
| [`evaluate_jobs.py`](evaluate_jobs.py) | Stage 3 deterministic evaluator, JD logistics/onsite blocker, context-aware requirement parser, and scorer. |
| [`llm_review.py`](llm_review.py) | Stage 4 LLM reviewer with SHA-256 caching, verbatim quote verification, and deterministic score calculation. |
| [`sync_notion_db.py`](sync_notion_db.py) | Stage 5 Notion database syncer (records synced roles in the blocklists) and Callout dashboard logger. |
| [`url_normalizer.py`](url_normalizer.py) | Canonical URL normalizer (strips tracking parameters, redundant `gh_jid`, and fragments). |
