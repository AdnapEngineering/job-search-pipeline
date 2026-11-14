"""Central configuration loader and shared file helpers for the job search pipeline.

All career-specific settings (titles, skills, scoring weights, compensation,
locations, company filters, search queries, and LLM rubric text) are loaded
from `config.json` so the pipeline can be adapted to any career without
editing Python source files.
"""
import contextlib
import json
import os
import re
import sys
import tempfile
from typing import Any

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.environ.get("JOB_PIPELINE_CONFIG") or os.path.join(SCRIPT_DIR, "config.json")


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


def load_config(path: str = CONFIG_PATH) -> dict:
    """The pipeline config; an empty dict (all defaults) when missing or malformed."""
    return load_json(path, default={}) or {}


CFG = load_config()


def get_path(key: str, default_rel: str) -> str:
    """Absolute path for `paths.<key>` in config.json (or `default_rel`); relative paths resolve from the script directory."""
    val = (CFG.get("paths") or {}).get(key) or default_rel
    return os.path.join(SCRIPT_DIR, os.path.expanduser(val))


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
