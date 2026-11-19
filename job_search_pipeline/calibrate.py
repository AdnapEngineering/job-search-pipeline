"""`--calibrate`: check the Stage 3 rules against the AI reviews already in the review cache, with no LLM calls.

Every cached review holds the reviewer's grades for a posting the pipeline has saved. Replaying the current
config's keyword score, hard rules and penalties over those postings shows which rules agree with the reviewer,
which would drop roles it rated APPLY, how well the keyword score ranks verdicts (and so where the review floor
belongs), and which reasons for its skips no rule catches yet. That is what lets a small AI budget, or none,
go further: tune the Python rules against grades already paid for.
"""
import collections

from job_search_pipeline.config import CFG, get_path, load_json
from job_search_pipeline.evaluate import DROP_RULES, evaluate_job_traced, score_breakdown
from job_search_pipeline.review import VERDICTS, _rescored
from job_search_pipeline.urls import normalize_url

REVIEW_MIN_SCORE = int(CFG["skills_and_scoring"]["min_review_score"])
BAND = 10
MAX_EXAMPLES = 3
MAX_UNCAUGHT = 15
_OPTIONAL_PREFIX = "Excluded skills offered as optional: "


def labeled_reviews(cache=None, jobs=None):
    """([(job, rescored review result)] for cached reviews whose posting is saved, count of reviews without one)."""
    cache = load_json(get_path("review_cache"), default={}) if cache is None else cache
    if jobs is None:
        jobs = [*(load_json(get_path("filtered_jobs"), default=[]) or []),
                *(load_json(get_path("enriched_jobs"), default=[]) or [])]  # enriched last: it holds the JD
    by_url = {normalize_url(j.get("url", "")): j for j in jobs if isinstance(j, dict) and j.get("url")}
    labeled, missing = [], 0
    for url, entry in (cache or {}).items():
        result = entry.get("result") if isinstance(entry, dict) else None
        if not isinstance(result, dict):
            continue
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
