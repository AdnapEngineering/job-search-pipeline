"""Stage 5 text export: appended runs, the score floor, and blocklisting so later runs add only new matches."""
import _fixture_config  # noqa: F401  (must come before any pipeline import)

import contextlib
import io
import os
import tempfile
import unittest

import job_search_pipeline.blocklist as blocklist
import job_search_pipeline.outputs.text_export as text_export
from job_search_pipeline.config import atomic_write_json


def match(n, score=90, **extra):
    return {"company": f"company {n}", "title": f"Frontend Engineer {n}", "score": score,
            "url": f"https://example.com/jobs/{n}", "location": "Remote", "reason": "APPLY: fits", **extra}


class TextExportTests(unittest.TestCase):
    def setUp(self):
        atomic_write_json(blocklist.BLOCKLIST_PATH, [])
        atomic_write_json(blocklist.ROLE_BLOCKLIST_PATH, [])
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, "out", "matches.txt")
        quiet = contextlib.ExitStack()
        quiet.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.addCleanup(quiet.close)

    def read(self):
        with open(self.path, encoding="utf-8") as f:
            return f.read()

    def test_qualified_matches_are_written_with_their_details(self):
        urls = text_export.export_jobs_to_text([match(1, posted="2026-11-01"), match(2, score=60)],
                                               path=self.path, min_score=80)
        self.assertEqual(urls, ["https://example.com/jobs/1"])
        text = self.read()
        self.assertIn("1 new match(es) (min score 80%)", text)
        self.assertIn("[90%] Company 1 — Frontend Engineer 1\n", text)
        for line in ("Location: Remote", "Posted: 2026-11-01", "Why: APPLY: fits", "Link: https://example.com/jobs/1"):
            self.assertIn("    " + line + "\n", text)
        self.assertNotIn("Frontend Engineer 2", text)

    def test_empty_fields_are_left_out(self):
        self.assertEqual(text_export.format_job({"score": 85, "company": None, "title": ""}),
                         "[85%] Unknown — Unknown Role")

    def test_later_runs_append_only_new_matches(self):
        text_export.export_jobs_to_text([match(1)], path=self.path, min_score=80)
        self.assertEqual(text_export.export_jobs_to_text([match(1), match(2)], path=self.path, min_score=80),
                         ["https://example.com/jobs/2"])
        text = self.read()
        self.assertEqual(text.count("===== Job Search Run"), 2)
        self.assertEqual(text.count("Company 1 — Frontend Engineer 1"), 1)

    def test_without_blocklisting_the_same_match_is_exported_again(self):
        for _ in range(2):
            text_export.export_jobs_to_text([match(1)], path=self.path, min_score=80, record_in_blocklist=False)
        self.assertEqual(blocklist.load_blocklist(), set())
        self.assertEqual(self.read().count("Company 1 — Frontend Engineer 1"), 2)

    def test_nothing_new_writes_nothing(self):
        self.assertEqual(text_export.export_jobs_to_text([match(1, score=50)], path=self.path, min_score=80), [])
        self.assertFalse(os.path.exists(self.path))


if __name__ == "__main__":
    unittest.main()
