"""--calibrate and --auto-calibrate: replaying and auto-tuning Stage 3 rules over cached AI reviews."""
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import _fixture_config  # noqa: F401  (must come before any pipeline import)

import job_search_pipeline.calibrate as cal

DESC = (" Build accessible payments SaaS with React, TypeScript, GraphQL, Node.js, SQL and AWS on our"
        " design system. Fully remote in the US." * 12)


def job(title, url, desc=DESC):
    return {"company": "Acme", "title": title, "location": "Remote, US", "remote": True, "url": url,
            "first_seen": "2999-01-01T00:00:00+00:00", "description_ai": desc, "description_full": desc}


JOBS = [job("Frontend Engineer", "https://example.com/1"),
        job("Staff Frontend Engineer", "https://example.com/2"),
        job("Full Stack Engineer", "https://example.com/3")]
CACHE = {
    "https://example.com/1": {"result": {"verdict": "apply"}},
    "https://example.com/2?utm_source=x": {"result": {"verdict": "stretch"}},
    "https://example.com/3": {"result": {"verdict": "skip", "blockers": ["5+ years Python backend"]}},
    "https://example.com/gone": {"result": {"verdict": "skip"}},
    "https://example.com/broken": {"result": "not a dict"},
}


class CalibrateTests(unittest.TestCase):
    def setUp(self):
        self.labeled, self.missing = cal.labeled_reviews(CACHE, JOBS)

    def test_reviews_are_matched_to_saved_postings_by_canonical_url(self):
        self.assertEqual([(j["url"], r["verdict"]) for j, r in self.labeled],
                         [("https://example.com/1", "apply"), ("https://example.com/2", "stretch"),
                          ("https://example.com/3", "skip")])
        self.assertEqual(self.missing, 1)

    def test_triage_skips_are_not_calibration_labels(self):
        cache = dict(CACHE, **{"https://example.com/1": {"result": {"verdict": "skip", "triaged_out": True}}})
        labeled, missing = cal.labeled_reviews(cache, JOBS)
        self.assertNotIn("https://example.com/1", [j["url"] for j, _ in labeled])
        self.assertEqual(len(labeled), 2)
        self.assertEqual(missing, 1)

    def test_archived_postings_are_labels_after_leaving_the_job_files(self):
        from job_search_pipeline.config import atomic_write_json, get_path
        paths = {name: get_path(name) for name in ("review_cache", "reviewed_postings", "filtered_jobs",
                                                    "enriched_jobs")}
        try:
            atomic_write_json(paths["review_cache"], CACHE)
            atomic_write_json(paths["reviewed_postings"], {j["url"]: j for j in JOBS})
            atomic_write_json(paths["filtered_jobs"], [])
            atomic_write_json(paths["enriched_jobs"], [])
            labeled, missing = cal.labeled_reviews()
        finally:
            for path in paths.values():
                if os.path.exists(path):
                    os.remove(path)
        self.assertEqual(len(labeled), 3)
        self.assertEqual(missing, 1)

    def test_analysis_flags_rules_that_drop_reviewed_roles_and_uncaught_skips(self):
        a = cal.analyze(self.labeled)
        self.assertEqual(sum(len(v) for v in a["bands"].values()), 3)
        self.assertEqual([(v, j["title"]) for v, j, _ in a["gates"]["seniority"]],
                         [("stretch", "Staff Frontend Engineer")])
        self.assertEqual([(j["title"], b) for j, b in a["uncaught"]],
                         [("Full Stack Engineer", ["5+ years Python backend"])])
        self.assertIsNotNone(a["lowest_apply"])

    def test_optional_skill_penalties_are_tallied_per_skill(self):
        desc = DESC + " Nice to have: Ruby or PHP experience."
        a = cal.analyze([(job("Frontend Engineer", "https://example.com/4", desc), {"verdict": "skip"})])
        optional = sorted(k for k in a["penalties"] if k.startswith(cal._OPTIONAL_PREFIX))
        self.assertTrue(optional)
        self.assertTrue(all(", " not in k[len(cal._OPTIONAL_PREFIX):] for k in optional))

    def test_report_runs_and_handles_an_empty_cache(self):
        lines = []
        cal.report(self.labeled, self.missing, out=lines.append)
        text = "\n".join(lines)
        self.assertIn("3 cached AI review(s)", text)
        self.assertIn("STRETCH: Acme · Staff Frontend Engineer", text)
        self.assertIn("5+ years Python backend", text)
        lines.clear()
        cal.report([], 0, out=lines.append)
        self.assertIn("Nothing to calibrate against yet", "\n".join(lines))

    def _auto(self, proposal, labeled=None, **kwargs):
        """(rc, output lines, saved config or None, prompt sent to the agent) for one auto_calibrate run."""
        reply = (json.dumps(proposal), {"input": 500, "cached": 0, "output": 100, "usd": 0.01})
        with tempfile.TemporaryDirectory() as tmp:
            out_path = os.path.join(tmp, "config.calibrated.json")
            lines = []
            with (patch.object(cal, "labeled_reviews", return_value=(labeled or self.labeled, 0)),
                  patch.object(cal, "_cli_path", return_value="/bin/fake-cli"),
                  patch.object(cal, "_cli_response", return_value=reply) as cli):
                rc = cal.auto_calibrate(output=out_path, out=lines.append, **kwargs)
            saved = None
            if os.path.exists(out_path):
                with open(out_path, encoding="utf-8") as f:
                    saved = json.load(f)
        prompt = cli.call_args.args[3] if cli.call_args else ""
        return rc, "\n".join(lines), saved, prompt

    @staticmethod
    def _skills(*extra):
        return {"changes": {"skills_and_scoring": {
            "hard_exclude_skills": ["c++", "rust", "golang", "c#", ".net", "ruby", *extra]}},
            "rationale": ["test change"]}

    def test_auto_calibrate_writes_a_change_that_catches_a_skip(self):
        labeled = [(j, r) if r["verdict"] != "skip" else (job(j["title"], j["url"], DESC + PY), r)
                   for j, r in self.labeled]
        rc, text, saved, _ = self._auto(self._skills("django"), labeled=labeled)
        self.assertEqual(rc, 0, text)
        self.assertIn("django", saved["skills_and_scoring"]["hard_exclude_skills"])
        self.assertIn("Uncaught SKIPs 1 -> 0", text)
        self.assertIn("Verified", text)

    def test_auto_calibrate_rejects_a_change_with_no_gain(self):
        # "python" is not in any posting, so nothing new is caught: the old gate wrote this anyway
        rc, text, saved, _ = self._auto(self._skills("python"))
        self.assertEqual(rc, 1)
        self.assertIsNone(saved)
        self.assertIn("no gain on the check set", text)

    def test_auto_calibrate_rejects_changes_that_increase_apply_drops(self):
        rc, text, saved, _ = self._auto(self._skills("react"))
        self.assertEqual(rc, 1)
        self.assertIsNone(saved)
        self.assertIn("Rejected: would drop more APPLY roles", text)

    def test_auto_calibrate_rejects_a_bad_holdout_fraction(self):
        rc, text, _, _ = self._auto(self._skills("django"), holdout=1)
        self.assertEqual(rc, 2)
        self.assertIn("--holdout must be", text)

    def test_auto_calibrate_hides_the_holdout_and_rejects_overfit_rules(self):
        labeled = HOLDOUT_SET
        train, held = cal.split_holdout(labeled)
        held_skip = next(j for j, r in held if r["verdict"] == "skip")
        train_skips = [j for j, r in train if r["verdict"] == "skip"]

        # A rule naming only the training skips' titles fixes train but not the held-out skip
        overfit = {"changes": {"titles": {"exclude_role_patterns": [
            rf"\b{j['title'].lower()}\b" for j in train_skips]}}, "rationale": ["overfit"]}
        rc, text, saved, prompt = self._auto(overfit, labeled=labeled)
        self.assertNotIn(held_skip["title"], prompt)
        self.assertTrue(all(j["title"] in prompt for j in train_skips))
        self.assertEqual(rc, 1, text)
        self.assertIsNone(saved)
        self.assertIn("Replay results (holdout", text)
        self.assertIn("no gain on the check set", text)

        # The general rule (the skill every skip asks for) also catches the held-out skip
        rc, text, saved, _ = self._auto(self._skills("django"), labeled=labeled)
        self.assertEqual(rc, 0, text)
        self.assertIn("Verified: no new APPLY/STRETCH drops, and a gain on the holdout", text)


PY = " Requires 5+ years of Python and Django on the backend."
HOLDOUT_SET = (
    [(job(f"Platform Integrations Engineer {n}", f"https://example.com/s{n}", DESC + PY),
      {"verdict": "skip", "blockers": ["Python/Django backend"]}) for n in ("alpha", "bravo", "charlie", "delta")]
    + [(job("Frontend Engineer", f"https://example.com/a{n}"), {"verdict": "apply"}) for n in range(3)]
    + [(job("Senior Frontend Engineer", f"https://example.com/t{n}"), {"verdict": "stretch"}) for n in range(3)]
)


class HoldoutTests(unittest.TestCase):
    def test_split_is_per_verdict_and_stable(self):
        train, held = cal.split_holdout(HOLDOUT_SET)
        self.assertEqual(len(train) + len(held), len(HOLDOUT_SET))
        self.assertEqual(sorted(r["verdict"] for _, r in held), ["apply", "skip", "stretch"])
        _again_train, again_held = cal.split_holdout(list(reversed(HOLDOUT_SET)))
        self.assertEqual({j["url"] for j, _ in held}, {j["url"] for j, _ in again_held})

    def test_single_review_verdicts_stay_in_train_and_zero_disables(self):
        one_each = HOLDOUT_SET[:1] + HOLDOUT_SET[4:5] + HOLDOUT_SET[7:8]
        train, held = cal.split_holdout(one_each)
        self.assertEqual(({j['url'] for j, _ in train}, held), ({j['url'] for j, _ in one_each}, []))
        self.assertEqual(cal.split_holdout(HOLDOUT_SET, 0), (HOLDOUT_SET, []))

    def test_gate(self):
        def m(apply=0, stretch=0, uncaught=0):
            return {"apply_drops": apply, "stretch_drops": stretch, "uncaught_skips": uncaught}
        self.assertEqual(cal.verify_proposal(m(), m(), m(uncaught=2), m(uncaught=1)), (True, []))
        self.assertEqual(cal.verify_proposal(m(stretch=1), m(), m(stretch=1), m()), (True, []))
        ok, why = cal.verify_proposal(m(), m(stretch=1), m(uncaught=2), m(uncaught=0))
        self.assertFalse(ok)
        self.assertIn("more STRETCH roles", why[0])
        ok, why = cal.verify_proposal(m(), m(), m(uncaught=1), m(uncaught=1))
        self.assertIn("no gain", why[0])
        ok, why = cal.verify_proposal(m(), m(), m(), m())
        self.assertIn("no gain can be measured", why[0])

if __name__ == "__main__":
    unittest.main()
