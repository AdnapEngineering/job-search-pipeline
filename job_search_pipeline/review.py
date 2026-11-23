"""Second-stage review of keyword-scored matches: asks an LLM via a CLI
to read each JD against `candidate_profile.md` and return apply / stretch / skip
with a structured rubric and reason.

Fail-open by design: if the provider is unavailable, times out, or returns something
unparseable, the job keeps its keyword verdict and the pipeline carries on.
"""
import difflib
import hashlib
import html as html_lib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from bs4 import BeautifulSoup

from job_search_pipeline.blocklist import role_key
from job_search_pipeline.config import CFG, atomic_write_json, get_path, load_json
from job_search_pipeline.defaults import DEFAULTS
from job_search_pipeline.urls import normalize_url

_LLM_CFG = CFG["llm_review"]
_COMP_CFG = CFG["compensation"]
_BUDGET_CFG = _LLM_CFG.get("budget") or {}
_PRICING_CFG = _LLM_CFG.get("pricing") or {}

PROFILE_PATH = get_path("candidate_profile")
CACHE_PATH = get_path("review_cache")
SPEND_PATH = get_path("llm_spend")
SPEND_KEEP_DAYS = 90
# A cached review is reused for a repost (new URL, same company and title) when the JD texts are this similar
REPOST_SIMILARITY = float(_LLM_CFG["repost_similarity"])
SIGNATURE_SIZE = 64
SHINGLE_WORDS = 5
# Output tokens assumed for a review before any has finished, when checking it against a cap
EST_OUTPUT_TOKENS = 600
CHARS_PER_TOKEN = 4

DEFAULT_AGY = "~/.local/bin/agy"
# Tried in this order when llm_review.provider is unset or "auto": agy first so existing review caches stay
# valid, then Claude Code, the most widely installed of the supported CLIs.
PROVIDERS = ("agy", "anthropic", "openai")
CLI_NAMES = {"agy": "agy", "anthropic": "claude", "openai": "codex"}
# Model used when llm_review.model is unset; "" lets Codex use the model from its own config
PROVIDER_MODELS = {"agy": "gemini-4-hydrogen-medium", "anthropic": "sonnet", "openai": ""}
TIMEOUT_SECONDS = int(_LLM_CFG["timeout_seconds"])
WORKERS = int(_LLM_CFG["workers"])
JD_CHARS = int(_LLM_CFG["jd_chars"])
JD_HEAD_CHARS = int(_LLM_CFG["jd_head_chars"])
VERDICTS = ("apply", "stretch", "skip")
THIRD_PARTY_LABEL = "[Third-party poster]"
REVIEW_FAILED_LABEL = "[AI review failed, held for retry]"
REVIEW_DEFERRED_LABEL = "[AI review deferred: budget cap reached, held for next run]"
REVIEW_VERSION = str(_LLM_CFG["review_version"])

COMPONENTS = _LLM_CFG["components"] or DEFAULTS["llm_review"]["components"]
MAX_TOTAL = sum(COMPONENTS.values())
BLOCKER_CAP = int(_LLM_CFG["blocker_cap"])
# Highest Match % for a role graded level 0 (under- or over-leveled for the candidate); 100 turns it off
LEVEL_MISMATCH_CAP = int(_LLM_CFG["level_mismatch_cap"])
# Highest Match % per core_work grade ({"0": 30, "1": 65}), so off-center work cannot reach APPLY on
# requirement and domain overlap alone; grades not listed are uncapped
CORE_WORK_CAPS = {int(k): int(v) for k, v in (_LLM_CFG.get("core_work_caps") or {}).items()}
# "apply" means "will be synced": unless set explicitly, the verdict line follows the sync threshold
APPLY_MIN = int(_LLM_CFG.get("apply_min_score") or CFG["skills_and_scoring"]["min_sync_score"])
STRETCH_MIN = int(_LLM_CFG["stretch_min_score"])
# With hide_third_party, postings the reviewer flags as agency/recruiter/aggregator are dropped like a skip verdict
HIDE_THIRD_PARTY = bool(_LLM_CFG["hide_third_party"])

MIN_SALARY = float(_COMP_CFG["min_salary"])
PAY_MIDPOINT_MAX_CAP = float(_COMP_CFG["pay_midpoint_max_cap"])
PAY_MIDPOINT_PENALTY = int(_COMP_CFG["pay_midpoint_penalty"])
# With pay_floor_taper_from set (below min_salary), a band topping out between the two is capped on a
# straight line from BLOCKER_CAP (at taper_from) to 100 (at min_salary) instead of dropping to the cap at once
PAY_FLOOR_TAPER_FROM = float(_COMP_CFG["pay_floor_taper_from"])
QUOTE_MATCH_RATIO = float(_LLM_CFG["quote_match_ratio"])
HOURS_PER_YEAR = 2080

# A partial rubric_descriptions keeps the built-in text for the components it leaves out
_RUBRIC = {**DEFAULTS["llm_review"]["rubric_descriptions"], **(_LLM_CFG["rubric_descriptions"] or {})}
_HARD_REQS_DESC = _RUBRIC["hard_reqs"]
_CORE_WORK_DESC = _RUBRIC["core_work"]
_EVIDENCE_DESC = _RUBRIC["evidence"]
_LEVEL_DESC = _RUBRIC["level"]
_CALIBRATION_LIST = _LLM_CFG.get("calibration_examples") or []
_CALIBRATION_BLOCK = (
    "\nCalibration (final Match % in brackets):\n" + "\n".join(f"- {c}" for c in _CALIBRATION_LIST) + "\n"
    if _CALIBRATION_LIST else ""
)

PROMPT = (
    "You are screening one job posting for a candidate. Decide from the text below only. Do not use tools, "
    "browse, or read files.\n\n"
    "CANDIDATE PROFILE\n{profile}\n\n"
    "JOB\n"
    "Company: {company}\n"
    "Title: {title}\n"
    "Location: {location}\n"
    "Posted salary: {salary}\n"
    "Posting text:\n{jd}\n\n"
    "Grade the posting against the profile. Read the responsibilities, not just the title. Treat \"preferred\", "
    "\"bonus\", \"nice to have\" and \"familiarity\" items as soft; only \"required\", \"must\", and minimum-years items are hard.\n\n"
    f"hard_reqs (0-4): {_HARD_REQS_DESC}\n"
    f"core_work (0-3): {_CORE_WORK_DESC}\n"
    f"evidence (0-3): {_EVIDENCE_DESC}\n"
    "evidence_quote: one sentence copied word for word from the posting that best supports the evidence grade. "
    "An evidence 3 without a verbatim quote is graded 2.\n"
    f"level (0-2): {_LEVEL_DESC}\n"
    "pay_min, pay_max: the lowest and highest annual base salary figures in the posting, in dollars "
    "(null when the posting gives none; hourly rates times 2080). Do not count pay in hard_reqs or level: "
    "pay is scored separately.\n"
    "logistics_ok: false if the location, hybrid/onsite requirement, or work authorization rules the candidate out.\n"
    "third_party: true if the posting comes from a staffing agency, recruiter, consultancy placing people with "
    "its clients, or a job aggregator rather than from the employer itself. This does not change any grade.\n"
    f"{_CALIBRATION_BLOCK}\n"
    "Reply with one JSON object and nothing else, in this shape:\n"
    "{{\"hard_reqs\": 0, \"core_work\": 0, \"evidence\": 0, \"evidence_quote\": \"verbatim sentence from the posting\", "
    "\"level\": 0, \"logistics_ok\": true, \"third_party\": false, \"pay_min\": null, \"pay_max\": null, \"verdict\": \"apply|stretch|skip\", "
    "\"reason\": \"one plain sentence, under 160 characters, naming the deciding factor\", "
    "\"blockers\": [\"short phrases for each hard gap, empty if none\"]}}"
)


def _profile_text():
    with open(PROFILE_PATH, encoding="utf-8") as f:
        return f.read()


def _profile_hash(profile, model, provider="agy", triage_model=""):
    cache_input = profile + "|" + model + "|" + PROMPT + "|" + REVIEW_VERSION
    if provider != "agy":
        cache_input += "|cli|" + provider
    if triage_model:
        cache_input += "|triage|" + triage_model
    return hashlib.sha256(cache_input.encode()).hexdigest()[:16]


def _cli_path(provider):
    """The provider's CLI executable, or None if absent. Raises ValueError for an unknown provider."""
    if provider == "agy":
        return _find_agy()
    if provider in ("anthropic", "openai"):
        name = CLI_NAMES[provider]
        return shutil.which(os.path.expanduser(_LLM_CFG.get(f"{name}_path") or name))
    raise ValueError(f"Unknown llm_review.provider {provider!r}; choose auto, agy, anthropic, or openai.")


def resolve_provider(llm_cfg):
    """The configured provider, or for an unset/"auto" one the first installed CLI (Claude Code when none is)."""
    provider = (llm_cfg.get("provider") or "auto").strip().lower()
    if provider != "auto":
        return provider
    return next((p for p in PROVIDERS if _cli_path(p)), "anthropic")


def resolve_model(llm_cfg, provider):
    return llm_cfg.get("model") or PROVIDER_MODELS.get(provider, "")


def _find_agy():
    """Path to the agy CLI (config, then PATH, then the default install location), or None if absent."""
    path = os.path.expanduser(_LLM_CFG.get("agy_path") or shutil.which("agy") or DEFAULT_AGY)
    return path if os.path.exists(path) else None


PROVIDER = resolve_provider(_LLM_CFG)
DEFAULT_MODEL = resolve_model(_LLM_CFG, PROVIDER)
TRIAGE_MODEL = (_LLM_CFG.get("triage_model") or "").strip()

TRIAGE_PROMPT = (
    "You are running a fast Pass-1 triage on a job posting for a candidate. Decide from the text below only. "
    "Do not use tools, browse, or read files.\n\n"
    "CANDIDATE PROFILE\n{profile}\n\n"
    "JOB\n"
    "Company: {company}\n"
    "Title: {title}\n"
    "Location: {location}\n"
    "Posted salary: {salary}\n"
    "Posting text:\n{jd}\n\n"
    "Check only hard blockers:\n"
    f"hard_reqs (0-4): {_HARD_REQS_DESC}\n"
    f"level (0-2): {_LEVEL_DESC}\n"
    "logistics_ok: false if the location, hybrid/onsite requirement, or work authorization rules the candidate out.\n"
    "third_party: true if the posting comes from a staffing agency, recruiter, consultancy placing people with "
    "its clients, or a job aggregator rather than from the employer itself.\n"
    "pay_min, pay_max: the lowest and highest annual base salary figures in the posting, in dollars "
    "(null when the posting gives none; hourly rates times 2080).\n\n"
    "Reply with one JSON object and nothing else, in this shape:\n"
    "{{\"hard_reqs\": 0, \"level\": 0, \"logistics_ok\": true, \"third_party\": false, \"pay_min\": null, \"pay_max\": null, "
    "\"reason\": \"one plain sentence, under 160 characters, naming the deciding blocker if any\", "
    "\"blockers\": [\"short phrases for each hard gap, empty if none\"]}}"
)


def _norm_quote(text):
    return " ".join(re.findall(r"[a-z0-9]+", (text or "").lower()))


def quote_in_jd(quote, jd):
    """True when the quote appears in the JD, allowing small edits."""
    q, j = _norm_quote(quote), _norm_quote(_strip_html(jd))
    if len(q) < 20:
        return False
    if q in j:
        return True
    m = difflib.SequenceMatcher(None, q, j, autojunk=False).find_longest_match(0, len(q), 0, len(j))
    return m.size >= QUOTE_MATCH_RATIO * len(q)


def _pay_number(v):
    try:
        n = float(v)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    return n * HOURS_PER_YEAR if n < 1000 else n


def _pay_band(obj, salary):
    """(min, max) annual base pay: the feed's structured salary when present, else what the model read."""
    if isinstance(salary, dict) and _pay_number(salary.get("max")):
        top = _pay_number(salary.get("max"))
        return _pay_number(salary.get("min")) or top, top
    top = _pay_number(obj.get("pay_max"))
    if not top:
        return None
    return _pay_number(obj.get("pay_min")) or top, top


def _below_pay_midpoint(pay):
    """Band midpoint under the salary floor, unless the top of the band reaches pay_midpoint_max_cap."""
    if MIN_SALARY <= 0 or not pay:
        return False
    return (pay[0] + pay[1]) / 2 < MIN_SALARY and (PAY_MIDPOINT_MAX_CAP <= 0 or pay[1] < PAY_MIDPOINT_MAX_CAP)


def _below_pay_floor(pay):
    return MIN_SALARY > 0 and bool(pay) and pay[1] < MIN_SALARY


def pay_floor_cap(pay):
    """Highest Match % a band topping out below min_salary may keep (100 when the floor does not apply)."""
    if not _below_pay_floor(pay):
        return 100
    if not 0 < PAY_FLOOR_TAPER_FROM < MIN_SALARY or pay[1] <= PAY_FLOOR_TAPER_FROM:
        return BLOCKER_CAP
    share = (pay[1] - PAY_FLOOR_TAPER_FROM) / (MIN_SALARY - PAY_FLOOR_TAPER_FROM)
    return round(BLOCKER_CAP + (100 - BLOCKER_CAP) * share)


def _parse_verdict(text, jd="", salary=None):
    """Pull the JSON object out of the model's reply, tolerating code fences and stray prose."""
    text = re.sub(r"```(?:json)?", "", text or "")
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        obj = json.loads(text[start:end + 1])
    except ValueError:
        return None
    parts = {}
    for name, top in COMPONENTS.items():
        try:
            parts[name] = max(0, min(top, int(obj.get(name))))
        except (TypeError, ValueError):
            return None
    quote = str(obj.get("evidence_quote") or "").strip()
    quote_ok = quote_in_jd(quote, jd)
    if parts["evidence"] == COMPONENTS["evidence"] and not quote_ok:
        parts["evidence"] -= 1
    logistics_ok = obj.get("logistics_ok") is not False
    pay = _pay_band(obj, salary)
    score, verdict = score_components(parts, logistics_ok, pay)
    model_verdict = str(obj.get("verdict", "")).strip().lower()
    blockers = obj.get("blockers") or []
    blockers = [str(b)[:60] for b in blockers][:6] if isinstance(blockers, list) else []
    if _below_pay_floor(pay):
        blockers.append(f"pay tops out at ${pay[1] / 1000:.0f}k")
    elif _below_pay_midpoint(pay):
        blockers.append(f"pay midpoint ${(pay[0] + pay[1]) / 2000:.0f}k")
    return {
        "verdict": verdict,
        "score": score,
        "components": dict(parts, logistics_ok=logistics_ok),
        "evidence_quote": quote[:300] if quote_ok else "",
        "third_party": obj.get("third_party") is True,
        "pay": list(pay) if pay else None,
        "model_verdict": model_verdict if model_verdict in VERDICTS else "",
        "reason": str(obj.get("reason", "")).strip()[:200],
        "blockers": blockers,
    }


def score_components(parts, logistics_ok=True, pay=None):
    """Match % and verdict from the graded rubric. The verdict follows the score, not the model's own label."""
    score = round(100 * sum(parts.values()) / MAX_TOTAL)
    if _below_pay_midpoint(pay):
        score -= PAY_MIDPOINT_PENALTY
    if parts["hard_reqs"] == 0 or not logistics_ok:
        score = min(score, BLOCKER_CAP)
    if parts.get("level") == 0:
        score = min(score, LEVEL_MISMATCH_CAP)
    if parts.get("core_work") in CORE_WORK_CAPS:
        score = min(score, CORE_WORK_CAPS[parts["core_work"]])
    score = min(score, pay_floor_cap(pay))
    verdict = "apply" if score >= APPLY_MIN else "stretch" if score >= STRETCH_MIN else "skip"
    return score, verdict


def _strip_html(text):
    if "<" not in text and "&lt;" not in text:
        return text
    text = html_lib.unescape(text)
    try:
        return BeautifulSoup(text, "html.parser").get_text("\n")
    except Exception:
        return re.sub(r"<[^>]+>", " ", text)


def jd_for_review(full, requirements=""):
    """The JD opening (what the role is) plus the requirements section, HTML stripped, within JD_CHARS."""
    full = re.sub(r"\n\s*\n+", "\n\n", _strip_html(full or "")).strip()
    requirements = _strip_html(requirements or "").strip()
    if len(full) <= JD_CHARS:
        return full
    if not requirements or requirements[:200] in full[:JD_HEAD_CHARS]:
        return full[:JD_CHARS]
    return full[:JD_HEAD_CHARS] + "\n\n[...]\n\n" + requirements[:JD_CHARS - JD_HEAD_CHARS - 10]


def _salary_text(salary):
    if not isinstance(salary, dict) or not salary.get("max"):
        return "not given in the feed; read it from the posting text"
    return f"${int(salary.get('min') or salary['max']):,} - ${int(salary['max']):,}"


def _provider_config(provider):
    """Resolve the configured provider's CLI executable, printing a warning when it is missing."""
    executable = _cli_path(provider)
    if not executable:
        print(f"[AI review] {CLI_NAMES[provider]} CLI not found; skipping AI review.", file=sys.stderr)
    return executable


def skip_message(provider):
    """Why the AI review did not run, for the notification and digest, or None when it can run."""
    if not _cli_path(provider):
        return (f"AI review skipped: the {CLI_NAMES[provider]} CLI for provider {provider!r} was not found, so "
                "matches are ranked on keyword scores only. Install it, set its *_path in llm_review, or change "
                "llm_review.provider; `python3 -m job_search_pipeline --check` shows what is missing.")
    if not os.path.exists(PROFILE_PATH):
        return (f"AI review skipped: {os.path.basename(PROFILE_PATH)} not found, so matches are ranked on keyword "
                "scores only. Copy candidate_profile.example.md to that path and fill it in.")
    return None


def _int(v):
    try:
        return max(0, int(v))
    except (TypeError, ValueError):
        return 0


def _usage(provider, source):
    """Token counts (and the CLI's own cost, when it reports one) from a reply envelope or Codex event:
    {"input": uncached input, "cached": cached input, "output": output incl. thinking, "usd": float|None}."""
    u = source.get("usage") if isinstance(source, dict) else None
    if not isinstance(u, dict):
        return None
    if provider == "anthropic":
        uncached = _int(u.get("input_tokens")) + _int(u.get("cache_creation_input_tokens"))
        cached = _int(u.get("cache_read_input_tokens"))
        output = _int(u.get("output_tokens"))
        cost = source.get("total_cost_usd")
        usd = float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None
    else:  # agy and Codex count cached tokens inside input_tokens
        cached = _int(u.get("cache_read_tokens") if provider == "agy" else u.get("cached_input_tokens"))
        uncached = max(0, _int(u.get("input_tokens")) - cached)
        output = _int(u.get("output_tokens")) + _int(u.get("thinking_tokens") or u.get("reasoning_output_tokens"))
        usd = None
    return {"input": uncached, "cached": cached, "output": output, "usd": usd}


def _rates(provider=None):
    """The pricing block for `provider`: llm_review.pricing.<provider> when present, else flat rates in
    llm_review.pricing itself (applied to whichever provider is configured)."""
    provider = provider or PROVIDER
    nested = _PRICING_CFG.get(provider)
    return nested if isinstance(nested, dict) else _PRICING_CFG


def priced_usd(usage, provider=None):
    """Cost of `usage` at the configured rates (USD per million tokens), or None when unpriced."""
    rates = _rates(provider)
    rate_in = rates.get("input_per_mtok")
    rate_out = rates.get("output_per_mtok")
    if rate_in is None or rate_out is None:
        return None
    rate_cached = rates.get("cached_input_per_mtok", rate_in)
    return (usage["input"] * float(rate_in) + usage["cached"] * float(rate_cached)
            + usage["output"] * float(rate_out)) / 1_000_000


def _cli_response(executable, provider, model, prompt, timeout=None):
    """Run a CLI in an empty directory; returns (final response text, usage dict or None)."""
    if provider == "agy":
        command = [executable, "-p", prompt, "--model", model, "--output-format", "json"]
        stdin = None
    elif provider == "anthropic":
        command = [executable, "-p", "--model", model, "--output-format", "json",
                   "--tools", "", "--strict-mcp-config", "--disable-slash-commands",
                   "--no-session-persistence", "--settings", '{"disableAllHooks":true}']
        stdin = prompt
    elif provider == "openai":
        command = [executable, "exec", *(["--model", model] if model else []), "--json", "--ephemeral",
                   "--sandbox", "read-only", "--skip-git-repo-check", "-"]
        stdin = prompt
    else:
        raise ValueError("unsupported CLI provider")
    with tempfile.TemporaryDirectory() as cwd:
        result = subprocess.run(command, input=stdin, capture_output=True, text=True,
                                timeout=timeout or TIMEOUT_SECONDS, cwd=cwd)
    if result.returncode != 0:
        raise RuntimeError(f"CLI exited with code {result.returncode}")
    if provider == "openai":
        events = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
        if not all(isinstance(event, dict) for event in events):
            raise ValueError("invalid Codex event")
        if any(event.get("type") in ("error", "turn.failed") for event in events):
            raise RuntimeError("Codex turn failed")
        if not events or events[-1].get("type") != "turn.completed":
            raise ValueError("Codex turn did not complete")
        messages = [event["item"].get("text") for event in events
                    if event.get("type") == "item.completed" and isinstance(event.get("item"), dict)
                    and event["item"].get("type") == "agent_message"]
        response = messages[-1] if messages else None
        usage = _usage(provider, events[-1])
    else:
        envelope = json.loads(result.stdout)
        if not isinstance(envelope, dict):
            raise ValueError("invalid CLI response")
        usage = _usage(provider, envelope)
        if provider == "agy":
            if envelope.get("status") != "SUCCESS":
                raise RuntimeError("agy reported failure")
            response = envelope.get("response")
        else:
            if envelope.get("type") != "result" or envelope.get("is_error") is not False:
                raise RuntimeError("Claude reported failure")
            response = envelope.get("result")
    if not isinstance(response, str):
        raise ValueError("CLI returned no text")
    return response, usage


def _parse_triage(text, salary=None):
    """Parse Pass-1 triage JSON; returns (blocked_verdict_dict_or_None, parsed_ok_bool).
    When the posting has a hard blocker (hard_reqs == 0, logistics_ok is False, pay below floor, or hidden
    third-party), returns a full skip verdict dict so Pass 2 is skipped. When the posting passes Pass 1,
    returns (None, True) so Pass 2 grades the full 12-point rubric."""
    text = re.sub(r"```(?:json)?", "", text or "")
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None, False
    try:
        obj = json.loads(text[start:end + 1])
    except ValueError:
        return None, False
    try:
        hard_reqs = max(0, min(COMPONENTS["hard_reqs"], int(obj.get("hard_reqs"))))
        level = max(0, min(COMPONENTS["level"], int(obj.get("level", COMPONENTS["level"]))))
    except (TypeError, ValueError):
        return None, False
    logistics_ok = obj.get("logistics_ok") is not False
    third_party = obj.get("third_party") is True
    pay = _pay_band(obj, salary)
    pay_blocked = _below_pay_floor(pay) and pay_floor_cap(pay) < STRETCH_MIN
    if hard_reqs > 0 and logistics_ok and not pay_blocked and not (HIDE_THIRD_PARTY and third_party):
        return None, True
    parts = {"hard_reqs": hard_reqs, "core_work": 0, "evidence": 0, "level": level}
    score, verdict = score_components(parts, logistics_ok, pay)
    blockers = obj.get("blockers") or []
    blockers = [str(b)[:60] for b in blockers][:6] if isinstance(blockers, list) else []
    if _below_pay_floor(pay):
        blockers.append(f"pay tops out at ${pay[1] / 1000:.0f}k")
    return {
        "verdict": "skip",
        "score": min(score, BLOCKER_CAP),
        "components": dict(parts, logistics_ok=logistics_ok),
        "evidence_quote": "",
        "third_party": third_party,
        "pay": list(pay) if pay else None,
        "model_verdict": "skip",
        "reason": str(obj.get("reason", "")).strip()[:200] or "Filtered by Pass-1 triage.",
        "blockers": blockers,
        "triaged_out": True,
    }, True


def _triage_one(executable, triage_model, profile, job, jd, salary=None, provider="agy", on_usage=None):
    """Run Pass-1 triage with `triage_model`; returns (early_skip_result_or_None, passed_triage_bool)."""
    jd = jd or ""
    if len(jd) < 200:
        return None, False
    prompt = TRIAGE_PROMPT.format(profile=profile, company=job.get("company") or "Unknown",
                                  title=job.get("title") or "", location=job.get("location") or "",
                                  salary=_salary_text(salary), jd=jd)
    try:
        response_text, usage = _cli_response(executable, provider, triage_model, prompt)
        if on_usage:
            on_usage(usage)
        early_skip, ok = _parse_triage(response_text, salary)
        if not ok:
            print(f"[AI review] unparseable triage reply for {job.get('company')}", file=sys.stderr)
        return early_skip, ok
    except subprocess.TimeoutExpired:
        print(f"[AI review] triage timeout for {job.get('company')}", file=sys.stderr)
    except RuntimeError as error:
        print(f"[AI review] {provider} triage {error} for {job.get('company')}", file=sys.stderr)
    except (OSError, ValueError) as error:
        print(f"[AI review] {provider} triage CLI error for {job.get('company')}: {type(error).__name__}",
              file=sys.stderr)
    return None, False


def _review_one(executable, model, profile, job, jd, salary=None, provider="agy", on_usage=None):
    """Run one provider review; return the parsed verdict or None on a provider failure.
    on_usage(usage) is called for every reply the CLI completed, parseable or not, since each one was paid for."""
    jd = jd or ""
    if len(jd) < 200:
        return None
    prompt = PROMPT.format(profile=profile, company=job.get("company") or "Unknown",
                           title=job.get("title") or "", location=job.get("location") or "",
                           salary=_salary_text(salary), jd=jd)
    try:
        response_text, usage = _cli_response(executable, provider, model, prompt)
        if on_usage:
            on_usage(usage)
        parsed = _parse_verdict(response_text, jd, salary)
        if not parsed:
            print(f"[AI review] unparseable reply for {job.get('company')}", file=sys.stderr)
        return parsed
    except subprocess.TimeoutExpired:
        print(f"[AI review] timeout for {job.get('company')}", file=sys.stderr)
    except RuntimeError as error:
        print(f"[AI review] {provider} {error} for {job.get('company')}; "
              "check CLI login and model configuration.", file=sys.stderr)
    except (OSError, ValueError) as error:
        print(f"[AI review] {provider} CLI error for {job.get('company')}: {type(error).__name__}; "
              "check CLI installation, login, and model configuration.", file=sys.stderr)
    return None


def _rescored(result):
    """A cached result with its Match % and verdict recomputed, so threshold and pay-rule changes in
    config.json apply to reviews already paid for (the model's grades themselves are unchanged)."""
    parts = result.get("components")
    if not isinstance(parts, dict) or any(name not in parts for name in COMPONENTS):
        return result
    pay = tuple(result["pay"]) if result.get("pay") else None
    score, verdict = score_components({name: parts[name] for name in COMPONENTS},
                                      parts.get("logistics_ok") is not False, pay)
    return dict(result, score=score, verdict=verdict)


def _jd_hash(jd):
    return hashlib.sha256(_norm_quote(jd).encode()).hexdigest()[:16]


def jd_signature(jd):
    """Bottom-k sketch of the JD's 5-word shingles: a few dozen numbers that estimate how much of two
    JDs' text is shared, so a repost with small edits still matches its earlier review."""
    words = _norm_quote(_strip_html(jd or "")).split()
    shingles = {" ".join(words[i:i + SHINGLE_WORDS]) for i in range(max(1, len(words) - SHINGLE_WORDS + 1))}
    # 52-bit hashes stay exact as JSON numbers
    hashes = {int.from_bytes(hashlib.blake2b(s.encode(), digest_size=8).digest(), "big") >> 12
              for s in shingles if s}
    return sorted(hashes)[:SIGNATURE_SIZE]


def signature_similarity(a, b):
    """Estimated Jaccard similarity (0-1) of the two JDs a and b were taken from."""
    if not a or not b:
        return 0.0
    sa, sb = set(a), set(b)
    union = sorted(sa | sb)[:SIGNATURE_SIZE]
    return sum(1 for h in union if h in sa and h in sb) / len(union)


def _find_repost(job, jd, cache, phash):
    """Key of a current-profile cached review of the same posting under another URL, or None:
    the same JD text, or the same company|title with JD text at least REPOST_SIMILARITY alike."""
    h, role = _jd_hash(jd), role_key(job.get("company"), job.get("title"))
    sig = None
    for key, hit in cache.items():
        if not isinstance(hit, dict) or hit.get("profile") != phash:
            continue
        if hit.get("jd") == h:
            return key
        if role and hit.get("role") == role and hit.get("sig"):
            sig = sig if sig is not None else jd_signature(jd)
            if signature_similarity(sig, hit["sig"]) >= REPOST_SIMILARITY:
                return key
    return None


def _cap(name, cast=float):
    try:
        return max(0, cast(_BUDGET_CFG.get(name) or 0))
    except (TypeError, ValueError):
        return 0


def load_spend(path=None):
    entries = load_json(path or SPEND_PATH, default=[])
    return entries if isinstance(entries, list) else []


def spent_on(day, entries):
    """(usd, tokens) the AI review used on `day` (YYYY-MM-DD, local time), from the spend ledger."""
    todays = [e for e in entries if isinstance(e, dict) and e.get("date") == day]
    return (sum(float(e.get("usd") or 0) for e in todays),
            sum(_int(e.get("input_tokens")) + _int(e.get("cached_tokens")) + _int(e.get("output_tokens"))
                for e in todays))


class Budget:
    """Running spend for one review pass, checked against the llm_review.budget caps (0 = no cap).

    Each review reserves its estimated cost before it starts and settles to the real usage after, so
    reviews running in parallel cannot jointly pass a cap. The first cap reached stops the pass: the
    remaining jobs (lower keyword scores, since the pass runs best-first) are deferred to the next run."""

    def __init__(self, today_usd=0.0, today_tokens=0):
        self.max_reviews = _cap("max_reviews_per_run", int)
        self.max_usd_run, self.max_usd_day = _cap("max_usd_per_run"), _cap("max_usd_per_day")
        self.max_tokens_run, self.max_tokens_day = _cap("max_tokens_per_run", int), _cap("max_tokens_per_day", int)
        self.today_usd, self.today_tokens = today_usd, today_tokens
        self.calls = self.input = self.cached = self.output = 0
        self.usd, self.usd_estimated, self.usd_unknown = 0.0, False, False
        self.started = self.reserved_tokens = 0
        self.reserved_usd = 0.0
        self.stopped_by = ""
        self._lock = threading.Lock()

    @property
    def tokens(self):
        return self.input + self.cached + self.output

    def caps(self):
        named = {"reviews/run": self.max_reviews, "USD/run": self.max_usd_run, "USD/day": self.max_usd_day,
                 "tokens/run": self.max_tokens_run, "tokens/day": self.max_tokens_day}
        return {k: v for k, v in named.items() if v}

    def _estimate(self, prompt_chars):
        out = self.output / self.calls if self.calls else EST_OUTPUT_TOKENS
        tokens = prompt_chars // CHARS_PER_TOKEN + int(out)
        usd = priced_usd({"input": tokens - int(out), "cached": 0, "output": int(out)})
        if usd is None:  # no pricing set: assume the average reported cost so far
            usd = self.usd / self.calls if self.calls else 0.0
        return tokens, usd

    def reserve(self, prompt_chars):
        """The (tokens, usd) reserved for one review, or None once a cap would be passed."""
        with self._lock:
            if self.stopped_by:
                return None
            tokens, usd = self._estimate(prompt_chars)
            run_tokens, run_usd = self.tokens + self.reserved_tokens + tokens, self.usd + self.reserved_usd + usd
            checks = (
                ("reviews/run", self.max_reviews, self.started + 1),
                ("tokens/run", self.max_tokens_run, run_tokens),
                ("tokens/day", self.max_tokens_day, self.today_tokens + run_tokens),
                ("USD/run", self.max_usd_run, run_usd),
                ("USD/day", self.max_usd_day, self.today_usd + run_usd),
            )
            for name, cap, value in checks:
                if cap and value > cap:
                    self.stopped_by = name
                    return None
            self.started += 1
            self.reserved_tokens += tokens
            self.reserved_usd += usd
            return tokens, usd

    def release(self, reservation):
        with self._lock:
            self.reserved_tokens -= reservation[0]
            self.reserved_usd -= reservation[1]

    def record(self, usage):
        """Add one completed reply's usage (None when the CLI reported none)."""
        with self._lock:
            self.calls += 1
            if not usage:
                self.usd_unknown = True
                return
            self.input += usage["input"]
            self.cached += usage["cached"]
            self.output += usage["output"]
            usd = usage.get("usd")
            if usd is None:
                usd = priced_usd(usage)
                if usd is None:
                    self.usd_unknown = True
                    return
                self.usd_estimated = True
            self.usd += usd

    def totals(self):
        """This pass's usage, spend and caps, plus the day's running totals, as summary fields."""
        return {
            "input_tokens": self.input, "cached_tokens": self.cached, "output_tokens": self.output,
            "tokens": self.tokens, "usd": round(self.usd, 4), "usd_estimated": self.usd_estimated,
            "usd_unknown": self.usd_unknown, "stopped_by": self.stopped_by, "caps": self.caps(),
            "today_usd": round(self.today_usd + self.usd, 4), "today_tokens": self.today_tokens + self.tokens,
        }


def _write_spend(stats, path=None):
    """Append this pass to the spend ledger, dropping entries older than SPEND_KEEP_DAYS."""
    path = path or SPEND_PATH
    cutoff = (datetime.now() - timedelta(days=SPEND_KEEP_DAYS)).strftime("%Y-%m-%d")
    entries = [e for e in load_spend(path) if isinstance(e, dict) and str(e.get("date", "")) >= cutoff]
    entries.append({k: stats[k] for k in (
        "time", "date", "provider", "model", "reviewed", "failed", "deferred", "cache_hits", "repost_hits",
        "input_tokens", "cached_tokens", "output_tokens", "usd", "usd_estimated", "usd_unknown")})
    atomic_write_json(path, entries, indent=1)


def spend_line(stats):
    """One-line summary of an AI review pass's spend, for logs and notifications."""
    if not stats:
        return ""
    if stats["usd_unknown"] and not stats["usd"]:
        cost = f"{stats['tokens']:,} tokens"
    else:
        cost = f"{'~' if stats['usd_estimated'] or stats['usd_unknown'] else ''}${stats['usd']:.2f} ({stats['tokens']:,} tokens)"
    text = (f"AI review: {stats['reviewed'] + stats['failed']} call(s), {cost}; "
            f"{stats['cache_hits'] + stats['repost_hits']} reused from cache"
            + (f" ({stats['repost_hits']} reposts)" if stats["repost_hits"] else ""))
    if stats["deferred"]:
        text += f"; {stats['deferred']} deferred by the {stats['stopped_by']} cap"
    return text + "."


def _url_key(job):
    return normalize_url(job.get("url", ""))


def _split_cached(due, cache, phash):
    """({url key: rescored result} for jobs with a current-profile cached review, [jobs without one])."""
    results, misses = {}, []
    for job in due:
        key = _url_key(job)
        hit = cache.get(key)
        if isinstance(hit, dict) and hit.get("profile") == phash:
            results[key] = _rescored(hit["result"])
        else:
            misses.append(job)
    return results, misses


def _load_review_inputs(jobs, enriched_path):
    """({url key: JD text for the prompt}, {url key: salary}) for `jobs`, read from the enriched file."""
    jd_by_url, salary_by_url = {}, {}
    if not jobs:
        return jd_by_url, salary_by_url
    keys = {_url_key(j) for j in jobs}
    for j in load_json(enriched_path, default=[]) or []:
        key = _url_key(j)
        if key not in keys:
            continue
        jd_by_url[key] = jd_for_review(j.get("description_full") or j.get("description_ai"), j.get("description_ai"))
        if j.get("salary"):
            salary_by_url[key] = j["salary"]
    return jd_by_url, salary_by_url


def _reuse_reposts(misses, jd_by_url, cache, phash, results):
    """Copy the paid-for review of a repost under a new URL into `results` and `cache`; return the jobs left."""
    todo = []
    for job in misses:
        key = _url_key(job)
        jd = jd_by_url.get(key) or ""
        source = _find_repost(job, jd, cache, phash) if len(jd) >= 200 else None
        if source:
            results[key] = _rescored(cache[source]["result"])
            cache[key] = dict(cache[source], repost_of=source)
        else:
            todo.append(job)
    return todo


def _run_reviews(todo, executable, model, profile, jd_by_url, salary_by_url, budget, triage_model=""):
    """Review `todo` in parallel within the budget: ({url key: result} for the reviews that succeeded,
    {url keys deferred because a cap was reached}).
    When `triage_model` is set, runs a two-stage fleet: Pass 1 screens all jobs in parallel with
    `triage_model` for hard blockers, and Pass 2 runs deep grading with `model` only on survivors."""
    deferred = set()
    prompt_chars = len(profile) + len(PROMPT)
    triage_chars = len(profile) + len(TRIAGE_PROMPT)

    def run_triage(job):
        key = _url_key(job)
        jd = jd_by_url.get(key) or ""
        reservation = budget.reserve(triage_chars + len(jd))
        if reservation is None:
            deferred.add(key)
            return None, False
        try:
            return _triage_one(executable, triage_model, profile, job, jd, salary_by_url.get(key), PROVIDER,
                               on_usage=budget.record)
        finally:
            budget.release(reservation)

    def run_one(job):
        key = _url_key(job)
        jd = jd_by_url.get(key) or ""
        reservation = budget.reserve(prompt_chars + len(jd))
        if reservation is None:
            deferred.add(key)
            return None
        try:
            return _review_one(executable, model, profile, job, jd, salary_by_url.get(key), PROVIDER,
                               on_usage=budget.record)
        finally:
            budget.release(reservation)

    results = {}
    survivors = list(todo)
    if triage_model and triage_model != model:
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            triage_outs = list(pool.map(run_triage, todo))
        survivors = []
        for job, (early_skip, ok) in zip(todo, triage_outs, strict=True):
            key = _url_key(job)
            if key in deferred:
                continue
            if early_skip is not None:
                results[key] = early_skip
            elif ok:
                survivors.append(job)
            else:
                # Fall back to deep review if triage returned an unparseable response
                survivors.append(job)

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        outs = list(pool.map(run_one, survivors))
    for job, out in zip(survivors, outs, strict=True):
        if out:
            results[_url_key(job)] = out
    return results, deferred


def _save_cache(cache, phash, changed):
    """Drop entries from another profile, prompt, model or provider (they can never be read again), and
    write the cache when anything changed."""
    stale = [key for key, hit in cache.items() if not isinstance(hit, dict) or hit.get("profile") != phash]
    if not (changed or stale):
        return
    for key in stale:
        del cache[key]
    atomic_write_json(CACHE_PATH, cache, indent=1)
    if stale:
        print(f"[AI review] pruned {len(stale)} cached review(s) from an older profile or prompt.")


def _apply_results(matches, results, deferred, min_score):
    """(kept, skipped, failed count, hidden count): each match with its review applied, or held back."""
    kept, skipped, failed, hidden = [], [], 0, 0
    for job in matches:
        key = _url_key(job)
        out = results.get(key)
        if out:
            job = dict(job, verdict=out["verdict"], ai_reason=out["reason"], blockers=out["blockers"],
                       components=out.get("components"), third_party=bool(out.get("third_party")))
            if out.get("score") is not None:
                job["keyword_score"], job["score"] = job.get("score", 0), out["score"]
        elif key in deferred:
            job = dict(job, review_failed=True, review_deferred=True)
        elif job.get("score", 0) >= min_score:
            job = dict(job, review_failed=True)
            failed += 1
        hide = bool(out) and HIDE_THIRD_PARTY and job["third_party"] and out["verdict"] != "skip"
        hidden += hide
        (skipped if out and (out["verdict"] == "skip" or hide) else kept).append(job)
    # Match % moves in 8-point steps, so the keyword score orders the many ties within a step
    kept.sort(key=lambda j: (j.get("score", 0), j.get("keyword_score", 0)), reverse=True)
    return kept, skipped, failed, hidden


def review_matches(matches, enriched_path, min_score=55, stats=None):
    """Review every match scoring >= min_score, best keyword score first. Returns (kept, skipped).

    A match that was due a review and did not get one (timeout, CLI failure, unparseable reply, JD too
    short, or a budget cap reached) is kept with review_failed=True: it is shown but not synced or
    exported, and is retried next run. Pass a dict as `stats` to receive this pass's usage and spend."""
    executable = _provider_config(PROVIDER)
    if not executable or not os.path.exists(PROFILE_PATH):
        if executable:
            print(f"[AI review] {PROFILE_PATH} not found; skipping AI review. Copy candidate_profile.example.md "
                  "to that path and fill it in to enable it.", file=sys.stderr)
        if stats is not None:
            stats["skipped"] = skip_message(PROVIDER)
        return matches, []

    model, triage_model, profile = DEFAULT_MODEL, TRIAGE_MODEL, _profile_text()
    phash = _profile_hash(profile, model, PROVIDER, triage_model)
    cache = load_json(CACHE_PATH, default={})
    if not isinstance(cache, dict):
        cache = {}
    due = sorted((j for j in matches if j.get("score", 0) >= min_score),
                 key=lambda j: j.get("score", 0), reverse=True)

    results, misses = _split_cached(due, cache, phash)
    cache_hits = len(results)
    jd_by_url, salary_by_url = _load_review_inputs(misses, enriched_path)
    todo = _reuse_reposts(misses, jd_by_url, cache, phash, results)
    repost_hits = len(misses) - len(todo)

    today = datetime.now().strftime("%Y-%m-%d")
    budget = Budget(*spent_on(today, load_spend()))
    tier_note = f" (Pass-1 triage: {triage_model} -> Pass-2 deep: {model})" if triage_model and triage_model != model else f" with {model}"
    print(f"[AI review] {cache_hits} cached, {repost_hits} reposts of cached reviews, "
          f"{len(todo)} to review{tier_note} via {PROVIDER}."
          + (f" Caps: {', '.join(f'{v:g} {k}' for k, v in budget.caps().items())}." if budget.caps() else ""))

    fresh, deferred = (_run_reviews(todo, executable, model, profile, jd_by_url, salary_by_url, budget,
                                    triage_model=triage_model)
                       if todo else ({}, set()))
    for job in todo:
        key = _url_key(job)
        if key in fresh:
            jd = jd_by_url.get(key) or ""
            cache[key] = {"profile": phash, "result": fresh[key], "role": role_key(job.get("company"), job.get("title")),
                          "jd": _jd_hash(jd), "sig": jd_signature(jd)}
    results.update(fresh)
    _save_cache(cache, phash, changed=bool(todo or repost_hits))

    kept, skipped, failed, hidden = _apply_results(matches, results, deferred, min_score)
    summary = {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M"), "date": today, "provider": PROVIDER, "model": model,
        "reviewed": len(fresh), "failed": failed, "deferred": len(deferred), "cache_hits": cache_hits,
        "repost_hits": repost_hits, **budget.totals(),
    }
    if due:
        _write_spend(summary)
    if stats is not None:
        stats.update(summary)
    print(f"[AI review] kept {len(kept)}, skipped {len(skipped)}"
          + (f" ({hidden} hidden as third-party posters)" if hidden else "")
          + (f", {failed} not reviewed (held back until a review succeeds)" if failed else "")
          + (f", {len(deferred)} deferred by the {budget.stopped_by} cap" if deferred else "") + ".")
    print(f"[AI review] {spend_line(summary)}")
    return kept, skipped


def reason_for_notion(job):
    """Text for the Notion 'Why It Matches' column: the AI verdict when there is one."""
    if not job.get("verdict"):
        if job.get("review_deferred"):
            return f"{REVIEW_DEFERRED_LABEL} {job.get('reason', '')}".strip()
        if job.get("review_failed"):
            return f"{REVIEW_FAILED_LABEL} {job.get('reason', '')}".strip()
        return job.get("reason", "")
    text = f"{THIRD_PARTY_LABEL} " if job.get("third_party") else ""
    text += f"{job['verdict'].upper()}: {job.get('ai_reason', '')}"
    if job.get("blockers"):
        text += f" Gaps: {'; '.join(job['blockers'])}."
    if job.get("keyword_score") is not None:
        text += f" (keyword score {job['keyword_score']}%)"
    return text[:1900]


if __name__ == "__main__":
    with open(sys.argv[1], encoding="utf-8") as f:
        data = json.load(f)
    kept, skipped = review_matches(data["top_matches"], sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else 55)
    for j in kept + skipped:
        print(f"{j.get('verdict', '-'):8} {j.get('score', 0):>3}% (kw {j.get('keyword_score', '-')}) "
              f"{j['company']} | {j['title'][:50]} | {reason_for_notion(j)}")
