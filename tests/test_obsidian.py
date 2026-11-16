"""Stage 5 Obsidian output: note contents, never overwriting, blocklisting, the summary note and the setup check."""
import _fixture_config  # noqa: F401  (must come before any pipeline import)

import contextlib
import io
import os
import tempfile
import unittest
from unittest.mock import patch

import job_search_pipeline.blocklist as blocklist
import job_search_pipeline.check as cs
import job_search_pipeline.outputs.obsidian as obsidian
import job_search_pipeline.pipeline as run_pipeline
from job_search_pipeline.config import atomic_write_json


def match(n, score=90, **extra):
    return {"company": f"company {n}", "title": f"Frontend Engineer {n}", "score": score,
            "url": f"https://example.com/jobs/{n}", "location": "Remote", "reason": "APPLY: fits", **extra}


class _Base(unittest.TestCase):
    def setUp(self):
        atomic_write_json(blocklist.BLOCKLIST_PATH, [])
        atomic_write_json(blocklist.ROLE_BLOCKLIST_PATH, [])
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.vault = tmp.name
        self.notes = os.path.join(self.vault, "Job Prospects")
        quiet = contextlib.ExitStack()
        quiet.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.err = quiet.enter_context(contextlib.redirect_stderr(io.StringIO()))
        self.addCleanup(quiet.close)

    def read(self, name):
        with open(os.path.join(self.notes, name), encoding="utf-8") as f:
            return f.read()


class NoteTests(_Base):
    def test_writes_one_note_per_match_with_properties(self):
        job = match(1, posted="2026-11-01T00:00:00", first_seen="2026-11-15T08:00:00", third_party=True,
                    reason='Strong "React" fit: design systems')
        urls = obsidian.sync_jobs_to_obsidian([job, match(2, score=50)], self.vault, min_score=80)
        self.assertEqual(urls, ["https://example.com/jobs/1"])
        self.assertEqual(os.listdir(self.notes), ["Company 1 - Frontend Engineer 1.md"])
        note = self.read("Company 1 - Frontend Engineer 1.md")
        self.assertTrue(note.startswith("---\ncompany: \"Company 1\"\nrole: \"Frontend Engineer 1\"\n"))
        for line in ("match: 90", 'link: "https://example.com/jobs/1"', "date_added: 2026-11-15",
                     "originally_posted: 2026-11-01", "third_party: true", "status: Prospect", "  - job-prospect"):
            self.assertIn(line + "\n", note)
        self.assertIn('Strong "React" fit: design systems', note)  # body text is not escaped

    def test_unsafe_characters_are_removed_from_the_note_name(self):
        name = obsidian.note_name({"company": "a/b: co", "title": 'Engineer [Remote] #1 | "UI"?'})
        self.assertEqual(name, "A B Co - Engineer Remote 1 UI")

    def test_existing_note_is_kept_and_still_counts(self):
        os.makedirs(self.notes)
        path = os.path.join(self.notes, "Company 1 - Frontend Engineer 1.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write("my edits")
        urls = obsidian.sync_jobs_to_obsidian([match(1)], self.vault, min_score=80)
        self.assertEqual(urls, ["https://example.com/jobs/1"])
        self.assertEqual(self.read("Company 1 - Frontend Engineer 1.md"), "my edits")

    def test_blocklisting_is_optional_and_stops_repeats(self):
        obsidian.sync_jobs_to_obsidian([match(1)], self.vault, min_score=80, record_in_blocklist=False)
        self.assertEqual(blocklist.load_blocklist(), set())
        obsidian.sync_jobs_to_obsidian([match(1)], self.vault, min_score=80)
        self.assertEqual(obsidian.sync_jobs_to_obsidian([match(1)], self.vault, min_score=80), [])

    def test_missing_vault_writes_nothing(self):
        missing = os.path.join(self.vault, "nope")
        self.assertEqual(obsidian.sync_jobs_to_obsidian([match(1)], missing, min_score=80), [])
        self.assertFalse(os.path.exists(missing))
        self.assertIn("does not exist", self.err.getvalue())

    def test_summary_note_is_rewritten_each_run(self):
        results = {"top_matches": [match(1)], "total_evaluated": 40, "ai_skipped": [], "manual_reviews": []}
        path = obsidian.log_run_summary_to_obsidian(results, ["u"], min_score=80, vault_path=self.vault,
                                                    history=[{"time": "2026-11-15 07:10", "summary": "old run"}])
        obsidian.log_run_summary_to_obsidian(results, ["u"], min_score=80, vault_path=self.vault,
                                             history=[{"time": "2026-11-16 07:10", "summary": "newer run"}])
        with open(path, encoding="utf-8") as f:
            text = f.read()
        self.assertEqual(os.path.basename(path), "Job Search Run Summary.md")
        self.assertIn("| 40 | 1 | 1 | 1 |", text)
        self.assertIn("[Company 1 — Frontend Engineer 1](https://example.com/jobs/1)", text)
        self.assertIn("newer run", text)
        self.assertNotIn("old run", text)
        self.assertEqual(os.listdir(self.notes), ["Job Search Run Summary.md"])


class PipelineWiringTests(unittest.TestCase):
    def test_flag_overrides_config_and_no_obsidian_wins_over_config(self):
        args = run_pipeline.parse_args(["--obsidian-vault", "~/vault"])
        self.assertEqual(run_pipeline.obsidian_vault(args), os.path.expanduser("~/vault"))
        with patch.object(run_pipeline, "OBSIDIAN_ENABLED", True), patch.object(run_pipeline, "VAULT_PATH", "/v"):
            self.assertEqual(run_pipeline.obsidian_vault(run_pipeline.parse_args([])), "/v")
            self.assertIsNone(run_pipeline.obsidian_vault(run_pipeline.parse_args(["--no-obsidian"])))
        self.assertIsNone(run_pipeline.obsidian_vault(run_pipeline.parse_args([])))


class CheckTests(_Base):
    def run_check(self, cfg, env=None):
        report = cs.Report()
        cs.check_obsidian(report, cfg, env or {})
        return report.results[-1][:2]

    def test_off_by_default(self):
        self.assertEqual(self.run_check({}), (cs.OFF, "Obsidian"))

    def test_enabled_without_a_vault_fails(self):
        self.assertEqual(self.run_check({"obsidian": {"enabled": True}}), (cs.FAIL, "Obsidian"))
        missing = {"obsidian": {"enabled": True, "vault_path": os.path.join(self.vault, "nope")}}
        self.assertEqual(self.run_check(missing), (cs.FAIL, "Obsidian"))

    def test_folder_without_dot_obsidian_warns_and_a_vault_passes(self):
        cfg = {"obsidian": {"enabled": True}}
        self.assertEqual(self.run_check(cfg, {"OBSIDIAN_VAULT": self.vault}), (cs.WARN, "Obsidian"))
        os.mkdir(os.path.join(self.vault, ".obsidian"))
        self.assertEqual(self.run_check(cfg, {"OBSIDIAN_VAULT": self.vault}), (cs.OK, "Obsidian"))


if __name__ == "__main__":
    unittest.main()
