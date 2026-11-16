import _fixture_config  # noqa: F401  (must come before any pipeline import)

import contextlib
import io
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import job_search_pipeline.outputs.html_digest as digest

MATCH = {"url": "https://example.com/jobs/1", "company": "acme", "title": "Frontend Engineer", "score": 88,
         "verdict": "apply", "ai_reason": "Strong fit", "blockers": ["no Go"], "location": "Remote"}


class DigestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = str(Path(self.tmp.name) / "store.json")
        patcher = patch.object(digest, "DIGEST_STORE_PATH", self.store)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_store_keeps_first_seen_and_drops_old_matches(self):
        run1 = "2026-11-01T10:00:00+00:00"
        digest.update_store({"top_matches": [MATCH]}, synced_urls=[MATCH["url"]], now=run1)
        later = dict(MATCH, url="https://example.com/jobs/2", score="70")
        store = digest.update_store({"top_matches": [dict(MATCH, score=91), later]}, now="2026-11-02T10:00:00+00:00")
        rec = store["https://example.com/jobs/1"]
        self.assertEqual((rec["first_seen"], rec["score"], rec["reason"]), (run1, 91, "Strong fit"))
        self.assertTrue(rec["synced"])  # sticky once sent to Notion
        self.assertEqual(store["https://example.com/jobs/2"]["score"], 70)
        store = digest.update_store({"top_matches": [later]}, now="2026-12-15T10:00:00+00:00")
        # job 1 was last seen more than keep_days ago; job 2 is in this run
        self.assertEqual(set(store), {"https://example.com/jobs/2"})
        self.assertEqual(set(json.loads(Path(self.store).read_text())), {"https://example.com/jobs/2"})

    def test_held_reviews_are_labelled(self):
        store = digest.update_store({"top_matches": [dict(MATCH, ai_reason="", keyword_reason="kw", reason="[label] kw", review_failed=True,
                                                          review_deferred=True)]}, now="2026-11-01T10:00:00+00:00")
        self.assertEqual(store[MATCH["url"]]["reason"], "AI review deferred by a budget cap; keyword match only. kw")

    def test_skipped_ai_review_is_a_digest_warning(self):
        data = digest.page_data({"ai_review_skipped": "AI review skipped: no CLI",
                                 "source_health": {"warnings": ["feed down"]}}, {}, "now", 80)
        self.assertEqual(data["warnings"], ["AI review skipped: no CLI", "feed down"])

    def test_page_embeds_data_safely_and_round_trips(self):
        hostile = dict(MATCH, title="</script><script>alert(1)</script>", ai_reason="a & b <!-- c")
        out = Path(self.tmp.name) / "digest.html"
        spend = {"usd": 0.12, "tokens": 5000, "usd_estimated": False, "usd_unknown": False, "cache_hits": 3,
                 "repost_hits": 1, "today_usd": 0.3, "today_tokens": 9000, "caps": {"USD/day": 1.0}, "deferred": 0}
        with contextlib.redirect_stdout(io.StringIO()):
            digest.write_digest({"top_matches": [hostile], "total_evaluated": 10,
                                 "source_health": {"line": "Sources: x", "warnings": ["feed stale"]}},
                                path=str(out), min_score=80, spend=spend)
        page = out.read_text(encoding="utf-8")
        self.assertEqual(page.count("</script>"), 2)  # only the two real closing tags
        raw = re.search(r'<script id="digest-data" type="application/json">(.*?)</script>', page, re.S).group(1)
        data = json.loads(raw)
        self.assertEqual(data["matches"][0]["title"], hostile["title"])
        self.assertEqual(data["matches"][0]["reason"], "a & b <!-- c")
        self.assertEqual((data["spend"]["usd"], data["warnings"]), (0.12, ["feed stale"]))
        self.assertEqual(data["matches"][0]["last_seen"], data["generated"])


if __name__ == "__main__":
    unittest.main()
