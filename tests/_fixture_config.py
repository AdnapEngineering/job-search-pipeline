"""Point the pipeline at the generic fixture config before any pipeline module is imported.

Every test module imports this first. The pipeline reads its config once, at import, from
JOB_PIPELINE_CONFIG, so the tests never depend on (or touch) the config.json and data/ next to the code:
all data paths are redirected into a throwaway directory.
"""
import atexit
import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "search_config.json")
_DATA_FILES = {
    "filtered_jobs": "filtered_jobs.json", "enriched_jobs": "enriched_jobs.json",
    "evaluated_jobs": "evaluated_jobs.json", "source_stats": "source_stats.json",
    "blocklist": "blocklist.json", "role_blocklist": "blocklist_roles.json",
    "review_cache": "review_cache.json", "reviewed_postings": "reviewed_postings.json",
    "run_history": "run_history.json",
    "linkedin_details_cache": "linkedin_details_cache.json", "candidate_profile": "candidate_profile.md",
    "text_export": "job_matches.txt", "llm_spend": "llm_spend.json",
    "html_digest": "digest.html", "digest_store": "digest_matches.json",
}

if "job_search_pipeline.config" in sys.modules and not os.environ.get("JOB_PIPELINE_TEST_DIR"):
    raise RuntimeError("tests/_fixture_config.py must be imported before any pipeline module")

if not os.environ.get("JOB_PIPELINE_TEST_DIR"):
    DATA_DIR = tempfile.mkdtemp(prefix="job-pipeline-tests-")
    atexit.register(shutil.rmtree, DATA_DIR, ignore_errors=True)
    with open(FIXTURE, encoding="utf-8") as f:
        config = json.load(f)
    config["paths"] = {key: os.path.join(DATA_DIR, name) for key, name in _DATA_FILES.items()}
    config_path = os.path.join(DATA_DIR, "config.json")
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f)
    os.environ["JOB_PIPELINE_CONFIG"] = config_path
    os.environ["JOB_PIPELINE_TEST_DIR"] = DATA_DIR
    for name in ("NOTION_TOKEN", "NOTION_API_KEY", "NOTION_DATABASE_ID", "NOTIFY_URL"):
        os.environ.pop(name, None)
else:
    DATA_DIR = os.environ["JOB_PIPELINE_TEST_DIR"]

if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
