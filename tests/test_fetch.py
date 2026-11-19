"""Stage 1: LinkedIn/Indeed remote evidence and stack detection, and surviving a feed outage."""
import _fixture_config  # noqa: F401  (must come before any pipeline import)

import asyncio
import contextlib
import io
import json
import logging
import unittest
from unittest.mock import patch

import aiohttp
import pandas as pd

from jobspy.linkedin import LinkedIn

import job_search_pipeline.sources.filter as ff
import job_search_pipeline.sources.linkedin_indeed as fi
import job_search_pipeline.pipeline as run_pipeline


class RemoteEvidenceTests(unittest.TestCase):
    def test_negated_or_conditional_office_wording_no_longer_drops_a_remote_job(self):
        cases = [
            "This is a fully remote role. We do not offer visa sponsorship or relocation assistance at this time.",
            "Remote - US. Employees located near an office are expected to work from that office three days per week.",
            "Candidates in Chicago will work hybrid 2 days per week. Candidates elsewhere in the U.S. will work remotely.",
            "100% remote. In-office perks are available if you visit our hub.",
        ]
        for desc in cases:
            with self.subTest(desc=desc[:50]):
                self.assertEqual(fi.remote_evidence("Austin, TX", desc), "unverified")
                self.assertTrue(fi.is_valid_jobspy_location("Austin, TX", desc))

    def test_flat_onsite_requirement_still_drops(self):
        cases = [
            "This role is 100% onsite in Hawthorne, CA. Remote work is common elsewhere in the company.",
            "This role requires you to be onsite. Remote and/or hybrid work will not be considered.",
            "Candidates must relocate to Austin. We are remote-friendly on Fridays.",
        ]
        for desc in cases:
            with self.subTest(desc=desc[:50]):
                self.assertEqual(fi.remote_evidence("Austin, TX", desc), "onsite")
                self.assertFalse(fi.is_valid_jobspy_location("Austin, TX", desc))

    def test_clean_remote_unverified_and_no_evidence(self):
        self.assertEqual(fi.remote_evidence("Austin, TX", "This is a fully remote position."), "remote")
        self.assertEqual(fi.remote_evidence("Austin, TX", "Some remote collaboration with other teams."), "unverified")
        self.assertEqual(fi.remote_evidence("Austin, TX", "Join our Austin team."), "none")
        # An office hint with no remote wording at all is still not kept
        self.assertFalse(fi.is_valid_jobspy_location("Austin, TX", "Hybrid work schedule in our Austin office."))

    def test_local_metro_is_kept_whatever_the_jd_says(self):
        self.assertTrue(fi.is_valid_jobspy_location("Denver, CO", "This role is 100% onsite."))

    def test_onsite_drops_are_counted(self):
        counts = {}
        row = {"job_url": "https://www.linkedin.com/jobs/view/1", "title": "Frontend Engineer", "company": "Acme",
               "location": "Austin, TX", "is_remote": True, "site": "linkedin",
               "description": "React and TypeScript. This role is 100% onsite in Austin."}
        self.assertIsNone(fi._row_to_job(row, counts))
        self.assertEqual(counts, {"onsite_dropped": 1, "dropped": {"location": 1}})


class StackDetectionTests(unittest.TestCase):
    def test_stack_holds_whole_terms_only(self):
        desc = ("Build UIs in JavaScript and ReactJS on PostgreSQL, respecting privacy laws. "
                "We ship a SaaS product with a design system.")
        score, matched = fi.score_job("Frontend Engineer", desc)
        self.assertGreater(score, 0)  # substrings still count toward the loose keep/drop gate
        self.assertIn("Javascript", matched)
        self.assertIn("Design system", matched)
        for inflated in ("Java", "Sql", "Aws", "React"):
            self.assertNotIn(inflated, matched)

    def test_real_mentions_are_detected(self):
        _, matched = fi.score_job("Engineer", "Java, SQL and AWS, plus React.js and Node.")
        self.assertLessEqual({"Java", "Sql", "Aws", "React", "Node"}, set(matched))


class ExclusionGateTests(unittest.TestCase):
    def test_aliases_and_optional_mentions_do_not_drop_a_posting(self):
        desc = "We build with React and TypeScript. Ruby on Rails or similar (Express) is a plus." * 3
        self.assertGreater(fi.score_job("Frontend Engineer", desc)[0], 0)

    def test_two_required_excluded_skills_still_drop_it(self):
        desc = "Requirements: production experience with Rust and with Golang services. React."
        self.assertEqual(fi.score_job("Frontend Engineer", desc), (0, []))


class LinkedInRateLimitTests(unittest.TestCase):
    def test_a_429_stops_the_remaining_linkedin_queries(self):
        calls = []

        def fake_scrape(**kwargs):
            calls.append(kwargs["search_term"])
            if len(calls) == 2:
                logging.getLogger(LinkedIn.__module__).error("response status code 429 (blocked: use a proxy)")
            return pd.DataFrame()

        terms = ["A", "B", "C", "D"]
        searches = [("linkedin", "United States", " remote", 10), ("linkedin", "Portland, OR", "", 10)]
        with patch.object(fi, "scrape_jobs", fake_scrape), patch.object(fi, "SEARCHES", searches), \
                patch.object(fi, "LINKEDIN_PAUSE_SECONDS", 0), \
                patch.object(fi, "_install_linkedin_details_cache", lambda: (lambda: 0)), \
                contextlib.redirect_stderr(io.StringIO()):
            _, stats = fi._scrape_site("linkedin", 72, terms)
        self.assertEqual(calls, ["A remote", "B remote"])
        self.assertEqual((stats["rate_limited"], stats["skipped_queries"]), (1, 6))

    def test_a_glassdoor_block_stops_its_remaining_queries(self):
        calls = []

        def fake_scrape(**kwargs):
            calls.append(kwargs["search_term"])
            logging.getLogger("jobspy.glassdoor").error("response status code 403 (blocked: use a proxy)")
            return pd.DataFrame()

        with patch.object(fi, "scrape_jobs", fake_scrape), \
                patch.object(fi, "SEARCHES", [("glassdoor", "United States", " remote", 10)]), \
                patch.object(fi, "LINKEDIN_PAUSE_SECONDS", 0), contextlib.redirect_stderr(io.StringIO()):
            _, stats = fi._scrape_site("glassdoor", 72, ["A", "B", "C"])
        self.assertEqual(calls, ["A remote"])
        self.assertEqual((stats["rate_limited"], stats["skipped_queries"]), (1, 2))


class JobspySiteTests(unittest.TestCase):
    def test_sites_come_from_searches_and_unknown_ones_are_skipped(self):
        searches = [("indeed", "Remote", "", 10), ("glassdoor", "United States", " remote", 10),
                    ("zip_recruiter", "United States", "", 10), ("indeed", "Portland, OR", "", 10)]
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(fi.searched_sites(searches), ["indeed", "glassdoor"])
        self.assertIn("zip_recruiter", err.getvalue())

    def test_glassdoor_rows_are_aggregator_links_and_keep_their_site(self):
        from job_search_pipeline.urls import is_aggregator_url
        self.assertTrue(is_aggregator_url("https://www.glassdoor.com/job-listing/j?jl=1010329103280"))
        row = {"site": "glassdoor", "job_url": "https://www.glassdoor.com/job-listing/j?jl=1", "title": "Frontend Engineer",
               "company": "Acme", "location": "Remote", "is_remote": True,
               "description": "Fully remote in the US. Build React and TypeScript apps. " * 5}
        job = fi._row_to_job(pd.Series(row))
        self.assertEqual(job["ats"], "Glassdoor")


class FeedOutageTests(unittest.TestCase):
    def run_main(self, feed_error):
        linkedin_job = {"title": "Frontend Engineer", "company": "Acme", "location": "Remote", "remote": True,
                        "url": "https://www.linkedin.com/jobs/view/42", "description_ai": "x",
                        "first_seen": "2999-01-01T00:00:00+00:00"}

        async def broken_feed(stats):
            raise feed_error

        def fake_jobspy(stats):
            stats["linkedin"] = {"raw": 1, "kept": 1}
            return [dict(linkedin_job)]

        with patch.object(ff, "fetch_github_feed", broken_feed), patch.object(ff, "fetch_jobspy", fake_jobspy), \
                patch.object(ff, "fetch_boards", lambda stats: []), \
                patch.object(ff, "is_recent", lambda first_seen: True), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
            asyncio.run(ff.main())
        with open(ff.FILTERED_JOBS_PATH, encoding="utf-8") as f:
            saved = json.load(f)
        with open(ff.SOURCE_STATS_PATH, encoding="utf-8") as f:
            stats = json.load(f)
        return saved, stats, err.getvalue()

    def test_manifest_failure_keeps_the_other_sources(self):
        for error in (json.JSONDecodeError("Expecting value", "", 0), aiohttp.ClientError("503"),
                      asyncio.TimeoutError()):
            with self.subTest(error=type(error).__name__):
                saved, stats, err = self.run_main(error)
                self.assertEqual([j["url"] for j in saved], ["https://www.linkedin.com/jobs/view/42"])
                self.assertIn(type(error).__name__, stats["github_feed"]["error"])
                self.assertIn("GitHub feed fetch failed", err)

    def test_feed_failure_is_a_source_warning(self):
        _, stats, _ = self.run_main(aiohttp.ClientError("503"))
        health = run_pipeline.source_health(ff.SOURCE_STATS_PATH)
        self.assertTrue(any("GitHub feed fetch failed" in w for w in health["warnings"]))


class LinkedInDetailsCacheTests(unittest.TestCase):
    """The cache wraps a private jobspy method; these fail when a jobspy release renames or reshapes it."""

    def test_cache_round_trips_a_job_page_through_json(self):
        import tempfile
        from jobspy.linkedin import LinkedIn
        from jobspy.model import CompensationInterval, JobType
        page = {"description": "Build React apps", "job_type": [JobType.FULL_TIME],
                "interval": CompensationInterval.YEARLY, "min_amount": 150000.0, "max_amount": 180000.0}
        calls = []
        method = "_fetch_details" if hasattr(LinkedIn, "_fetch_details") else "_get_job_details"
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(fi, "LI_DETAILS_CACHE_PATH", f"{tmp}/cache.json"), \
                patch.object(LinkedIn, method, lambda self, job_id: calls.append(job_id) or dict(page)):
            fake = getattr(LinkedIn, method)
            finish = fi._install_linkedin_details_cache()
            self.assertIsNot(getattr(LinkedIn, method), fake, "cache did not wrap jobspy's job-page fetch")
            getattr(LinkedIn, method)(None, "1")
            self.assertEqual(finish(), 0)
            with open(f"{tmp}/cache.json") as f:
                self.assertEqual(json.load(f)["1"]["details"]["interval"], "YEARLY")
            finish = fi._install_linkedin_details_cache()
            cached = getattr(LinkedIn, method)(None, "1")
            self.assertEqual(finish(), 1)
        self.assertEqual(calls, ["1"])
        self.assertEqual(cached, page)


if __name__ == "__main__":
    unittest.main()
