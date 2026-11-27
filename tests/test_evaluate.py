"""Stage 3: years-of-experience parsing, remote restrictions, stack credit and cross-company dedup."""
import unittest
import unittest.mock

import _fixture_config  # noqa: F401  (must come before any pipeline import)

import job_search_pipeline.evaluate as ev
from job_search_pipeline.blocklist import (
    is_blocked,
    job_keys,
    load_role_blocklist,
    record_exported,
    select_new_matches,
)

FILLER = " We build accessible SaaS products with React and TypeScript for thousands of customers." * 12


class YearsOfExperienceTests(unittest.TestCase):
    def rejected(self, text):
        return ev._requires_high_yoe(text.lower())

    def test_a_range_counts_as_its_lower_bound(self):
        for text in ("5-8+ years of software engineering experience",
                     "6–10+ years of experience in full stack development.",
                     "5 to 15+ years of professional software development experience"):
            with self.subTest(text=text):
                self.assertFalse(self.rejected(text))
        self.assertTrue(self.rejected("8–12+ years of professional software development experience."))
        self.assertEqual(ev._max_min_yoe("6–10+ years of experience in full stack development."), 6)

    def test_a_figure_for_another_level_is_ignored(self):
        self.assertFalse(self.rejected("5+ years of experience for senior engineer, 8+ years for staff engineer"))
        self.assertFalse(self.rejected("4+ years of professional experience (senior) or 9+ years (lead consultant)"))

    def test_soft_asks_are_not_minimums(self):
        self.assertFalse(self.rejected("5 or more years of experience (ideally 8+ years of experience) in "
                                       "full-stack engineering"))
        self.assertFalse(self.rejected("Requirements\n5+ years of experience with React.\n\n"
                                       "Nice to have\n10+ years of overall experience in development."))

    def test_plain_high_asks_still_reject(self):
        for text in ("8+ years of professional software engineering experience",
                     "minimum 10 years prior relevant experience",
                     "at least 9 years building web applications"):
            with self.subTest(text=text):
                self.assertTrue(self.rejected(text))
        self.assertFalse(self.rejected("7+ years of professional software engineering experience"))

    def test_secondary_skill_ceiling_still_applies(self):
        self.assertTrue(self.rejected("5+ years of experience building backend services in java"))
        self.assertFalse(self.rejected("5+ years of experience building react front-end applications"))


class RemoteRestrictionTests(unittest.TestCase):
    """Fixture candidate: home state CO, Mountain time."""

    def blocked(self, desc="A fully remote role.", **job):
        job = {"title": "Frontend Engineer", "location": "Remote", "ats": "Greenhouse", "remote": True,
               "description_full": desc, **job}
        return ev.jd_logistics_block(job)

    def test_remote_limited_to_another_state_in_title_or_feed_location(self):
        self.assertTrue(self.blocked(title="Full Stack Senior Software Engineer (Remote - CA)"))
        self.assertTrue(self.blocked(location="Remote - Texas"))
        self.assertTrue(self.blocked(title="Software Engineer, Remote in NC only"))

    def test_home_state_and_unrestricted_remote_pass(self):
        self.assertFalse(self.blocked(location="Remote - Colorado"))
        self.assertFalse(self.blocked(location="Remote - CA, CO, WA"))
        self.assertFalse(self.blocked(location="Remote, US"))
        self.assertFalse(self.blocked(title="Frontend Engineer (Remote)"))

    def test_jobspy_display_locations_are_not_restrictions(self):
        for ats in ("Linkedin", "Indeed", "Glassdoor"):
            with self.subTest(ats=ats):
                self.assertFalse(self.blocked(location="Remote (Austin, TX)", ats=ats, remote=False))
                self.assertFalse(self.blocked(location="Remote - TX", ats=ats, remote=False))
        self.assertTrue(self.blocked(location="Remote - TX", ats="Greenhouse", remote=False))

    def test_residency_wording_in_the_jd(self):
        self.assertTrue(self.blocked("Applicants must reside in MA, IN, or TX."))
        self.assertTrue(self.blocked("Candidates must be located in one of the following states: Texas, Florida"))
        self.assertFalse(self.blocked("Applicants must reside in MA, CO, or TX."))
        self.assertFalse(self.blocked("Candidates must reside in the United States."))
        self.assertFalse(self.blocked("You must be located in Texas or work remotely from anywhere in the US."))
        # Lower-case "in"/"or" are words, not Indiana and Oregon
        self.assertFalse(self.blocked("You must live in or near a major airport."))

    def test_time_zone_residency(self):
        self.assertTrue(self.blocked("You must be located in the Eastern or Central time zone."))
        self.assertTrue(self.blocked("Remote, restricted to Central or Eastern time."))
        self.assertFalse(self.blocked("You must be based in the Mountain or Pacific time zones."))
        self.assertFalse(self.blocked("We collaborate during Eastern time business hours."))

    def test_time_zone_abbreviations(self):
        self.assertTrue(self.blocked("Candidates must be located in the EST time zone."))
        self.assertTrue(self.blocked("This role is restricted to ET or CT."))
        self.assertTrue(self.blocked("You must be based in PST."))
        self.assertFalse(self.blocked("Must reside in PST or MST."))
        self.assertFalse(self.blocked("Must reside in the MST time zone."))

    def test_mt_and_ct_read_as_time_zones_when_listed_as_zones(self):
        # MT and CT are also Montana and Connecticut; zone context sends them to the time zone rule
        self.assertFalse(self.blocked("You must be based in CT, MT, or PT."))
        self.assertFalse(self.blocked("Must be located in CT time zone or MT time zone."))
        self.assertTrue(self.blocked("Must be located in CT time zone or ET time zone."))
        self.assertTrue(self.blocked("You must be based in CT or ET."))
        # Without zone context they are still states (and CT excludes the candidate either way)
        self.assertTrue(self.blocked("Applicants must reside in CT or NY."))
        self.assertTrue(self.blocked("Must be located in CT."))

    def test_working_hours_time_zone_is_not_residency(self):
        self.assertFalse(self.blocked("We are considering candidates from US and Canada only (EST time zone)."))
        self.assertFalse(self.blocked("You will work EST hours."))
        self.assertFalse(self.blocked("Must be located in the US and able to overlap with ET business hours."))
        self.assertFalse(self.blocked("Located in EST hours overlap is required."))
        # Lower-case words are not zone abbreviations
        self.assertFalse(self.blocked("Must be based in pt-BR locales."))

    def test_rules_are_off_without_a_configured_home(self):
        with unittest.mock.patch.object(ev, "_HOME_STATES", set()), \
                unittest.mock.patch.object(ev, "_HOME_TIMEZONES", set()):
            self.assertFalse(self.blocked("Applicants must reside in MA, IN, or TX.", location="Remote - Texas"))


def job(company, title, desc, **extra):
    return {"company": company, "title": title, "location": "Remote, US", "remote": True, "ats": "Greenhouse",
            "url": f"https://example.com/{company}/{title}".replace(" ", "-"),
            "description_ai": desc, "description_full": desc, "stack_detected": [], **extra}


class StackCreditTests(unittest.TestCase):
    def test_stack_list_cannot_add_skills_the_jd_does_not_name(self):
        desc = "Senior frontend role. You will use JavaScript, React and PostgreSQL daily." + FILLER
        honest = ev.evaluate_single_job(job("Acme", "Frontend Engineer", desc))
        inflated = ev.evaluate_single_job(job("Acme", "Frontend Engineer", desc,
                                              stack_detected=["Java", "Sql", "Aws", "Javascript", "React"]))
        self.assertIsNotNone(honest)
        self.assertEqual(inflated["score"], honest["score"])


class KeywordWeightTests(unittest.TestCase):
    def score(self, desc, optional=()):
        return ev._keyword_score("software engineer", desc, desc, [], False, list(optional))[0]

    def test_core_strength_weights_override_the_flat_points(self):
        desc = "we use react and java."
        with unittest.mock.patch.object(ev, "CORE_STRENGTH_WEIGHTS", {}):
            flat = self.score(desc)
        with unittest.mock.patch.object(ev, "CORE_STRENGTH_WEIGHTS", {"react": 8, "java": 3}):
            self.assertEqual(self.score(desc), flat + (8 - 5) + (3 - 5))

    def test_grouped_synonyms_count_once_at_their_best_weight(self):
        desc = "we care about accessibility (wcag 2.2)."
        keywords = {"accessibility": 10, "wcag": 15}
        with unittest.mock.patch.object(ev, "FOCUS_KEYWORDS", keywords), \
                unittest.mock.patch.object(ev, "DOMAIN_SCORE_CAP", 100):
            with unittest.mock.patch.object(ev, "_FOCUS_GROUP_OF", {}):
                ungrouped = self.score(desc)
            with unittest.mock.patch.object(ev, "_FOCUS_GROUP_OF", {"accessibility": 0, "wcag": 0}):
                self.assertEqual(self.score(desc), ungrouped - 10)
                reasons = ev._keyword_score("software engineer", desc, desc, [], False, [])[1]
                self.assertEqual([r for r in reasons if r in ("Accessibility", "Wcag")], ["Accessibility"])

    def test_optional_excluded_skills_are_penalized_per_skill_up_to_the_cap(self):
        desc = "we use react."
        base = self.score(desc)
        self.assertEqual(self.score(desc, ["rust"]), base - 5)
        self.assertEqual(self.score(desc, ["rust", "rust", "golang"]), base - 10)
        self.assertEqual(self.score(desc, ["rust", "golang", "c++", "ruby"]), base - ev.SOFT_SKILL_PENALTY_CAP)


class AlternativeMentionTests(unittest.TestCase):
    def test_aliases_fold_into_the_longer_name(self):
        self.assertEqual(ev.distinct_skills(["ruby", "ruby on rails", "c#", ".net", "asp.net", "ruby"]),
                         ["ruby on rails", "c#", "asp.net"])

    def test_a_framework_listed_beside_react_is_not_penalized(self):
        with unittest.mock.patch.object(ev, "PENALIZED_KEYWORDS", {"vue": 5}):
            for text in ("proficient with react, vue, angular or similar.",
                         "at least one modern framework (react, vue, angular, etc)"):
                with self.subTest(text=text):
                    self.assertFalse(ev._penalized_mention("vue", [], text))
            self.assertTrue(ev._penalized_mention("vue", [], "our app is built in vue 3 with pinia."))

    def test_desire_to_learn_is_a_soft_ask(self):
        self.assertEqual(list(ev._excluded_skill_mentions("* experience in or desire to learn rust\n")),
                         [("rust", True)])


class DedupTests(unittest.TestCase):
    JD = "Build and maintain claims software with Java, React and TypeScript for EMS agencies." + FILLER

    def entry(self, company, title, score=70, url=None, desc=None):
        j = job(company, title, desc or self.JD)
        return {"company": company, "title": title, "score": score, "url": url or j["url"],
                "posted": "2026-01-02", "jd_key": ev.jd_key(j)}

    def test_same_jd_and_title_under_another_company_name_is_one_role(self):
        out = ev._dedupe_by_role([
            self.entry("Digitech", "Java Developer - Digitech - Remote", 70, "https://www.linkedin.com/jobs/view/1"),
            self.entry("Sarnova HC, LLC", "Java Developer - Digitech - Remote", 65, "https://jobs.example.com/1923"),
        ])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["score"], 70)
        self.assertEqual(out[0]["url"], "https://jobs.example.com/1923")  # the direct posting wins the link
        self.assertIn("sarnovahc|javadeveloperdigitech", out[0]["dup_keys"])

    def test_title_with_a_trailing_tag_matches(self):
        out = ev._dedupe_by_role([self.entry("Brave", "Web3 Core Engineer"),
                                  self.entry("Brave Browser Co", "Web3 Core Engineer New")])
        self.assertEqual(len(out), 1)

    def test_different_roles_sharing_boilerplate_stay_separate(self):
        out = ev._dedupe_by_role([self.entry("Initech", "React Developer"), self.entry("Initech", "Angular Developer"),
                                  self.entry("Initech", "Senior React Developer")])
        self.assertEqual(len(out), 3)

    def test_same_title_with_different_jd_stays_separate(self):
        other = "Own our payments checkout in React and TypeScript, working with design on accessibility." + FILLER
        out = ev._dedupe_by_role([self.entry("Acme", "Frontend Engineer"),
                                  self.entry("Globex Corp", "Frontend Engineer", desc=other)])
        self.assertEqual(len(out), 2)

    def test_short_jd_has_no_key(self):
        self.assertEqual(ev.jd_key({"title": "Frontend Engineer", "description_full": "Too short."}), "")

    def test_synced_role_blocks_its_repost_under_another_company_name(self):
        first = self.entry("Digitech", "Java Developer", 90)
        repost = self.entry("Sarnova HC, LLC", "Java Developer", 90)
        self.assertEqual(select_new_matches([first, repost], 80, verbose=False), [first])
        record_exported([first])
        self.assertTrue(is_blocked(repost, set(), load_role_blocklist()))
        self.assertEqual(select_new_matches([repost], 80, verbose=False), [])
        self.assertIn(first["jd_key"], job_keys(first))


if __name__ == "__main__":
    unittest.main()
