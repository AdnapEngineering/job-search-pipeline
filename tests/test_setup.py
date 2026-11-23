"""Guided setup creates usable searches without overwriting existing settings."""
import _fixture_config  # noqa: F401

import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from job_search_pipeline import setup


class GuidedSetupTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, "config.json")

    def run_setup(self, answers):
        with patch.dict(os.environ, {"JOB_PIPELINE_CONFIG": self.path}), \
                patch.object(setup, "load_env_files"), patch("builtins.input", side_effect=answers):
            return setup.main()

    def test_remote_setup_is_usable_by_filters_and_scorer(self):
        self.assertEqual(self.run_setup([
            "Product Manager", "planning, planning, SQL", "planning, SQL, budgeting",
            "", "not a number", "$80,000", "",
        ]), 0)
        with open(self.path, encoding="utf-8") as f:
            config = json.load(f)
        self.assertEqual(config["skills_and_scoring"]["core_strengths"], ["planning", "sql", "budgeting"])
        self.assertEqual(config["compensation"]["min_salary"], 80000)
        self.assertEqual(config["experience"]["max_min_yoe"], 99)
        self.assertEqual(config["location"], {})
        env = dict(os.environ, JOB_PIPELINE_CONFIG=self.path)
        result = subprocess.run(
            [sys.executable, "-c",
             "from job_search_pipeline.sources.filter import is_valid_title; "
             "from job_search_pipeline.sources.linkedin_indeed import is_valid_jobspy_title; "
             "from job_search_pipeline.evaluate import evaluate_job_traced; "
             "assert is_valid_title('Senior Product Manager'); "
             "assert is_valid_jobspy_title('Senior Product Manager'); "
             "assert not is_valid_title('Software Engineer'); "
             "job = {'company': 'Example', 'title': 'Product Manager', 'location': 'Remote', "
             "'description_ai': 'Remote work. Planning, SQL and budgeting. ' * 4}; "
             "result, rejection = evaluate_job_traced(job); "
             "assert result and result['score'] >= 55, rejection"],
            env=env, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_local_setup_escapes_input_and_adds_searches(self):
        self.assertEqual(self.run_setup([
            "C++ Developer", "C++, SQL, Git", "St. Louis, MO", "St. Charles", "", "7",
        ]), 0)
        with open(self.path, encoding="utf-8") as f:
            config = json.load(f)
        self.assertEqual(config["location"]["local_metro_patterns"], [r"st\.\ louis", r"st\.\ charles"])
        self.assertEqual(config["sources"]["searches"][2][1], "St. Louis, MO")
        self.assertEqual(config["experience"]["max_min_yoe"], 7)
        env = dict(os.environ, JOB_PIPELINE_CONFIG=self.path)
        result = subprocess.run(
            [sys.executable, "-c",
             "from job_search_pipeline.sources.filter import is_valid_title, is_local_metro; "
             "from job_search_pipeline.evaluate import jd_location_ok; "
             "assert is_valid_title('C++ Developer'); assert not is_valid_title('CXX Developer'); "
             "assert is_local_metro('St. Louis, MO'); "
             "assert jd_location_ok('This position is onsite in St. Louis.')"],
            env=env, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_existing_config_is_never_replaced(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("existing settings")
        self.assertEqual(self.run_setup([]), 1)
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(f.read(), "existing settings")

    def test_cancel_does_not_create_config(self):
        self.assertEqual(self.run_setup(["Product Manager", EOFError()]), 1)
        self.assertFalse(os.path.exists(self.path))


if __name__ == "__main__":
    unittest.main()
