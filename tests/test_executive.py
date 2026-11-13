import os
import unittest
from unittest.mock import AsyncMock, patch

from pydantic import ValidationError

from nxs.agents.analyst import run_analyst
from nxs.agents.assessor import run_assessor
from nxs.agents.auditor import run_auditor
from nxs.agents.executive import run_executive
from nxs.graph.archetype import NXS_Archetype
from nxs.profile import CANDIDATE_PROFILE
from nxs.schemas.executive import ExecutiveReport, grade_for, verdict_for
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
        "assessment_report": None, "nexus_score": None,
        "executive_report": None, "executive_verdict": None,
    }


class ThresholdTests(unittest.TestCase):
    def test_published_grade_bands(self):
        for score, grade in [(850, "A+"), (800, "A+"), (799, "A"), (750, "A"), (749, "B+"),
                             (700, "B+"), (699, "B"), (650, "B"), (649, "C"), (600, "C"),
                             (599, "F"), (300, "F")]:
            with self.subTest(score=score):
                self.assertEqual(grade_for(score), grade)

    def test_proceed_on_merit(self):
        self.assertEqual(verdict_for(700, 850), "Proceed")
        self.assertEqual(verdict_for(850, 850), "Proceed")

    def test_discard_only_when_the_listing_could_have_passed(self):
        # Enough evidence to clear the bar, and it did not.
        self.assertEqual(verdict_for(450, 850), "Discard")
        self.assertEqual(verdict_for(599, 600), "Discard")

    def test_thin_capture_is_observed_rather_than_discarded(self):
        # The ceiling is below the bar, so the shortfall is missing evidence, not a bad job.
        self.assertEqual(verdict_for(450, 450), "Observe")
        self.assertEqual(verdict_for(450, 599), "Observe")

    def test_out_of_reach_overrides_the_score(self):
        self.assertEqual(verdict_for(850, 850, out_of_reach=True), "Discard")

    def test_middle_band_is_observed(self):
        self.assertEqual(verdict_for(650, 850), "Observe")
        self.assertEqual(verdict_for(699, 850), "Observe")


class ExecutiveTests(unittest.IsolatedAsyncioTestCase):
    async def state(self, **kwargs):
        state = initial_state(**kwargs)
        state["candidate_profile"] = CANDIDATE_PROFILE
        with patch.dict(os.environ, {"NXS_MARKET_SEARCH": "off"}):
            state["analyst_report"] = await run_analyst(
                state["job_id"], state["job_listing"], CANDIDATE_PROFILE,
            )
        state["audit_report"] = run_auditor(state)
        state["assessment_report"] = run_assessor(state)
        return state

    async def test_strong_listing_proceeds(self):
        report = run_executive(await self.state(salary="£30,000", seniority="placement"))
        self.assertEqual(report.verdict, "Proceed")
        self.assertEqual(report.nexus_score, 850)
        self.assertEqual(report.grade, "A+")
        self.assertTrue(report.supporting)
        self.assertIn("Proceed", report.reasoning)

    async def test_out_of_reach_role_does_not_proceed(self):
        report = run_executive(await self.state(salary="£30,000", seniority="senior"))
        self.assertEqual(report.nexus_score, 750)  # Would clear the Proceed bar on score alone.
        self.assertEqual(report.verdict, "Discard")
        self.assertTrue(report.out_of_reach)
        self.assertTrue(any("Seniority fit" in line for line in report.against))
        self.assertIn("beyond the stated seniority target", report.reasoning)

    async def test_thin_capture_is_observed_with_a_stated_reason(self):
        report = run_executive(await self.state(
            html="<p>About the job: a role.</p>", tech_stack=()))
        self.assertEqual(report.verdict, "Observe")
        self.assertLess(report.score_ceiling, 600)
        self.assertIn("too thin to reject on", report.reasoning)
        self.assertTrue(report.evidence_limits)

    async def test_reasoning_names_the_role_and_the_score(self):
        report = run_executive(await self.state(salary="£30,000", seniority="placement"))
        self.assertIn("Placement developer", report.reasoning)
        self.assertIn("Example", report.reasoning)
        self.assertIn("850", report.reasoning)

    async def test_missing_inputs_are_refused(self):
        for field in ("assessment_report", "audit_report", "analyst_report"):
            state = await self.state()
            state[field] = None
            with self.subTest(field=field), self.assertRaises(ValueError):
                run_executive(state)


class ReportContractTests(unittest.TestCase):
    def report(self, **kwargs):
        data = {"verdict": "Proceed", "grade": "A+", "nexus_score": 850, "score_ceiling": 850,
                "out_of_reach": False, "reasoning": "Scored at the ceiling.", "supporting": ["Skill coverage 200/200."],
                "against": [], "evidence_limits": []}
        return ExecutiveReport(**(data | kwargs))

    def test_valid_report_accepted(self):
        self.assertEqual(self.report().verdict, "Proceed")

    def test_inconsistent_reports_are_refused(self):
        for update in [
            {"grade": "A"},                                    # Grade disagrees with the score.
            {"verdict": "Discard"},                            # Verdict disagrees with the rule.
            {"nexus_score": 900},                              # Outside the published range.
            {"score_ceiling": 700},                            # Score exceeds its own ceiling.
            {"verdict": "Discard", "nexus_score": 450,         # Discard with nothing against it.
             "grade": "F", "against": []},
        ]:
            with self.subTest(update=update), self.assertRaises(ValidationError):
                self.report(**update)


class GraphTests(unittest.IsolatedAsyncioTestCase):
    async def test_verdict_is_logged_and_written_back(self):
        state = initial_state(salary="£30,000", seniority="placement")
        with patch.dict(os.environ, {"NXS_MARKET_SEARCH": "off"}), patch(
            "nxs.graph.archetype.log_to_supabase", new_callable=AsyncMock,
        ) as persist, patch(
            "nxs.graph.archetype.persist_result", new_callable=AsyncMock,
        ) as write_back:
            result = await NXS_Archetype.ainvoke(state)
        self.assertEqual(result["executive_verdict"], "Proceed")
        self.assertEqual(result["executive_report"].nexus_score, 850)
        statuses = [call.args[2] for call in persist.call_args_list]
        self.assertEqual(statuses.count("VERDICT_PENDING"), 1)
        self.assertEqual(statuses.count("PROCEED"), 1)
        write_back.assert_awaited_once()
        self.assertEqual(write_back.await_args.args[0], JOB_ID)
        self.assertEqual(write_back.await_args.args[1].verdict, "Proceed")

    async def test_rejected_audit_writes_no_verdict(self):
        state = initial_state(html="<p>This job is closed.</p>")
        with patch.dict(os.environ, {"NXS_MARKET_SEARCH": "off"}), patch(
            "nxs.graph.archetype.log_to_supabase", new_callable=AsyncMock,
        ), patch("nxs.graph.archetype.persist_result", new_callable=AsyncMock) as write_back:
            result = await NXS_Archetype.ainvoke(state)
        self.assertIsNone(result["executive_verdict"])
        write_back.assert_not_awaited()


class WriteBackTests(unittest.IsolatedAsyncioTestCase):
    async def test_payload_matches_the_contract_columns(self):
        from nxs.db.results import persist_result

        report = ExecutiveReport(
            verdict="Observe", grade="B", nexus_score=650, score_ceiling=850,
            out_of_reach=False, reasoning="Mid band.", supporting=[], against=[], evidence_limits=[],
        )
        with patch("nxs.db.results._update") as update:
            await persist_result(JOB_ID, report)
        job_id, payload = update.call_args.args
        self.assertEqual(job_id, JOB_ID)
        self.assertEqual(payload["nexus_score"], 650)
        self.assertEqual(payload["verdict"], "Observe")
        self.assertEqual(payload["status"], "OBSERVE")
        self.assertIn("updated_at", payload)


if __name__ == "__main__":
    unittest.main()
