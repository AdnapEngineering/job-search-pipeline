# Data sources

[← README](../README.md)

The endpoints each source uses, with `curl` commands for looking at their live responses.

## job-board-aggregator

- Project: [Riley-Dorrington/job-board-aggregator](https://github.com/Riley-Dorrington/job-board-aggregator) (MIT): the multithreaded ETL that scrapes the ATS boards daily. Its output is published to [Riley-Dorrington/job-board-data](https://github.com/Riley-Dorrington/job-board-data), which this pipeline reads.
- Manifest URL: `https://riley-dorrington.github.io/job-board-data/data/chunks/jobs_manifest.json`
- Chunk base URL: `https://riley-dorrington.github.io/job-board-data/data/chunks/`
- Contents: gzipped JSON chunks, updated daily (`jobs_0.json.gz`, `jobs_1.json.gz`, ...) holding about 1.4 million postings scraped directly from company ATS boards (Workday, iCIMS, Greenhouse, Paylocity, Lever, BambooHR, Ashby and Join). It only covers the companies the aggregator knows about, so some postings on those ATSs are missing, and ATSs outside that list (SmartRecruiters, Workable, JazzHR, UKG, Dover, ...) are not covered at all. Rows carry title, company, location and link but no description; Stage 2 fetches that.

<details>
<summary><code>curl</code> commands</summary>

Inspect the manifest (chunk list & `last_updated` timestamp):
```bash
curl -sL "https://riley-dorrington.github.io/job-board-data/data/chunks/jobs_manifest.json" | jq .
```

Download and inspect the first 3 job records from chunk 0:
```bash
curl -sL "https://riley-dorrington.github.io/job-board-data/data/chunks/jobs_0.json.gz" \
  | gunzip \
  | jq '.[0:3]'
```

Count how many jobs are in a single chunk:
```bash
curl -sL "https://riley-dorrington.github.io/job-board-data/data/chunks/jobs_0.json.gz" \
  | gunzip \
  | jq 'length'
```

</details>

## Remote OK

- Endpoint: `https://remoteok.com/api`
- Used in: [`sources/job_boards.py`](../job_search_pipeline/sources/job_boards.py) (`fetch_remoteok()`)
- Note: element `[0]` of the array is Remote OK's legal notice; job objects start at index `[1]`. The free API lags the site by a few days, so with a short `lookback_days` it can return no recent postings; `sources.remoteok_enabled: false` turns it off.

<details>
<summary><code>curl</code> commands</summary>

Fetch and inspect the newest 2 listings on Remote OK:
```bash
curl -sL -A "Mozilla/5.0" "https://remoteok.com/api" | jq '.[1:3]'
```

</details>

## Jobicy

- Endpoint: `https://jobicy.com/api/v2/remote-jobs?count=100&geo=usa&industry=engineering`
- Used in: [`sources/job_boards.py`](../job_search_pipeline/sources/job_boards.py) (`fetch_jobicy()`)
- Other careers: change `industry=` in `config.json` (`sources.jobicy_url`) to match your field (e.g., `marketing`, `design-multimedia`, `data-science`, `business`, `hr`, `Customer-Support`, `finance-legal`, `management`, `copywriting`).

<details>
<summary><code>curl</code> commands</summary>

Fetch and inspect the latest Jobicy remote listings:
```bash
curl -sL -A "Mozilla/5.0" \
  "https://jobicy.com/api/v2/remote-jobs?count=5&geo=usa&industry=engineering" \
  | jq '.jobs[] | {companyName, jobTitle, jobGeo, salaryMin, salaryMax, pubDate, url}'
```

</details>

## Himalayas

- Endpoint: `https://himalayas.app/jobs/api/search` (`sources.himalayas_search_url`)
- Used in: [`sources/job_boards.py`](../job_search_pipeline/sources/job_boards.py) (`fetch_himalayas()`)
- One search per `sources.search_terms` entry, limited to US, full-time, newest first. It pages (20 per page, with a 1-second pause) until postings fall outside `lookback_days`, up to `sources.himalayas_max_pages` pages per term. Rows carry the full description and, often, a salary range.

<details>
<summary><code>curl</code> commands</summary>

```bash
curl -sL -A "Mozilla/5.0" \
  "https://himalayas.app/jobs/api/search?q=frontend%20engineer&country=United%20States&employment_type=Full%20Time&sort=recent&page=1" \
  | jq '.jobs[] | {companyName, title, locationRestrictions, minSalary, maxSalary, pubDate: (.pubDate | todate), guid}'
```

</details>

## We Work Remotely

- Feeds: the category RSS feeds in `sources.wwr_feeds` (front-end and full-stack programming by default)
- Used in: [`sources/job_boards.py`](../job_search_pipeline/sources/job_boards.py) (`fetch_wwr()`)
- Item titles are `Company: Title`. Regions such as "Europe Only" or "Asia Only" are dropped; "USA Only", "North America Only" and "Anywhere in the World" are kept. The feeds carry no salary field.

<details>
<summary><code>curl</code> commands</summary>

```bash
curl -sL -A "Mozilla/5.0" "https://weworkremotely.com/categories/remote-front-end-programming-jobs.rss" \
  | grep -E "<title>|<region>|<pubDate>" | head -20
```

</details>

## Hacker News "Who is hiring?"

- Search endpoint: `https://hn.algolia.com/api/v1/search_by_date?tags=story,author_whoishiring&query=who%20is%20hiring&hitsPerPage=5`
- Thread item endpoint: `https://hn.algolia.com/api/v1/items/{objectID}`
- Used in: [`sources/job_boards.py`](../job_search_pipeline/sources/job_boards.py) (`fetch_hn_whos_hiring()`)

<details>
<summary><code>curl</code> commands</summary>

Step 1: find the latest monthly "Ask HN: Who is hiring?" thread `objectID`:
```bash
curl -sL "https://hn.algolia.com/api/v1/search_by_date?tags=story,author_whoishiring&query=who%20is%20hiring&hitsPerPage=5" \
  | jq '.hits[] | select(.title | test("^Ask HN: Who is hiring"; "i")) | {objectID, title, created_at}'
```

Step 2: fetch the thread comments (replace `<OBJECT_ID>` with the ID from Step 1):
```bash
curl -sL "https://hn.algolia.com/api/v1/items/<OBJECT_ID>" \
  | jq '.children[0:3] | map({id, created_at, text})'
```

</details>

## LinkedIn, Indeed and Glassdoor

- Used in: [`sources/linkedin_indeed.py`](../job_search_pipeline/sources/linkedin_indeed.py)
- Queries each site that `sources.searches` names (`linkedin`, `indeed`, `glassdoor`) in parallel threads via [`python-jobspy`](https://github.com/speedyapply/JobSpy), once per `sources.search_terms` entry. Glassdoor is off until you add a search for it, e.g. `["glassdoor", "United States", " remote", 30]`. jobspy's other sites are not supported: ZipRecruiter and Google Jobs return 403 or nothing without proxies, and the rest are regional boards.
- Caches LinkedIn job detail pages (description, pay, seniority) in `data/linkedin_details_cache.json` across runs to avoid `HTTP 429` rate limits, and uses `job_url_direct` when a site gives an offsite company ATS link.
- LinkedIn and Glassdoor queries are spaced `linkedin_pause_seconds` apart (default 10), and the first 429 or 403 block from either site skips its remaining queries for the run (a source warning says how many).
- Uncached LinkedIn job pages are fetched 2–5 seconds apart (jobspy itself fetches them back to back, and that burst is what trips the 429). A 429 on a job page counts as a block just like one on a search page: no further job pages are requested and the query stops there.
- A block also puts that site on a cooldown: later runs skip it completely for `block_cooldown_hours` (default 24), so the block can expire instead of being renewed by every scheduled run. Each block in a row doubles the wait, up to 4× (96h at the default), and the first clean run clears it. The cooldown is kept in `data/throttle_state.json` (path `paths.throttle_state`); delete that file to try the site again sooner. While a site is cooling down the digest shows `LinkedIn 0 (cooling down)` and a warning with the time of the next try.

## Job descriptions from ATS APIs

[`scrape.py`](../job_search_pipeline/scrape.py) gets descriptions from the public JSON APIs of Ashby, Lever, Greenhouse, BambooHR and Workday, and parses the page's HTML for other career sites.

<details>
<summary><code>curl</code> commands for each ATS</summary>

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

</details>

## Notion and ntfy

<details>
<summary><code>curl</code> commands</summary>

Test creating a page in your Notion Job Prospects database:
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

Test sending a push notification via `ntfy`:
```bash
curl -X POST "${NOTIFY_URL}" \
  -H "Title: Job Search: 3 Qualified Matches" \
  -H "Priority: 3" \
  -d "3 matches found (3 synced to Notion)."
```

</details>

---
