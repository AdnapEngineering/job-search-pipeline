"""Canonical URLs: the same posting must normalize to one key across sources, runs and the blocklist."""
import unittest

import _fixture_config  # noqa: F401  (must come before any pipeline import)

from job_search_pipeline.urls import (
    is_aggregator_url,
    normalize_url,
    normalize_url_list,
)


class NormalizeUrlTests(unittest.TestCase):
    def test_spellings_of_one_posting_share_a_key(self):
        key = "https://jobs.lever.co/acme/abc-123?a=1&b=2"
        for url in ("HTTPS://Jobs.Lever.co:443/acme/abc-123/?b=2&a=1",
                    "https://jobs.lever.co/acme/abc-123?a=1&b=2#apply",
                    "  https://jobs.lever.co/acme/abc-123/?utm_source=linkedin&a=1&b=2&gclid=xyz  "):
            with self.subTest(url=url):
                self.assertEqual(normalize_url(url), key)

    def test_tracking_params_go_and_real_ones_stay(self):
        self.assertEqual(normalize_url("https://example.com/jobs/1?ref=hn&trk=abc&TrackingId=9&id=5"),
                         "https://example.com/jobs/1?id=5")

    def test_gh_jid_is_dropped_only_when_the_path_already_names_the_job(self):
        self.assertEqual(normalize_url("https://boards.greenhouse.io/acme/jobs/4753567005?gh_jid=4753567005&gh_src=li"),
                         "https://boards.greenhouse.io/acme/jobs/4753567005")
        # On a company career site the gh_jid is the only thing that identifies the posting
        self.assertEqual(normalize_url("https://careers.acme.com/jobs?gh_jid=123"),
                         "https://careers.acme.com/jobs?gh_jid=123")
        self.assertNotEqual(normalize_url("https://careers.acme.com/jobs?gh_jid=123"),
                            normalize_url("https://careers.acme.com/jobs?gh_jid=456"))

    def test_default_ports_and_root_path(self):
        self.assertEqual(normalize_url("http://example.com:80"), "http://example.com/")
        self.assertEqual(normalize_url("https://example.com:8443/x/"), "https://example.com:8443/x")

    def test_empty_and_non_string_inputs(self):
        self.assertEqual(normalize_url(""), "")
        self.assertEqual(normalize_url(None), "")

    def test_list_is_canonical_sorted_and_deduplicated(self):
        self.assertEqual(normalize_url_list(["https://b.com/2", "https://a.com/1/", "https://a.com/1?utm_source=x", ""]),
                         ["https://a.com/1", "https://b.com/2"])


class AggregatorTests(unittest.TestCase):
    def test_aggregators_versus_company_ats(self):
        self.assertTrue(is_aggregator_url("https://www.linkedin.com/jobs/view/1"))
        self.assertFalse(is_aggregator_url("https://jobs.lever.co/acme/1"))
        self.assertFalse(is_aggregator_url(None))


if __name__ == "__main__":
    unittest.main()
