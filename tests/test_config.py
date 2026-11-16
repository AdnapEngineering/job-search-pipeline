"""Config loading: your own config.json wins, and the tracked template stands in until it exists."""
import _fixture_config  # noqa: F401  (must come before any pipeline import)

import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import pipeline_config
import sync_notion_db as sync


class ConfigTemplateTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.own = os.path.join(tmp.name, "config.json")
        self.template = os.path.join(tmp.name, "config.example.json")
        with open(self.template, "w", encoding="utf-8") as f:
            json.dump({"compensation": {"min_salary": 100000}, "notion": {"database_id": "db"}}, f)
        for name, value in (("DEFAULT_CONFIG_PATH", self.own), ("EXAMPLE_CONFIG_PATH", self.template)):
            patcher = patch.object(pipeline_config, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _load(self, path):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            return pipeline_config.load_config(path), err.getvalue()

    def test_template_is_used_until_config_json_exists(self):
        cfg, notice = self._load(self.own)
        self.assertEqual(cfg["compensation"]["min_salary"], 100000)
        self.assertIn("cp config.example.json config.json", notice)

    def test_own_config_wins_over_template(self):
        with open(self.own, "w", encoding="utf-8") as f:
            json.dump({"compensation": {"min_salary": 150000}}, f)
        cfg, notice = self._load(self.own)
        self.assertEqual(cfg, {"compensation": {"min_salary": 150000}})
        self.assertEqual(notice, "")

    def test_explicit_config_path_never_falls_back_to_template(self):
        elsewhere = os.path.join(os.path.dirname(self.own), "other-career.json")
        cfg, notice = self._load(elsewhere)
        self.assertEqual(cfg, {})
        self.assertEqual(notice, "")

    def test_malformed_config_stops_the_run_with_the_location(self):
        with open(self.own, "w", encoding="utf-8") as f:
            f.write('{\n  "compensation": {"min_salary": 150000,}\n}\n')
        with self.assertRaises(SystemExit) as ctx:
            self._load(self.own)
        self.assertIn(self.own, str(ctx.exception.code))
        self.assertIn("line 2", str(ctx.exception.code))

    def test_non_object_config_stops_the_run(self):
        with open(self.own, "w", encoding="utf-8") as f:
            json.dump(["not", "an", "object"], f)
        with self.assertRaises(SystemExit) as ctx:
            self._load(self.own)
        self.assertIn("JSON object", str(ctx.exception.code))

    def test_saving_the_summary_block_copies_the_template_and_leaves_it_untouched(self):
        with open(self.template, encoding="utf-8") as f:
            before = f.read()
        with patch.object(sync, "CONFIG_PATH", self.own):
            sync._remember_summary_block("block-1")
        with open(self.own, encoding="utf-8") as f:
            saved = json.load(f)
        self.assertEqual(saved["notion"], {"database_id": "db", "summary_block_id": "block-1"})
        self.assertEqual(saved["compensation"], {"min_salary": 100000})
        with open(self.template, encoding="utf-8") as f:
            self.assertEqual(f.read(), before)


class ShippedTemplateTest(unittest.TestCase):
    def test_shipped_template_is_valid_json(self):
        path = os.path.join(pipeline_config.SCRIPT_DIR, "config.example.json")
        with open(path, encoding="utf-8") as f:
            self.assertIsInstance(json.load(f), dict)


if __name__ == "__main__":
    unittest.main()
