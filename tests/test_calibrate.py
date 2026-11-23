"""--calibrate and --auto-calibrate: replaying and auto-tuning Stage 3 rules over cached AI reviews."""
import _fixture_config  # noqa: F401  (must come before any pipeline import)

import json
import os
import tempfile
import unittest
from unittest.mock import patch

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

    def test_auto_calibrate_proposes_replays_and_writes_calibrated_config(self):
        proposal = json.dumps({
            "rationale": ["Add python backend requirement to hard_exclude_skills to catch uncaught skip."],
            "changes": {
                "skills_and_scoring": {
                    "hard_exclude_skills": ["c++", "rust", "golang", "c#", ".net", "ruby", "python"]
                }
            }
        })
        with tempfile.TemporaryDirectory() as tmp:
            out_path = os.path.join(tmp, "config.calibrated.json")
            lines = []
            with (patch.object(cal, "labeled_reviews", return_value=(self.labeled, 0)),
                  patch.object(cal, "_cli_path", return_value="/bin/fake-cli"),
                  patch.object(cal, "_cli_response", return_value=(proposal, {"input": 500, "cached": 0, "output": 100, "usd": 0.01}))):
                rc = cal.auto_calibrate(output=out_path, out=lines.append)
            self.assertEqual(rc, 0)
            self.assertTrue(os.path.exists(out_path))
            with open(out_path, encoding="utf-8") as f:
                saved = json.load(f)
            self.assertIn("python", saved["skills_and_scoring"]["hard_exclude_skills"])
            text = "\n".join(lines)
            self.assertIn("Replay results:", text)
            self.assertIn("Wrote calibrated config", text)

    def test_auto_calibrate_rejects_changes_that_increase_apply_drops(self):
        bad_proposal = json.dumps({
            "rationale": ["Drop React roles (regression)."],
            "changes": {
                "skills_and_scoring": {
                    "hard_exclude_skills": ["react"]
                }
            }
        })
        with tempfile.TemporaryDirectory() as tmp:
            out_path = os.path.join(tmp, "config.calibrated.json")
            lines = []
            with (patch.object(cal, "labeled_reviews", return_value=(self.labeled, 0)),
                  patch.object(cal, "_cli_path", return_value="/bin/fake-cli"),
                  patch.object(cal, "_cli_response", return_value=(bad_proposal, None))):
                rc = cal.auto_calibrate(output=out_path, out=lines.append)
            self.assertEqual(rc, 1)
            self.assertFalse(os.path.exists(out_path))
            self.assertIn("Rejected: proposed rules would drop more APPLY roles", "\n".join(lines))


if __name__ == "__main__":
    unittest.main()
