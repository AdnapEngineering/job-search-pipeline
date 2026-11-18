import _fixture_config  # noqa: F401  (must come before any pipeline import)

import json
import os
import unittest

from job_search_pipeline import config
from job_search_pipeline.defaults import DEFAULTS, PERSONAL_KEYS

EXAMPLE = os.path.join(_fixture_config.ROOT, "config.example.json")


class DefaultsMatchTemplateTest(unittest.TestCase):
    """config.example.json and defaults.py must not drift apart (see the defaults.py docstring)."""

    @classmethod
    def setUpClass(cls):
        with open(EXAMPLE, encoding="utf-8") as f:
            cls.example = json.load(f)

    def test_same_sections_and_keys(self):
        self.assertEqual(list(self.example), list(DEFAULTS))
        for section, keys in DEFAULTS.items():
            with self.subTest(section=section):
                self.assertEqual(set(self.example[section]), set(keys))

    def test_general_settings_show_their_default(self):
        for section, keys in DEFAULTS.items():
            for key, default in keys.items():
                if (section, key) not in PERSONAL_KEYS:
                    with self.subTest(setting=f"{section}.{key}"):
                        self.assertEqual(self.example[section][key], default)

    def test_personal_keys_exist(self):
        for section, key in PERSONAL_KEYS:
            self.assertIn(key, DEFAULTS.get(section, {}), f"{section}.{key}")


class MergeDefaultsTest(unittest.TestCase):
    def test_missing_settings_use_defaults(self):
        merged = config.merge_defaults({"experience": {"max_min_yoe": 5}})
        self.assertEqual(merged["experience"]["max_min_yoe"], 5)
        self.assertEqual(merged["experience"]["top_yoe_penalty"], DEFAULTS["experience"]["top_yoe_penalty"])
        self.assertEqual(merged["llm_review"], DEFAULTS["llm_review"])

    def test_list_and_map_values_replace_whole(self):
        merged = config.merge_defaults({"llm_review": {"components": {"hard_reqs": 4}}})
        self.assertEqual(merged["llm_review"]["components"], {"hard_reqs": 4})

    def test_does_not_alias_defaults(self):
        merged = config.merge_defaults({})
        merged["sources"]["search_terms"].append("x")
        self.assertNotIn("x", DEFAULTS["sources"]["search_terms"])

    def test_non_object_section_stops_the_run(self):
        path = os.path.join(_fixture_config.DATA_DIR, "bad_section.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"llm_review": []}, f)
        with self.assertRaises(SystemExit):
            config.load_config(path)


if __name__ == "__main__":
    unittest.main()
