"""Setup check: config typos and placeholders, env-file loading, and the Notion property list."""
import _fixture_config  # noqa: F401  (must come before any pipeline import)

import os
import tempfile
import unittest

import job_search_pipeline.check as cs
import job_search_pipeline.outputs.notion as sync

TEMPLATE = {
    "sources": {"lookback_days": 3, "search_terms": ["Target Role Title 1"]},
    "location": {"home_states": [], "local_display_name": "Your Metro Area, ST"},
    "notion": {"database_id": ""},
}


class ConfigKeyIssuesTest(unittest.TestCase):
    def test_known_keys_pass(self):
        self.assertEqual(cs.config_key_issues({"sources": {"lookback_days": 5}, "notion": {}}, TEMPLATE), [])

    def test_typos_are_flagged_with_a_suggestion(self):
        issues = cs.config_key_issues({"sourcs": {}, "location": {"home_state": ["CO"]}}, TEMPLATE)
        self.assertEqual(len(issues), 2)
        self.assertIn("unknown key sourcs (did you mean sources?)", issues[0])
        self.assertIn("unknown key location.home_state (did you mean location.home_states?)", issues[1])

    def test_legacy_keys_still_read_are_not_flagged(self):
        cfg = {"notify_url": "https://ntfy.sh/x", "location": {"foreign_countries": ["oz"]}}
        self.assertEqual(cs.config_key_issues(cfg, TEMPLATE), [])

    def test_free_form_values_below_the_second_level_are_not_checked(self):
        template = {"skills_and_scoring": {"focus_keywords": {"example": 10}}}
        cfg = {"skills_and_scoring": {"focus_keywords": {"kubernetes": 15}}}
        self.assertEqual(cs.config_key_issues(cfg, template), [])


class PlaceholderIssuesTest(unittest.TestCase):
    def test_unchanged_placeholders_are_listed(self):
        cfg = {"sources": {"search_terms": ["Target Role Title 1"]},
               "location": {"local_display_name": "Denver, CO"}}
        self.assertEqual(cs.placeholder_issues(cfg, TEMPLATE), ["sources.search_terms"])

    def test_left_out_keys_are_not_placeholders(self):
        self.assertEqual(cs.placeholder_issues({}, TEMPLATE), [])


class EnvFileTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name

    def _write(self, name, text):
        path = os.path.join(self.dir, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def test_parses_shell_style_lines(self):
        path = self._write(".env", '# comment\nNOTION_TOKEN="ntn_abc"\nexport NOTIFY_URL=\'https://ntfy.sh/t\'\n\nBARE=1\n')
        self.assertEqual(cs.read_env_file(path),
                         {"NOTION_TOKEN": "ntn_abc", "NOTIFY_URL": "https://ntfy.sh/t", "BARE": "1"})

    def test_later_files_win_like_the_cron_wrapper(self):
        global_env = self._write("global.env", "NOTION_TOKEN=global\nNOTION_DATABASE_ID=db1\n")
        local_env = self._write(".env", "NOTION_TOKEN=local\n")
        env = cs.effective_env({"NOTION_TOKEN": "process", "HOME": "/h"}, [global_env, local_env])
        self.assertEqual(env["NOTION_TOKEN"], "local")
        self.assertEqual(env["NOTION_DATABASE_ID"], "db1")
        self.assertEqual(env["HOME"], "/h")

    def test_missing_files_are_skipped(self):
        self.assertEqual(cs.effective_env({}, [os.path.join(self.dir, "nope.env")]), {})


class NotionPropertiesTest(unittest.TestCase):
    def test_check_list_matches_what_the_sync_writes(self):
        job = {"company": "Acme", "title": "Role", "url": "https://x", "score": 90, "posted": "2026-11-01"}
        written = sync._build_notion_payload(job, "db")["properties"]
        expected = {**sync.NOTION_PROPERTIES, **sync.OPTIONAL_NOTION_PROPERTIES}
        self.assertEqual(set(written), set(expected))
        for name, kind in expected.items():
            self.assertIn(kind, written[name], name)


if __name__ == "__main__":
    unittest.main()
