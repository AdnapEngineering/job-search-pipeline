"""Dynamic Antigravity Workflow: Self-Calibrating Rule Tuning Pipeline (Mine -> Parallel Propose -> Replay & Verify).

Run via Antigravity `run_workflow`:
    run_workflow(
        ScriptPath="<repo>/workflows/auto_calibrate_workflow.py",
        Cwd="<repo>",
        Description="Mine cached AI reviews, fan out rule-tuning agents, and verify via replay",
    )

Optional `Args`:
    - "holdout" (float): share of reviews kept from the agents to verify their changes (default 0.3)
    - "apply" (bool): write verified changes directly to config.json instead of config.calibrated.json
    - "output" (str): custom output path for the calibrated config
"""
import json
import os
import subprocess
import sys

# The repo root: this file's parent folder, or the working directory when the runner gives no __file__
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))) if "__file__" in globals() else os.getcwd()
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from job_search_pipeline.calibrate import (
    DEFAULT_CALIBRATED_OUTPUT,
    HOLDOUT_FRACTION,
    _metrics_line,
    analyze,
    apply_proposed_changes,
    calibration_telemetry,
    labeled_reviews,
    metrics_for_analysis,
    replay_with_config,
    split_holdout,
    verify_proposal,
)
from job_search_pipeline.config import CFG, CONFIG_PATH, atomic_write_json, load_json
from job_search_pipeline.review import PROFILE_PATH

WORKFLOW_ARGS = globals().get("args") or {}
phase = globals().get("phase")
log = globals().get("log")
agent = globals().get("agent")
parallel = globals().get("parallel")


async def run_auto_calibrate():
    phase("1. Mine Cached Reviews & Discrepancies")
    labeled, missing = labeled_reviews()
    log(f"Loaded {len(labeled)} labeled cached AI reviews ({missing} without saved JD).")

    if not labeled:
        log("No cached reviews with saved JDs found. Run the pipeline first.")
        return

    train, held = split_holdout(labeled, float(WORKFLOW_ARGS.get("holdout", HOLDOUT_FRACTION)))
    check = held or train
    baseline_metrics = metrics_for_analysis(analyze(labeled), len(labeled))
    baseline_check = metrics_for_analysis(analyze(check), len(check))
    telemetry = calibration_telemetry(analyze(train))  # agents only ever see the training reviews
    log(f"Split: {len(train)} reviews shown to the agents, {len(held)} held out to verify.")
    log(
        f"Baseline (all): {baseline_metrics['apply_drops']} APPLY drops, "
        f"{baseline_metrics['stretch_drops']} STRETCH drops, "
        f"{baseline_metrics['uncaught_skips']} uncaught SKIPs, "
        f"{baseline_metrics['skip_gate_catches']} caught SKIPs."
    )

    profile = ""
    if os.path.exists(PROFILE_PATH):
        with open(PROFILE_PATH, encoding="utf-8") as f:
            profile = f.read().strip()

    raw_cfg = load_json(CONFIG_PATH, default={}) or {}

    phase("2. Parallel Specialist Rule Proposers")
    specialist_tasks = []

    if telemetry["false_negative_gates"] or telemetry["uncaught_skips"]:
        specialist_tasks.append({
            "name": "Skills & Exclusions Specialist",
            "section": "skills_and_scoring",
            "prompt": (
                "You are the Skills & Exclusions Calibration Agent for a job-search pipeline.\n"
                "Analyze the candidate profile, current `skills_and_scoring` config, and calibration telemetry.\n"
                "1. If any `hard_exclude_skills` or `avoid_keywords` gate dropped an APPLY role in "
                "`false_negative_gates`, remove or loosen that skill.\n"
                "2. Inspect `uncaught_skips` for repeated required non-target languages, frameworks, or domain "
                "blockers that do NOT conflict with the candidate profile. Propose additions to "
                "`hard_exclude_skills`, `avoid_keywords`, or `penalized_keywords` (map of term -> 5..15).\n"
                "Return the COMPLETE replacement value for any key you modify in `skills_and_scoring`.\n\n"
                f"CANDIDATE PROFILE:\n{profile}\n\n"
                f"CURRENT skills_and_scoring:\n{json.dumps(CFG.get('skills_and_scoring', {}), indent=2)}\n\n"
                f"CALIBRATION TELEMETRY:\n{json.dumps(telemetry, indent=2)}"
            ),
            "schema": {
                "type": "object",
                "properties": {
                    "rationale": {"type": "array", "items": {"type": "string"}},
                    "skills_and_scoring": {"type": "object"},
                },
                "required": ["rationale", "skills_and_scoring"],
            },
        })

        specialist_tasks.append({
            "name": "Title & Experience Gates Specialist",
            "section": "titles_and_experience",
            "prompt": (
                "You are the Title & Experience Calibration Agent for a job-search pipeline.\n"
                "Analyze the candidate profile, current `titles` and `experience` config, and calibration telemetry.\n"
                "1. If any `seniority`, `title`, or `max_min_yoe` gate dropped an APPLY role in "
                "`false_negative_gates`, loosen that setting.\n"
                "2. Inspect `uncaught_skips` for off-target role titles that passed Stage 3 without penalty, "
                "and add word-bounded lower-case regexes to `exclude_role_patterns` or `exclude_tech_patterns`.\n"
                "Return the COMPLETE replacement value for any key you modify.\n\n"
                f"CANDIDATE PROFILE:\n{profile}\n\n"
                f"CURRENT titles:\n{json.dumps(CFG.get('titles', {}), indent=2)}\n\n"
                f"CURRENT experience:\n{json.dumps(CFG.get('experience', {}), indent=2)}\n\n"
                f"CALIBRATION TELEMETRY:\n{json.dumps(telemetry, indent=2)}"
            ),
            "schema": {
                "type": "object",
                "properties": {
                    "rationale": {"type": "array", "items": {"type": "string"}},
                    "titles": {"type": "object"},
                    "experience": {"type": "object"},
                },
                "required": ["rationale"],
            },
        })

    if not specialist_tasks:
        log("No false-negative gates or uncaught skips to calibrate.")
        return

    proposals = await parallel(
        specialist_tasks,
        lambda task: agent(
            task["prompt"],
            schema=task["schema"],
            role=task["name"],
        ),
    )

    merged_reply = {"rationale": [], "changes": {}}
    for prop in proposals:
        if isinstance(prop, dict):
            merged_reply["rationale"].extend(prop.get("rationale") or [])
            for sec in ("skills_and_scoring", "titles", "experience"):
                if isinstance(prop.get(sec), dict) and prop[sec]:
                    merged_reply["changes"].setdefault(sec, {}).update(prop[sec])

    phase("3. Replay Verification & Unit Test Check")
    candidate_cfg, changed_keys, rationale, problems = apply_proposed_changes(raw_cfg, merged_reply)
    for p in problems:
        log(f"Validation warning: {p}")

    if not changed_keys:
        log("Specialists proposed no config changes.")
        return

    proposed_metrics = metrics_for_analysis(replay_with_config(candidate_cfg, labeled), len(labeled))
    proposed_check = metrics_for_analysis(replay_with_config(candidate_cfg, check), len(check))

    for r in rationale:
        log(f"• {r}")
    log(f"Replay (all {len(labeled)}): {_metrics_line(baseline_metrics, proposed_metrics)}")
    if held:
        log(f"Replay (holdout {len(held)}): {_metrics_line(baseline_check, proposed_check)}")
    accepted, reasons = verify_proposal(baseline_metrics, proposed_metrics, baseline_check, proposed_check)

    test_res = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    tests_ok = test_res.returncode == 0
    log(f"Unit test suite check: {'PASSED' if tests_ok else 'FAILED'}")

    if not accepted:
        log("REJECTED: " + "; ".join(reasons) + ".")
    elif not tests_ok:
        log(f"REJECTED: Unit tests failed:\n{test_res.stderr[-500:]}")
    else:
        apply_in_place = bool(WORKFLOW_ARGS.get("apply"))
        out_path = os.path.abspath(
            CONFIG_PATH if apply_in_place else (WORKFLOW_ARGS.get("output") or DEFAULT_CALIBRATED_OUTPUT)
        )
        atomic_write_json(out_path, candidate_cfg, indent=2)
        log(f"Saved verified calibrated config ({', '.join(changed_keys)}) to {out_path}")
        print(json.dumps({
            "output_path": out_path,
            "changed_keys": changed_keys,
            "rationale": rationale,
            "baseline_metrics": baseline_metrics,
            "proposed_metrics": proposed_metrics,
            "baseline_holdout": baseline_check if held else None,
            "proposed_holdout": proposed_check if held else None,
        }, indent=2))


await run_auto_calibrate()  # noqa: F704
