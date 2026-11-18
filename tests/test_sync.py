"""Stage 5: Notion rate limits, per-page blocklisting, held reviews, and honest summary counts."""
import _fixture_config  # noqa: F401  (must come before any pipeline import)

import contextlib
import io
import unittest
from unittest.mock import patch

import job_search_pipeline.blocklist as blocklist
import job_search_pipeline.pipeline as run_pipeline
import job_search_pipeline.review as review
import job_search_pipeline.outputs.notion as sync
from job_search_pipeline.config import atomic_write_json, load_json


class _Res:
    def __init__(self, status_code, headers=None):
        self.status_code, self.headers, self.text = status_code, headers or {}, ""

    def json(self):
        return {}


def match(n, score=90, **extra):
    return {"company": f"Company {n}", "title": f"Frontend Engineer {n}", "score": score,
            "url": f"https://example.com/jobs/{n}", "location": "Remote", "reason": "APPLY: fits", **extra}


class _Base(unittest.TestCase):
    def setUp(self):
        atomic_write_json(blocklist.BLOCKLIST_PATH, [])
        atomic_write_json(blocklist.ROLE_BLOCKLIST_PATH, [])
        self.quiet = contextlib.ExitStack()
        self.quiet.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.err = self.quiet.enter_context(contextlib.redirect_stderr(io.StringIO()))
        self.quiet.enter_context(patch.object(sync, "_notion_headers", return_value={}))
        self.sleep = self.quiet.enter_context(patch.object(sync.time, "sleep"))
        self.addCleanup(self.quiet.close)


class NotionSyncTests(_Base):
    def test_rate_limited_post_is_retried_after_the_advertised_wait(self):
        with patch.object(sync._session, "post", side_effect=[_Res(429, {"Retry-After": "2"}), _Res(200)]) as post:
            urls = sync.sync_jobs_to_notion([match(1)], database_id="db", min_score=80)
        self.assertEqual(urls, ["https://example.com/jobs/1"])
        self.assertEqual(post.call_count, 2)
        self.assertIn(2.0, [c.args[0] for c in self.sleep.call_args_list])
        self.assertIn("429", self.err.getvalue())

    def test_persistent_rate_limit_gives_up_without_blocklisting(self):
        with patch.object(sync._session, "post", return_value=_Res(429)) as post:
            urls = sync.sync_jobs_to_notion([match(1)], database_id="db", min_score=80)
        self.assertEqual(urls, [])
        self.assertEqual(post.call_count, sync.RATE_LIMIT_RETRIES + 1)
        self.assertEqual(blocklist.load_blocklist(), set())  # retried on the next run
        waits = [c.args[0] for c in self.sleep.call_args_list if c.args[0] != sync.SYNC_DELAY_SECONDS]
        self.assertTrue(all(w <= sync.RATE_LIMIT_MAX_WAIT for w in waits))

    def test_server_errors_are_not_retried(self):
        with patch.object(sync._session, "post", return_value=_Res(500)) as post:
            sync.sync_jobs_to_notion([match(1)], database_id="db", min_score=80)
        self.assertEqual(post.call_count, 1)  # a 5xx may have created the page; repeating could duplicate it

    def test_each_page_is_blocklisted_as_soon_as_it_is_created(self):
        seen = []

        def post(*args, **kwargs):
            seen.append(set(blocklist.load_blocklist()))
            if len(seen) == 3:
                raise KeyboardInterrupt  # the run dies while creating the third page
            return _Res(200)

        with patch.object(sync._session, "post", side_effect=post), self.assertRaises(KeyboardInterrupt):
            sync.sync_jobs_to_notion([match(1), match(2), match(3)], database_id="db", min_score=80)
        self.assertEqual(seen[1], {"https://example.com/jobs/1"})
        self.assertEqual(blocklist.load_blocklist(), {"https://example.com/jobs/1", "https://example.com/jobs/2"})
        self.assertEqual(len(blocklist.load_role_blocklist()), 2)

    def test_failed_ai_review_is_never_synced_or_exported(self):
        held = match(1, score=95, review_failed=True)
        with patch.object(sync._session, "post", return_value=_Res(200)) as post:
            urls = sync.sync_jobs_to_notion([held, match(2)], database_id="db", min_score=80)
        self.assertEqual(urls, ["https://example.com/jobs/2"])
        self.assertEqual(post.call_count, 1)
        self.assertNotIn("https://example.com/jobs/1", blocklist.load_blocklist())  # so it is retried
        self.assertEqual(blocklist.select_new_matches([held], 80, verbose=False), [])

    def test_third_party_flag_reaches_the_notion_row(self):
        job = match(1, reason="[Third-party poster] APPLY: fits", third_party=True)
        props = sync._build_notion_payload(job, "db")["properties"]
        self.assertTrue(props["Why It Matches"]["rich_text"][0]["text"]["content"].startswith("[Third-party poster]"))


class SummaryLabelTests(_Base):
    MATCHES = [match(1, 92), match(2, 75, third_party=True), match(3, 67), match(4, 95, review_failed=True)]

    def test_tally_separates_threshold_matches_from_open_ones(self):
        self.assertEqual(sync.match_tally(self.MATCHES, 80), {"at_threshold": 1, "held": 1, "third_party": 1})

    def test_callout_does_not_call_every_open_match_qualified(self):
        text = "".join(seg["text"]["content"] for seg in sync._build_summary_rich_text(
            {"top_matches": self.MATCHES, "total_evaluated": 100}, [], min_score=80))
        self.assertIn("Open matches: 4", text)
        self.assertIn("At 80%+: 1", text)
        self.assertIn("Held (AI review failed): 1", text)
        self.assertNotIn("Qualified", text)

    def test_run_history_is_saved_and_trimmed(self):
        old = [{"time": f"2026-01-01 00:{i:02d}", "summary": f"run {i}"} for i in range(sync.RUN_HISTORY_KEEP)]
        atomic_write_json(sync.RUN_HISTORY_PATH, old)
        previous = sync.record_run({"top_matches": self.MATCHES, "total_evaluated": 100}, [], 80)
        self.assertEqual(previous, old)
        saved = load_json(sync.RUN_HISTORY_PATH, default=[])
        self.assertEqual(len(saved), sync.RUN_HISTORY_KEEP)
        self.assertEqual(saved[0], old[1])  # the oldest run is dropped
        self.assertIn("4 open matches (1 at 80%+), 0 synced", saved[-1]["summary"])

    def notify(self, matches, synced=()):
        with patch.object(run_pipeline, "send_notification") as send:
            run_pipeline.notify_results({"top_matches": matches}, list(synced), [], None, True, min_score=80)
        return send.call_args.args[0], send.call_args.args[1]

    def test_notification_reports_threshold_held_and_third_party_counts(self):
        title, body = self.notify(self.MATCHES)
        self.assertEqual(title, "Job Search: 1 Match(es) at 80%+")
        self.assertIn("4 open match(es), 1 at 80%+ (0 synced to Notion).", body)
        self.assertIn("1 held back: AI review failed", body)
        self.assertIn("1 from third-party posters", body)
        self.assertIn("(75%) [third-party]", body)

    def test_leftover_run_is_not_announced_as_qualified_matches(self):
        title, _ = self.notify([match(2, 75), match(3, 67)])
        self.assertEqual(title, "Job Search: No New Matches at 80%+ (2 below)")
        title.encode("latin-1")  # HTTP header values must encode

    def test_skipped_ai_review_is_flagged_in_the_notification(self):
        results = {"top_matches": [match(1, 85)]}
        # The real review, with no claude CLI installed
        with patch.object(review, "PROVIDER", "anthropic"), patch.object(review, "_LLM_CFG", {}), \
                patch("job_search_pipeline.review.shutil.which", return_value=None):
            run_pipeline.ai_review(results, 55)
        self.assertIn("claude CLI", results["ai_review_skipped"])
        self.assertNotIn("ai_spend", results)
        self.assertEqual(results["top_matches"], [match(1, 85)])  # keyword results pass through untouched
        with patch.object(run_pipeline, "send_notification") as send:
            run_pipeline.notify_results(results, [], [], None, True, min_score=80)
        title, body = send.call_args.args[:2]
        self.assertTrue(title.endswith("(AI review skipped)"))
        self.assertIn("⚠️ " + results["ai_review_skipped"], body)
        self.assertEqual(send.call_args.kwargs["priority"], run_pipeline.PRIORITY_HIGH)

    def test_failed_review_gets_a_visible_reason(self):
        results = {"top_matches": [match(1, 80)]}
        failed = dict(match(1, 80), review_failed=True, reason="Frontend, React")
        with patch.object(run_pipeline, "review_matches", return_value=([failed], [])):
            run_pipeline.ai_review(results, 55)
        self.assertEqual(results["top_matches"][0]["reason"], "[AI review failed, held for retry] Frontend, React")


if __name__ == "__main__":
    unittest.main()
