import os
import unittest
from unittest.mock import AsyncMock, patch

from pydantic import ValidationError

from nxs.agents.analyst import run_analyst
from nxs.agents.assessor import parse_salary, run_assessor
from nxs.agents.auditor import run_auditor
from nxs.agents.scout import _extract_seniority
from nxs.graph.archetype import NXS_Archetype
from nxs.profile import CANDIDATE_PROFILE
from nxs.schemas.assessment import SCORE_FLOOR, SCORE_SPAN, AssessmentReport, ScoreComponent
from nxs.schemas.models import JobListing

JOB_ID = "72464349-bbe1-4d6d-aefa-e634eddf3011"
DESCRIPTION = "<p>About the job: build services using Python and TypeScript.</p>"


def initial_state(html=DESCRIPTION, salary=None, tech_stack=("Python", "TypeScript"),
                  seniority=None):
    return {
        "job_id": JOB_ID, "current_agent": "Scout", "audit_retry_count": 0,
        "job_listing": JobListing(company="Example", role="Placement developer",
                                  salary=salary, tech_stack=list(tech_stack),
                                  seniority=seniority,
                                  url="https://example.com/job", raw_html=html),
        "candidate_profile": None, "analyst_report": None, "audit_report": None,
        "assessment_report": None, "nexus_score": None, "executive_report": None, "executive_verdict": None,
    }


class SalaryParsingTests(unittest.TestCase):
    def test_usable_sterling_figures(self):
        for text, expected in [
            ("£30,000", (30000, 30000)),
            ("£25,000 – £35,000", (25000, 35000)),
            ("£35,000-£25,000", (25000, 35000)),
            ("£28k", (28000, 28000)),
            ("£20,000–£28,000", (20000, 28000)),
            ("Salary: £24,000 per annum", (24000, 24000)),
        ]:
            with self.subTest(text=text):
                self.assertEqual(parse_salary(text), expected)

    def test_unusable_figures_return_none(self):
        # Other currencies are not converted, and four-figure pay is not guessed at.
        for text in [None, "", "competitive", "$80,000", "€40,000", "£900"]:
            with self.subTest(text=text):
                self.assertIsNone(parse_salary(text))


class AssessorTests(unittest.IsolatedAsyncioTestCase):
    async def state(self, **kwargs):
        state = initial_state(**kwargs)
        state["candidate_profile"] = CANDIDATE_PROFILE
        with patch.dict(os.environ, {"NXS_MARKET_SEARCH": "off"}):
            state["analyst_report"] = await run_analyst(
                state["job_id"], state["job_listing"], CANDIDATE_PROFILE,
            )
        state["audit_report"] = run_auditor(state)
        return state

    async def test_full_evidence_scores_within_bounds(self):
        report = run_assessor(await self.state(salary="£30,000", seniority="placement"))
        self.assertEqual(report.score_ceiling, 850)
        self.assertEqual(report.evidence_coverage, 1)
        self.assertLessEqual(report.nexus_score, report.score_ceiling)
        self.assertGreaterEqual(report.nexus_score, SCORE_FLOOR)
        self.assertEqual(
            report.nexus_score,
            SCORE_FLOOR + sum(item.awarded for item in report.components),
        )

    async def test_unknown_salary_lowers_the_ceiling_without_redistribution(self):
        report = run_assessor(await self.state(seniority="placement"))
        salary = next(c for c in report.components if c.name == "salary_alignment")
        self.assertEqual(salary.evidence_status, "unknown")
        self.assertEqual(salary.awarded, 0)
        self.assertEqual(report.score_ceiling, 750)
        self.assertLessEqual(report.nexus_score, 750)
        self.assertAlmostEqual(report.evidence_coverage, 450 / SCORE_SPAN)
        self.assertTrue(any("caps this listing at 750" in item for item in report.limitations))

    async def test_no_technologies_leaves_only_audit_integrity_scorable(self):
        state = await self.state(html="<p>About the job: a placement role.</p>", tech_stack=())
        report = run_assessor(state)
        skills = next(c for c in report.components if c.name == "skill_coverage")
        self.assertEqual(skills.evidence_status, "unknown")
        self.assertEqual(report.score_ceiling, 450)
        self.assertLess(report.nexus_score, 500)

    async def test_salary_below_target_scores_proportionally(self):
        low = run_assessor(await self.state(salary="£12,000"))
        high = run_assessor(await self.state(salary="£30,000"))
        low_award = next(c for c in low.components if c.name == "salary_alignment").awarded
        high_award = next(c for c in high.components if c.name == "salary_alignment").awarded
        self.assertEqual(high_award, 100)
        self.assertEqual(low_award, 50)
        self.assertLess(low.nexus_score, high.nexus_score)

    async def test_partial_skill_coverage_awards_a_share(self):
        state = await self.state(html="<p>About the job: Python, TypeScript, Rust and Scala.</p>",
                                 tech_stack=("Python", "TypeScript", "Rust", "Scala"))
        report = run_assessor(state)
        skills = next(c for c in report.components if c.name == "skill_coverage")
        self.assertEqual(skills.awarded, 100)
        self.assertEqual(skills.evidence_status, "supported")

    async def test_unscored_audit_is_refused(self):
        state = await self.state()
        state["job_listing"].raw_html = ""
        state["audit_report"] = run_auditor(state)
        self.assertNotEqual(state["audit_report"].decision, "pass")
        with self.assertRaises(ValueError):
            run_assessor(state)

    async def test_missing_inputs_are_refused(self):
        for field in ("candidate_profile", "analyst_report", "audit_report"):
            state = await self.state()
            state[field] = None
            with self.subTest(field=field), self.assertRaises(ValueError):
                run_assessor(state)


class SeniorityTests(unittest.IsolatedAsyncioTestCase):
    async def state(self, **kwargs):
        state = initial_state(**kwargs)
        state["candidate_profile"] = CANDIDATE_PROFILE
        with patch.dict(os.environ, {"NXS_MARKET_SEARCH": "off"}):
            state["analyst_report"] = await run_analyst(
                state["job_id"], state["job_listing"], CANDIDATE_PROFILE,
            )
        state["audit_report"] = run_auditor(state)
        return state

    async def award(self, seniority):
        report = run_assessor(await self.state(seniority=seniority, salary="£30,000"))
        return next(c for c in report.components if c.name == "seniority_fit")

    async def test_level_within_reach_scores_in_full(self):
        for level in ("intern", "placement", "graduate"):
            with self.subTest(level=level):
                self.assertEqual((await self.award(level)).awarded, 100)

    async def test_one_level_above_reach_scores_a_half(self):
        self.assertEqual((await self.award("junior")).awarded, 50)

    async def test_out_of_reach_level_scores_nothing_despite_skill_overlap(self):
        report = run_assessor(await self.state(seniority="senior", salary="£30,000"))
        seniority = next(c for c in report.components if c.name == "seniority_fit")
        skills = next(c for c in report.components if c.name == "skill_coverage")
        self.assertEqual(seniority.awarded, 0)
        self.assertEqual(skills.awarded, 200)  # Full skill overlap does not offset it.
        self.assertEqual(report.nexus_score, 750)
        self.assertTrue(any("beyond the stated target" in item for item in report.limitations))

    async def test_silent_listing_lowers_the_ceiling(self):
        report = run_assessor(await self.state(salary="£30,000"))
        seniority = next(c for c in report.components if c.name == "seniority_fit")
        self.assertEqual(seniority.evidence_status, "unknown")
        self.assertEqual(report.score_ceiling, 750)


class SeniorityExtractionTests(unittest.TestCase):
    def test_stated_levels_are_recognised(self):
        for text, expected in [
            ("Summer internship in London", "intern"),
            ("Industrial placement, 12 months", "placement"),
            ("Graduate scheme starting September", "graduate"),
            ("Junior Developer", "junior"),
            ("Senior Software Engineer", "senior"),
            ("Principal Engineer", "principal"),
        ]:
            with self.subTest(text=text):
                self.assertEqual(_extract_seniority(text), expected)

    def test_highest_stated_level_wins(self):
        # A listing naming two levels is pitched at the harder one.
        self.assertEqual(_extract_seniority("Graduate or Senior Engineer"), "senior")

    def test_years_of_experience_imply_a_level(self):
        self.assertEqual(_extract_seniority("5 years experience required"), "senior")
        self.assertEqual(_extract_seniority("2 years of commercial experience"), "mid")

    def test_silence_and_partial_words_return_none(self):
        for text in ["Build services using Python", "Our internal tooling team", ""]:
            with self.subTest(text=text):
                self.assertIsNone(_extract_seniority(text))


class ReportContractTests(unittest.TestCase):
    def component(self, **kwargs):
        data = {"name": "skill_coverage", "evidence_status": "supported",
                "awarded": 200, "available": 200, "detail": "All extracted mentions match."}
        return ScoreComponent(**(data | kwargs))

    def report(self, **kwargs):
        components = [
            self.component(),
            self.component(name="audit_integrity", awarded=150, available=150),
            self.component(name="seniority_fit", awarded=100, available=100),
            self.component(name="salary_alignment", awarded=100, available=100),
        ]
        data = {"nexus_score": 850, "score_ceiling": 850, "evidence_coverage": 1.0,
                "components": components, "tech_demand_context": "No demand evidence.",
                "limitations": []}
        return AssessmentReport(**(data | kwargs))

    def test_valid_report_accepted(self):
        self.assertEqual(self.report().nexus_score, 850)

    def test_arithmetic_and_bounds_are_enforced(self):
        for update in [
            {"nexus_score": 851},
            {"nexus_score": 299},
            {"nexus_score": 800},                       # Does not match awarded points.
            {"score_ceiling": 700},                     # Ceiling below the supported total.
            {"evidence_coverage": 0.5},                 # Coverage disagrees with components.
        ]:
            with self.subTest(update=update), self.assertRaises(ValidationError):
                self.report(**update)

    def test_unknown_evidence_cannot_award_points(self):
        with self.assertRaises(ValidationError):
            self.component(evidence_status="unknown", awarded=10)

    def test_every_component_must_appear_once(self):
        with self.assertRaises(ValidationError):
            self.report(components=[self.component()], nexus_score=500,
                        score_ceiling=500, evidence_coverage=200 / SCORE_SPAN)


class GraphTests(unittest.IsolatedAsyncioTestCase):
    async def test_passed_audit_scores_and_logs(self):
        state = initial_state(salary="£30,000", seniority="placement")
        with patch.dict(os.environ, {"NXS_MARKET_SEARCH": "off"}), patch(
            "nxs.graph.archetype.log_to_supabase", new_callable=AsyncMock,
        ) as persist:
            result = await NXS_Archetype.ainvoke(state)
        self.assertIsNotNone(result["assessment_report"])
        self.assertEqual(result["nexus_score"], result["assessment_report"].nexus_score)
        statuses = [call.args[2] for call in persist.call_args_list]
        self.assertEqual(statuses.count("SCORING"), 1)
        self.assertEqual(statuses.count("SCORE_COMPLETE"), 1)
        self.assertTrue(all(call.args[0] == JOB_ID for call in persist.call_args_list))

    async def test_rejected_audit_never_reaches_the_assessor(self):
        state = initial_state(html="<p>This job is closed.</p>")
        with patch.dict(os.environ, {"NXS_MARKET_SEARCH": "off"}), patch(
            "nxs.graph.archetype.log_to_supabase", new_callable=AsyncMock,
        ) as persist:
            result = await NXS_Archetype.ainvoke(state)
        self.assertIsNone(result["nexus_score"])
        self.assertIsNone(result["assessment_report"])
        self.assertNotIn("SCORING", [call.args[2] for call in persist.call_args_list])


if __name__ == "__main__":
    unittest.main()
