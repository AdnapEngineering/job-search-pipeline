"""Glassdoor links re-pointed at the employer's Greenhouse / Lever / Ashby posting before sync."""
import _fixture_config  # noqa: F401  (must come before any pipeline import)

import contextlib
import io
import unittest

import job_search_pipeline.blocklist as blocklist
import job_search_pipeline.outputs.notion as sync
from job_search_pipeline.config import atomic_write_json, load_json
from job_search_pipeline.direct_links import (Resolver, board_from_url, known_boards, match_posting,
                                              resolve_direct_links, slug_guesses)

GD = "https://www.glassdoor.com/job-listing/j?jl=101"


def boards(table):
    """A fetch stub: {(ats, slug): [(title, url)]}; boards not listed don't exist."""
    calls = []

    def fetch(ats, slug):
        calls.append((ats, slug))
        return table.get((ats, slug))
    fetch.calls = calls
    return fetch


def job(**extra):
    return {"company": "Acme Robotics, Inc.", "title": "Senior Frontend Engineer", "score": 90, "url": GD, **extra}


def run(jobs, fetch, known=None, enriched=()):
    with contextlib.redirect_stdout(io.StringIO()):
        return resolve_direct_links(jobs, 80, resolver=Resolver(known, fetch), enriched=list(enriched))


class BoardFromUrl(unittest.TestCase):
    def test_ats_urls(self):
        self.assertEqual(board_from_url("https://job-boards.greenhouse.io/boxinc/jobs/8328785"), ("greenhouse", "boxinc"))
        self.assertEqual(board_from_url("https://boards.greenhouse.io/embed/job_app?for=Acme&token=1"),
                         ("greenhouse", "acme"))
        self.assertEqual(board_from_url("https://jobs.lever.co/acme/abc-123"), ("lever", "acme"))
        self.assertEqual(board_from_url("https://jobs.ashbyhq.com/netboxlabs/b3d5"), ("ashby", "netboxlabs"))
        self.assertIsNone(board_from_url(GD))
        self.assertIsNone(board_from_url("https://careers.upstart.com/jobs?gh_jid=1"))


class SlugGuesses(unittest.TestCase):
    def test_name_suffix_and_website(self):
        slugs = dict(slug_guesses("Upstart Network, Inc.", "https://www.upstart.com"))
        self.assertTrue(slugs["upstartnetwork"])
        self.assertTrue(slugs["upstart"])  # the website's label is trusted
        self.assertFalse(slugs["upstartinc"])

    def test_first_word_alone_is_untrusted(self):
        self.assertFalse(dict(slug_guesses("Acme Robotics"))["acme"])


class MatchPosting(unittest.TestCase):
    def test_title_ignores_case_punctuation_and_remote_tags(self):
        postings = [("Senior Front-End Engineer", "u1"), ("Backend Engineer", "u2")]
        self.assertIsNone(match_posting({"title": "Senior Frontend Engineer"}, postings))  # front-end != frontend
        self.assertEqual(match_posting({"title": "Sr. Front-End Engineer (Remote)"}, postings), "u1")
        self.assertEqual(match_posting({"title": "Senior Front End Engineer - Remote, US"}, postings), "u1")

    def test_jd_heading_when_the_board_title_differs(self):
        j = {"title": "Software Developer", "description_full": "**Full-Stack Engineer, Sharing**\n\nAbout the role"}
        self.assertEqual(match_posting(j, [("Full-Stack Engineer, Sharing", "u1")]), "u1")

    def test_ambiguous_title_is_no_match(self):
        self.assertIsNone(match_posting({"title": "Software Engineer"},
                                        [("Software Engineer", "u1"), ("Software Engineer", "u2")]))


class Resolve(unittest.TestCase):
    def test_swaps_to_the_guessed_board_and_keeps_the_board_link(self):
        jobs = [job()]
        fetch = boards({("greenhouse", "acmerobotics"): [("Senior Frontend Engineer", "https://gh/acme/1")]})
        self.assertEqual(run(jobs, fetch), 1)
        self.assertEqual(jobs[0]["url"], "https://gh/acme/1")
        self.assertEqual(jobs[0]["board_url"], GD)

    def test_known_board_from_saved_direct_links(self):
        enriched = [{"company": "Acme Robotics", "url": "https://jobs.ashbyhq.com/acme-r/xyz", "title": "Other"}]
        fetch = boards({("ashby", "acme-r"): [("Senior Frontend Engineer", "https://ashby/acme-r/1")]})
        jobs = [job()]
        run(jobs, fetch, known=known_boards(enriched))
        self.assertEqual(jobs[0]["url"], "https://ashby/acme-r/1")
        self.assertEqual(fetch.calls[0], ("ashby", "acme-r"))  # known boards are tried first

    def test_company_board_without_the_role_gets_a_note(self):
        jobs = [job()]
        run(jobs, boards({("greenhouse", "acmerobotics"): [("Account Executive", "u")]}))
        self.assertEqual(jobs[0]["url"], GD)
        self.assertIn("Greenhouse board", jobs[0]["direct_link_note"])

    def test_untrusted_guess_without_the_role_gets_no_note(self):
        jobs = [job()]
        run(jobs, boards({("lever", "acme"): [("Account Executive", "u")]}))  # some other "Acme"
        self.assertNotIn("direct_link_note", jobs[0])

    def test_jd_and_website_come_from_the_enriched_job(self):
        enriched = [{"url": GD, "company_site": "acme.dev", "description_full": "Platform UI Engineer\n..."}]
        jobs = [job(title="Software Developer")]
        run(jobs, boards({("lever", "acme"): [("Platform UI Engineer", "https://lever/acme/1")]}), enriched=enriched)
        self.assertEqual(jobs[0]["url"], "https://lever/acme/1")

    def test_only_resolve_hosts_at_or_above_min_score(self):
        fetch = boards({})
        run([job(score=70), job(url="https://www.linkedin.com/jobs/view/1"),
             job(url="https://jobs.lever.co/acme/1")], fetch)
        self.assertEqual(fetch.calls, [])

    def test_a_failing_lookup_never_blocks(self):
        def boom(ats, slug):
            raise RuntimeError("network down")
        jobs = [job()]
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(run(jobs, boom), 0)
        self.assertEqual(jobs[0]["url"], GD)


class SyncSide(unittest.TestCase):
    def test_note_reaches_notion_and_board_link_is_blocklisted(self):
        atomic_write_json(blocklist.BLOCKLIST_PATH, [])
        atomic_write_json(blocklist.ROLE_BLOCKLIST_PATH, [])
        j = job(url="https://gh/acme/1", board_url=GD, reason="APPLY: fits", location="Remote",
                direct_link_note="No posting with this title on the company's Greenhouse board; check it is still open.")
        why = sync._build_notion_payload(j, "db")["properties"]["Why It Matches"]["rich_text"][0]["text"]["content"]
        self.assertIn("Greenhouse board", why)
        with contextlib.redirect_stdout(io.StringIO()):
            blocklist.record_exported([j])
        saved = load_json(blocklist.BLOCKLIST_PATH)
        self.assertIn(GD, saved)
        self.assertIn("https://gh/acme/1", saved)


if __name__ == "__main__":
    unittest.main()
