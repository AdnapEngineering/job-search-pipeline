"""Central configuration loader and shared file helpers for the job search pipeline.

All career-specific settings (titles, skills, scoring weights, compensation,
locations, company filters, search queries, and LLM rubric text) are loaded
from `config.json` so the pipeline can be adapted to any career without
editing Python source files.

`config.json` is your own, untracked file (draft it with `--init-config`, or copy `config.example.json`).
It is laid over the built-in defaults in `defaults.py`, so it only needs the settings you change.
"""
import contextlib
import copy
import json
import os
import re
import sys
import tempfile
from typing import Any

from job_search_pipeline.defaults import DEFAULTS

# The repository root, one level above this package: config.json, candidate_profile.md, .env and data/ live there
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CONFIG_PATH = os.path.join(ROOT_DIR, "config.json")
EXAMPLE_CONFIG_PATH = os.path.join(ROOT_DIR, "config.example.json")
CONFIG_PATH = os.environ.get("JOB_PIPELINE_CONFIG") or DEFAULT_CONFIG_PATH


def load_json(path: str, default: Any = None) -> Any:
    """Parsed JSON from `path`, or `default` when the file is missing or unreadable."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as e:
        print(f"Could not read {path}: {e}", file=sys.stderr)
        return default


def atomic_write_json(path: str, data: Any, indent: int = 2) -> None:
    """Write JSON via a temp file in the same directory, then rename, so readers never see a partial file."""
    dir_name = os.path.dirname(path) or "."
    os.makedirs(dir_name, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=indent)
        os.replace(tmp_path, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise


def merge_defaults(cfg: dict) -> dict:
    """`cfg` laid over DEFAULTS: a section's keys override the default ones; anything else replaces whole."""
    merged = copy.deepcopy(DEFAULTS)
    for key, value in cfg.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key].update(copy.deepcopy(value))
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def load_config(path: str = CONFIG_PATH) -> dict:
    """The pipeline config laid over DEFAULTS; just the defaults when the file is missing.

    A file that exists but cannot be read or parsed stops the run (SystemExit) instead of silently
    running the built-in defaults in place of the user's search."""
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    except FileNotFoundError:
        return merge_defaults({})
    except json.JSONDecodeError as e:
        sys.exit(f"Invalid JSON in {path} at line {e.lineno}, column {e.colno}: {e.msg}. "
                 f"Fix the file and run again.")
    except (OSError, UnicodeDecodeError) as e:
        sys.exit(f"Could not read {path}: {e}")
    if not isinstance(cfg, dict):
        sys.exit(f"{path} must contain a JSON object ({{...}}), not {type(cfg).__name__}.")
    for section, value in cfg.items():
        if isinstance(DEFAULTS.get(section), dict) and not isinstance(value, dict):
            sys.exit(f"{path}: \"{section}\" must be a JSON object ({{...}}), not {type(value).__name__}.")
    return merge_defaults(cfg)


CFG = load_config()


def get_path(key: str) -> str:
    """Absolute path for `paths.<key>`; relative paths resolve from the repository root."""
    return os.path.join(ROOT_DIR, os.path.expanduser(CFG["paths"][key] or DEFAULTS["paths"][key]))


def term_regex(term: str, allow_suffix: bool = False) -> re.Pattern:
    """Whole-term match that also works for terms with symbols (c++, c#, .net, next.js), so 'java'
    does not match inside 'javascript'. With allow_suffix the term may continue (design system -> systems)."""
    tail = '' if allow_suffix else r'(?![\w+#])'
    return re.compile(r'(?<![\w.+#])' + re.escape(term) + tail)


def compile_regex_list(patterns: list[str] | None, flags: int = 0) -> list[re.Pattern]:
    return [re.compile(p, flags) for p in (patterns or []) if p]


def compile_alternation(patterns: list[str] | None, word_bounded: bool = True, flags: int = 0) -> re.Pattern | None:
    """One regex matching any of `patterns` (optionally wrapped in word boundaries), or None when empty."""
    cleaned = [p for p in (patterns or []) if p]
    if not cleaned:
        return None
    joined = "|".join(cleaned)
    expr = rf"\b({joined})\b" if word_bounded else f"({joined})"
    return re.compile(expr, flags)
