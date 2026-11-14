"""Regression cases for the location, title, company, dedup-key and clearance rules.

They run against tests/fixtures/search_config.json (a made-up Denver-metro frontend/full-stack search), so they
hold whatever the real config.json says. Each case is the phrasing of a posting a rule once got wrong;
add one whenever a rule changes.
"""
import _fixture_config  # noqa: F401  (must come before any pipeline import)

import unittest
import unittest.mock

import evaluate_jobs as ev
import fetch_and_filter as ff
from blocklist import canon_company, role_key


def logistics(desc, loc="Remote? (unverified)", ats="Linkedin", remote=False, title="Software Engineer"):
    """True when the job is dropped before the AI review for its work-arrangement wording."""
    job = {"title": title, "location": loc, "ats": ats, "remote": remote, "description_full": desc}
    return bool(ev.jd_logistics_block(job))


class LocationTests(unittest.TestCase):
    def test_local_metro_is_word_bounded_and_state_aware(self):
        cases = {
            "Denver, Colorado": True, "Aurora, CO": True, "Jefferson County, CO": True, "Golden, CO, US": True,
            "Boulder, CO": True, "Colorado": True, "Colorado, United States of America": True,
            # Other towns in the state, same-named places elsewhere, and street names are not the metro
            "Pueblo, CO": False, "Grand Junction, CO": False, "Mesa County, CO": False,
            "Aurora, IL": False, "Westminster, CA": False, "Golden Valley, MN": False,
            "AZ, Phoenix, 2501 W. Golden Rd.": False, "Jefferson County, AL": False,
        }
        for loc, want in cases.items():
            with self.subTest(loc=loc):
                self.assertEqual(ff.is_local_metro(loc), want)

    def test_feed_location_filter(self):
        cases = {"DUNDEE, GBR": False, "Remote - Colorado": True, "Vancouver, BC": False, "Remote, US": True}
        for loc, want in cases.items():
            with self.subTest(loc=loc):
                self.assertEqual(ff.is_valid_location(loc), want)


class TitleTests(unittest.TestCase):
    def test_title_filter(self):
        cases = {
            "Senior Frontend Engineer": True,
            "Sr. Software Engineer, UI Developer Tooling (Hybrid, New York)": False,
            "Software Engineer (Remote/Hybrid)": True,
            "Full Stack Developer – NetSuite": False,
            "Front End/Salesforce Developer": False,
            "BI Tableau Developer": False,
            "Geotechnical Engineer   Spring 2027 Graduates": False,
            "Software Engineer II (Backend)": False,
            "Product Engineer, Identity": True,   # a strong title overrides a soft role exclusion
            "Full Stack Java Developer (C2C)": False,
        }
        for title, want in cases.items():
            with self.subTest(title=title):
                self.assertEqual(ff.is_valid_title(title), want)


class LogisticsTests(unittest.TestCase):
    def test_jd_work_arrangement_wording(self):
        cases = [
            ("This role requires you to be onsite. Remote and/or hybrid work will not be considered", True),
            ("Work Location: Hybrid remote in Texas City, TX", True),
            ("Work Location: Hybrid remote in Denver, CO 80202", False),
            ("Work Location: Remote", False),
            ("LOCATION: Bucharest, Romania TYPE OF CONTRACT: Permanent", True),
            ("Location: Remote (US or Canada)", False),
            ("Our office locations: Dublin, Paris and San Francisco. This role is Remote - US.", False),
            ("Hybrid office arrangement in the South Jordan, Utah office location.", True),
            ("You will work in the office 3 days per week in Austin.", True),
            ("candidates must live within approximately a 70-mile radius of our office", True),
            ("Candidates must reside within the United States. We are remote first.", False),
            ("whether they are working on-site, in a hybrid model, or fully remotely.", False),
            ("This role is 100% onsite in Hawthorne, CA.", True),
            ("you are welcome to drop by, though it is entirely optional and not a hybrid requirement.", False),
        ]
        for desc, want in cases:
            with self.subTest(desc=desc[:60]):
                self.assertEqual(logistics(desc), want)

    def test_company_hybrid_policy_under_a_feed_labeled_remote_role_is_not_a_requirement(self):
        blurb = "Our Approach to Hybrid Working: We embrace a hybrid work model"
        self.assertFalse(logistics(blurb, loc="Remote, US", ats="Greenhouse", remote=True))

    def test_hybrid_policy_under_an_inferred_remote_label_still_counts(self):
        self.assertTrue(logistics("Our hybrid approach is designed to foster strong collaboration"))


class CompanyTests(unittest.TestCase):
    def test_recruiters_and_excluded_companies(self):
        cases = {
            "Acme Teck": False, "Hartwell Staffing Partners": False, "Pinnacle Recruiters": False,
            "BodyShop: Verified Engineers. Interview-Ready.": False, "MegaCorp": False,
            # An allow-listed recruiter and ordinary employers pass, including one that shares a brand word
            "Northwind TalentSource": True, "SeatGeek": True, "BodyShop Fantasy": True,
        }
        for company, want in cases.items():
            with self.subTest(company=company):
                self.assertEqual(ff.is_valid_company(company), want)


class RoleKeyTests(unittest.TestCase):
    def test_brand_parent_and_suffix_spellings_share_one_key(self):
        pairs = [
            (("Initech", "Senior Front-End Developer"), ("Initech Holdings (Globex)", "Senior Front-End Developer")),
            (("Globex Group", "Frontend Engineer"), ("Initech", "Frontend Engineer")),
            (("Troveo Ai", "Front End Engineer"), ("Troveo", "Front End Engineer ")),
            (("Hooli Labs", "Sr. Full-Stack Engineer (Remote)"), ("hoolilabs", "Senior FullStack Engineer | US | Remote")),
            (("Brave Software", "Web3 Core Engineer"), ("BRAVE", "Web3 Core Engineer")),
        ]
        for (c1, t1), (c2, t2) in pairs:
            with self.subTest(company=c1):
                self.assertEqual(role_key(c1, t1), role_key(c2, t2))

    def test_parenthesized_long_name_keeps_the_short_one(self):
        self.assertEqual(role_key("Siemens EDA (Siemens Digital Industries Software)", "Product Engineer"),
                         "siemenseda|productengineer")

    def test_stored_keys_recanonicalize_and_short_names_keep_their_suffix(self):
        self.assertEqual(canon_company("initechholdingsglobex"), "initech")
        self.assertEqual(canon_company("openai"), "openai")


class ClearanceTests(unittest.TestCase):
    def test_clearance_wording(self):
        cases = {
            "no security clearance required": False,
            "ability to obtain a secret clearance": True,
            "must hold an active ts/sci with polygraph": True,
            "must be able to obtain a public trust": True,
        }
        for text, want in cases.items():
            with self.subTest(text=text):
                self.assertEqual(ev._requires_clearance(text), want)

    def test_ignored_terms_are_let_through_but_real_clearances_still_reject(self):
        with unittest.mock.patch.object(ev, "_CLEARANCE_IGNORE_TERMS", {"public trust"}):
            self.assertFalse(ev._requires_clearance("must be able to obtain a public trust"))
            self.assertTrue(ev._requires_clearance("public trust today, ts/sci within a year"))


if __name__ == "__main__":
    unittest.main()
