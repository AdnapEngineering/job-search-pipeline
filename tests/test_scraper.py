"""Stage 2: closed-posting detection on redirects, and ATS API failures that are reported, not swallowed."""
import _fixture_config  # noqa: F401  (must come before any pipeline import)

import asyncio
import contextlib
import io
import unittest
from types import SimpleNamespace
from urllib.parse import urlparse

import aiohttp

import job_search_pipeline.scrape as bs


class RequirementsWindowTests(unittest.TestCase):
    INTRO = "About the company. " * 200

    def window(self, heading, after=""):
        body = "\n* 4+ years of React and TypeScript.\n" + "* Ship product features.\n" * 20
        return bs.extract_requirements(self.INTRO + "\n" + heading + body + after)

    def test_bold_and_curly_apostrophe_headings_start_the_window(self):
        for heading in ("**Required Qualifications**", "What you\u2019ll bring", "**What We\u2019re Looking For**",
                        "This role could be a fit if you have:", "**Must\\-Have Skills**"):
            with self.subTest(heading=heading):
                self.assertTrue(self.window(heading).startswith(heading.strip()))

    def test_a_preferred_heading_does_not_start_the_window(self):
        self.assertFalse(self.window("**Preferred Qualifications**").startswith("**Preferred"))

    def test_the_window_stops_at_the_perks(self):
        text = self.window("**Requirements**", "\n**Why Join Us**\nGrow into distributed systems.\n")
        self.assertIn("React and TypeScript", text)
        self.assertNotIn("distributed systems", text)


class RedirectTests(unittest.TestCase):
    def test_redirect_to_a_reissued_posting_is_not_expired(self):
        # Some boards re-issue a live posting under a new id and forward the old link to it
        self.assertFalse(bs._redirected_to_board("/companies/acme/16835869",
                                                 "/companies/acme/16897589-founding-engineer"))

    def test_redirect_up_to_the_board_is_expired(self):
        self.assertTrue(bs._redirected_to_board("/acme/jobs/4753567005", "/acme"))
        self.assertTrue(bs._redirected_to_board("/acme/jobs/4753567005", "/"))

    def test_no_redirect_or_same_id_is_not_expired(self):
        self.assertFalse(bs._redirected_to_board("/acme/jobs/123", None))
        self.assertFalse(bs._redirected_to_board("/acme/jobs/123", "/en/acme/jobs/123-engineer"))


class _Response:
    def __init__(self, status=200, payload=None, error=None):
        self.status, self._payload, self._error = status, payload, error

    async def __aenter__(self):
        if self._error:
            raise self._error
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self, content_type=None):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _Session:
    def __init__(self, response):
        self._response = response

    def get(self, url, **kwargs):
        return self._response


class GetJsonTests(unittest.TestCase):
    URL = "https://api.example-ats.com/postings/acme/1"

    def call(self, response):
        bs._api_failures.clear()
        with contextlib.redirect_stderr(io.StringIO()) as err:
            result = asyncio.run(bs._get_json(_Session(response), self.URL))
        return result, err.getvalue(), dict(bs._api_failures)

    def test_success_and_missing_posting_are_quiet(self):
        self.assertEqual(self.call(_Response(200, {"content": "x"})), ({"content": "x"}, "", {}))
        self.assertEqual(self.call(_Response(404)), (None, "", {}))
        self.assertEqual(self.call(_Response(200, ["not", "an", "object"])), (None, "", {}))

    def test_failures_are_logged_and_counted(self):
        cases = [(_Response(503), "HTTP 503"), (_Response(error=asyncio.TimeoutError()), "timeout"),
                 (_Response(error=aiohttp.ClientError("boom")), "ClientError"),
                 (_Response(200, ValueError("bad json")), "ValueError")]
        for response, why in cases:
            with self.subTest(why=why):
                result, err, failures = self.call(response)
                self.assertIsNone(result)
                self.assertIn(why, err)
                self.assertIn(self.URL, err)
                self.assertEqual(failures, {"api.example-ats.com": 1})

    def test_unexpected_errors_are_raised(self):
        with self.assertRaises(KeyError):
            self.call(_Response(error=KeyError("bug")))


LONG_JD = "<p>Build accessible product features in React and TypeScript with our design team.</p>" * 4


class HtmlTests(unittest.TestCase):
    def test_clean_html_drops_chrome_and_keeps_line_breaks(self):
        text = bs.clean_html("<nav>Menu</nav><script>track()</script><h2>Role</h2><ul><li>React</li><li>CSS</li></ul>"
                             "<footer>Legal</footer>")
        self.assertEqual(text.split(), ["Role", "React", "CSS"])
        self.assertIn("React\n", text)
        self.assertNotIn("track", text)

    def test_clean_html_unescapes_escaped_markup(self):
        self.assertEqual(bs.clean_html("&lt;p&gt;Hello &amp;amp; welcome&lt;/p&gt;"), "Hello & welcome")
        self.assertEqual(bs.clean_html(""), "")

    def test_description_prefers_the_job_description_block(self):
        page = ("<html><body><div class='sidebar'>" + "Other openings. " * 30 + "</div>"
                "<div class='job-description'>" + LONG_JD + "</div></body></html>")
        desc = bs._description_from_html(page)
        self.assertIn("design team", desc)
        self.assertNotIn("Other openings", desc)

    def test_closed_posting_page_is_marked_expired(self):
        page = "<html><body><h1>This job is no longer accepting applications</h1>" + LONG_JD + "</body></html>"
        self.assertEqual(bs._description_from_html(page), "Posting expired (closed-posting page)")

    def test_a_page_with_no_real_text_gives_nothing(self):
        self.assertIsNone(bs._description_from_html("<html><body><p>Loading...</p></body></html>"))


class GreenhouseUrlTests(unittest.TestCase):
    def ids(self, url, company="acme"):
        return bs._greenhouse_board_and_job(url, urlparse(url), company)

    def test_board_and_job_from_each_url_shape(self):
        self.assertEqual(self.ids("https://boards.greenhouse.io/acme/jobs/123"), ("acme", "123"))
        self.assertEqual(self.ids("https://job-boards.greenhouse.io/embed/job_app?for=globex&token=456"),
                         ("globex", "456"))
        # A company career site with a gh_jid uses the company name as the board token
        self.assertEqual(self.ids("https://careers.acme.com/open-roles?gh_jid=789"), ("acme", "789"))
        self.assertEqual(self.ids("https://careers.acme.com/open-roles/789"), (None, None))


class StoredJdTests(unittest.TestCase):
    def test_prescraped_sources_backfill_the_full_jd(self):
        job = {"ats": "LinkedIn", "description_ai": "x" * 200}
        self.assertTrue(bs._has_complete_jd(job))
        self.assertEqual(job["description_full"], "x" * 200)

    def test_ats_jobs_without_a_full_jd_are_rescraped(self):
        self.assertFalse(bs._has_complete_jd({"ats": "Greenhouse", "description_ai": "x" * 200}))

    def test_errors_and_closed_postings_are_rescraped(self):
        for desc in ("Error: timeout on direct scrape" + "x" * 200, "Position has been filled. " + "x" * 200, "short"):
            with self.subTest(desc=desc[:30]):
                self.assertFalse(bs._has_complete_jd({"description_full": desc, "description_ai": desc}))
        self.assertTrue(bs._has_complete_jd({"description_full": "x" * 200, "description_ai": "x" * 200}))


class _RoutedResponse(_Response):
    def __init__(self, status=200, payload=None, text="", final_path=None):
        super().__init__(status, payload)
        self._text = text
        self.history = [object()] if final_path else []
        self.url = SimpleNamespace(path=final_path)

    async def text(self, errors=None):
        return self._text


class _RoutedSession:
    """Answers each URL from a table (404 for anything else) and records what was requested."""

    def __init__(self, routes):
        self.routes, self.requested = routes, []

    def get(self, url, **kwargs):
        self.requested.append(url)
        return self.routes.get(url, _RoutedResponse(404))


class FetchJdTests(unittest.TestCase):
    """fetch_jd's real ATS dispatch and HTML fallback, with only the HTTP session faked."""

    def fetch(self, url, routes, company="acme"):
        session = _RoutedSession(routes)
        with contextlib.redirect_stderr(io.StringIO()):
            desc = asyncio.run(bs.fetch_jd(session, {"url": url, "company": company}))
        return desc, session.requested

    def test_greenhouse_posting_uses_the_board_api(self):
        api = "https://boards-api.greenhouse.io/v1/boards/acme/jobs/123"
        desc, requested = self.fetch("https://boards.greenhouse.io/acme/jobs/123",
                                     {api: _RoutedResponse(payload={"content": LONG_JD})})
        self.assertIn("design team", desc)
        self.assertEqual(requested, [api])

    def test_lever_posting_joins_description_lists_and_additional(self):
        api = "https://api.lever.co/v0/postings/acme/abc-123"
        payload = {"description": "<p>We build invoicing software for small businesses.</p>",
                   "lists": [{"text": "Requirements", "content": "<li>4+ years of React</li>"}],
                   "additional": "<p>Remote in the US.</p>"}
        desc, _ = self.fetch("https://jobs.lever.co/acme/abc-123", {api: _RoutedResponse(payload=payload)})
        self.assertIn("Requirements:", desc)
        self.assertIn("4+ years of React", desc)
        self.assertIn("Remote in the US.", desc)

    def test_ashby_board_is_searched_for_the_job_id(self):
        api = "https://api.ashbyhq.com/posting-api/job-board/acme"
        payload = {"jobs": [{"id": "other", "descriptionHtml": "<p>Wrong job</p>"},
                            {"id": "ABC-123", "descriptionHtml": LONG_JD}]}
        desc, _ = self.fetch("https://jobs.ashbyhq.com/acme/abc-123", {api: _RoutedResponse(payload=payload)})
        self.assertIn("design team", desc)

    def test_unknown_site_falls_back_to_the_page(self):
        url = "https://careers.acme.com/jobs/42"
        desc, requested = self.fetch(url, {url: _RoutedResponse(text="<div class='description'>" + LONG_JD + "</div>")})
        self.assertIn("design team", desc)
        self.assertEqual(requested, [url])

    def test_empty_api_answer_falls_back_to_the_page(self):
        url = "https://boards.greenhouse.io/acme/jobs/123"
        api = "https://boards-api.greenhouse.io/v1/boards/acme/jobs/123"
        desc, requested = self.fetch(url, {api: _RoutedResponse(payload={"content": ""}),
                                           url: _RoutedResponse(text="<p>" + LONG_JD + "</p>")})
        self.assertIn("design team", desc)
        self.assertEqual(requested, [api, url])

    def test_expired_postings(self):
        url = "https://careers.acme.com/jobs/42"
        self.assertEqual(self.fetch(url, {})[0], "Posting expired (HTTP 404)")
        moved = {url: _RoutedResponse(text="<p>" + LONG_JD + "</p>", final_path="/jobs")}
        self.assertEqual(self.fetch(url, moved)[0], "Posting expired (redirected to job board)")

    def test_missing_url(self):
        self.assertEqual(asyncio.run(bs.fetch_jd(_RoutedSession({}), {})), "No description extracted")


if __name__ == "__main__":
    unittest.main()
