import contextlib
import hashlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import llm_review as review


JOB = {"url": "https://example.com/jobs/123", "company": "Example", "title": "Engineer", "score": 80}
QUOTE = "Build reliable services for customers and partners."
JD = QUOTE + " Additional job description. " * 15
VERDICT = json.dumps({
    "hard_reqs": 4,
    "core_work": 3,
    "evidence": 3,
    "evidence_quote": QUOTE,
    "level": 2,
    "logistics_ok": True,
    "verdict": "skip",
    "reason": "Strong match",
    "blockers": [],
})
CLAUDE_OUTPUT = json.dumps({"type": "result", "is_error": False, "result": VERDICT})
CODEX_OUTPUT = "\n".join(json.dumps(event) for event in [
    {"type": "thread.started"},
    {"type": "item.completed", "item": {"type": "agent_message", "text": VERDICT}},
    {"type": "turn.completed"},
])
AGY_OUTPUT = json.dumps({"status": "SUCCESS", "response": VERDICT})


def completed(stdout, returncode=0):
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr="private diagnostic")


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.jd_by_url = {review.normalize_url(JOB["url"]): JD}

    def review_one(self, provider):
        return review._review_one("test-cli", "test-model", "test profile", JOB,
                                  self.jd_by_url, provider=provider)

    @patch("llm_review.subprocess.run")
    def test_claude_cli_and_shared_scoring(self, run):
        run.return_value = completed(CLAUDE_OUTPUT)
        result = self.review_one("anthropic")
        self.assertEqual((result["score"], result["verdict"]), (100, "apply"))
        self.assertEqual(result["evidence_quote"], QUOTE)
        command = run.call_args.args[0]
        self.assertEqual(command[:4], ["test-cli", "-p", "--model", "test-model"])
        self.assertEqual(command[command.index("--tools") + 1], "")
        self.assertIn("--strict-mcp-config", command)
        self.assertIn("--no-session-persistence", command)
        self.assertEqual(json.loads(command[command.index("--settings") + 1]), {"disableAllHooks": True})
        self.assertIn("test profile", run.call_args.kwargs["input"])
        self.assertEqual(run.call_args.kwargs["timeout"], review.TIMEOUT_SECONDS)

    @patch("llm_review.subprocess.run")
    def test_codex_cli_and_shared_scoring(self, run):
        run.return_value = completed(CODEX_OUTPUT)
        result = self.review_one("openai")
        self.assertEqual((result["score"], result["verdict"]), (100, "apply"))
        command = run.call_args.args[0]
        self.assertEqual(command[:4], ["test-cli", "exec", "--model", "test-model"])
        self.assertIn("--json", command)
        self.assertIn("--ephemeral", command)
        self.assertEqual(command[command.index("--sandbox") + 1], "read-only")
        self.assertIn("--skip-git-repo-check", command)
        self.assertEqual(command[-1], "-")
        self.assertIn(JD, run.call_args.kwargs["input"])

    @patch("llm_review.subprocess.run")
    def test_agy_uses_existing_envelope_and_arguments(self, run):
        run.return_value = completed(AGY_OUTPUT)
        result = self.review_one("agy")
        self.assertEqual((result["score"], result["verdict"]), (100, "apply"))
        command = run.call_args.args[0]
        self.assertEqual(command[0:2], ["test-cli", "-p"])
        self.assertIn("test profile", command[2])
        self.assertEqual(command[-4:], ["--model", "test-model", "--output-format", "json"])
        self.assertIsNone(run.call_args.kwargs["input"])

    @patch("llm_review.subprocess.run")
    def test_cli_errors_and_malformed_results_fail_open(self, run):
        cases = [
            ("agy", completed('{"status":"ERROR"}')),
            ("agy", completed("not json")),
            ("anthropic", completed(json.dumps({"type": "result", "is_error": True, "result": VERDICT}))),
            ("anthropic", completed(json.dumps({"type": "result", "result": VERDICT}))),
            ("anthropic", completed(CLAUDE_OUTPUT, 1)),
            ("openai", completed('{"type":"turn.failed"}')),
            ("openai", completed(CODEX_OUTPUT + '\n{"type":"turn.failed"}')),
            ("openai", completed('{"type":"turn.completed"}')),
            ("openai", completed(CODEX_OUTPUT.rsplit("\n", 1)[0])),
            ("openai", completed('[]\n{"type":"turn.completed"}')),
            ("openai", completed('not json')),
        ]
        for provider, process in cases:
            with self.subTest(provider=provider, output=process.stdout), contextlib.redirect_stderr(io.StringIO()) as err:
                run.return_value = process
                self.assertIsNone(self.review_one(provider))
                self.assertTrue(err.getvalue())
                self.assertNotIn("private diagnostic", err.getvalue())
        for error in (subprocess.TimeoutExpired("test-cli", 1), OSError("private diagnostic")):
            run.side_effect = error
            with self.subTest(error=error), contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertIsNone(self.review_one("openai"))
                self.assertTrue(err.getvalue())
                self.assertNotIn("private diagnostic", err.getvalue())

    def test_cli_resolution_and_invalid_provider(self):
        with patch.object(review, "_LLM_CFG", {"claude_path": "~/bin/custom-claude"}), \
                patch("llm_review.shutil.which", return_value="/home/example/claude") as which:
            self.assertEqual(review._provider_config("anthropic"), "/home/example/claude")
            self.assertTrue(which.call_args.args[0].endswith("/bin/custom-claude"))
        with patch.object(review, "_LLM_CFG", {}), patch("llm_review.shutil.which", return_value=None) as which, \
                contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertIsNone(review._provider_config("openai"))
            which.assert_called_once_with("codex")
            self.assertIn("codex CLI not found", stderr.getvalue())
        with self.assertRaisesRegex(ValueError, "Unknown llm_review.provider"):
            review._provider_config("unsupported")

    def test_cache_identity_preserves_agy_and_isolates_providers(self):
        old_input = "profile|model|" + review.PROMPT + "|" + review.REVIEW_VERSION
        old_hash = hashlib.sha256(old_input.encode()).hexdigest()[:16]
        self.assertEqual(review._profile_hash("profile", "model", "agy"), old_hash)
        hashes = {review._profile_hash("profile", "model", provider)
                  for provider in ("agy", "openai", "anthropic")}
        self.assertEqual(len(hashes), 3)

    def test_unverified_quote_is_downgraded(self):
        parsed = review._parse_verdict(VERDICT, "A different job description.")
        self.assertEqual(parsed["components"]["evidence"], 2)
        self.assertEqual(parsed["evidence_quote"], "")
        self.assertEqual(parsed["score"], 92)

    def test_missing_cli_keeps_original_keyword_results(self):
        with patch.object(review, "_provider_config", return_value=None):
            self.assertEqual(review.review_matches([JOB], "unused.json"), ([JOB], []))

    def test_real_subprocess_pipes_prompt_and_parses_cli_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "fake_cli"
            executable.write_text(
                f"#!{sys.executable}\n"
                "import json, sys\n"
                "prompt = sys.stdin.read()\n"
                "assert 'test profile' in prompt\n"
                f"verdict = {VERDICT!r}\n"
                "if sys.argv[1] == 'exec':\n"
                "    print(json.dumps({'type': 'item.completed', "
                "'item': {'type': 'agent_message', 'text': verdict}}))\n"
                "    print(json.dumps({'type': 'turn.completed'}))\n"
                "else:\n"
                "    print(json.dumps({'type': 'result', 'is_error': False, 'result': verdict}))\n",
                encoding="utf-8",
            )
            executable.chmod(0o700)
            for provider in ("anthropic", "openai"):
                with self.subTest(provider=provider):
                    result = review._review_one(str(executable), "test-model", "test profile", JOB,
                                                self.jd_by_url, provider=provider)
                    self.assertEqual((result["score"], result["verdict"]), (100, "apply"))

    @patch("llm_review.subprocess.run")
    def test_review_matches_caches_success_and_retains_failed_keyword_score(self, run):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile = root / "profile.md"
            profile.write_text("test profile", encoding="utf-8")
            enriched = root / "jobs.json"
            enriched.write_text(json.dumps([dict(JOB, description_full=JD)]), encoding="utf-8")
            cache = root / "cache.json"
            run.return_value = completed(CODEX_OUTPUT)
            with (patch.object(review, "PROVIDER", "openai"), patch.object(review, "PROFILE_PATH", str(profile)),
                  patch.object(review, "CACHE_PATH", str(cache)),
                  patch.object(review, "_provider_config", return_value="/test/cli"),
                  contextlib.redirect_stdout(io.StringIO())):
                kept, skipped = review.review_matches([JOB], str(enriched))
                self.assertEqual((kept[0]["score"], kept[0]["keyword_score"]), (100, 80))
                self.assertEqual(skipped, [])
                review.review_matches([JOB], str(enriched))
                self.assertEqual(run.call_count, 1)
                with patch.object(review, "PROVIDER", "anthropic"), contextlib.redirect_stderr(io.StringIO()):
                    run.return_value = completed("", 1)
                    kept, skipped = review.review_matches([JOB], str(enriched))
                    self.assertEqual(kept, [JOB])
                    self.assertEqual(skipped, [])
                    self.assertEqual(run.call_count, 2)


if __name__ == "__main__":
    unittest.main()
