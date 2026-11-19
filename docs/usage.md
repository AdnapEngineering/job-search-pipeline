# Usage: flags, tracing drops and troubleshooting

[← README](../README.md)
## Flags

| Flag | Description |
| :--- | :--- |
| `--check` | Check the setup and exit without running (same as `python3 -m job_search_pipeline.check`); see [Checking the setup](../README.md#checking-the-setup). |
| `--explain URL` | Trace one posting through the Stage 1 filters, every Stage 3 rule and the score breakdown, then exit; see [Why was a posting dropped?](#why-was-a-posting-dropped). `--title`, `--company` and `--location` fill in a posting the pipeline has not seen; `--rescrape` fetches its description again. |
| `--init-config [URL ...]` | Draft the career-specific settings from `candidate_profile.md` (and up to 3 sample postings) with the AI reviewer's CLI, write them to `config.draft.json`, then exit; see [Your config and profile](../README.md#your-config-and-profile). `--init-output PATH` writes elsewhere; `--force` overwrites an existing file there. |
| `--skip-fetch` | Skip Stage 1 (`sources/filter.py`) and reuse `data/filtered_jobs.json`. |
| `--skip-scrape` | Skip Stage 2 (`scrape.py`) and reuse `data/enriched_jobs.json`. |
| `--no-llm-review` | Skip Stage 4 (`review.py`) and keep Stage 3 keyword scores. |
| `--no-notion` | Skip syncing matches to Notion. |
| `--no-notion-log` | Skip updating the Notion parent-page Callout summary block. |
| `--obsidian-vault PATH` | Write new matches as notes in the Obsidian vault at `PATH` (overrides `obsidian` in `config.json`). |
| `--no-obsidian` | Skip the Obsidian notes even when enabled in `config.json`. Cannot be combined with `--obsidian-vault`. |
| `--no-obsidian-log` | Write the Obsidian notes but skip rewriting the run-summary note. |
| `--no-notify` | Skip sending the `ntfy` push notification. |
| `--min-score INT` | Minimum Match % to sync to Notion / Obsidian / export to `.txt` (defaults to `skills_and_scoring.min_sync_score`, `80`). |
| `--review-min-score INT` | Minimum Stage 3 keyword score to send to LLM review (defaults to `55`). |
| `--md-output PATH` | Write a Markdown summary table of matches, manual reviews, and skips to `PATH`. |
| `--txt-output PATH` | Append new qualified matches to `PATH` (overrides `text_export` in `config.json`). |
| `--no-txt-export` | Skip exporting matches to a `.txt` file even when enabled in `config.json`. Cannot be combined with `--txt-output`. |
| `--html-output PATH` | Write the HTML digest to `PATH` (overrides `html_digest` in `config.json`). |
| `--no-html` | Skip the HTML digest even when enabled in `config.json`. Cannot be combined with `--html-output`. |

## Why was a posting dropped?

Every run counts what each deterministic rule dropped: the Stage 1 pre-filters per source (title, location, company, pay and, for LinkedIn/Indeed, the keyword gate) and every Stage 3 rule (seniority, title, feed location, missing JD location, `hard_exclude_skills`, `avoid_keywords`, `conditional_require_any`, clearance, language, `specialty_reject_rules`, `max_min_yoe`, `max_min_secondary_yoe`, `off_center_role_check`, `max_senior_role_years`, and scoring under `secondary_floor`). The HTML digest shows this as a collapsed **Dropped by deterministic rules** table, and `--md-output` as a collapsible section, each with the count per rule and an example posting with the exact phrase that matched. A rule dropping far more than you expect is the first place to look when matches dry up. Title rules run before scoring, so both also list, collapsed, the Stage 3 title drops whose keyword score still reached `strong_floor`, with the pattern that fired: a fitting role there (say `\bembedded\b` dropping "Fullstack Engineer - Embedded Payments") means the pattern is too broad. Stage 3 counts cover every posting in the lookback window, so a posting is counted again on each run it stays in the window. Stage 1 leaves out the ATS feed's rows older than `lookback_days`, which are most of the feed's archive.

To see exactly why one posting was dropped or scored as it was:

```bash
python3 -m job_search_pipeline --explain 'https://jobs.lever.co/acme/123'
```

It prints each Stage 1 filter and Stage 3 rule with ✓ or ✗ and what matched, then the score line by line (base, title credit, each core strength and domain term with its points, each penalty) and the tier it lands in. For a dropped posting it also shows what it would have scored without the rule that dropped it, which tells you whether loosening that rule would let a good match through. A posting that passes goes on to the location-wording check and the blocklist. The posting's company, title and location come from `data/enriched_jobs.json` or `data/filtered_jobs.json` when the pipeline has seen it; for a new one the company is guessed from the URL and `--title` / `--location` fill in the rest. Quote the URL, since shells treat `?` and `&` specially. It reads your current `config.json`, so you can edit a rule and run it again to see the effect.

## Troubleshooting

**Notion returns 404 / "database not found".**
The database isn't shared with your integration: open it, choose **••• → Connections**, and add the integration. Then run `--check`, which also reports missing or mistyped properties. See [Notion](outputs.md).

**A match shows `[AI review failed, held for retry]` or `[AI review deferred]`.**
Failed means the reviewer timed out, crashed or returned an unusable reply; deferred means a `budget` cap was reached. Either way the job is held back from Notion, Obsidian and the `.txt` export and reviewed first on the next run. See [AI reviewer](configuration.md#ai-reviewer) and [AI review spend and caps](configuration.md#ai-review-spend-and-caps).

**How do I re-sync or re-export everything from scratch?**
Delete `data/blocklist.json` and `data/blocklist_roles.json`. Every open match is then treated as new on the next run. Existing Obsidian notes are kept, not duplicated.

**How do I let one synced or dismissed role show up again?**
Synced and exported roles go into the blocklists by URL and by `company|title` (so reposts stay out too), and deleting the Notion page or Obsidian note doesn't change that. Remove the role's URL from `data/blocklist.json` and its keys from `data/blocklist_roles.json`.

**Why does a run take 15+ minutes?**
Almost all of it is the LinkedIn and Indeed searches (one per search term per entry in `sources.searches`, with a pause between LinkedIn queries). The ATS feed takes under a minute and the JD scrape seconds. Trim `search_terms` or `searches` to speed it up.

**Is there a shorter command?**
`pip install -e .` adds `job-search`, which takes the same flags as `python3 -m job_search_pipeline`.

**A posting I expected never showed up.**
Run `python3 -m job_search_pipeline --explain 'URL'`: it names the rule that dropped it, the phrase that matched, and what it would have scored otherwise. See [Why was a posting dropped?](#why-was-a-posting-dropped).

**I changed `config.json`; do I have to fetch again?**
No: `--skip-fetch --skip-scrape` re-evaluates the jobs already in `data/enriched_jobs.json`, and cached AI reviews are rescored under the new settings for free.

---
