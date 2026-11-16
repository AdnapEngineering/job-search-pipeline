# Automated End-to-End Job Search Pipeline

An automated, idempotent, two-stage job search and screening pipeline that aggregates postings from **~1.4M+ direct ATS listings**, **LinkedIn**, **Indeed**, **Remote OK**, **Jobicy**, and **Hacker News ("Who is hiring?")**, enriches them with full job descriptions via direct ATS JSON APIs, scores them deterministically, performs a calibrated LLM rubric review against a candidate profile, and syncs top matches to **Notion** with push notifications via **ntfy**.

All career-specific rules—target titles, skills, scoring weights, experience ceilings, compensation floors, location rules, company/recruiter blocklists, and LLM rubric calibration—are externalized into `config.json` and `candidate_profile.md`, your own copies of the tracked templates [`config.example.json`](config.example.json) and [`candidate_profile.example.md`](candidate_profile.example.md). You can adapt the entire pipeline to **any career or industry** without editing Python code.

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
   - [AI Review Spend and Caps](#ai-review-spend-and-caps)
5. [Usage, CLI Flags & Cron Automation](#5-usage-cli-flags--cron-automation)
   - [Using Your Own Documentation Platform (Disable Notion / Export to .txt)](#using-your-own-documentation-platform-disable-notion--export-to-txt)
   - [View Matches in Your Browser (HTML Digest)](#view-matches-in-your-browser-html-digest)
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
│    • Reuses cached reviews (also for reposts), records token/USD spend,    │
│      and stops at the configured per-run / per-day caps.                   │
│    • Drops roles already in Notion blocklists, and remote roles limited to │
│      other states/time zones, before spending LLM quota.                   │
│    • Grades each match against candidate_profile.md on a 12-point rubric:  │
│      hard_reqs (0–4), core_work (0–3), evidence (0–3), level (0–2).        │
│    • Verifies verbatim evidence_quote against the JD to prevent            │
│      hallucinated top scores; computes final Match % and verdict in Python.│
└─────────────────────────────────────┬──────────────────────────────────────┘
                                      ▼
┌────────────────────────────────────────────────────────────────────────────┐
│ 5. Output, Summary & Notifications (sync_notion_db.py / text_export.py /   │
│    html_digest.py)                                                         │
│    • Rewrites data/digest.html, a self-contained page of matches.          │
│    • Syncs qualified matches (>= min_score) to a Notion database and/or    │
│      appends them to a plain .txt log for any other documentation tool.    │
│    • Appends output URLs and company|title keys to persistent blocklists.  │
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

Your settings live in `config.json` and your background in `candidate_profile.md`. Git ignores both, so pulling never touches them. New settings arrive in the tracked template `config.example.json`; after a pull, `git diff HEAD@{1} -- config.example.json` shows what was added so you can copy the keys you want into your own file. Any key you leave out keeps its built-in default.

> **Upgrading from a version where `config.json` and `candidate_profile.md` were tracked (one time only).** That pull removes both files from the repository, so git refuses to run it while your edited copies are in the way. Set your copies aside, pull, and put them back:
>
> ```bash
> mkdir -p ~/job-pipeline-backup
> cp config.json candidate_profile.md ~/job-pipeline-backup/   # keep your settings and profile
> git checkout -- config.json candidate_profile.md             # restore the tracked versions so the pull can remove them
> git pull origin main
> cp ~/job-pipeline-backup/config.json ~/job-pipeline-backup/candidate_profile.md .   # yours again, now ignored by git
> ```
>
> If you never edited either file, just pull, then create your copies as described under [Create Your Config and Profile](#create-your-config-and-profile).

### Install Python Dependencies

Python 3.10+ is required:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

### Create Your Config and Profile

```bash
cp config.example.json config.json
cp candidate_profile.example.md candidate_profile.md
```

Run the pipeline (and cron) straight from your clone: `config.json`, `candidate_profile.md`, `.env`, `data/`, the log and the summary are all ignored by git, so `git pull` updates the code and leaves them alone. To keep extra personal files in the clone (your own scripts or notes), list them in `.git/info/exclude`, which works like `.gitignore` but is never committed.

Edit the copies, not the templates: see [Configuring for Another Career](#4-configuring-for-another-career-configjson--candidate_profilemd). Until `config.json` exists the pipeline runs on the template's placeholder values and prints a reminder. Until `candidate_profile.md` exists the AI review is skipped (the placeholder profile is never sent to the LLM) and matches are output on their keyword scores.

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

All three are optional. Without a Notion token and database ID the pipeline still runs end to end; see [Using Your Own Documentation Platform](#using-your-own-documentation-platform-disable-notion--export-to-txt) to send matches to a `.txt` file instead.

The shell wrapper loads `~/.config/notion/job-search.env` and then `.env`, so a value in `.env` wins when both set it. Direct `python3` invocations do **not** load either file: export the variables in your shell first.

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
- Queries LinkedIn and Indeed in parallel threads via [`python-jobspy`](https://github.com/Bunsly/JobSpy) using the `sources.search_terms` and `sources.searches` arrays in `config.json`.
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
Create it from the template first if you have not already (`cp candidate_profile.example.md candidate_profile.md`). Write an honest summary of the candidate's background. [`llm_review.py`](llm_review.py) hashes `candidate_profile.md`—any edit automatically invalidates stale cached LLM reviews:
- Total years of experience and years per core tool/skill.
- Explicit list of tools/certifications/specialties the candidate **does not** have.
- Specific accomplishments ("Strongest Evidence") that a top-matching job description should mirror.
- Target seniority levels and hard blockers.

### Step 2: Edit `config.json`
Create it from the template first if you have not already (`cp config.example.json config.json`). The template [`config.example.json`](config.example.json) lists every setting: built-in defaults for the general ones, placeholders for the career-specific ones. In your copy, delete any setting you leave at its default; a missing key uses the built-in default, so only what you change needs to be there, and later default improvements reach you.

| Section | What to Customize |
| :--- | :--- |
| `paths` | Override where data files (`filtered_jobs`, `enriched_jobs`, `blocklist`, `review_cache`, `llm_spend`, `text_export`, `html_digest`, `digest_store`, ...) and `candidate_profile` live. Relative paths resolve from the repository directory, not the directory you run from; `~` is expanded. |
| `sources` | Set `lookback_days`, LinkedIn/Indeed `search_terms` and `searches` (`[site, location, suffix, count]`), and `jobicy_url` industry parameter. |
| `location` | Set `allow_remote`, `local_metro_patterns` (cities/suburbs in your metro area), `local_metro_exclude_patterns` (same-named cities in other states), and `allowed_country_pattern`. Set `home_states` (e.g. `["CO"]`) and `home_timezones` (any of `eastern`, `central`, `mountain`, `pacific`) to drop remote roles restricted to other states or time zones ("Remote - TX", "must reside in MA, IN, or TX", "must be located in the Eastern time zone") before the LLM review; both rules are off while the lists are empty. `foreign_places` (countries, regions, provinces) rules out a feed location or title that names one and not your country, JD residency wording ("remote within Canada", "anywhere in Australia") and a JD `Location:` line; `foreign_cities` only rules out a JD `Location:` line, since a city name alone can be a town in your country (Dublin, OH). Entries are lower-case regex fragments matched as whole words. **Searching from outside the US:** both lists default to a US-based candidate's (shown in the template). Set `allowed_country_pattern` and `country_token_pattern` to your own country and replace both lists. The pre-merge keys `foreign_exclude_patterns`, `jd_non_allowed_locations`, `jd_foreign_places`, `jd_foreign_terms` and `foreign_countries` are still read and added to them. |
| `compensation` | Set `min_salary` (annual USD floor), `pay_midpoint_max_cap`, and `pay_midpoint_penalty`. Set `min_salary: 0` to disable salary filtering. A posting whose pay band tops out below `min_salary` is capped at `llm_review.blocker_cap`. Set `pay_floor_taper_from` (a figure below `min_salary`) to soften that cliff: the cap then rises in a straight line from `blocker_cap` at `pay_floor_taper_from` to 100 at `min_salary`. `0` keeps the cliff. |
| `experience` | Set `max_min_yoe` (rejects JDs asking for more than N years; a range such as "5-8+ years" counts as its lower bound, and figures quoted for another level or as "ideally"/nice-to-have are ignored), `max_min_secondary_yoe` (ceiling for a secondary skill area), `reject_clearance`, `clearance_ignore_terms` (clearance wording to let through, e.g. `["public trust"]`), and `reject_foreign_language_req`. |
| `titles` | Define `target_patterns` (Stage 1 pre-filter), `strong_target_patterns` (titles that override soft exclusions), `primary_role_groups` (+20 pt title bonus), `primary_role_patterns` (+20 pt LinkedIn/Indeed title boost), `exclude_seniority_patterns`, `exclude_tech_patterns`, `exclude_role_patterns` (title words that reject a role) and `soft_exclude_role_patterns` (reject it too, unless the title matches `strong_target_patterns`; no need to repeat them in `exclude_role_patterns`). The pre-merge word lists `bad_role_patterns` and `soft_bad_role_patterns` are still read and added to these two. |
| `companies` | List `exclude_companies`, `exclude_recruiters` (staffing agencies/aggregators), `allow_recruiters`, and `company_aliases` (e.g. parent company -> brand name). |
| `skills_and_scoring` | Define `core_strengths` (+5 pts each, up to `core_strengths_cap`), `focus_keywords` (weighted +5/+10/+15 domain terms, up to `domain_score_cap`), `hard_exclude_skills` (rejected in requirements unless listed as an optional/alternative skill), `hard_ask_penalties`, and `off_center_role_check`. |
| `llm_review` | Select `provider` and `model`, customize `rubric_descriptions` (`hard_reqs`, `core_work`, `evidence`, `level`), and provide 3–8 `calibration_examples` for your career. `apply_min_score` is the Match % at which the verdict reads APPLY; leave it out to follow `skills_and_scoring.min_sync_score`, so that APPLY always means "will be synced". `level_mismatch_cap` caps the Match % of a role graded level 0 (under- or over-leveled for you), so strong skill overlap cannot lift a junior or executive role to APPLY; the template sets 65, and leaving it out means no cap. Set `hide_third_party: true` to drop postings the reviewer flags as coming from a staffing agency, recruiter or aggregator, as if they were skipped, so they never reach Notion, the digest or the text export; the default `false` only labels them. Set spending limits in `budget` and token prices in `pricing`, and tune `repost_similarity` (see [AI Review Spend and Caps](#ai-review-spend-and-caps)). |
| `url_normalization` | `aggregator_hosts` (job boards whose links are re-pointed to the employer's own posting when possible) and `extra_tracking_params` (query parameters to strip when comparing URLs). The defaults suit most searches. |
| `notion` | Set `database_id` (or use `NOTION_DATABASE_ID` in `.env`). Optionally set `summary_page_id` (the page that hosts the run-summary Callout; defaults to the database's parent page). `summary_block_id` is filled in automatically the first time the Callout is created. Expects Notion database properties: `Company & Stage` (Title), `Role Title & Compensation` (Text), `Location` (Text), `Why It Matches` (Text), `Match %` (Text), `Link` (URL), `Date Posted` (Date), and `Originally Posted` (Date). |
| `text_export` | Set `enabled: true` to append new qualified matches to `paths.text_export` (`data/job_matches.txt` by default)—a plain-text alternative or companion to Notion. |
| `html_digest` | On by default: every run rewrites a self-contained web page of matches at `paths.html_digest` (`data/digest.html`). Set `title`, `keep_days` (how long matches stay listed after they were last seen, default 30), and `url` (a link to the page if you host it; ntfy then opens it on tap). `enabled: false` turns it off. See [View Matches in Your Browser](#view-matches-in-your-browser-html-digest). |

**How the config is loaded:**
- The pipeline reads `config.json` from the repository directory. It is your own copy of the tracked template `config.example.json` and is ignored by git. Set `JOB_PIPELINE_CONFIG=/path/to/other.json` to use a different file (e.g. one per career).
- If `config.json` does not exist yet, the template `config.example.json` is read instead and a reminder is printed to stderr. The template itself is never written to.
- If a file named by `JOB_PIPELINE_CONFIG` is missing, every setting falls back to its built-in default. If it is not valid JSON, a `Could not read ...` warning is printed to stderr and defaults are used, so check the logs after editing.
- The Notion logger only writes `summary_block_id` back when `config.json` parsed cleanly, so a typo in the file is never overwritten. If `config.json` does not exist yet, it is created as a copy of the template with the block id added.

### Choose an LLM Provider

The default is `agy`, preserving the existing CLI workflow. Edit the existing `llm_review` object in `config.json`; keep its other rubric settings:

| Provider | Settings to change | Setup |
| :--- | :--- | :--- |
| agy (default) | `"provider": "agy", "model": "gemini-4-hydrogen-medium"` | Install and configure `agy`; set `agy_path` if needed. |
| Claude Code | `"provider": "anthropic", "model": "YOUR_CLAUDE_MODEL"` | Install [Claude Code](https://code.claude.com/docs/en/quickstart) and sign in; optionally set `claude_path`. |
| OpenAI Codex | `"provider": "openai", "model": "YOUR_CODEX_MODEL"` | Install [Codex CLI](https://developers.openai.com/codex/cli/) and run `codex login`; optionally set `codex_path`. |

Replace the placeholder with a model or alias supported by the selected CLI and available to your account. Switching `provider` alone does not change `model`. `claude_path` and `codex_path` can specify an executable path; otherwise they are found on `PATH`. `timeout_seconds` and `workers` apply to all CLIs.

The pipeline uses CLI authentication; it does not call Anthropic or OpenAI APIs directly or require pipeline-specific API keys. Sign in under the same OS user that runs the pipeline, and check that your plan permits CLI usage. For cron, ensure that user's home directory, authentication, and `PATH` are available; the wrapper supplies a standard PATH including `~/.local/bin`, or configure an explicit CLI path.

CLI reviews send the candidate profile and selected job details to the provider and can consume subscription limits or incur charges depending on authentication and plan. Review the provider's data and retention policies. Each eligible uncached job can trigger a CLI invocation. Cached reviews are separated by provider, model, profile, prompt, and review version. Existing agy cache entries remain compatible.

Claude runs with built-in tools disabled, MCP auto-configuration and slash commands disabled, and session persistence off. Codex runs in a read-only sandbox with ephemeral sessions. Both run from an empty temporary directory, but CLI configuration and managed policies can still affect behavior; review them before running on sensitive data. Codex may still use tools permitted by its sandbox and configuration.

If a review times out, fails, or returns an invalid/incomplete reply, the pipeline reports the problem and keeps the job's keyword score, but **holds the job back**: it is listed in the summary as `[AI review failed, held for retry]`, is not synced or exported, and is reviewed again on the next run. If the CLI is not installed at all (or `candidate_profile.md` has not been created from its template), the stage is skipped and matches are output on their keyword scores, as with `--no-llm-review`. An unknown provider is a configuration error.

**Third-party posters.** The review also flags postings that come from a staffing agency, recruiter, consultancy or aggregator rather than the employer. The flag never changes the score: flagged roles sync as usual, with `[Third-party poster]` at the start of the "Why It Matches" text in Notion, the Markdown summary and the `.txt` export, and a count in the push notification.

**Cache upkeep.** Cached reviews made under an older profile, prompt, model or provider can never be read again and are pruned on the next run. The Match % and verdict of a cached review are recomputed from its stored grades on every run, so changes to `min_sync_score`, `apply_min_score` or the pay rules apply without paying for new reviews. Editing the profile, rubric or calibration examples still re-reviews every open match once.

**Reposts reuse reviews.** A posting with a new URL is not reviewed again when it is a repost of one already reviewed: either the JD text is identical, or the company and title match and the JD text is at least `repost_similarity` alike (0–1, default `0.8`, estimated from a small fingerprint stored with each review). This covers the same role on LinkedIn and on the company's ATS, and reposts under a new ID with small edits. Reviews cached before this feature have no fingerprint, so only reviews made since then can be matched this way.

### AI Review Spend and Caps

Every run records what the review used, in `paths.llm_spend` (`data/llm_spend.json`, kept for 90 days): calls made, reviews reused from the cache or from reposts, input/cached/output tokens, and cost. The figures appear in the console log, the push notification, the Markdown summary and the HTML digest (this run and today).

How cost is known depends on the CLI:

| Provider | Tokens | Cost in USD |
| :--- | :--- | :--- |
| Claude Code (`anthropic`) | Reported | Reported by the CLI (`total_cost_usd`); no setup needed. On a subscription this is the API-equivalent figure, not a charge. |
| Codex (`openai`) | Reported | Estimated from `pricing` when you set it; otherwise tokens only. |
| agy | Reported | Estimated from `pricing` when you set it; otherwise tokens only. |

Prices are USD per million tokens, either keyed by provider (so switching `provider` picks up the right rates) or flat (applied to whichever provider is configured):

```json
"pricing": {
  "openai": { "input_per_mtok": 1.25, "cached_input_per_mtok": 0.125, "output_per_mtok": 10 }
}
```

Caps go in `llm_review.budget`. Every cap defaults to `0`, which means no limit:

| Key | Limit |
| :--- | :--- |
| `max_reviews_per_run` | New reviews per run (cached and repost reviews are free and never count). |
| `max_tokens_per_run` / `max_tokens_per_day` | Tokens per run / per calendar day across runs. Good for subscription CLIs, where quota is counted in tokens. |
| `max_usd_per_run` / `max_usd_per_day` | Dollars per run / per day. Needs a reported cost (Claude) or `pricing`; without either, cost is unknown and these caps cannot trigger, so use a token cap. |

```json
"budget": { "max_reviews_per_run": 20, "max_tokens_per_day": 600000, "max_usd_per_day": 1.50 }
```

Reviews run highest keyword score first. Each one reserves its estimated cost (prompt length, plus the average output of earlier reviews) before it starts, so reviews running in parallel cannot jointly pass a cap. When the next review would pass a cap, the pass stops: the remaining matches are **deferred**, i.e. held like a failed review (`[AI review deferred: budget cap reached, held for next run]`, not synced or exported) and reviewed first on a later run. Real usage can still run over a cap slightly when a reply is longer than estimated.

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
| `--min-score INT` | Minimum Match % to sync to Notion / export to `.txt` (defaults to `skills_and_scoring.min_sync_score`, `80`). |
| `--review-min-score INT` | Minimum Stage 3 keyword score to send to LLM review (defaults to `55`). |
| `--md-output PATH` | Write a Markdown summary table of matches, manual reviews, and skips to `PATH`. |
| `--txt-output PATH` | Append new qualified matches to `PATH` (overrides `text_export` in `config.json`). |
| `--no-txt-export` | Skip exporting matches to a `.txt` file even when enabled in `config.json`. Cannot be combined with `--txt-output`. |
| `--html-output PATH` | Write the HTML digest to `PATH` (overrides `html_digest` in `config.json`). |
| `--no-html` | Skip the HTML digest even when enabled in `config.json`. Cannot be combined with `--html-output`. |

### Using Your Own Documentation Platform (Disable Notion / Export to .txt)

Notion is optional. If you track applications somewhere else (Obsidian, Confluence, a wiki, a spreadsheet, a plain notes folder), turn off Notion writes and have the pipeline write matches to a text file instead.

**1. Disable Notion writes**

| Scope | How |
| :--- | :--- |
| One run | Pass `--no-notion` (skips both the database sync and the run-summary Callout). Pass `--no-notion-log` to keep the sync but skip only the Callout. |
| Permanently | Leave `notion.database_id` empty in `config.json` and don't set `NOTION_DATABASE_ID` / `NOTION_TOKEN`. Both Notion steps then skip themselves with a notice, and no token is needed. |

**2. View matches in your browser**

Nothing to set up: every run writes a web page of matches to `data/digest.html`. See [View Matches in Your Browser](#view-matches-in-your-browser-html-digest).

**3. Export matches to a `.txt` file**

Enable it permanently in `config.json` (ideal for cron):

```json
"paths": { "text_export": "data/job_matches.txt" },
"text_export": { "enabled": true }
```

Or per run from the command line:

```bash
# Text file only, no Notion
python3 run_pipeline.py --no-notion --txt-output ~/Documents/job_matches.txt

# Through the cron wrapper (arguments passed to the wrapper replace its default --md-output)
./run_job_search.sh --no-notion --txt-output ~/Documents/job_matches.txt --md-output latest_job_summary.md
```

Each run appends a timestamped block containing only matches at or above `--min-score` that haven't been output before:

```text
===== Job Search Run 2026-11-13 09:25 — 2 new match(es) (min score 80%) =====

[92%] Example Corp — Senior Specialist
    Location: Remote
    Posted: 2026-11-12
    Why: APPLY: Strong skill alignment (keyword score 85%)
    Link: https://example.com/jobs/123

[84%] Another Co — Target Role 1
    Location: Your City, ST
    Posted: 2026-11-11
    Why: STRETCH: Matches core work; one tool gap. Gaps: tool_b.
    Link: https://jobs.example.org/456
```

**How deduplication works:** roles are matched by `company|title`, and also by JD text plus title, so the same posting under another company name (a brand and its parent, a reposter) is one role. Exported roles are added to the same URL and role blocklists (`data/blocklist.json`, `data/blocklist_roles.json`) that Notion sync uses, so a role is written once and reposts on other boards are skipped. If Notion is also active, Notion sync handles the blocklisting instead, so a role that fails to reach Notion is retried on the next run (and may appear in the `.txt` again). To re-export everything from scratch, delete those two blocklist files.

**Other formats:** `--md-output PATH` writes the full Markdown table (matches, manual reviews, AI skips, location drops), overwritten each run. `data/evaluated_jobs.json` holds the raw Stage 3 results as JSON for custom integrations.

### View Matches in Your Browser (HTML Digest)

Without any setup, every run writes `data/digest.html`: a single self-contained page (no server, no external scripts, works offline) that you open in any browser:

```bash
xdg-open data/digest.html   # macOS: open data/digest.html
```

It lists matches in three groups: **New this run**, **Still open** (seen before and again now), and **Earlier** (no longer in the run because the posting closed, was reposted, or was already sent to Notion or the `.txt` export; kept for `keep_days`). Each card shows the Match %, the AI verdict and reason, the gaps, and badges for third-party posters, roles already in Notion, and held reviews. Search, a minimum-match filter and a "hide third-party" toggle sit at the top. Below the matches are this run's AI-filtered roles, location drops and failed scrapes, plus source counts, source warnings and AI review spend.

**Mark applied** and **Hide** remember your choice in that browser only (local storage); they do not change the pipeline's blocklists. The matches behind the page are kept in `paths.digest_store` (`data/digest_matches.json`), so the page keeps matches from earlier runs even though each run only finds new ones.

To open the page from your phone, serve the folder (for example `python3 -m http.server -d data 8080` on your home network, or any static host) and set `html_digest.url` to its address. The push notification then opens the digest when tapped; without a `url`, the notification shows the file path.

### Automate with Cron

Run 3 times daily (e.g., 7:10 AM, 12:10 PM, 7:10 PM):

```cron
10 7,12,19 * * * /path/to/job-search-pipeline/run_job_search.sh
```

The wrapper appends every run to `cron_job_search.log` next to the scripts. When the log passes 5 MB, the wrapper keeps its newest half at the start of the next run. Set `JOB_SEARCH_LOG_MAX_BYTES` in `.env` to change the limit, or to `0` to turn trimming off (for example, to manage the file with `logrotate` instead).

### Run the Tests

```bash
ruff check .
python3 -m unittest discover -s tests
```

The tests load `tests/fixtures/search_config.json` (a made-up search) through `tests/_fixture_config.py` and write only to a temporary directory, so they pass whatever your own `config.json` says and never touch `data/`.

---

## 6. Repository File Reference

| File | Purpose |
| :--- | :--- |
| [`run_job_search.sh`](run_job_search.sh) | Shell wrapper that runs `.venv`'s Python when present, with a concurrency lock (`flock`, or a `mkdir` lock on macOS) and `.env` loading; appends each run to `cron_job_search.log`, trimmed to its newest half past 5 MB. |
| [`run_pipeline.py`](run_pipeline.py) | End-to-end orchestrator, source-health monitor, pre-LLM blocklist filter, and notifier. |
| [`pipeline_config.py`](pipeline_config.py) | Central loader for `config.json`, path resolution (relative `paths` entries resolve from the repo directory), shared JSON read/atomic-write helpers, and regex compilation helpers. |
| [`blocklist.py`](blocklist.py) | Shared dedup state: `company\|title` role keys plus the URL and role blocklists used by every stage. |
| [`config.example.json`](config.example.json) | Tracked template for the career-agnostic configuration (sources, locations, pay, titles, skills, LLM rubric). |
| `config.json` | Your own copy of the template, ignored by git; this is the file the pipeline reads and the one you edit. |
| [`candidate_profile.example.md`](candidate_profile.example.md) | Tracked template for the candidate profile. |
| `candidate_profile.md` | Your own copy of the template, ignored by git; read by `llm_review.py` during Stage 4 rubric grading. |
| [`fetch_and_filter.py`](fetch_and_filter.py) | Stage 1 async downloader for the GitHub ATS chunk feed + orchestrator for board scrapers. |
| [`fetch_indeed.py`](fetch_indeed.py) | Stage 1 LinkedIn & Indeed scraper (`python-jobspy`) with detail caching and remote evidence checks. |
| [`fetch_job_boards.py`](fetch_job_boards.py) | Stage 1 connector for Remote OK, Jobicy, and Hacker News "Who is hiring?". |
| [`batch_scraper.py`](batch_scraper.py) | Stage 2 async JD enricher supporting Ashby, Lever, Greenhouse, BambooHR, Workday CXS, and HTML fallback. |
| [`evaluate_jobs.py`](evaluate_jobs.py) | Stage 3 deterministic evaluator, JD logistics/onsite blocker, context-aware requirement parser, and scorer. |
| [`llm_review.py`](llm_review.py) | Stage 4 LLM reviewer with SHA-256 caching, verbatim quote verification, and deterministic score calculation. |
| [`sync_notion_db.py`](sync_notion_db.py) | Stage 5 Notion database syncer (records synced roles in the blocklists) and Callout dashboard logger. |
| [`html_digest.py`](html_digest.py) | Stage 5 self-contained HTML page of matches (`data/digest.html`), with a store that keeps earlier runs' matches for `keep_days`. |
| [`text_export.py`](text_export.py) | Stage 5 plain-text alternative to Notion: appends new qualified matches to a `.txt` log and records them in the blocklists. |
| [`url_normalizer.py`](url_normalizer.py) | Canonical URL normalizer (strips tracking parameters, redundant `gh_jid`, and fragments). |
| [`tests/`](tests) | Unit and regression tests, run against the generic `tests/fixtures/search_config.json`. |

---

## License

[MIT](LICENSE)
