"""Stage 2: closed-posting detection on redirects, and ATS API failures that are reported, not swallowed."""
import _fixture_config  # noqa: F401  (must come before any pipeline import)

import asyncio
import contextlib
import io
import unittest

import aiohttp

import job_search_pipeline.scrape as bs


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


if __name__ == "__main__":
    unittest.main()
