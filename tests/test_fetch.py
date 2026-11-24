"""Stage 1: LinkedIn/Indeed remote evidence and stack detection, and surviving a feed outage."""
import asyncio
import contextlib
import io
import json
import logging
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import _fixture_config  # noqa: F401  (must come before any pipeline import)
import aiohttp
import pandas as pd
from jobspy.linkedin import LinkedIn

import job_search_pipeline.pipeline as run_pipeline
import job_search_pipeline.sources.filter as ff
import job_search_pipeline.sources.linkedin_indeed as fi


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


class BlockCooldownTests(unittest.TestCase):
    NOW = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)

    def test_each_block_in_a_row_doubles_the_wait_up_to_the_cap(self):
        state, hours = {}, []
        for _ in range(4):
            until = fi.record_block_outcome("linkedin", True, state, now=self.NOW)
            hours.append((until - self.NOW).total_seconds() / 3600)
        base = fi.BLOCK_COOLDOWN_HOURS
        self.assertEqual(hours, [base, base * 2, base * 4, base * 4])
        self.assertEqual(fi.cooldown_until("linkedin", state, now=self.NOW), until)
        self.assertIsNone(fi.cooldown_until("linkedin", state, now=until + timedelta(seconds=1)))

    def test_a_clean_run_clears_the_strikes(self):
        state = {}
        fi.record_block_outcome("linkedin", True, state, now=self.NOW)
        self.assertIsNone(fi.record_block_outcome("linkedin", False, state, now=self.NOW))
        self.assertEqual(state, {})
        self.assertIsNone(fi.cooldown_until("linkedin", {"linkedin": {"until": "garbage"}}, now=self.NOW))

    def run_fetch(self, state, blocked_sites=()):
        calls = []

        def fake_scrape(**kwargs):
            site = kwargs["site_name"][0]
            calls.append(site)
            if site in blocked_sites:
                logging.getLogger(f"jobspy.{site}").error("response status code 429 (blocked: use a proxy)")
            return pd.DataFrame()

        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = f"{tmp}/throttle.json"
            if state is not None:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(state, f)
            stats = {}
            searches = [("linkedin", "United States", " remote", 10), ("indeed", "Remote", "", 10)]
            with patch.object(fi, "scrape_jobs", fake_scrape), patch.object(fi, "SEARCHES", searches), \
                    patch.object(fi, "THROTTLE_STATE_PATH", path), patch.object(fi, "LINKEDIN_PAUSE_SECONDS", 0), \
                    patch.object(fi, "_install_linkedin_details_cache", lambda: (lambda: 0)), \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                fi.fetch_indeed_jobs(stats=stats, terms=["A", "B"])
            saved = fi.load_json(path, default=None)
        return calls, stats, saved

    def test_a_block_starts_a_cooldown_that_is_saved_and_reported(self):
        calls, stats, saved = self.run_fetch(None, blocked_sites=("linkedin",))
        self.assertEqual(calls.count("linkedin"), 1)
        self.assertEqual(saved["linkedin"]["strikes"], 1)
        self.assertEqual(stats["linkedin"]["cooldown_until"], saved["linkedin"]["until"])
        with patch.object(run_pipeline, "load_json", lambda path: stats):
            warnings = run_pipeline.source_health()["warnings"]
        self.assertTrue(any(w.startswith("LinkedIn rate-limited: skipped 1 remaining queries; paused until")
                            for w in warnings), warnings)

    def test_a_cooling_site_is_not_queried_and_the_others_still_are(self):
        until = (datetime.now(UTC) + timedelta(hours=5)).isoformat()
        state = {"linkedin": {"strikes": 1, "until": until}}
        calls, stats, saved = self.run_fetch(state)
        self.assertEqual(calls, ["indeed", "indeed"])
        self.assertEqual(stats["linkedin"], {"raw": 0, "kept": 0, "cooldown_until": until, "cooldown_queries": 2})
        self.assertEqual(saved, state)
        with patch.object(run_pipeline, "load_json", lambda path: stats):
            health = run_pipeline.source_health()
        self.assertIn("LinkedIn 0 (cooling down)", health["line"])
        self.assertTrue(any(w.startswith("LinkedIn cooling down after a block: skipped all 2 queries, next try after")
                            for w in health["warnings"]), health["warnings"])
        self.assertFalse(any(w.startswith("LinkedIn returned no rows") for w in health["warnings"]))

    def test_an_expired_cooldown_is_retried_and_cleared_when_clean(self):
        state = {"linkedin": {"strikes": 2, "until": "2026-01-01T00:00:00+00:00"}}
        calls, _, saved = self.run_fetch(state)
        self.assertEqual(calls.count("linkedin"), 2)
        self.assertEqual(saved, {})


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
                      TimeoutError()):
            with self.subTest(error=type(error).__name__):
                saved, stats, err = self.run_main(error)
                self.assertEqual([j["url"] for j in saved], ["https://www.linkedin.com/jobs/view/42"])
                self.assertIn(type(error).__name__, stats["github_feed"]["error"])
                self.assertIn("GitHub feed fetch failed", err)

    def test_feed_failure_is_a_source_warning(self):
        _, _stats, _ = self.run_main(aiohttp.ClientError("503"))
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
