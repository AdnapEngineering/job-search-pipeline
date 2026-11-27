"""`--calibrate`: check the Stage 3 rules against the AI reviews already in the review cache, with no LLM calls.

Every cached review holds the reviewer's grades for a posting the pipeline has saved. Replaying the current
config's keyword score, hard rules and penalties over those postings shows which rules agree with the reviewer,
which would drop roles it rated APPLY, how well the keyword score ranks verdicts (and so where the review floor
belongs), and which reasons for its skips no rule catches yet. That is what lets a small AI budget, or none,
go further: tune the Python rules against grades already paid for.

With `--auto-calibrate`, runs a 3-stage agent workflow (Mine -> Propose -> Replay & Verify):
1. Mines false-negative hard gates (that drop APPLY/STRETCH roles) and uncaught SKIP blockers.
2. Asks the configured AI reviewer CLI to propose minimal config.json rule updates.
3. Replays the proposed config in memory. Guardrails on every review: no more APPLY or STRETCH drops.
   The gain (fewer uncaught SKIPs or dropped roles) must show on a holdout the agent never saw (30% by
   default, `--holdout`), so changes that only fit the postings it was shown are rejected. Only then is a
   calibrated draft written (or `config.json` updated with `--apply`).
"""
import collections
import copy
import hashlib
import importlib
import json
import os
import subprocess

from job_search_pipeline.config import (
    CFG,
    CONFIG_PATH,
    ROOT_DIR,
    atomic_write_json,
    get_path,
    load_json,
)
from job_search_pipeline.evaluate import (
    DROP_RULES,
    evaluate_job_traced,
    score_breakdown,
)
from job_search_pipeline.init_config import (
    GROUPS,
    INT,
    INT_MAP,
    REGEX_LIST,
    ROLE_GROUPS,
    STR_LIST,
    _clean,
    parse_reply,
)
from job_search_pipeline.review import (
    CLI_NAMES,
    DEFAULT_MODEL,
    PROFILE_PATH,
    PROVIDER,
    VERDICTS,
    _cli_path,
    _cli_response,
    _rescored,
    priced_usd,
)
from job_search_pipeline.urls import normalize_url

REVIEW_MIN_SCORE = int(CFG["skills_and_scoring"]["min_review_score"])
BAND = 10
MAX_EXAMPLES = 3
MAX_UNCAUGHT = 15
_OPTIONAL_PREFIX = "Excluded skills offered as optional: "
DEFAULT_CALIBRATED_OUTPUT = os.path.join(ROOT_DIR, "config.calibrated.json")
AUTO_CALIBRATE_TIMEOUT = 300
HOLDOUT_FRACTION = 0.3

CALIBRATE_FIELDS = {
    ("titles", "exclude_seniority_patterns"): REGEX_LIST,
    ("titles", "exclude_tech_patterns"): REGEX_LIST,
    ("titles", "exclude_role_patterns"): REGEX_LIST,
    ("titles", "soft_exclude_role_patterns"): REGEX_LIST,
    ("titles", "target_patterns"): REGEX_LIST,
    ("titles", "strong_target_patterns"): REGEX_LIST,
    ("titles", "primary_role_groups"): ROLE_GROUPS,
    ("skills_and_scoring", "hard_exclude_skills"): STR_LIST,
    ("skills_and_scoring", "avoid_keywords"): STR_LIST,
    ("skills_and_scoring", "penalized_keywords"): INT_MAP,
    ("skills_and_scoring", "core_strengths"): STR_LIST,
    ("skills_and_scoring", "core_strength_weights"): INT_MAP,
    ("skills_and_scoring", "focus_keywords"): INT_MAP,
    ("skills_and_scoring", "focus_keyword_groups"): GROUPS,
    ("skills_and_scoring", "min_review_score"): INT,
    ("skills_and_scoring", "secondary_floor"): INT,
    ("experience", "max_min_yoe"): INT,
    ("experience", "max_min_secondary_yoe"): INT,
}

AUTO_CALIBRATE_PROMPT = """You are an automated rule-calibration agent for a job-search pipeline.
Your goal is to propose minimal, high-precision updates to `config.json` based on empirical discrepancies
between Stage 3 deterministic rules and Stage 4 AI reviews.

Rules of calibration:
1. NEVER drop an APPLY role: If a hard rule in `false_negative_gates` dropped an APPLY role, loosen or remove
   the offending pattern/skill from that setting.
2. CATCH repeated SKIP blockers: Look at `uncaught_skips` (postings the AI reviewer rated SKIP that passed
   every deterministic rule and penalty). When the same required skill, specialty, or title word appears in
   uncaught skips and does NOT conflict with the candidate profile, add it to:
   - `skills_and_scoring.hard_exclude_skills` (only for unambiguous non-target languages/specialties)
   - `skills_and_scoring.avoid_keywords` (technologies to reject when required)
   - `skills_and_scoring.penalized_keywords` (map of lower-case term -> 5..15 points penalty)
   - `titles.exclude_role_patterns` or `titles.exclude_tech_patterns` (word-bounded lower-case regexes)
3. Keep all existing valid entries in any list/map you touch unless you are loosening a false-negative rule.
   Return the COMPLETE replacement value for each config key you modify (only include keys you are changing).

CANDIDATE PROFILE
{profile}

CURRENT CONFIG (relevant sections)
{current_config}

CALIBRATION TELEMETRY
{telemetry}

Reply with one JSON object and nothing else, in this shape:
{{
  "rationale": ["1-sentence explanation for each rule change"],
  "changes": {{
    "titles": {{ ...only modified keys... }},
    "skills_and_scoring": {{ ...only modified keys... }},
    "experience": {{ ...only modified keys... }}
  }}
}}
"""


def labeled_reviews(cache=None, jobs=None):
    """([(job, rescored review result)] for cached full reviews whose posting is saved, count of reviews without one).

    Pass-1 triage skips are left out: they come from the cheap triage model, which never grades the rubric."""
    cache = load_json(get_path("review_cache"), default={}) if cache is None else cache
    if jobs is None:
        archive = load_json(get_path("reviewed_postings"), default={}) or {}
        jobs = [*(archive.values() if isinstance(archive, dict) else []),
                *(load_json(get_path("filtered_jobs"), default=[]) or []),
                *(load_json(get_path("enriched_jobs"), default=[]) or [])]  # enriched last: it holds the JD
    by_url = {normalize_url(j.get("url", "")): j for j in jobs if isinstance(j, dict) and j.get("url")}
    labeled, missing = [], 0
    for url, entry in (cache or {}).items():
        result = entry.get("result") if isinstance(entry, dict) else None
        if not isinstance(result, dict) or result.get("triaged_out"):
            continue  # a Pass-1 triage skip is the cheap model's call on blockers only, not a graded label
        result = _rescored(result)
        job = by_url.get(normalize_url(url))
        if result.get("verdict") not in VERDICTS:
            continue
        if job and (job.get("description_full") or job.get("description_ai")):
            labeled.append((job, result))
        else:
            missing += 1
    return labeled, missing


def _tally(verdicts):
    c = collections.Counter(verdicts)
    return " · ".join(f"{c[v]} {v}" for v in VERDICTS)


def _role(job):
    return f"{job.get('company') or 'Unknown'} · {job.get('title') or 'Unknown role'}"


def analyze(labeled):
    """Everything the report prints, as data (also what the tests check)."""
    bands = collections.defaultdict(list)
    gates = collections.defaultdict(list)
    penalties = collections.defaultdict(list)
    uncaught, scores = [], collections.defaultdict(list)
    for job, result in labeled:
        verdict = result["verdict"]
        score, parts = score_breakdown(job)
        scores[verdict].append(score)
        bands[score // BAND * BAND].append(verdict)
        _, rejection = evaluate_job_traced(job)
        if rejection:
            gates[rejection["rejected_by"]].append((verdict, job, rejection["detail"]))
        fired = [label for label, points in parts if points < 0]
        for label in [f for f in fired if f.startswith(_OPTIONAL_PREFIX)]:
            # One label lists every excluded skill offered as optional; tally each skill on its own
            fired.remove(label)
            fired.extend(f"{_OPTIONAL_PREFIX}{skill}" for skill in label[len(_OPTIONAL_PREFIX):].split(", "))
        for label in fired:
            penalties[label].append(verdict)
        if verdict == "skip" and not rejection and not fired:
            uncaught.append((job, result.get("blockers") or [result.get("reason", "")]))

    worth = scores["apply"] + scores["stretch"]
    return {
        "bands": dict(sorted(bands.items(), reverse=True)),
        "lowest_apply": min(scores["apply"]) if scores["apply"] else None,
        "lowest_stretch_or_apply": min(worth) if worth else None,
        "gates": gates,
        "penalties": penalties,
        "uncaught": uncaught,
    }


def report(labeled, missing, out=print):
    verdicts = [r["verdict"] for _, r in labeled]
    out(f"Calibrating against {len(labeled)} cached AI review(s) with a saved posting ({_tally(verdicts)})"
        + (f"; {missing} more have no saved posting (pruned or never saved) and are left out." if missing else "."))
    if not labeled:
        out("Nothing to calibrate against yet: run the pipeline with the AI review on to build the cache.")
        return
    a = analyze(labeled)
    skip_rate = verdicts.count("skip") / len(verdicts)

    out("\nKeyword score vs. verdict (the score should rise with the verdict; if it doesn't, it is a poor review floor)")
    for band, vs in a["bands"].items():
        out(f"  {band:>3}-{band + BAND - 1:<3} {_tally(vs)}")
    out(f"  Lowest keyword score of an APPLY: {a['lowest_apply']}; of an APPLY or STRETCH: "
        f"{a['lowest_stretch_or_apply']}. min_review_score is {REVIEW_MIN_SCORE}.")
    out("  Reviews exist only above the floor in use when they ran, so this cannot show what lies below it: lower "
        "skills_and_scoring.secondary_floor and min_review_score for a while to find out.")

    out("\nHard rules that would drop reviewed postings today (an APPLY here is a rule to loosen)")
    if not a["gates"]:
        out("  None: every reviewed posting passes the current rules.")
    for rule, hits in sorted(a["gates"].items(), key=lambda kv: -len(kv[1])):
        out(f"  {DROP_RULES.get(rule, rule)}: {_tally(v for v, _, _ in hits)}")
        for verdict, job, detail in [h for h in hits if h[0] != "skip"][:MAX_EXAMPLES]:
            out(f"    {verdict.upper()}: {_role(job)} ({detail})")

    out(f"\nPenalties: skip share where each fires (all reviews: {skip_rate:.0%}; much higher means it could be a "
        "hard rule, near or under it means it costs good roles points)")
    if not a["penalties"]:
        out("  No penalty fired on a reviewed posting.")
    for label, vs in sorted(a["penalties"].items(), key=lambda kv: -len(kv[1])):
        out(f"  {vs.count('skip') / len(vs):>4.0%} skip  {_tally(vs)}  {label}")

    out(f"\nSkips no rule or penalty touched ({len(a['uncaught'])}): their blockers are candidates for new "
        "hard_exclude_skills, hard_ask_penalties or penalized_keywords")
    for job, blockers in a["uncaught"][:MAX_UNCAUGHT]:
        out(f"  {_role(job)}: {'; '.join(b for b in blockers if b)[:150]}")


def calibrate():
    labeled, missing = labeled_reviews()
    report(labeled, missing)
    return 0


def calibration_telemetry(analysis):
    """Structured telemetry dict of false-negative gates, penalty skip rates, and uncaught skips for the agent."""
    false_negatives = []
    for rule, hits in sorted(analysis["gates"].items(), key=lambda kv: -len(kv[1])):
        non_skips = [
            {"verdict": v, "company": j.get("company"), "title": j.get("title"), "detail": detail}
            for v, j, detail in hits if v != "skip"
        ]
        if non_skips:
            false_negatives.append({
                "rule": rule,
                "description": DROP_RULES.get(rule, rule),
                "total_hits": len(hits),
                "apply_dropped": sum(1 for v, _, _ in hits if v == "apply"),
                "stretch_dropped": sum(1 for v, _, _ in hits if v == "stretch"),
                "examples": non_skips[:5],
            })
    penalties = [
        {"label": label, "total": len(vs), "skip_rate": round(vs.count("skip") / len(vs), 2),
         "apply_hits": vs.count("apply"), "stretch_hits": vs.count("stretch")}
        for label, vs in sorted(analysis["penalties"].items(), key=lambda kv: -len(kv[1]))
    ]
    uncaught = [
        {"company": j.get("company"), "title": j.get("title"),
         "blockers": [b for b in blockers if b][:4]}
        for j, blockers in analysis["uncaught"][:30]
    ]
    return {
        "lowest_apply_score": analysis["lowest_apply"],
        "lowest_stretch_or_apply_score": analysis["lowest_stretch_or_apply"],
        "false_negative_gates": false_negatives,
        "penalties": penalties,
        "uncaught_skips_count": len(analysis["uncaught"]),
        "uncaught_skips": uncaught,
    }


def metrics_for_analysis(analysis, total_labeled):
    """Summary metrics for comparing baseline vs proposed rules on the same labeled set."""
    apply_drops = sum(1 for hits in analysis["gates"].values() for v, _, _ in hits if v == "apply")
    stretch_drops = sum(1 for hits in analysis["gates"].values() for v, _, _ in hits if v == "stretch")
    skip_Gate_catches = sum(1 for hits in analysis["gates"].values() for v, _, _ in hits if v == "skip")
    return {
        "total_labeled": total_labeled,
        "apply_drops": apply_drops,
        "stretch_drops": stretch_drops,
        "skip_gate_catches": skip_Gate_catches,
        "uncaught_skips": len(analysis["uncaught"]),
        "lowest_apply": analysis["lowest_apply"],
    }


def _split_key(job):
    return hashlib.sha256(normalize_url(job.get("url", "")).encode()).hexdigest()


def split_holdout(labeled, fraction=HOLDOUT_FRACTION):
    """(train, holdout): reviews split per verdict so the holdout gets some of each.

    The agent only sees telemetry from `train`; `holdout` is kept back to check that its changes generalize
    instead of fitting the postings it was shown. The split is by URL hash, so the same reviews always split
    the same way. A verdict with a single review stays in train. `fraction` 0 disables the holdout.
    """
    if fraction <= 0:
        return list(labeled), []
    by_verdict = collections.defaultdict(list)
    for item in labeled:
        by_verdict[item[1]["verdict"]].append(item)
    train, holdout = [], []
    for verdict in VERDICTS:
        group = sorted(by_verdict.get(verdict, []), key=lambda item: _split_key(item[0]))
        n = min(len(group) - 1, max(1, round(len(group) * fraction))) if len(group) > 1 else 0
        holdout.extend(group[:n])
        train.extend(group[n:])
    return train, holdout


def _drops(m):
    return m["apply_drops"] + m["stretch_drops"]


def verify_proposal(base_all, prop_all, base_check, prop_check):
    """(accepted, reasons) for a proposal: the gate between an agent's suggestion and a written config.

    Guardrails on every labeled review: no more APPLY drops and no more STRETCH drops than today.
    Gain on the check set (the holdout, or train when the holdout is off): fewer uncaught SKIPs or fewer
    dropped APPLY/STRETCH roles. A proposal that only moves numbers on the postings the agent saw is rejected.
    """
    reasons = []
    if prop_all["apply_drops"] > base_all["apply_drops"]:
        reasons.append(f"would drop more APPLY roles ({base_all['apply_drops']} -> {prop_all['apply_drops']})")
    if prop_all["stretch_drops"] > base_all["stretch_drops"]:
        reasons.append(f"would drop more STRETCH roles ({base_all['stretch_drops']} -> {prop_all['stretch_drops']})")
    if not base_check["uncaught_skips"] and not _drops(base_check):
        reasons.append("the check set has no uncaught SKIPs or dropped roles, so no gain can be measured "
                       "(collect more reviews, or lower --holdout)")
    elif prop_check["uncaught_skips"] >= base_check["uncaught_skips"] and _drops(prop_check) >= _drops(base_check):
        reasons.append(f"no gain on the check set (uncaught SKIPs {base_check['uncaught_skips']} -> "
                       f"{prop_check['uncaught_skips']}, dropped APPLY/STRETCH {_drops(base_check)} -> "
                       f"{_drops(prop_check)})")
    return not reasons, reasons


def _metrics_line(base, prop):
    return (f"APPLY drops {base['apply_drops']} -> {prop['apply_drops']} · "
            f"STRETCH drops {base['stretch_drops']} -> {prop['stretch_drops']} · "
            f"Uncaught SKIPs {base['uncaught_skips']} -> {prop['uncaught_skips']} · "
            f"Caught SKIPs {base['skip_gate_catches']} -> {prop['skip_gate_catches']}")


def apply_proposed_changes(base_cfg, reply):
    """(updated_cfg, changed_keys, rationale, problems) laying validated agent changes onto `base_cfg`."""
    updated = copy.deepcopy(base_cfg)
    changes = reply.get("changes") if isinstance(reply.get("changes"), dict) else reply
    changed_keys, problems = [], []
    for (section, key), shape in CALIBRATE_FIELDS.items():
        sec_obj = changes.get(section) if isinstance(changes, dict) else None
        if not isinstance(sec_obj, dict) or key not in sec_obj:
            continue
        val = sec_obj[key]
        if val is None:
            continue
        cleaned, issues = _clean(val, shape)
        problems.extend(f"{section}.{key}: {p}" for p in issues)
        if cleaned is not None:
            updated.setdefault(section, {})[key] = cleaned
            changed_keys.append(f"{section}.{key}")
    # Settings the agent proposed that calibration may not change: report them instead of dropping them silently
    for section, sec_obj in (changes.items() if isinstance(changes, dict) else []):
        if section == "rationale" or not isinstance(sec_obj, dict):
            continue
        problems.extend(f"{section}.{key}: not a setting calibration may change; ignored"
                        for key in sec_obj if (section, key) not in CALIBRATE_FIELDS)
    rationale = [str(r).strip() for r in (reply.get("rationale") or []) if isinstance(r, str) and str(r).strip()]
    return updated, changed_keys, rationale, problems


def replay_with_config(candidate_cfg, labeled):
    """Replay Stage 3 evaluation over `labeled` using `candidate_cfg` in-memory, restoring CFG afterwards."""
    import job_search_pipeline.config as cfg_mod
    import job_search_pipeline.evaluate as ev_mod
    import job_search_pipeline.sources.filter as ff_mod

    merged = cfg_mod.merge_defaults(candidate_cfg)
    saved = copy.deepcopy(cfg_mod.CFG)
    try:
        cfg_mod.CFG.clear()
        cfg_mod.CFG.update(merged)
        importlib.reload(ff_mod)
        importlib.reload(ev_mod)
        return analyze(labeled)
    finally:
        cfg_mod.CFG.clear()
        cfg_mod.CFG.update(saved)
        importlib.reload(ff_mod)
        importlib.reload(ev_mod)


def auto_calibrate(output=None, apply_in_place=False, out=print, holdout=HOLDOUT_FRACTION):
    """Run the 3-stage Auto-Calibration Agent Workflow: Mine -> Propose -> Replay & Verify."""
    if not 0 <= holdout < 1:
        out(f"[Auto-Calibrate] --holdout must be at least 0 and below 1, got {holdout}.")
        return 2
    labeled, missing = labeled_reviews()
    report(labeled, missing, out=out)
    if not labeled:
        return 0

    train, held = split_holdout(labeled, holdout)
    check = held or train
    baseline_all = metrics_for_analysis(analyze(labeled), len(labeled))
    baseline_check = metrics_for_analysis(analyze(check), len(check))
    telemetry = calibration_telemetry(analyze(train))
    out(f"\n[Auto-Calibrate · Stage 1] Split {len(labeled)} review(s): {len(train)} shown to the agent, "
        + (f"{len(held)} held out to verify." if held else "none held out (verifying on the same reviews)."))
    if not telemetry["false_negative_gates"] and not telemetry["uncaught_skips"]:
        out("[Auto-Calibrate] No dropped APPLY/STRETCH roles or uncaught SKIPs in the training reviews. "
            "No rule changes needed.")
        return 0

    executable = _cli_path(PROVIDER)
    if not executable:
        out(f"\n[Auto-Calibrate] The {CLI_NAMES[PROVIDER]} CLI for provider {PROVIDER!r} was not found.")
        return 1

    profile = ""
    if os.path.exists(PROFILE_PATH):
        with open(PROFILE_PATH, encoding="utf-8") as f:
            profile = f.read().strip()

    raw_cfg = load_json(CONFIG_PATH, default={}) or {}
    relevant_cfg = {sec: CFG.get(sec, {}) for sec in ("titles", "skills_and_scoring", "experience")}
    prompt = AUTO_CALIBRATE_PROMPT.format(
        profile=profile or "(no candidate_profile.md)",
        current_config=json.dumps(relevant_cfg, indent=2),
        telemetry=json.dumps(telemetry, indent=2),
    )

    out(f"\n[Auto-Calibrate · Stage 2] Proposing rule updates with {PROVIDER}"
        + (f" ({DEFAULT_MODEL})" if DEFAULT_MODEL else "") + "...")
    try:
        reply_text, usage = _cli_response(executable, PROVIDER, DEFAULT_MODEL, prompt, timeout=AUTO_CALIBRATE_TIMEOUT)
    except subprocess.TimeoutExpired:
        out(f"[Auto-Calibrate] CLI timed out after {AUTO_CALIBRATE_TIMEOUT}s.")
        return 1
    except (RuntimeError, OSError, ValueError) as e:
        out(f"[Auto-Calibrate] CLI failed ({type(e).__name__}: {e}).")
        return 1

    reply = parse_reply(reply_text)
    if not reply:
        out("[Auto-Calibrate] Agent reply did not contain a valid JSON object.")
        return 1

    candidate_cfg, changed_keys, rationale, problems = apply_proposed_changes(raw_cfg, reply)
    for p in problems:
        out(f"  ! {p}")
    if not changed_keys:
        out("[Auto-Calibrate] Agent proposed no valid config changes.")
        for r in rationale:
            out(f"  • {r}")
        return 0

    out(f"[Auto-Calibrate · Stage 3] Replaying proposed changes ({', '.join(changed_keys)}) across "
        f"{len(labeled)} cached review(s)...")
    proposed_all = metrics_for_analysis(replay_with_config(candidate_cfg, labeled), len(labeled))
    proposed_check = metrics_for_analysis(replay_with_config(candidate_cfg, check), len(check))

    for r in rationale:
        out(f"  • {r}")
    out(f"  Replay results (all {len(labeled)}): {_metrics_line(baseline_all, proposed_all)}")
    if held:
        out(f"  Replay results (holdout {len(held)}): {_metrics_line(baseline_check, proposed_check)}")

    if usage:
        usd = usage.get("usd") if usage.get("usd") is not None else priced_usd(usage)
        tokens = usage["input"] + usage["cached"] + usage["output"]
        out(f"  Used {tokens:,} tokens" + (f" (${usd:.3f})" if usd is not None else ""))

    accepted, reasons = verify_proposal(baseline_all, proposed_all, baseline_check, proposed_check)
    if not accepted:
        out("  ✗ Rejected: " + "; ".join(reasons) + ".")
        return 1
    out("  ✓ Verified: no new APPLY/STRETCH drops, and a gain on the " + ("holdout." if held else "same reviews."))

    target_path = os.path.abspath(CONFIG_PATH if apply_in_place else (output or DEFAULT_CALIBRATED_OUTPUT))
    atomic_write_json(target_path, candidate_cfg, indent=2)
    out(f"\nWrote calibrated config to {target_path}")
    if target_path != os.path.abspath(CONFIG_PATH):
        out(f"Review the diff with `diff -u {os.path.relpath(CONFIG_PATH)} {os.path.relpath(target_path)}` "
            "or pass `--auto-calibrate --apply` to update config.json in place.")
    return 0
