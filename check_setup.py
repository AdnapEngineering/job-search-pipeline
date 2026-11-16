#!/usr/bin/env python3
"""Check that the pipeline is set up: dependencies, config, profile, AI reviewer, Notion, outputs.

Run `python3 check_setup.py` (or `python3 run_pipeline.py --check`). It fetches no jobs, spends no AI
quota and writes nothing to Notion; the only network call is one read of the Notion database, when
Notion is configured. Exits 1 when something would make a run fail, 0 otherwise.
"""
import contextlib
import difflib
import importlib.util
import io
import os
import sys
from urllib.parse import urlparse

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# pip package -> import name, for every runtime dependency in requirements.txt
DEPENDENCIES = {"requests": "requests", "beautifulsoup4": "bs4", "aiohttp": "aiohttp", "pandas": "pandas",
                "python-jobspy": "jobspy"}

# Keys the pipeline still reads although the template no longer lists them
LEGACY_KEYS = {
    "": {"notify_url", "notion_token", "summary_block_id"},
    "notion": {"notion_token"},
    "titles": {"bad_role_patterns", "soft_bad_role_patterns"},
    "location": {"foreign_exclude_patterns", "jd_non_allowed_locations", "jd_foreign_places", "jd_foreign_terms",
                 "foreign_countries"},
}

# Career-specific settings that should not keep the template's placeholder value
PLACEHOLDER_KEYS = [
    ("sources", "search_terms"), ("sources", "searches"), ("location", "local_display_name"),
    ("location", "local_metro_patterns"), ("titles", "strong_target_patterns"), ("titles", "primary_role_groups"),
    ("skills_and_scoring", "core_strengths"), ("skills_and_scoring", "focus_keywords"),
]

# The cron wrapper loads these before running; read them here too so a direct run checks the same values
ENV_FILES = [os.path.expanduser("~/.config/notion/job-search.env"), os.path.join(SCRIPT_DIR, ".env")]

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


def read_env_file(path):
    """KEY=VALUE pairs from a shell-style env file (comments, `export` and quotes allowed)."""
    values = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.removeprefix("export ").partition("=")
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                values[key.strip()] = value
    except OSError:
        pass
    return values


def effective_env(environ=None, env_files=None):
    """The environment a run through run_job_search.sh would see: the process env, then the env files."""
    env = dict(os.environ if environ is None else environ)
    for path in ENV_FILES if env_files is None else env_files:
        env.update(read_env_file(path))
    return env


def config_key_issues(cfg, template):
    """Warnings for keys in `cfg` that the pipeline does not read (two levels deep), with a likely fix."""
    issues = []
    sections = [("", cfg, template)]
    sections += [(k, v, template.get(k)) for k, v in cfg.items() if isinstance(v, dict) and isinstance(template.get(k), dict)]
    for section, values, known in sections:
        allowed = set(known) | LEGACY_KEYS.get(section, set())
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
    if v < (3, 10):
        report.add(FAIL, "Python", f"{v.major}.{v.minor} found; 3.10 or newer is required")
    else:
        report.add(OK, "Python", f"{v.major}.{v.minor}.{v.micro}")


def check_dependencies(report):
    missing = [pkg for pkg, mod in DEPENDENCIES.items() if importlib.util.find_spec(mod) is None]
    if missing:
        report.add(FAIL, "Dependencies", f"missing {', '.join(missing)}; run `pip install -r requirements.txt` "
                                         "(inside .venv if you made one)")
        return False
    report.add(OK, "Dependencies", "all installed")
    return True


def check_config(report):
    """Load the config the way a run does; returns (pipeline_config module, cfg) or (None, None)."""
    try:
        import pipeline_config
    except SystemExit as e:  # a malformed config.json stops the run with this message
        report.add(FAIL, "Config", str(e.code))
        return None, None
    path, source = pipeline_config.CONFIG_PATH, pipeline_config.config_source()
    template = pipeline_config.load_json(pipeline_config.EXAMPLE_CONFIG_PATH, default={}) or {}
    cfg = pipeline_config.CFG
    if source != path:
        report.add(WARN, "Config", f"no {os.path.basename(path)} yet, so the template's placeholder search is used; "
                                   "run `cp config.starter.json config.json` and edit it")
        return pipeline_config, cfg
    if not os.path.exists(path):
        report.add(WARN, "Config", f"{path} (JOB_PIPELINE_CONFIG) does not exist; every setting uses its default")
        return pipeline_config, cfg
    report.add(OK, "Config", os.path.relpath(path, SCRIPT_DIR) if path.startswith(SCRIPT_DIR) else path)
    for issue in config_key_issues(cfg, template):
        report.add(WARN, "Config", issue)
    placeholders = placeholder_issues(cfg, template)
    if placeholders:
        report.add(WARN, "Config", f"still the template's placeholder value: {', '.join(placeholders)}")
    return pipeline_config, cfg


def check_profile(report, pipeline_config):
    path = pipeline_config.get_path("candidate_profile", "candidate_profile.md")
    example = os.path.join(SCRIPT_DIR, "candidate_profile.example.md")
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
    provider = llm.get("provider", "agy")
    import llm_review
    try:
        with contextlib.redirect_stderr(io.StringIO()):  # it prints its own 'not found' line
            executable = llm_review._provider_config(provider)
    except ValueError as e:
        report.add(FAIL, "AI review", str(e))
        return
    if not executable:
        report.add(WARN, "AI review", f"provider {provider!r} is set but its CLI was not found, so matches go out "
                                      "on keyword scores only; install and sign in to it, set its *_path in "
                                      "llm_review, or pick another provider")
    elif has_profile:
        report.add(OK, "AI review", f"{provider} via {executable} (model {llm.get('model', llm_review.DEFAULT_MODEL)})")
    else:
        report.add(WARN, "AI review", f"{provider} CLI found, but skipped until candidate_profile.md exists")


def check_notion(report, cfg, env):
    notion = cfg.get("notion") or {}
    db_id = env.get("NOTION_DATABASE_ID") or notion.get("database_id") or ""
    token = env.get("NOTION_TOKEN") or env.get("NOTION_API_KEY") or notion.get("notion_token") or cfg.get("notion_token")
    if not db_id and not token:
        report.add(OFF, "Notion", "off (no NOTION_TOKEN or database id)")
        return
    if not db_id or not token:
        missing = "NOTION_DATABASE_ID (or notion.database_id)" if not db_id else "NOTION_TOKEN"
        report.add(FAIL, "Notion", f"{missing} is missing; set it in .env, or run with --no-notion")
        return
    import requests

    import sync_notion_db as sync
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


def check_outputs(report, cfg, env, pipeline_config):
    data_dir = os.path.dirname(pipeline_config.get_path("filtered_jobs", "data/filtered_jobs.json"))
    probe = data_dir
    while not os.path.exists(probe):
        probe = os.path.dirname(probe)
    if os.access(probe, os.W_OK):
        report.add(OK, "Data folder", os.path.relpath(data_dir, SCRIPT_DIR) if data_dir.startswith(SCRIPT_DIR) else data_dir)
    else:
        report.add(FAIL, "Data folder", f"{data_dir} is not writable")
    outputs = []
    if (cfg.get("html_digest") or {}).get("enabled", True):
        outputs.append("HTML digest (" + pipeline_config.get_path("html_digest", "data/digest.html")
                       .removeprefix(SCRIPT_DIR + os.sep) + ")")
    if (cfg.get("text_export") or {}).get("enabled", False):
        outputs.append("text export")
    if outputs:
        report.add(OK, "Outputs", ", ".join(outputs) + " (plus Notion, when on)")
    notify = env.get("NOTIFY_URL") or (cfg.get("notifications") or {}).get("notify_url") or cfg.get("notify_url")
    if notify:
        report.add(OK, "Notifications", f"set ({urlparse(notify).netloc or 'custom URL'})")
    else:
        report.add(OFF, "Notifications", "off (no NOTIFY_URL); results are only in the outputs above")


def run_checks():
    os.chdir(SCRIPT_DIR)
    if SCRIPT_DIR not in sys.path:
        sys.path.insert(0, SCRIPT_DIR)
    print("Setup check")
    report = Report()
    check_python(report)
    if check_dependencies(report):
        env = effective_env()
        pipeline_config, cfg = check_config(report)
        if pipeline_config is not None:
            has_profile = check_profile(report, pipeline_config)
            check_ai_review(report, cfg, has_profile)
            check_notion(report, cfg, env)
            check_outputs(report, cfg, env, pipeline_config)
    errors, warnings = report.count(FAIL), report.count(WARN)
    print(f"\n{errors} problem(s), {warnings} warning(s). "
          + ("Fix the problems (✗) before running." if errors else "Ready to run: ./run_job_search.sh"))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(run_checks())
