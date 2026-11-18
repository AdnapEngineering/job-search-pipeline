# Outputs: Notion, Obsidian, text and HTML

[← README](../README.md)
## Notion

Notion is optional (see [Obsidian](#obsidian) and [other platforms](#without-notion)). To use it:

1. Create an integration at [notion.so/my-integrations](https://www.notion.so/my-integrations) and copy its secret into `NOTION_TOKEN`.
2. Create a database with these properties, named exactly as shown (any other properties you add are left alone), and copy its ID (the 32 characters in its URL) into `NOTION_DATABASE_ID`:

   | Property | Type |
   | :--- | :--- |
   | `Company & Stage` | Title |
   | `Role Title & Compensation` | Text |
   | `Location` | Text |
   | `Why It Matches` | Text |
   | `Match %` | Text |
   | `Link` | URL |
   | `Date Posted` | Date |
   | `Originally Posted` | Date (optional) |

3. Connect the integration to the database: open the database, choose **••• → Connections** and add your integration. Skipping this is the usual cause of 404 errors.
4. Run `python3 -m job_search_pipeline --check`. It confirms the token works, the database is reachable, and every property exists with the right type.

The first run also adds a run-summary Callout below the database on its parent page, and remembers it in `notion.summary_block_id`.

## Obsidian

Instead of (or as well as) Notion, the pipeline can write each new match as a note in an Obsidian vault. The vault is just a folder, so Obsidian doesn't need to be running, and the notes sync like any others (Obsidian Sync, iCloud, Syncthing, git).

Turn it on in `config.json`, or set `OBSIDIAN_VAULT` in `.env` in place of `vault_path`:

```json
"obsidian": {
  "enabled": true,
  "vault_path": "~/Documents/MyVault",
  "folder": "Job Prospects",
  "summary_note": "Job Search Run Summary",
  "tag": "job-prospect"
}
```

Or per run: `./run_job_search.sh --obsidian-vault ~/Documents/MyVault`. For Obsidian only, also leave Notion unset (see below) or pass `--no-notion`.

Each match at or above `--min-score` becomes `<folder>/<Company> - <Title>.md`. Its frontmatter holds the same fields as the Notion database, shown as Obsidian Properties:

```markdown
---
company: "Example Corp"
role: "Senior Specialist"
location: "Remote"
match: 92
link: "https://example.com/jobs/123"
date_added: 2026-11-16
originally_posted: 2026-11-12
status: Prospect
tags:
  - job-prospect
---

## Why It Matches

APPLY: Strong skill alignment (keyword score 85%)
```

Change `status` or add your own notes freely: an existing note is never overwritten. To get a table like the Notion database, create a Base (or a Dataview `TABLE company, role, match, status FROM #job-prospect SORT match DESC`). Each run also rewrites `<folder>/Job Search Run Summary.md` with the same counts, top matches and recent runs as the Notion Callout (`--no-obsidian-log` skips it).

Written roles go into the same blocklists as Notion and the `.txt` export. When Notion is also on, Notion does the blocklisting, so a role that fails to reach Notion is retried next run (its Obsidian note is left as it is). `--check` reports whether the vault folder exists and is writable.

## Without Notion

Notion is optional. If you use Obsidian, see [above](#obsidian). If you track applications somewhere else (Confluence, a wiki, a spreadsheet, a plain notes folder), turn off Notion writes and have the pipeline write matches to a text file instead.

To turn off Notion:

| Scope | How |
| :--- | :--- |
| One run | Pass `--no-notion` (skips both the database sync and the run-summary Callout). Pass `--no-notion-log` to keep the sync but skip only the Callout. |
| Permanently | Leave `notion.database_id` empty in `config.json` and don't set `NOTION_DATABASE_ID` / `NOTION_TOKEN`. Both Notion steps then skip themselves with a notice, and no token is needed. |

Matches still reach the [HTML digest](#html-digest) with no setup. To also write them to a `.txt` file, turn it on in `config.json`:

```json
"paths": { "text_export": "data/job_matches.txt" },
"text_export": { "enabled": true }
```

Or per run from the command line:

```bash
# Text file only, no Notion
python3 -m job_search_pipeline --no-notion --txt-output ~/Documents/job_matches.txt

# Through the cron wrapper (arguments passed to the wrapper replace its default --md-output)
./run_job_search.sh --no-notion --txt-output ~/Documents/job_matches.txt --md-output data/latest_job_summary.md
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

Roles are matched by `company|title`, and also by JD text plus title, so the same posting under another company name (a brand and its parent, a reposter) is one role. Exported roles are added to the same URL and role blocklists (`data/blocklist.json`, `data/blocklist_roles.json`) that Notion sync uses, so a role is written once and reposts on other boards are skipped. If Notion or Obsidian is also active, that output handles the blocklisting instead, so a role that fails to reach it is retried on the next run (and may appear in the `.txt` again). To re-export everything from scratch, delete those two blocklist files.

For other formats, `--md-output PATH` writes the full Markdown table (matches, manual reviews, AI skips, location drops, drop counts per rule), overwritten each run. `data/evaluated_jobs.json` holds the raw Stage 3 results as JSON for custom integrations.

## HTML digest

Every run writes `data/digest.html`, a single page with no external scripts that works offline. The [screenshot](../README.md#job-search-pipeline) at the top shows one. Open it with:

```bash
xdg-open data/digest.html   # macOS: open data/digest.html
```

It lists matches in three groups: **New this run**, **Still open** (seen before and again now), and **Earlier** (no longer in the run because the posting closed, was reposted, or was already sent to Notion or the `.txt` export; kept for `keep_days`). Each card shows the Match %, the AI verdict and reason, the gaps, and badges for third-party posters, roles already in Notion, and held reviews. Search, a minimum-match filter and a "hide third-party" toggle sit at the top. Below the matches are this run's AI-filtered roles, location drops, failed scrapes and the [drop counts per rule](usage.md#why-was-a-posting-dropped), plus source counts, source warnings and AI review spend.

**Mark applied** and **Hide** remember your choice in that browser only (local storage); they do not change the pipeline's blocklists. The matches behind the page are kept in `paths.digest_store` (`data/digest_matches.json`), so the page keeps matches from earlier runs even though each run only finds new ones.

To open the page from your phone, serve the folder (for example `python3 -m http.server -d data 8080` on your home network, or any static host) and set `html_digest.url` to its address. The push notification then opens the digest when tapped; without a `url`, the notification shows the file path.
