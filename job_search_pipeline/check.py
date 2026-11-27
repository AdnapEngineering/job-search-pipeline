#!/usr/bin/env python3
"""Check that the pipeline is set up: dependencies, config, profile, AI reviewer, Notion, Obsidian, outputs.

Run `uv run python -m job_search_pipeline --check` (or `uv run python -m job_search_pipeline.check`). It fetches no jobs, spends no AI
quota and writes nothing to Notion; the only network call is one read of the Notion database, when
Notion is configured. Exits 1 when something would make a run fail, 0 otherwise.
"""
import difflib
import importlib.util
import os
import sys
from urllib.parse import urlparse

from job_search_pipeline.env import effective_env, read_env_file  # noqa: F401  (read_env_file: tests)

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# pip package -> import name, for every runtime dependency in pyproject.toml
DEPENDENCIES = {"requests": "requests", "beautifulsoup4": "bs4", "aiohttp": "aiohttp", "pandas": "pandas",
                "python-jobspy": "jobspy"}

# Keys the pipeline reads although the template leaves them out (a secret belongs in .env)
UNLISTED_KEYS = {"notion": {"notion_token"}}

# Career-specific settings that should not keep the template's placeholder value
PLACEHOLDER_KEYS = [
    ("sources", "search_terms"), ("sources", "searches"), ("location", "local_display_name"),
    ("location", "local_metro_patterns"), ("titles", "strong_target_patterns"), ("titles", "primary_role_groups"),
    ("skills_and_scoring", "core_strengths"), ("skills_and_scoring", "focus_keywords"),
]

OK, WARN, FAIL, OFF = "ok", "warn", "fail", "off"
_MARKS = {OK: "✓", WARN: "!", FAIL: "✗", OFF: "-"}


class Report:
    def __init__(self):
        self.results = []

    def add(self, status, area, message):
        self.results.append((status, area, message))
        print(f"  {_MARKS[status]} {area}: {message}")

    def count(self, status):
        return sum(1 for s, _, _ in self.results if s == status)


def config_key_issues(cfg, template):
    """Warnings for keys in `cfg` that the pipeline does not read (two levels deep), with a likely fix."""
    issues = []
    sections = [("", cfg, template)]
    sections += [(k, v, template.get(k)) for k, v in cfg.items() if isinstance(v, dict) and isinstance(template.get(k), dict)]
    for section, values, known in sections:
        allowed = set(known) | UNLISTED_KEYS.get(section, set())
        for key in values:
            if key in allowed:
                continue
            where = f"{section}.{key}" if section else key
            close = difflib.get_close_matches(key, allowed, n=1)
            hint = f" (did you mean {section + '.' if section else ''}{close[0]}?)" if close else ""
            issues.append(f"unknown key {where}{hint}; it is ignored")
    return issues


def placeholder_issues(cfg, template):
    """Career-specific settings still equal to the template's placeholder value."""
    return [f"{s}.{k}" for s, k in PLACEHOLDER_KEYS
            if (cfg.get(s) or {}).get(k) is not None and (cfg.get(s) or {}).get(k) == (template.get(s) or {}).get(k)]


def check_python(report):
    v = sys.version_info
    if v < (3, 13):
        report.add(FAIL, "Python", f"{v.major}.{v.minor} found; 3.13 or newer is required")
    else:
        report.add(OK, "Python", f"{v.major}.{v.minor}.{v.micro}")


def check_dependencies(report):
    missing = [pkg for pkg, mod in DEPENDENCIES.items() if importlib.util.find_spec(mod) is None]
    if missing:
        report.add(FAIL, "Dependencies", f"missing {', '.join(missing)}; run `uv sync` (or `pip install -e .` "
                                         "inside a virtualenv)")
        return False
    report.add(OK, "Dependencies", "all installed")
    return True


def check_config(report):
    """Load the config the way a run does; returns (config module, cfg) or (None, None)."""
    try:
        from job_search_pipeline import config
    except SystemExit as e:  # a malformed config.json stops the run with this message
        report.add(FAIL, "Config", str(e.code))
        return None, None
    path = config.CONFIG_PATH
    template = config.load_json(config.EXAMPLE_CONFIG_PATH, default={}) or {}
    cfg = config.CFG
    if not os.path.exists(path):
        report.add(FAIL, "Config", f"no {os.path.relpath(path, ROOT_DIR) if path.startswith(ROOT_DIR) else path} yet; "
                                   "run `uv run python -m job_search_pipeline.setup` to create one")
        return config, cfg
    report.add(OK, "Config", os.path.relpath(path, ROOT_DIR) if path.startswith(ROOT_DIR) else path)
    raw = config.load_json(path, default={}) or {}
    for issue in config_key_issues(raw, template):
        report.add(WARN, "Config", issue)
    placeholders = placeholder_issues(raw, template)
    if placeholders:
        report.add(WARN, "Config", f"still the template's placeholder value: {', '.join(placeholders)}")
    return config, cfg


def check_profile(report, config):
    path = config.get_path("candidate_profile")
    example = os.path.join(ROOT_DIR, "candidate_profile.example.md")
    if not os.path.exists(path):
        report.add(WARN, "Profile", f"{os.path.basename(path)} not found, so the AI review is skipped; "
                                    "run `cp candidate_profile.example.md candidate_profile.md` and edit it")
        return False
    with open(path, encoding="utf-8") as f, open(example, encoding="utf-8") as g:
        if f.read().strip() == g.read().strip():
            report.add(WARN, "Profile", f"{os.path.basename(path)} is still the unedited example")
            return True
    report.add(OK, "Profile", os.path.basename(path))
    return True


def check_ai_review(report, cfg, has_profile):
    llm = cfg.get("llm_review") or {}
    import job_search_pipeline.review as llm_review
    auto = (llm.get("provider") or "auto").strip().lower() == "auto"
    provider = llm_review.resolve_provider(llm)
    try:
        executable = llm_review._cli_path(provider)
    except ValueError as e:
        report.add(FAIL, "AI review", str(e))
        return
    if not executable:
        if auto:
            report.add(WARN, "AI review", "no supported CLI (agy, claude or codex) was found, so matches go out on "
                                          "keyword scores only; install and sign in to one, or set its *_path in "
                                          "llm_review")
        else:
            report.add(WARN, "AI review", f"provider {provider!r} is set but its CLI was not found, so matches go "
                                          "out on keyword scores only; install and sign in to it, set its *_path "
                                          "in llm_review, or pick another provider (or \"auto\")")
        return
    detail = f"{provider}{' (auto-detected)' if auto else ''} via {executable}"
    model = llm_review.resolve_model(llm, provider)
    detail += f" (model {model})" if model else " (the CLI's default model)"
    if has_profile:
        report.add(OK, "AI review", detail)
    else:
        report.add(WARN, "AI review", f"{detail} found, but skipped until candidate_profile.md exists")


def check_notion(report, cfg, env):
    notion = cfg.get("notion") or {}
    db_id = env.get("NOTION_DATABASE_ID") or notion.get("database_id") or ""
    token = env.get("NOTION_TOKEN") or env.get("NOTION_API_KEY") or notion.get("notion_token")
    if not db_id and not token:
        report.add(OFF, "Notion", "off (no NOTION_TOKEN or database id)")
        return
    if not db_id or not token:
        missing = "NOTION_DATABASE_ID (or notion.database_id)" if not db_id else "NOTION_TOKEN"
        report.add(FAIL, "Notion", f"{missing} is missing; set it in .env, or run with --no-notion")
        return
    import requests

    import job_search_pipeline.outputs.notion as sync
    headers = {"Authorization": token if token.startswith("Bearer ") else f"Bearer {token}",
               "Notion-Version": notion.get("notion_version", sync.NOTION_VERSION)}
    try:
        res = requests.get(f"{sync.NOTION_API}/databases/{db_id}", headers=headers, timeout=sync.REQUEST_TIMEOUT)
    except requests.RequestException as e:
        report.add(FAIL, "Notion", f"could not reach the Notion API: {e}")
        return
    if res.status_code == 401:
        report.add(FAIL, "Notion", "the token was rejected (401); copy it again from notion.so/my-integrations")
        return
    if res.status_code == 404:
        report.add(FAIL, "Notion", "database not found (404); check the id and share the database with your "
                                   "integration (database ··· menu > Connections)")
        return
    if res.status_code != 200:
        report.add(FAIL, "Notion", f"HTTP {res.status_code} reading the database: {res.text[:200]}")
        return
    data = res.json()
    title = "".join(t.get("plain_text", "") for t in data.get("title") or []) or db_id
    props = {name: p.get("type") for name, p in (data.get("properties") or {}).items()}
    problems = []
    for name, kind in {**sync.NOTION_PROPERTIES, **sync.OPTIONAL_NOTION_PROPERTIES}.items():
        if name not in props:
            if name in sync.NOTION_PROPERTIES:
                problems.append(f"missing property {name!r} ({kind})")
        elif props[name] != kind:
            problems.append(f"property {name!r} is {props[name]}, needs {kind}")
    if problems:
        report.add(FAIL, "Notion", f"database {title!r}: " + "; ".join(problems))
    else:
        report.add(OK, "Notion", f"database {title!r} reachable with all required properties")


def check_obsidian(report, cfg, env):
    obsidian = cfg.get("obsidian") or {}
    if not obsidian.get("enabled", False):
        report.add(OFF, "Obsidian", "off (obsidian.enabled is false)")
        return
    vault = os.path.expanduser(env.get("OBSIDIAN_VAULT") or obsidian.get("vault_path") or "")
    if not vault:
        report.add(FAIL, "Obsidian", "enabled, but obsidian.vault_path (or OBSIDIAN_VAULT) is empty; set it to your "
                                     "vault folder, or run with --no-obsidian")
        return
    if not os.path.isdir(vault):
        report.add(FAIL, "Obsidian", f"vault folder {vault} does not exist")
        return
    folder = os.path.join(vault, obsidian.get("folder") or "Job Prospects")
    probe = folder if os.path.isdir(folder) else vault
    if not os.access(probe, os.W_OK):
        report.add(FAIL, "Obsidian", f"{probe} is not writable")
        return
    if not os.path.isdir(os.path.join(vault, ".obsidian")):
        report.add(WARN, "Obsidian", f"{vault} has no .obsidian folder, so it may not be a vault (open it in "
                                     "Obsidian once); notes will still be written there")
        return
    report.add(OK, "Obsidian", f"notes go to {folder}")


def check_outputs(report, cfg, env, config):
    data_dir = os.path.dirname(config.get_path("filtered_jobs"))
    probe = data_dir
    while not os.path.exists(probe):
        probe = os.path.dirname(probe)
    if os.access(probe, os.W_OK):
        report.add(OK, "Data folder", os.path.relpath(data_dir, ROOT_DIR) if data_dir.startswith(ROOT_DIR) else data_dir)
    else:
        report.add(FAIL, "Data folder", f"{data_dir} is not writable")
    outputs = []
    if cfg["html_digest"]["enabled"]:
        outputs.append("HTML digest (" + config.get_path("html_digest")
                       .removeprefix(ROOT_DIR + os.sep) + ")")
    if cfg["text_export"]["enabled"]:
        outputs.append("text export")
    if outputs:
        report.add(OK, "Outputs", ", ".join(outputs) + " (plus Notion and Obsidian, when on)")
    notify = env.get("NOTIFY_URL") or cfg["notifications"]["notify_url"]
    if notify:
        report.add(OK, "Notifications", f"set ({urlparse(notify).netloc or 'custom URL'})")
    else:
        report.add(OFF, "Notifications", "off (no NOTIFY_URL); results are only in the outputs above")


def run_checks():
    print("Setup check")
    report = Report()
    check_python(report)
    if check_dependencies(report):
        env = effective_env()
        config, cfg = check_config(report)
        if config is not None:
            has_profile = check_profile(report, config)
            check_ai_review(report, cfg, has_profile)
            check_notion(report, cfg, env)
            check_obsidian(report, cfg, env)
            check_outputs(report, cfg, env, config)
    errors, warnings = report.count(FAIL), report.count(WARN)
    print(f"\n{errors} problem(s), {warnings} warning(s). "
          + ("Fix the problems (✗) before running." if errors else "Ready to run: ./run_job_search.sh"))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(run_checks())
