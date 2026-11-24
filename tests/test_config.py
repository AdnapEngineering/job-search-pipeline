"""Config loading: your own config.json is laid over the built-in defaults; the template is never read."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import _fixture_config  # noqa: F401  (must come before any pipeline import)

import job_search_pipeline.config as pipeline_config
import job_search_pipeline.outputs.notion as sync
from job_search_pipeline.defaults import DEFAULTS
from job_search_pipeline.init_config import STARTER_KEYS, starter_config


class ConfigLoadTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.own = os.path.join(tmp.name, "config.json")

    def _load(self, path):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            return pipeline_config.load_config(path), err.getvalue()

    def test_missing_config_is_just_the_defaults_never_the_template(self):
        cfg, notice = self._load(self.own)
        self.assertEqual(cfg, DEFAULTS)
        self.assertEqual(notice, "")

    def test_own_config_is_laid_over_the_defaults(self):
        with open(self.own, "w", encoding="utf-8") as f:
            json.dump({"compensation": {"min_salary": 150000}}, f)
        cfg, notice = self._load(self.own)
        self.assertEqual(cfg["compensation"]["min_salary"], 150000)
        self.assertEqual(cfg["compensation"]["pay_midpoint_penalty"], DEFAULTS["compensation"]["pay_midpoint_penalty"])
        self.assertEqual(cfg["sources"], DEFAULTS["sources"])
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

    def test_saving_the_summary_block_keeps_the_other_settings(self):
        with open(self.own, "w", encoding="utf-8") as f:
            json.dump({"compensation": {"min_salary": 100000}, "notion": {"database_id": "db"}}, f)
        with patch.object(sync, "CONFIG_PATH", self.own):
            sync._remember_summary_block("block-1")
        with open(self.own, encoding="utf-8") as f:
            saved = json.load(f)
        self.assertEqual(saved, {"compensation": {"min_salary": 100000},
                                 "notion": {"database_id": "db", "summary_block_id": "block-1"}})


class ShippedTemplateTest(unittest.TestCase):
    def test_shipped_template_is_valid_json(self):
        path = os.path.join(pipeline_config.ROOT_DIR, "config.example.json")
        with open(path, encoding="utf-8") as f:
            self.assertIsInstance(json.load(f), dict)

    def test_init_config_starts_from_template_placeholders(self):
        with open(os.path.join(pipeline_config.ROOT_DIR, "config.example.json"), encoding="utf-8") as f:
            template = json.load(f)
        starter = starter_config(template)
        for section, keys in STARTER_KEYS.items():
            for key in keys:
                # same value as the template, so --check flags whatever the user leaves unedited
                self.assertEqual(starter[section][key], template[section][key], f"{section}.{key}")


if __name__ == "__main__":
    unittest.main()
