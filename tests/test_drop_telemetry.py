"""Structured drop reasons, the drop funnel in the digest and summary, --explain and --init-config."""
import _fixture_config  # noqa: F401  (must come before any pipeline import)

import contextlib
import gzip
import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import job_search_pipeline.evaluate as ev
import job_search_pipeline.explain as ex
import job_search_pipeline.init_config as ic
import job_search_pipeline.outputs.html_digest as digest
import job_search_pipeline.pipeline as pl
import job_search_pipeline.sources.filter as ff

FILLER = " We build accessible SaaS products with React and TypeScript for thousands of customers." * 12
NOW = datetime.now(timezone.utc).isoformat()


def job(title="Frontend Engineer", desc=FILLER, **extra):
    return {"company": "Acme", "title": title, "location": "Remote, US", "remote": True, "ats": "Greenhouse",
            "url": f"https://example.com/acme/{title}".replace(" ", "-"), "first_seen": NOW,
            "description_ai": desc, "description_full": desc, "stack_detected": [], **extra}


class StructuredRejectionTests(unittest.TestCase):
    def test_years_of_experience_names_the_rule_and_the_phrase(self):
        result, rejection = ev.evaluate_job_traced(job(desc="You have 9+ years of experience in software." + FILLER))
        self.assertIsNone(result)
        self.assertEqual(rejection["rejected_by"], "max_min_yoe")
        self.assertIn("9 years > max 7", rejection["detail"])

    def test_each_gate_reports_its_own_rule(self):
        cases = {
            "seniority": job(title="Staff Frontend Engineer"),
            "title": job(title="Ruby Developer"),
            "hard_exclude_skills": job(desc="Required: production Rust experience." + FILLER),
            "clearance": job(desc="An active security clearance is required." + FILLER),
            "missing_company": job(company="unknown"),
            "expired": job(desc="Posting expired (HTTP 404)"),
        }
        for rule, posting in cases.items():
            with self.subTest(rule=rule):
                self.assertEqual(ev.evaluate_job_traced(posting)[1]["rejected_by"], rule)

    def test_trace_lists_passed_gates_and_breakdown_sums_to_the_score(self):
        trace, parts = [], []
        result, rejection = ev.evaluate_job_traced(job(), trace, parts)
        self.assertIsNone(rejection)
        self.assertTrue(all(step["passed"] for step in trace))
        self.assertEqual(trace[-1]["rule"], "secondary_floor")
        self.assertEqual(min(100, sum(p for _, p in parts)), result["score"])
        self.assertEqual(ev.evaluate_single_job(job()), result)

    def test_score_breakdown_works_for_a_dropped_job(self):
        score, parts = ev.score_breakdown(job(title="Staff Frontend Engineer"))
        self.assertGreater(score, 0)
        self.assertTrue(any(label.startswith("Core strengths") for label, _ in parts))

    def test_evaluate_jobs_tallies_drops_with_examples(self):
        jobs = [job(title=f"Staff Frontend Engineer {i}") for i in range(5)] + [job()]
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "enriched.json")
            with open(path, "w") as f:
                json.dump(jobs, f)
            result = ev.evaluate_jobs(path)
        funnel = {d["rule"]: d for d in result["dropped_by_rule"]}
        self.assertEqual(funnel["seniority"]["count"], 5)
        self.assertEqual(len(funnel["seniority"]["examples"]), ev.DROP_EXAMPLES)
        self.assertIn('"staff"', funnel["seniority"]["examples"][0]["detail"])
        self.assertEqual(len(result["top_matches"]), 1)


class PrefilterReasonTests(unittest.TestCase):
    def test_reasons_follow_the_check_order(self):
        self.assertEqual(ff.filter_reason({"title": "Frontend Engineer"}), "no_url")
        self.assertEqual(ff.filter_reason(job(first_seen="2000-01-01T00:00:00Z")), "not_recent")
        self.assertEqual(ff.filter_reason(job(location="Toronto, Canada", remote=False)), "location")
        self.assertEqual(ff.filter_reason(job(title="Ruby Developer")), "title")
        self.assertIsNone(ff.filter_reason(job()))

    def test_title_reason_names_the_config_key(self):
        self.assertIn("titles.exclude_tech_patterns", ff.title_reject_reason("Senior Ruby Engineer"))
        self.assertIn("target_patterns", ff.title_reject_reason("Account Executive"))
        self.assertEqual(ff.title_reject_reason("Frontend Engineer"), "")

    def test_feed_chunks_count_drops(self):
        data = gzip.compress(json.dumps([job(), job(title="Ruby Developer"), job(title="Ruby Developer 2")]).encode())
        fetched, kept, _, dropped = ff._filter_chunk(data)
        self.assertEqual((fetched, len(kept), dict(dropped)), (3, 1, {"title": 2}))

    def test_pipeline_reads_stage1_drops_without_the_recency_archive(self):
        stats = {"github_feed": {"dropped": {"not_recent": 1_000_000, "title": 40, "location": 7}},
                 "linkedin": {"dropped": {"keyword_score": 3}}}
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "stats.json")
            with open(path, "w") as f:
                json.dump(stats, f)
            drops = pl.prefilter_drops(path)
        self.assertEqual([(d["stage"], d["rule"], d["count"]) for d in drops],
                         [("Stage 1 · GitHub feed", "title", 40), ("Stage 1 · GitHub feed", "location", 7),
                          ("Stage 1 · LinkedIn", "keyword_score", 3)])


FUNNEL = [{"rule": "max_min_yoe", "label": ev.DROP_RULES["max_min_yoe"], "stage": "Stage 3", "count": 4,
           "examples": [{"company": "Acme", "title": "Engineer", "url": "https://example.com/1",
                         "detail": '"9+ years": 9 years > max 7'}]}]


class FunnelOutputTests(unittest.TestCase):
    def test_markdown_has_a_collapsible_funnel(self):
        md = ev.generate_markdown({"top_matches": [], "dropped_by_rule": FUNNEL,
                                   "prefilter_drops": [dict(FUNNEL[0], stage="Stage 1 · GitHub feed", rule="title",
                                                            label="Title rules", count=10, examples=[])]})
        self.assertIn("<details><summary>Dropped by Deterministic Rules (14)</summary>", md)
        self.assertIn("| Stage 3 | Years asked over experience.max_min_yoe | 4 | [Acme · Engineer]", md)

    def test_digest_carries_the_funnel(self):
        data = digest.page_data({"dropped_by_rule": FUNNEL}, {}, "now", 80)
        self.assertEqual(data["drops"][0]["count"], 4)
        self.assertIn("Dropped by deterministic rules", digest.render_html(data))


class ExplainTests(unittest.TestCase):
    def run_explain(self, saved, url, **kw):
        lines = []
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "enriched.json")
            with open(path, "w") as f:
                json.dump(saved, f)
            with patch.object(ex, "ENRICHED_JOBS", path), patch.object(ex, "FILTERED_JOBS", path + ".none"):
                code = ex.explain(url, out=lines.append, **kw)
        return code, "\n".join(lines)

    def test_traces_a_dropped_saved_posting(self):
        posting = job(desc="You have 9+ years of experience in software." + FILLER)
        code, text = self.run_explain([posting], posting["url"])
        self.assertEqual(code, 0)
        self.assertIn("✗ max_min_yoe", text)
        self.assertIn("DROPPED by max_min_yoe", text)
        self.assertIn("what it would have scored", text)

    def test_traces_a_match_to_the_review(self):
        posting = job()
        code, text = self.run_explain([posting], posting["url"] + "?utm_source=x")
        self.assertIn("Score breakdown", text)
        self.assertIn("✓ nothing rules it out", text)
        self.assertIn("Result: Secondary match at 55%; goes to the AI review", text)

    def test_unseen_posting_is_scraped_with_overrides(self):
        async def fake_scrape(j):
            j["description_full"] = j["description_ai"] = FILLER
        with patch.object(ex, "_scrape", fake_scrape):
            code, text = self.run_explain([], "https://jobs.lever.co/acme-co/123", title="Frontend Engineer",
                                          location="Remote")
        self.assertIn("Company:  acme co", text)
        self.assertIn("description scraped now", text)
        self.assertIn("Score breakdown", text)

    def test_company_from_url(self):
        self.assertEqual(ex.company_from_url("https://job-boards.greenhouse.io/creativex/jobs/1"), "creativex")
        self.assertEqual(ex.company_from_url("https://acme.wd5.myworkdayjobs.com/en-US/x"), "acme")
        self.assertEqual(ex.company_from_url("https://careers.upstart.com/jobs?gh_jid=1"), "upstart")


REPLY = """Here you go:
```json
{"sources": {"search_terms": ["Frontend Engineer", "UI Engineer"]},
 "titles": {"strong_target_patterns": ["front[- ]?end", "(unclosed"],
            "primary_role_groups": [{"label": "Frontend", "pattern": "\\\\bfrontend\\\\b", "points": 20}, "bad"]},
 "skills_and_scoring": {"core_strengths": ["react", "typescript", "react"], "focus_keywords": {"accessibility": 15,
                        "x": "high"}, "hard_exclude_skills": "ruby"},
 "experience": {"max_min_yoe": 8},
 "llm_review": {"calibration_examples": ["Acme, Frontend Engineer: fits. hard 4, core 3, evidence 3, level 2 [96%]."]},
 "notion": {"database_id": "should be ignored"}}
```"""


class InitConfigTests(unittest.TestCase):
    def test_parse_reply_strips_fences_and_prose(self):
        self.assertEqual(ic.parse_reply(REPLY)["experience"], {"max_min_yoe": 8})
        self.assertIsNone(ic.parse_reply("no json here"))

    def test_build_draft_validates_fields(self):
        draft, filled, problems = ic.build_draft(ic.parse_reply(REPLY), {"html_digest": {"enabled": True}})
        skills = draft["skills_and_scoring"]
        self.assertEqual(skills["core_strengths"], ["react", "typescript"])
        self.assertEqual(skills["detect_stack_keywords"], ["React", "Typescript"])
        self.assertEqual(skills["focus_keywords"], {"accessibility": 15})
        self.assertNotIn("hard_exclude_skills", skills)
        self.assertEqual(draft["titles"]["strong_target_patterns"], ["front[- ]?end"])
        self.assertEqual(len(draft["titles"]["primary_role_groups"]), 1)
        self.assertNotIn("notion", draft)
        self.assertTrue(draft["html_digest"]["enabled"])
        self.assertIn("experience.max_min_yoe", filled)
        self.assertTrue(any("invalid regex '(unclosed'" in p for p in problems))
        self.assertTrue(any(p.startswith("skills_and_scoring.hard_exclude_skills") for p in problems))

    def test_init_config_writes_a_draft(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile, output = os.path.join(tmp, "profile.md"), os.path.join(tmp, "draft.json")
            with open(profile, "w") as f:
                f.write("Frontend engineer, 6 years, React and TypeScript.")
            usage = {"input": 1000, "cached": 0, "output": 500, "usd": 0.01}
            with patch.object(ic, "PROFILE_PATH", profile), patch.object(ic, "_cli_path", return_value="/bin/true"), \
                    patch.object(ic, "_cli_response", return_value=(REPLY, usage)) as cli, \
                    contextlib.redirect_stdout(io.StringIO()):
                lines = []
                code = ic.init_config([], output=output, out=lines.append)
                self.assertEqual(code, 0)
                self.assertIn("Frontend engineer, 6 years", cli.call_args[0][3])
                with open(output) as f:
                    self.assertEqual(json.load(f)["experience"]["max_min_yoe"], 8)
                # an existing file other than the default draft is not overwritten without force
                self.assertEqual(ic.init_config([], output=output, out=lines.append), 1)
                self.assertEqual(ic.init_config([], output=output, force=True, out=lines.append), 0)
        self.assertTrue(any("Used 1,500 tokens ($0.010)" in line for line in lines))

    def test_missing_profile_is_reported(self):
        lines = []
        with patch.object(ic, "PROFILE_PATH", "/nonexistent/profile.md"):
            self.assertEqual(ic.init_config([], out=lines.append), 1)
        self.assertIn("not found", lines[0])


class CliFlagTests(unittest.TestCase):
    def test_flags_parse(self):
        args = pl.parse_args(["--explain", "https://x.test/1", "--title", "T"])
        self.assertEqual((args.explain, args.title, args.init_config), ("https://x.test/1", "T", None))
        self.assertEqual(pl.parse_args(["--init-config"]).init_config, [])
        self.assertEqual(pl.parse_args(["--init-config", "https://a", "https://b"]).init_config,
                         ["https://a", "https://b"])


if __name__ == "__main__":
    unittest.main()
