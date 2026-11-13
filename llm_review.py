"""Second-stage review of keyword-scored matches: asks an LLM (via the agy CLI)
to read each JD against `candidate_profile.md` and return apply / stretch / skip
with a structured rubric and reason.

Fail-open by design: if the LLM CLI is missing, times out, or returns something
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
from concurrent.futures import ThreadPoolExecutor

from bs4 import BeautifulSoup

from pipeline_config import CFG, atomic_write_json, get_path, load_json
from url_normalizer import normalize_url

_LLM_CFG = CFG.get("llm_review") or {}
_COMP_CFG = CFG.get("compensation") or {}

PROFILE_PATH = get_path("candidate_profile", "candidate_profile.md")
CACHE_PATH = get_path("review_cache", "data/review_cache.json")

DEFAULT_MODEL = _LLM_CFG.get("model", "gemini-4-hydrogen-medium")
DEFAULT_AGY = "~/.local/bin/agy"
TIMEOUT_SECONDS = int(_LLM_CFG.get("timeout_seconds", 180))
WORKERS = int(_LLM_CFG.get("workers", 3))
JD_CHARS = int(_LLM_CFG.get("jd_chars", 9000))
JD_HEAD_CHARS = int(_LLM_CFG.get("jd_head_chars", 2500))
VERDICTS = ("apply", "stretch", "skip")
REVIEW_VERSION = str(_LLM_CFG.get("review_version", "1"))

COMPONENTS = _LLM_CFG.get("components") or {"hard_reqs": 4, "core_work": 3, "evidence": 3, "level": 2}
MAX_TOTAL = sum(COMPONENTS.values())
BLOCKER_CAP = int(_LLM_CFG.get("blocker_cap", 30))
APPLY_MIN = int(_LLM_CFG.get("apply_min_score", 75))
STRETCH_MIN = int(_LLM_CFG.get("stretch_min_score", 50))

MIN_SALARY = float(_COMP_CFG.get("min_salary", 0))
PAY_MIDPOINT_MAX_CAP = float(_COMP_CFG.get("pay_midpoint_max_cap", 0))
PAY_MIDPOINT_PENALTY = int(_COMP_CFG.get("pay_midpoint_penalty", 10))
QUOTE_MATCH_RATIO = float(_LLM_CFG.get("quote_match_ratio", 0.85))
HOURS_PER_YEAR = 2080

_RUBRIC = _LLM_CFG.get("rubric_descriptions") or {}
_HARD_REQS_DESC = _RUBRIC.get(
    "hard_reqs",
    "4 = every hard requirement met; 3 = met, with only soft gaps; 2 = one bounded hard gap (one named tool/skill or years 1 over); "
    "1 = several hard gaps; 0 = a clear blocker (years far over, a required specialty/credential the candidate lacks)."
)
_CORE_WORK_DESC = _RUBRIC.get(
    "core_work",
    "3 = directly matches the candidate's primary target role and day-to-day responsibilities; "
    "2 = balanced/adjacent role in the candidate's core domain; 1 = peripheral overlap; 0 = off-target specialty."
)
_EVIDENCE_DESC = _RUBRIC.get(
    "evidence",
    "3 = the posting itself asks for work that mirrors a specific accomplishment in the candidate profile; "
    "2 = strong skill and domain overlap; 1 = partial overlap; 0 = little overlap. Grade only what the posting says, not what the company is known for."
)
_LEVEL_DESC = _RUBRIC.get(
    "level",
    "2 = matches the candidate's target seniority/level range; 1 = slight stretch in seniority or pay band mismatch; "
    "0 = under-leveled (entry/junior when not targeted) or over-leveled (executive/principal/director when not targeted)."
)
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
    "(null when the posting gives none; hourly rates times 2080). Do not count pay in hard_reqs.\n"
    "logistics_ok: false if the location, hybrid/onsite requirement, or work authorization rules the candidate out.\n"
    f"{_CALIBRATION_BLOCK}\n"
    "Reply with one JSON object and nothing else, in this shape:\n"
    "{{\"hard_reqs\": 0, \"core_work\": 0, \"evidence\": 0, \"evidence_quote\": \"verbatim sentence from the posting\", "
    "\"level\": 0, \"logistics_ok\": true, \"pay_min\": null, \"pay_max\": null, \"verdict\": \"apply|stretch|skip\", "
    "\"reason\": \"one plain sentence, under 160 characters, naming the deciding factor\", "
    "\"blockers\": [\"short phrases for each hard gap, empty if none\"]}}"
)


def _profile_text():
    with open(PROFILE_PATH, encoding="utf-8") as f:
        return f.read()


def _profile_hash(profile, model):
    return hashlib.sha256((profile + "|" + model + "|" + PROMPT + "|" + REVIEW_VERSION).encode()).hexdigest()[:16]


def _find_agy():
    """Path to the agy CLI (config, then PATH, then the default install location), or None if absent."""
    path = os.path.expanduser(_LLM_CFG.get("agy_path") or shutil.which("agy") or DEFAULT_AGY)
    return path if os.path.exists(path) else None


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
    if parts["hard_reqs"] == 0 or not logistics_ok or _below_pay_floor(pay):
        score = min(score, BLOCKER_CAP)
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


def _review_one(agy, model, profile, job, jd_by_url, salary_by_url=None):
    """Run one agy review; returns the parsed verdict, or None (fail-open) on any problem."""
    key = normalize_url(job.get("url", ""))
    jd = jd_by_url.get(key) or ""
    salary = (salary_by_url or {}).get(key)
    if len(jd) < 200:
        return None
    prompt = PROMPT.format(profile=profile, company=job.get("company") or "Unknown",
                           title=job.get("title") or "", location=job.get("location") or "",
                           salary=_salary_text(salary), jd=jd)
    try:
        with tempfile.TemporaryDirectory() as cwd:
            res = subprocess.run([agy, "-p", prompt, "--model", model, "--output-format", "json"],
                                 capture_output=True, text=True, timeout=TIMEOUT_SECONDS, cwd=cwd)
        if res.returncode != 0:
            print(f"[AI review] agy exit {res.returncode} for {job.get('company')}: {res.stderr[:200]}", file=sys.stderr)
            return None
        envelope = json.loads(res.stdout)
        if envelope.get("status") != "SUCCESS":
            print(f"[AI review] agy status {envelope.get('status')} for {job.get('company')}", file=sys.stderr)
            return None
        parsed = _parse_verdict(envelope.get("response", ""), jd, salary)
        if not parsed:
            print(f"[AI review] unparseable reply for {job.get('company')}: {envelope.get('response', '')[:120]!r}", file=sys.stderr)
        return parsed
    except subprocess.TimeoutExpired:
        print(f"[AI review] timeout for {job.get('company')}", file=sys.stderr)
    except Exception as e:
        print(f"[AI review] error for {job.get('company')}: {e}", file=sys.stderr)
    return None


def review_matches(matches, enriched_path, min_score=55):
    """Review every match scoring >= min_score. Returns (kept, skipped)."""
    agy = _find_agy()
    if not agy:
        print("[AI review] agy not found; skipping AI review.", file=sys.stderr)
        return matches, []
    if not os.path.exists(PROFILE_PATH):
        print(f"[AI review] {PROFILE_PATH} not found; skipping AI review.", file=sys.stderr)
        return matches, []

    model = DEFAULT_MODEL
    profile = _profile_text()
    phash = _profile_hash(profile, model)

    cache = load_json(CACHE_PATH, default={})
    todo, results = [], {}
    for job in matches:
        if job.get("score", 0) < min_score:
            continue
        key = normalize_url(job.get("url", ""))
        hit = cache.get(key)
        if hit and hit.get("profile") == phash:
            results[key] = hit["result"]
        else:
            todo.append(job)

    print(f"[AI review] {len(results)} cached, {len(todo)} to review with {model} via agy.")
    if todo:
        todo_keys = {normalize_url(j.get("url", "")) for j in todo}
        with open(enriched_path) as f:
            enriched = [j for j in json.load(f) if normalize_url(j.get("url", "")) in todo_keys]
        jd_by_url = {normalize_url(j.get("url", "")): jd_for_review(j.get("description_full") or j.get("description_ai"),
                                                                    j.get("description_ai"))
                     for j in enriched}
        salary_by_url = {normalize_url(j.get("url", "")): j.get("salary") for j in enriched if j.get("salary")}
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            outs = list(pool.map(lambda j: _review_one(agy, model, profile, j, jd_by_url, salary_by_url), todo))
        for job, out in zip(todo, outs, strict=True):
            if out:
                key = normalize_url(job.get("url", ""))
                results[key] = out
                cache[key] = {"profile": phash, "result": out}
        atomic_write_json(CACHE_PATH, cache, indent=1)

    kept, skipped = [], []
    for job in matches:
        out = results.get(normalize_url(job.get("url", "")))
        if out:
            job = dict(job, verdict=out["verdict"], ai_reason=out["reason"], blockers=out["blockers"],
                       components=out.get("components"))
            if out.get("score") is not None:
                job["keyword_score"], job["score"] = job.get("score", 0), out["score"]
        (skipped if out and out["verdict"] == "skip" else kept).append(job)
    kept.sort(key=lambda j: j.get("score", 0), reverse=True)
    print(f"[AI review] kept {len(kept)}, skipped {len(skipped)}.")
    return kept, skipped


def reason_for_notion(job):
    """Text for the Notion 'Why It Matches' column: the AI verdict when there is one."""
    if not job.get("verdict"):
        return job.get("reason", "")
    text = f"{job['verdict'].upper()}: {job.get('ai_reason', '')}"
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
              f"{j['company']} | {j['title'][:50]} | {j.get('ai_reason', '')}")
