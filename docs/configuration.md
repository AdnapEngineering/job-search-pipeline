# Configuration

[← README](../README.md)

## candidate_profile.md
Create it from the template first if you have not already (`cp candidate_profile.example.md candidate_profile.md`). Describe your background honestly; the AI review grades every posting against it. Editing the file makes the cached reviews out of date, so open matches are reviewed again on the next run. Include:
- Your total years of experience, and years with each core tool or skill.
- The tools, certifications and specialties you don't have.
- Specific accomplishments ("Strongest Evidence") that a well-matched job description would mirror.
- The levels you're aiming for, and anything that rules a job out.

## config.json
For a US-based first search, run `uv run python -m job_search_pipeline.setup` (or `python3 -m job_search_pipeline.setup` inside an activated `.venv`) and answer the questions. This creates a small config without AI, and refuses to replace an existing one (including the path selected by `JOB_PIPELINE_CONFIG`). It searches remote jobs and optionally your city, matches the literal title phrases you provide, and sets the output score threshold to 55. It leaves state and time-zone remote restrictions off; check eligibility yourself. The starter retains the default clearance and foreign-language filters. Some sources target software jobs. To change answers later, edit `config.json`, or move it aside and run setup again.

Alternatively, copy `config.example.json` and edit it, or use `--init-config` to draft settings with an AI CLI. The template [`config.example.json`](../config.example.json) lists every setting: the built-in default for the general ones, a placeholder or example for the career-specific ones (`PERSONAL_KEYS` in [`defaults.py`](../job_search_pipeline/defaults.py), which holds every default; a test keeps the two in step). Your file is laid over the defaults, so delete any setting you leave at its default: only what you change needs to be there, and later default improvements reach you. Inside a section a setting's value replaces the default whole (a list is not appended to).

| Section | What to set |
| :--- | :--- |
| `paths` | Where data files (`filtered_jobs`, `enriched_jobs`, `blocklist`, `review_cache`, `reviewed_postings`, `llm_spend`, `text_export`, `html_digest`, `digest_store`, ...) and `candidate_profile` live. Relative paths resolve from the repository directory; `~` is expanded. |
| `sources` | `lookback_days`, LinkedIn/Indeed/Glassdoor `search_terms` and `searches` (`[site, location, suffix, count]`, site one of `linkedin`, `indeed`, `glassdoor`), the `*_enabled` switches, the `jobicy_url` industry parameter, `himalayas_max_pages`, and the We Work Remotely `wwr_feeds` RSS list. |
| `location` | `allow_remote`, your metro (`local_metro_patterns`, `local_metro_exclude_patterns`), `home_states` / `home_timezones` for state- or zone-limited remote roles, and the foreign-place lists. See [Location rules](#location-rules). |
| `compensation` | `min_salary` (annual USD floor; `min_salary: 0` disables pay filtering), `pay_midpoint_max_cap` and `pay_midpoint_penalty`, and `pay_floor_taper_from` to soften the floor's cap.<sup>1</sup> |
| `experience` | `max_min_yoe` (most years a JD may ask), `max_min_secondary_yoe`, `reject_clearance` with `clearance_ignore_terms` (e.g. `["public trust"]`), and `reject_foreign_language_req`.<sup>2</sup> |
| `titles` | `target_patterns` (Stage 1 pre-filter), `strong_target_patterns`, `primary_role_groups` (title bonus), `primary_role_patterns`, and the lists that reject titles: `exclude_seniority_patterns`, `exclude_tech_patterns`, `exclude_role_patterns` and `soft_exclude_role_patterns`.<sup>3</sup> |
| `companies` | `exclude_companies`, `exclude_recruiters` (staffing agencies/aggregators), `allow_recruiters`, `company_aliases` (parent company → brand), and `max_roles_per_run`, the most roles from one company output in a run (default `0`, no limit). It applies to Notion, Obsidian and the text export; the HTML digest still lists every role. With `1`, a company posting nine matching roles at once gives one row, its highest scored; the others are named on that row ("Also open at this company: …") and blocklisted with it. |
| `skills_and_scoring` | `core_strengths` (with optional per-skill `core_strength_weights`), `focus_keywords` (with `focus_keyword_groups` for synonyms), `hard_exclude_skills`, `hard_ask_penalties`, `off_center_role_check`, and the score floors and caps in [How scores are calculated](../README.md#how-scores-are-calculated). |
| `llm_review` | `provider`, `model` and optional `triage_model` (Pass-1 blocker triage model), `rubric_descriptions` and 3–8 `calibration_examples` for your career, score caps and verdict thresholds, `hide_third_party`, spending limits in `budget` and prices in `pricing`.<sup>4</sup> |
| `url_normalization` | `aggregator_hosts` (job boards whose links are re-pointed to the employer's posting) `extra_tracking_params`, and `resolve_direct_hosts` (job boards, Glassdoor by default, whose links on roles about to be synced are looked up on the company's own Greenhouse, Lever or Ashby board and swapped for that posting; a role missing from the company's board gets a note instead, shown in Notion only). The defaults suit most searches. |
| `notion` | `database_id` (or `NOTION_DATABASE_ID`), and optionally `summary_page_id` for the run-summary Callout. `summary_block_id` is filled in automatically. See [Notion](outputs.md). |
| `obsidian` | `enabled`, `vault_path` (or `OBSIDIAN_VAULT`), `folder`, `summary_note` and `tag`. See [Obsidian](outputs.md#obsidian). |
| `text_export` | `enabled: true` appends new qualified matches to `paths.text_export` (`data/job_matches.txt`). |
| `html_digest` | On by default, written to `paths.html_digest` (`enabled: false` turns it off); set `title`, `keep_days` (default 30) and `url` if you host the page. See [HTML digest](outputs.md#html-digest). |

<sup>1</sup> A posting whose band tops out below `min_salary` is capped at `llm_review.blocker_cap`. With `pay_floor_taper_from` (a figure below `min_salary`), the cap instead rises in a straight line from `blocker_cap` at `pay_floor_taper_from` to 100 at `min_salary`; `0` keeps the cliff.

<sup>2</sup> For years of experience, a range such as "5-8+ years" counts as its lower bound, and figures quoted for another level or as "ideally"/nice-to-have are ignored. A clearance listed only under a preferred or "ways to stand out" section does not reject a posting.

<sup>3</sup> `primary_role_groups` give their `points` (default +20); `primary_role_patterns` boost LinkedIn/Indeed titles by 20. `exclude_role_patterns` reject a title outright; `soft_exclude_role_patterns` reject it unless it also matches `strong_target_patterns`, so there's no need to repeat them in both.

<sup>4</sup> `apply_min_score` is the Match % at which the verdict reads APPLY; leave it out to follow `skills_and_scoring.min_sync_score`, so APPLY always means "will be synced". `level_mismatch_cap` caps a role graded level 0 (under- or over-leveled), so strong skill overlap cannot lift a junior or executive role to APPLY; it defaults to 65, and 100 turns it off. `core_work_caps` does the same per core_work grade, so mostly-backend or off-target work cannot reach APPLY on requirement and domain overlap alone; it defaults to `{"0": 30, "1": 65}`, and `{}` turns it off. `hide_third_party: true` drops postings flagged as coming from a staffing agency, recruiter or aggregator, as if skipped; the default `false` only labels them. Tune `repost_similarity` too; see [AI review spend and caps](#ai-review-spend-and-caps).

### Location rules

- Set `home_states` (e.g. `["CO"]`) and `home_timezones` (any of `eastern`, `central`, `mountain`, `pacific`) to drop remote roles restricted to other states or zones ("Remote - TX", "must reside in MA, IN, or TX", "must be located in the Eastern time zone") before the LLM review. Both rules are off while the lists are empty.
- `foreign_places` (countries, regions, provinces) rules out a feed location or title that names one and not your country, JD residency wording ("remote within Canada", "anywhere in Australia") and a JD `Location:` line. `foreign_cities` only rules out a JD `Location:` line, since a city name alone can be a town in your country (Dublin, OH). Entries are lower-case regex fragments matched as whole words.
- `local_metro_exclude_patterns` drops a city in your metro's name that is in another state.
- If you're searching from outside the US: both foreign lists default to a US-based search (shown in the template). Set `allowed_country_pattern` and `country_token_pattern` to your own country and replace both lists.

### Where the config is read from

- The pipeline reads `config.json` from the repository directory. It is your own copy of the tracked template `config.example.json` and is ignored by git. Set `JOB_PIPELINE_CONFIG=/path/to/other.json` to use a different file (e.g. one per career).
- If `config.json` does not exist yet, a search run stops and says how to create it; the template is never read as your config.
- If a file named by `JOB_PIPELINE_CONFIG` is missing, a search run stops the same way. If a config file is not valid JSON, the run stops with the file, line and column of the error. `uv run python -m job_search_pipeline --check` also catches unknown keys, such as typos, which would otherwise be ignored.
- The Notion logger only writes `summary_block_id` back when `config.json` parsed cleanly, so a typo in the file is never overwritten.

## AI reviewer

By default (`"provider": "auto"`, or no `provider` at all) the pipeline uses the first of these CLIs it finds installed, in the order listed, and reports which one in `--check`. agy comes first so existing agy setups and their review caches carry on unchanged; on a machine without it, Claude Code is used. To pin one, edit the `llm_review` object in `config.json`; keep its other rubric settings:

| Provider | Settings to change | Setup |
| :--- | :--- | :--- |
| agy | `"provider": "agy"` (default model `gemini-4-hydrogen-medium`) | Install and configure `agy`; set `agy_path` if needed. |
| Claude Code | `"provider": "anthropic"` (default model `sonnet`) | Install [Claude Code](https://code.claude.com/docs/en/quickstart) and sign in; optionally set `claude_path`. |
| OpenAI Codex | `"provider": "openai"` (default: the model in Codex's own config) | Install [Codex CLI](https://developers.openai.com/codex/cli/) and run `codex login`; optionally set `codex_path`. |

Leave `model` empty to use the provider's default above, or set a model or alias supported by the selected CLI and available to your account. A `model` you set is kept when `provider` changes, so clear it when switching. To run Stage 4 as a **two-pass review fleet**, set `triage_model` (or pass `--triage-model`) to a faster, cheaper model tier (such as `haiku` for Claude Code or `gemini-4-hydrogen-flash` for agy): Pass 1 screens every candidate in parallel for hard blockers (`hard_reqs: 0`, `logistics_ok: false`, pay below floor, or hidden third-party posters), and only postings that pass advance to Pass 2 for full 12-point rubric grading and quote verification with `model`. `claude_path` and `codex_path` can specify an executable path; otherwise they are found on `PATH`. `timeout_seconds` and `workers` apply to all CLIs.

The pipeline uses CLI authentication; it does not call Anthropic or OpenAI APIs directly or require pipeline-specific API keys. Sign in under the same OS user that runs the pipeline, and check that your plan permits CLI usage. For cron, ensure that user's home directory, authentication, and `PATH` are available; the wrapper supplies a standard PATH including `~/.local/bin`, or configure an explicit CLI path.

CLI reviews send the candidate profile and selected job details to the provider and can consume subscription limits or incur charges depending on authentication and plan. Review the provider's data and retention policies. Each eligible uncached job can trigger a CLI invocation. Cached reviews are separated by provider, model, profile, prompt, and review version. Existing agy cache entries remain compatible.

Claude runs with built-in tools disabled, MCP auto-configuration and slash commands disabled, and session persistence off. Codex runs in a read-only sandbox with ephemeral sessions. Both run from an empty temporary directory, but CLI configuration and managed policies can still affect behavior; review them before running on sensitive data. Codex may still use tools permitted by its sandbox and configuration.

If a review times out, fails, or returns an invalid/incomplete reply, the pipeline reports the problem and keeps the job's keyword score, but holds the job back: it is listed in the summary as `[AI review failed, held for retry]`, is not synced or exported, and is reviewed again on the next run. If the CLI is not installed at all (or `candidate_profile.md` has not been created from its template), the stage is skipped and matches are output on their keyword scores, as with `--no-llm-review`, but unlike that flag the skip is flagged: the push notification is sent at high priority with "(AI review skipped)" in its title and the reason as a ⚠️ line, and the HTML digest shows the same warning at the top. An unknown provider is a configuration error.

The review also flags postings that come from a staffing agency, recruiter, consultancy or aggregator rather than the employer. The flag never changes the score: flagged roles sync as usual, with `[Third-party poster]` at the start of the "Why It Matches" text in Notion, the Markdown summary and the `.txt` export, and a count in the push notification.

Cached reviews made under an older profile, prompt, model or provider can never be read again and are pruned on the next run. Turning `triage_model` on, off or to another model keeps every full review; only Pass-1 triage skips are tied to the triage model that made them, and get a fresh review (or a fresh triage) when it changes. `--auto-calibrate` never learns from triage skips.

`filtered_jobs.json` and `enriched_jobs.json` only hold the last `sources.lookback_days` of postings, so the AI review also copies each reviewed posting into `data/reviewed_postings.json` (path `paths.reviewed_postings`). That is what lets `--calibrate` and `--auto-calibrate` keep replaying the rules on a review long after its posting ages out. Entries are dropped when their review is pruned, the newest `llm_review.posting_archive_max` (default 1000, roughly 12 MB) are kept, and 0 turns it off. The Match % and verdict of a cached review are recomputed from its stored grades on every run, so changes to `min_sync_score`, `apply_min_score` or the pay rules apply without paying for new reviews. Editing the profile, rubric or calibration examples still re-reviews every open match once.

A posting with a new URL is not reviewed again when it is a repost of one already reviewed: either the JD text is identical, or the company and title match and the JD text is at least `repost_similarity` alike (0–1, default `0.8`, estimated from a small fingerprint stored with each review). This covers the same role on LinkedIn and on the company's ATS, and reposts under a new ID with small edits. Reviews cached before this feature have no fingerprint, so only reviews made since then can be matched this way.

## AI review spend and caps

Every run records what the review used, in `paths.llm_spend` (`data/llm_spend.json`, kept for 90 days): calls made, reviews reused from the cache or from reposts, input/cached/output tokens, and cost. The figures appear in the console log, the push notification, the Markdown summary and the HTML digest (this run and today).

How cost is known depends on the CLI:

| Provider | Tokens | Cost in USD |
| :--- | :--- | :--- |
| Claude Code (`anthropic`) | Reported | Reported by the CLI (`total_cost_usd`); no setup needed. On a subscription this is the API-equivalent figure, not a charge. |
| Codex (`openai`) | Reported | Estimated from `pricing` when you set it; otherwise tokens only. |
| agy | Reported | Estimated from `pricing` when you set it; otherwise tokens only. |

Prices are USD per million tokens, keyed by model name, by provider (so switching `provider` picks up the right rates), or flat (applied to whichever provider is configured). Each call uses the most specific block that exists: its model, then its provider, then the flat rates. A model key lets the Pass-1 `triage_model` and the Pass-2 `model` carry different rates:

```json
"pricing": {
  "openai": { "input_per_mtok": 1.25, "cached_input_per_mtok": 0.125, "output_per_mtok": 10 },
  "gpt-5-mini": { "input_per_mtok": 0.25, "cached_input_per_mtok": 0.025, "output_per_mtok": 2 }
}
```

Each ledger entry also has a `passes` object with separate calls, tokens and cost for `review` (Pass 2) and, when `triage_model` is set, `triage` (Pass 1), each priced at its own model's rates. Those entries also record `triage_model`. Budget estimates also use each pass's own average output, so short triage replies don't lower the estimate for a deep review.

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

Since a capped run reviews in keyword-score order, the keyword score decides which matches get a review. Run [`--calibrate`](usage.md#tuning-the-rules-against-past-reviews) after a few runs to check that it ranks the reviewer's verdicts, and to tune the rules against the reviews already cached at no cost.

---
