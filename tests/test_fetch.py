"""Stage 1: LinkedIn/Indeed remote evidence and stack detection, and surviving a feed outage."""
import _fixture_config  # noqa: F401  (must come before any pipeline import)

import asyncio
import contextlib
import io
import json
import unittest
from unittest.mock import patch

import aiohttp

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
        self.assertEqual(counts, {"onsite_dropped": 1})


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


if __name__ == "__main__":
    unittest.main()
