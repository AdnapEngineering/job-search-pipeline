"""Job-board sources: salary parsing (it feeds the pay floor) and the Hacker News "Who is hiring?" parser."""
import _fixture_config  # noqa: F401  (must come before any pipeline import)

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import job_search_pipeline.sources.job_boards as jb


class SalaryTests(unittest.TestCase):
    def test_annual_ranges(self):
        for text, want in {
            "$150 - 210K": (150000, 210000),       # one k covers both ends
            "$150K - $210K": (150000, 210000),
            "Salary $140,000 to $180,000 plus equity": (140000, 180000),
            "$150k": (150000, 150000),
        }.items():
            with self.subTest(text=text):
                self.assertEqual(jb.parse_usd_salary(text), {"min": want[0], "max": want[1]})

    def test_hourly_rates_are_annualized(self):
        self.assertEqual(jb.parse_usd_salary("$120-160/hr"), {"min": 249600, "max": 332800})
        self.assertEqual(jb.parse_usd_salary("$60 - $80 per hour"), {"min": 124800, "max": 166400})

    def test_no_salary(self):
        self.assertIsNone(jb.parse_usd_salary("Competitive pay"))
        self.assertIsNone(jb.parse_usd_salary(""))


class RemoteBoardLocationTests(unittest.TestCase):
    def test_foreign_only_locations_are_rejected(self):
        self.assertTrue(jb._is_us_eligible("remote (us)"))
        self.assertTrue(jb._is_us_eligible("remote, worldwide"))
        self.assertTrue(jb._is_us_eligible("remote (us or europe)"))
        self.assertFalse(jb._is_us_eligible("remote - europe"))
        self.assertFalse(jb._is_us_eligible("berlin, germany"))


def _iso(days_ago):
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat().replace("+00:00", "Z")


def comment(cid, text, days_ago=0):
    return {"id": cid, "created_at": _iso(days_ago), "text": text}


class HackerNewsTests(unittest.TestCase):
    """Only the two HTTP calls are faked; the header parsing and the shared filters run for real."""

    def fetch(self, comments, thread_age_days=2):
        def fake_get_json(url, timeout=30):
            if url == jb.HN_SEARCH_URL:
                return {"hits": [
                    {"title": "Ask HN: Who wants to be hired? (November 2026)", "objectID": "1",
                     "created_at": _iso(1)},
                    {"title": "Ask HN: Who is hiring? (November 2026)", "objectID": "99",
                     "created_at": _iso(thread_age_days)},
                ]}
            self.assertEqual(url, jb.HN_ITEM_URL.format("99"))  # the hiring thread, not the first hit
            return {"children": comments}
        with patch.object(jb, "_get_json", fake_get_json):
            return {j["company"]: j for j in jb.fetch_hn_whos_hiring()}

    def test_eligible_posts_are_parsed(self):
        jobs = self.fetch([
            comment(1, "Acme Corp | Senior Frontend Engineer | Remote (US) | Full-time | $150K - $190K"
                       "<p>We build React apps."),
            comment(2, "Hooli | Frontend Engineer | Boulder, CO | Full-time<p>React."),
            comment(3, "Vandelay | Remote (US) | Full-time<p>We are hiring a Frontend Engineer to own our UI."),
        ])
        self.assertEqual(set(jobs), {"Acme Corp", "Hooli", "Vandelay"})
        acme = jobs["Acme Corp"]
        self.assertEqual((acme["title"], acme["location"], acme["remote"]),
                         ("Senior Frontend Engineer", "Remote (US)", True))
        self.assertEqual(acme["salary"], {"min": 150000, "max": 190000})
        self.assertEqual(acme["url"], "https://news.ycombinator.com/item?id=1")
        self.assertIn("We build React apps.", acme["description_full"])
        self.assertEqual((jobs["Hooli"]["location"], jobs["Hooli"]["remote"]), ("Boulder, CO", False))  # local metro
        self.assertEqual(jobs["Vandelay"]["title"], "Frontend Engineer")  # role found in the body

    def test_ineligible_posts_are_dropped(self):
        jobs = self.fetch([
            comment(1, "Globex | Frontend Engineer | Remote (EU only) | Full-time<p>React."),
            comment(2, "Initech | Frontend Engineer | Remote | Contract<p>React."),
            comment(3, "Umbrella | Account Executive | Remote (US)<p>Sales."),
            comment(4, "Old Co | Frontend Engineer | Remote (US)<p>React.", days_ago=30),
            comment(5, "Massive Dynamic | Frontend Engineer | NYC, onsite<p>React."),
            comment(6, "No header separators at all<p>Frontend Engineer, remote."),
            {"id": 7, "created_at": _iso(0)},  # deleted comment
        ])
        self.assertEqual(jobs, {})

    def test_stale_thread_is_ignored(self):
        post = comment(1, "Acme | Frontend Engineer | Remote (US)<p>React.")
        self.assertEqual(self.fetch([post], thread_age_days=jb.HN_THREAD_MAX_AGE_DAYS + 1), {})


if __name__ == "__main__":
    unittest.main()
