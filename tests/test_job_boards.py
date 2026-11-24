"""Job-board sources: salary parsing (it feeds the pay floor), the Hacker News "Who is hiring?" parser,
and the Himalayas and We Work Remotely fetchers."""
import unittest
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from unittest.mock import patch

import _fixture_config  # noqa: F401  (must come before any pipeline import)

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
    return (datetime.now(UTC) - timedelta(days=days_ago)).isoformat().replace("+00:00", "Z")


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


def hima(title, days_ago=0, **extra):
    item = {"title": title, "companyName": "Acme", "employmentType": "Full Time",
            "locationRestrictions": ["United States"], "currency": "USD", "minSalary": None, "maxSalary": None,
            "salaryPeriod": "annual", "description": "<p>React and TypeScript.</p>",
            "pubDate": int((datetime.now(UTC) - timedelta(days=days_ago)).timestamp()),
            "guid": f"https://himalayas.app/companies/acme/jobs/{title.lower().replace(' ', '-')}"}
    item.update(extra)
    return item


class HimalayasTests(unittest.TestCase):
    """Only the HTTP call is faked; location, recency, salary and title filters run for real."""

    def fetch(self, pages, terms=("Frontend Engineer",)):
        calls = []

        def fake_get_json(url, timeout=30):
            calls.append(url)
            page = int(url.rsplit("page=", 1)[1])
            return {"jobs": pages[page - 1] if page <= len(pages) else []}
        with patch.object(jb, "_get_json", fake_get_json), patch.object(jb, "HIMALAYAS_PAUSE_SECONDS", 0):
            return jb.fetch_himalayas(list(terms)), calls

    def test_eligible_hits_are_kept_with_salary(self):
        jobs, _ = self.fetch([[
            hima("Senior Frontend Engineer", minSalary=150000, maxSalary=190000),
            hima("Frontend Engineer", salaryPeriod="hourly", minSalary=80, maxSalary=100,
                 locationRestrictions=[]),
        ]])
        by_title = {j["title"]: j for j in jobs}
        self.assertEqual(by_title["Senior Frontend Engineer"]["salary"], {"min": 150000, "max": 190000})
        self.assertEqual(by_title["Frontend Engineer"]["salary"], {"min": 166400, "max": 208000})
        self.assertEqual(by_title["Frontend Engineer"]["location"], "Remote")
        self.assertEqual(by_title["Senior Frontend Engineer"]["ats"], "Himalayas")
        self.assertIn("React and TypeScript.", by_title["Senior Frontend Engineer"]["description_full"])

    def test_ineligible_hits_are_dropped(self):
        jobs, _ = self.fetch([[
            hima("Frontend Engineer", days_ago=30),
            hima("Frontend Engineer II", employmentType="Contractor"),
            hima("Senior Frontend Engineer", locationRestrictions=["Germany"]),
            hima("Account Executive"),
        ]])
        self.assertEqual(jobs, [])

    def test_paging_stops_once_postings_are_older_than_the_lookback(self):
        fresh = [hima(f"Frontend Engineer {i}") for i in range(20)]
        stale = [hima(f"Senior Frontend Engineer {i}", days_ago=30) for i in range(20)]
        _, calls = self.fetch([fresh, fresh, stale, fresh])
        self.assertEqual(len(calls), 3)

    def test_a_posting_found_by_two_terms_is_kept_once(self):
        jobs, calls = self.fetch([[hima("Senior Frontend Engineer")]], terms=("Frontend Engineer", "Frontend"))
        self.assertEqual((len(jobs), len(calls)), (1, 2))


def wwr_item(title, region="USA Only", days_ago=0, job_type="Full-Time"):
    when = format_datetime(datetime.now(UTC) - timedelta(days=days_ago))
    slug = title.lower().replace(" ", "-").replace(":", "")
    return (f"<item><title>{title}</title><region>{region}</region><country></country><type>{job_type}</type>"
            f"<description>&lt;p&gt;React.&lt;/p&gt;</description><pubDate>{when}</pubDate>"
            f"<link>https://weworkremotely.com/remote-jobs/{slug}</link></item>")


class WeWorkRemotelyTests(unittest.TestCase):
    def fetch(self, items):
        rss = f"<rss><channel>{''.join(items)}</channel></rss>"
        with patch.object(jb, "_get_text", lambda url, timeout=30: rss), \
                patch.object(jb, "WWR_FEEDS", ["https://example.test/a.rss", "https://example.test/b.rss"]):
            return {j["company"]: j for j in jb.fetch_wwr()}

    def test_us_eligible_full_time_posts_are_kept_once(self):
        jobs = self.fetch([wwr_item("Acme: Senior Frontend Engineer"),
                           wwr_item("Hooli: Frontend Engineer", region="Anywhere in the World"),
                           wwr_item("Initech: Frontend Engineer", region="North America Only")])
        self.assertEqual(set(jobs), {"Acme", "Hooli", "Initech"})  # each feed served the same items: no dupes
        acme = jobs["Acme"]
        self.assertEqual((acme["title"], acme["location"], acme["ats"]),
                         ("Senior Frontend Engineer", "Remote - USA Only", "We Work Remotely"))
        self.assertEqual(acme["description_full"].strip(), "React.")

    def test_other_regions_stale_and_contract_posts_are_dropped(self):
        jobs = self.fetch([wwr_item("Globex: Frontend Engineer", region="Asia Only"),
                           wwr_item("Umbrella: Frontend Engineer", region="Europe Only"),
                           wwr_item("Old Co: Frontend Engineer", days_ago=30),
                           wwr_item("Gig Co: Frontend Engineer", job_type="Contract"),
                           wwr_item("No company separator")])
        self.assertEqual(jobs, {})


if __name__ == "__main__":
    unittest.main()
