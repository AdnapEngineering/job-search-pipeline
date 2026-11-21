"""Built-in defaults for every config.json setting: the one place a default value is written down.

`config.load_config()` lays the user"s config.json over DEFAULTS, so pipeline code reads `CFG[section][key]`
without its own fallback. Inside a section each key"s value is replaced whole (a list or a map is not merged
with the default one).

config.example.json lists the same keys. For a general setting its value must equal the default here; only the
PERSONAL_KEYS (search terms, titles, skills, locations, pay floor and the like) hold placeholder or example
values instead. tests/test_defaults.py fails when the two drift apart.
"""

# Career-specific settings: the template shows a placeholder or an example, not the default
PERSONAL_KEYS = {
    ("sources", "search_terms"),
    ("sources", "searches"),
    ("location", "local_display_name"),
    ("location", "local_metro_patterns"),
    ("location", "local_metro_exclude_patterns"),
    ("location", "local_state_only_pattern"),
    ("location", "jd_local_metro_pattern"),
    ("compensation", "min_salary"),
    ("compensation", "pay_midpoint_max_cap"),
    ("experience", "max_min_yoe"),
    ("experience", "max_min_secondary_yoe"),
    ("experience", "secondary_yoe_context_pattern"),
    ("experience", "primary_yoe_context_pattern"),
    ("experience", "yoe_field_terms_pattern"),
    ("experience", "max_senior_role_years"),
    ("titles", "target_patterns"),
    ("titles", "strong_target_patterns"),
    ("titles", "primary_role_groups"),
    ("titles", "primary_role_patterns"),
    ("titles", "generic_role_patterns"),
    ("titles", "generic_role_required_skills"),
    ("titles", "generic_body_signal_patterns"),
    ("titles", "exclude_seniority_patterns"),
    ("titles", "exclude_tech_patterns"),
    ("titles", "exclude_role_patterns"),
    ("titles", "soft_exclude_role_patterns"),
    ("titles", "hn_role_pattern"),
    ("companies", "exclude_companies"),
    ("companies", "exclude_recruiters"),
    ("companies", "company_aliases"),
    ("skills_and_scoring", "detect_stack_keywords"),
    ("skills_and_scoring", "core_strengths"),
    ("skills_and_scoring", "core_strength_weights"),
    ("skills_and_scoring", "focus_keyword_groups"),
    ("skills_and_scoring", "focus_keywords"),
    ("skills_and_scoring", "hard_exclude_skills"),
    ("skills_and_scoring", "accepted_alternative_skills"),
    ("skills_and_scoring", "avoid_keywords"),
    ("skills_and_scoring", "penalized_keywords"),
    ("skills_and_scoring", "conditional_require_any"),
    ("skills_and_scoring", "hard_ask_penalties"),
    ("skills_and_scoring", "specialty_reject_rules"),
    ("skills_and_scoring", "off_center_role_check"),
    ("llm_review", "pricing"),
    ("llm_review", "rubric_descriptions"),
    ("llm_review", "calibration_examples"),
}

DEFAULTS = {
    "paths": {
        "filtered_jobs": "data/filtered_jobs.json",
        "enriched_jobs": "data/enriched_jobs.json",
        "evaluated_jobs": "data/evaluated_jobs.json",
        "source_stats": "data/source_stats.json",
        "blocklist": "data/blocklist.json",
        "role_blocklist": "data/blocklist_roles.json",
        "review_cache": "data/review_cache.json",
        "run_history": "data/run_history.json",
        "linkedin_details_cache": "data/linkedin_details_cache.json",
        "candidate_profile": "candidate_profile.md",
        "text_export": "data/job_matches.txt",
        "llm_spend": "data/llm_spend.json",
        "html_digest": "data/digest.html",
        "digest_store": "data/digest_matches.json",
    },
    "sources": {
        "lookback_days": 3,
        "feed_stale_hours": 36,
        "chunk_concurrency": 20,
        "github_feed_enabled": True,
        "github_feed_manifest_url": "https://riley-dorrington.github.io/job-board-data/data/chunks/jobs_manifest.json",
        "github_feed_base_chunk_url": "https://riley-dorrington.github.io/job-board-data/data/chunks/",
        "jobspy_enabled": True,
        "country_indeed": "USA",
        "linkedin_pause_seconds": 3,
        "search_terms": ["Software Engineer"],
        "searches": [["linkedin", "United States", " remote", 40], ["indeed", "Remote", "", 40]],
        "job_boards_enabled": True,
        "remoteok_enabled": True,
        "remoteok_url": "https://remoteok.com/api",
        "jobicy_enabled": True,
        "jobicy_url": "https://jobicy.com/api/v2/remote-jobs?count=100&geo=usa&industry=engineering",
        "himalayas_enabled": True,
        "himalayas_search_url": "https://himalayas.app/jobs/api/search",
        "himalayas_max_pages": 5,
        "wwr_enabled": True,
        "wwr_feeds": [
            "https://weworkremotely.com/categories/remote-front-end-programming-jobs.rss",
            "https://weworkremotely.com/categories/remote-full-stack-programming-jobs.rss",
        ],
        "hn_enabled": True,
        "hn_search_url": "https://hn.algolia.com/api/v1/search_by_date?tags=story,author_whoishiring&query=who%20is%20hiring&hitsPerPage=5",
        "hn_item_url": "https://hn.algolia.com/api/v1/items/{}",
        "hn_thread_max_age_days": 40,
    },
    "location": {
        "allow_remote": True,
        "local_display_name": "Local Metro",
        "local_metro_patterns": [],
        "local_metro_exclude_patterns": [],
        "local_state_only_pattern": "",
        "jd_local_metro_pattern": "",
        "home_states": [],
        "home_timezones": [],
        "allowed_country_pattern": "\\b(us|united states|usa)\\b",
        "country_token_pattern": "(?:US|USA|U\\.S\\.A?\\.?|United States|continental U\\.?S\\.?)",
        "global_eligible_pattern": "\\b(worldwide|anywhere|global|north america|northern america|americas)\\b",
        # Places outside the candidate's own country, as lower-case regex fragments matched as whole words; these
        # assume a US-based candidate. foreign_places (countries, regions, provinces) rule out a feed location or
        # title, JD residency wording ("remote within Canada") and a JD "Location:" line. foreign_cities only rule
        # out a JD "Location:" line, since a city name alone can be a home-country town (Dublin, OH; Paris, TX).
        "foreign_places": [
            "canada", "poland", "brazil", "costa rica", "france", "italy", "netherlands", "spain", "lithuania",
            "uk", "emea", "latam", "apac", "india", "germany", "ireland", "mexico", "australia", "montréal",
            "montreal", "europe", "latin america", "el salvador", "colombia", "argentina", "nz", "new zealand",
            "singapore", "united kingdom", "romania", "portugal", "ontario", "krakow", "british columbia", "bc",
            "london", "gbr", "scotland", "england", "türkiye", "burnaby", "quebec", "alberta", "eu", "philippines",
            "manitoba", "saskatchewan", "nova scotia", "newfoundland", "prince edward island", "sweden", "norway",
            "denmark", "finland", "switzerland", "austria", "belgium", "czechia", "czech republic", "estonia",
            "latvia", "hungary", "slovakia", "slovenia", "iceland", "luxembourg", "bulgaria", "ukraine", "serbia",
            "croatia", "greece", "vietnam", "nigeria", "israel", "japan", "south korea", "egypt", "pakistan",
            "south africa", "chile", "peru", "uruguay", "guatemala", "china", "korea", "turkey",
        ],
        "foreign_cities": [
            "bucharest", "hanoi", "sofia", "kyiv", "manila", "tel aviv", "noida", "bangalore", "bengaluru",
            "hyderabad", "pune", "chennai", "warsaw", "berlin", "paris", "amsterdam", "dublin", "toronto",
            "vancouver,? bc", "stockholm", "oslo", "copenhagen", "helsinki", "zurich", "brussels", "prague",
            "budapest", "winnipeg",
        ],
    },
    "compensation": {
        "min_salary": 0,
        "pay_midpoint_max_cap": 0,
        "pay_midpoint_penalty": 10,
        "pay_floor_taper_from": 0,
    },
    "experience": {
        "max_min_yoe": 99,
        "max_min_secondary_yoe": 99,
        "secondary_yoe_context_pattern": "",
        "primary_yoe_context_pattern": "",
        "yoe_field_terms_pattern": (
            "(?:front[- ]?end|full[- ]?stack|back[- "
            "]?end|software|web|professional|engineering|development|programming)"
        ),
        "max_senior_role_years": 99,
        "top_yoe_penalty": 10,
        "reject_clearance": True,
        "clearance_ignore_terms": [],
        "reject_foreign_language_req": True,
    },
    "titles": {
        "target_patterns": ["\\bengineer\\b", "\\bdeveloper\\b"],
        "strong_target_patterns": [],
        "primary_role_groups": [],
        "primary_role_patterns": [],
        "generic_role_patterns": [],
        "generic_role_required_skills": [],
        "generic_body_signal_patterns": [],
        "generic_body_signal_min": 2,
        "exclude_seniority_patterns": [
            "staff", "principal", "director", "manager", "managing", "vp", "head", "architect", "lead(?![- ]gen)",
        ],
        "exclude_tech_patterns": [],
        "exclude_role_patterns": [],
        "soft_exclude_role_patterns": [],
        "hn_role_pattern": (
            "\\b((?:senior |sr\\.? )?(?:frontend|front-end|front end|full[- ]?stack|product|software|web) "
            "(?:engineer|developer))\\b"
        ),
    },
    "companies": {
        "exclude_companies": [],
        "exclude_recruiters": [],
        "allow_recruiters": [],
        "company_aliases": {},
        "max_roles_per_run": 0,
    },
    "skills_and_scoring": {
        "base_score": 20,
        "core_strength_points": 5,
        "core_strengths_cap": 40,
        "domain_score_cap": 30,
        "strong_floor": 75,
        "secondary_floor": 55,
        "min_sync_score": 80,
        "min_review_score": 55,
        "detect_stack_keywords": [],
        "core_strengths": [],
        "core_strength_weights": {},
        "focus_keyword_groups": [],
        "focus_keywords": {},
        "hard_exclude_skills": [],
        "accepted_alternative_skills": [],
        "soft_skill_penalty": 5,
        "soft_skill_penalty_cap": 15,
        "avoid_keywords": [],
        "penalized_keywords": {},
        "conditional_require_any": {},
        "hard_ask_penalties": [],
        "specialty_reject_rules": [],
        "off_center_role_check": {"min_mentions": 6, "ratio": 1.5},
    },
    "scraper": {
        "concurrency": 15,
    },
    "url_normalization": {
        "aggregator_hosts": [
            "indeed.com", "linkedin.com", "news.ycombinator.com", "remoteok.com", "remotive.com", "jobicy.com",
            "himalayas.app", "weworkremotely.com", "glassdoor.com",
        ],
        "extra_tracking_params": [],
        "resolve_direct_hosts": ["glassdoor.com"],
    },
    "llm_review": {
        "provider": "auto",
        "model": "",
        "agy_path": "",
        "claude_path": "",
        "codex_path": "",
        "timeout_seconds": 180,
        "workers": 3,
        "jd_chars": 9000,
        "jd_head_chars": 2500,
        "review_version": "1",
        "repost_similarity": 0.8,
        "budget": {
            "max_reviews_per_run": 0,
            "max_tokens_per_run": 0,
            "max_tokens_per_day": 0,
            "max_usd_per_run": 0,
            "max_usd_per_day": 0,
        },
        "pricing": {},
        "components": {"hard_reqs": 4, "core_work": 3, "evidence": 3, "level": 2},
        "blocker_cap": 30,
        "level_mismatch_cap": 65,
        "core_work_caps": {"0": 30, "1": 65},
        "apply_min_score": None,
        "stretch_min_score": 50,
        "hide_third_party": False,
        "quote_match_ratio": 0.85,
        "rubric_descriptions": {
            "hard_reqs": (
                "4 = every hard requirement met; 3 = met, with only soft gaps; 2 = one bounded hard gap (one named "
                "tool/skill or years 1 over); 1 = several hard gaps; 0 = a clear blocker (years far over, a "
                "required specialty/credential the candidate lacks)."
            ),
            "core_work": (
                "3 = directly matches the candidate's primary target role and day-to-day responsibilities; 2 = "
                "balanced/adjacent role in the candidate's core domain; 1 = peripheral overlap; 0 = off-target "
                "specialty."
            ),
            "evidence": (
                "3 = the posting itself asks for work that mirrors a specific accomplishment in the candidate "
                "profile; 2 = strong skill and domain overlap; 1 = partial overlap; 0 = little overlap. Grade only "
                "what the posting says, not what the company is known for."
            ),
            "level": (
                "2 = matches the candidate's target seniority/level range; 1 = slight stretch in seniority; 0 = "
                "under-leveled (entry/junior when not targeted) or over-leveled (executive/principal/director when "
                "not targeted)."
            ),
        },
        "calibration_examples": [],
    },
    "notion": {
        "notion_version": "2022-06-28",
        "database_id": "",
        "summary_page_id": "",
        "summary_block_id": "",
    },
    "obsidian": {
        "enabled": False,
        "vault_path": "",
        "folder": "Job Prospects",
        "summary_note": "Job Search Run Summary",
        "tag": "job-prospect",
    },
    "text_export": {
        "enabled": False,
    },
    "html_digest": {
        "enabled": True,
        "title": "Job Matches",
        "keep_days": 30,
        "url": "",
    },
    "notifications": {
        "notify_url": "",
    },
}
