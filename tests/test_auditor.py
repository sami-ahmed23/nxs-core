import os
import unittest
from unittest.mock import AsyncMock, patch

from pydantic import ValidationError

from nxs.agents.auditor import run_auditor
from nxs.agents.analyst import run_analyst
from nxs.graph.archetype import NXS_Archetype, auditor_node
from nxs.profile import CANDIDATE_PROFILE
from nxs.schemas.models import AuditReport, JobListing


def initial_state():
    return {
        "job_id": "72464349-bbe1-4d6d-aefa-e634eddf3011",
        "current_agent": "Scout", "audit_retry_count": 0,
        "job_listing": JobListing(company="Example", role="Developer",
                                  tech_stack=[], url="https://example.com/job", raw_html=""),
        "candidate_profile": None, "analyst_report": None, "audit_report": None,
        "market_match_score": None, "executive_verdict": None,
    }


class AuditorTests(unittest.IsolatedAsyncioTestCase):
    async def state(self, html="<p>Build services using Python.</p>"):
        state = initial_state()
        state["job_listing"].raw_html = html
        state["job_listing"].tech_stack = ["Python"]
        state["candidate_profile"] = CANDIDATE_PROFILE
        with patch.dict(os.environ, {"NXS_MARKET_SEARCH": "off"}):
            state["analyst_report"] = await run_analyst(
                state["job_id"], state["job_listing"], CANDIDATE_PROFILE,
            )
        return state

    async def test_unknown_salary_and_market_pass_with_warnings(self):
        report = run_auditor(await self.state())
        self.assertEqual(report.decision, "pass")
        self.assertIn("SALARY_UNKNOWN", report.red_flags)
        self.assertIn("MARKET_EVIDENCE_INCOMPLETE", report.red_flags)
        self.assertEqual(report.confidence_score, 1)

    async def test_closed_status_paragraph_and_inline_markup_reject(self):
        for html in [
            "<h2>About the job</h2><p>No longer accepting applications</p><p>Build services.</p>",
            '<div itemprop="description"><p>This role is <b>closed</b>.</p></div>',
            "<p>Applications have closed.</p>",
        ]:
            with self.subTest(html=html):
                report = run_auditor(await self.state(html))
                self.assertEqual(report.decision, "reject")
                self.assertFalse(report.retry_required)
                self.assertIn("CLOSED_LISTING", report.red_flags)

    async def test_unrelated_and_negated_closure_do_not_reject(self):
        for html in [
            "<p>This role is not closed.</p>",
            "<p>Do not assume this role is closed.</p>",
            '<div itemprop="description">Build Python services.</div><p>This role is closed.</p>',
            "<p>Build Python services.</p><aside>No longer accepting applications</aside>",
            "<p>Build Python services.</p><h2>Similar jobs</h2><p>This role is closed.</p>",
        ]:
            with self.subTest(html=html):
                self.assertEqual(run_auditor(await self.state(html)).decision, "pass")

    async def test_talent_pool_is_warning_only(self):
        report = run_auditor(await self.state("<p>Join our talent pool for future opportunities.</p>"))
        self.assertEqual(report.decision, "pass")
        self.assertIn("TALENT_POOL_WORDING", report.red_flags)

    async def test_incomplete_inputs_retry_then_reject(self):
        for field, value, code in [
            ("company", " ", "MISSING_IDENTITY"),
            ("role", "", "MISSING_IDENTITY"),
            ("url", "javascript:alert(1)", "INVALID_URL"),
            ("url", "https://bad host/job", "INVALID_URL"),
            ("url", "https://example.com:abc/job", "INVALID_URL"),
            ("raw_html", "<script>Python</script>", "MISSING_DESCRIPTION"),
            ("raw_html", "<h2>About the job</h2>", "MISSING_DESCRIPTION"),
        ]:
            with self.subTest(field=field, value=value):
                state = await self.state()
                setattr(state["job_listing"], field, value)
                first = run_auditor(state)
                self.assertEqual(first.decision, "retry")
                self.assertIn(code, first.red_flags)
                state["audit_retry_count"] = 1
                second = run_auditor(state)
                self.assertEqual(second.decision, "reject")
                self.assertIn("RETRY_EXHAUSTED", second.red_flags)

    async def test_missing_profile_or_analysis_blocks(self):
        for field in ["candidate_profile", "analyst_report"]:
            state = await self.state()
            state[field] = None
            self.assertEqual(run_auditor(state).decision, "retry")

    async def test_inconsistent_skills_block(self):
        state = await self.state()
        state["analyst_report"].skill_matches = []
        self.assertIn("INCONSISTENT_ANALYSIS", run_auditor(state).red_flags)

    async def test_skill_gaps_and_no_technologies_do_not_block(self):
        state = await self.state()
        state["job_listing"].tech_stack = ["UnlistedSkill"]
        state["analyst_report"].skill_matches = []
        state["analyst_report"].skill_gaps = ["UnlistedSkill"]
        report = run_auditor(state)
        self.assertEqual(report.decision, "pass")
        self.assertIn("SKILL_GAPS", report.red_flags)
        state["job_listing"].tech_stack = []
        state["analyst_report"].skill_gaps = []
        self.assertEqual(run_auditor(state).decision, "pass")

    async def test_invalid_retry_count_rejected(self):
        for count in [-1, True, "1"]:
            state = await self.state()
            state["audit_retry_count"] = count
            with self.assertRaises(ValueError):
                run_auditor(state)

    async def test_schema_rejects_contradictory_decisions_and_confidence(self):
        state = await self.state("")
        data = run_auditor(state).model_dump()
        for update in [
            {"confidence_score": 1.1}, {"confidence_score": -0.1},
            {"retry_required": False}, {"evidence": []},
            {"decision": "pass", "retry_required": False},
        ]:
            with self.subTest(update=update), self.assertRaises(ValidationError):
                AuditReport(**(data | update))

    async def test_graph_retries_once_and_stops_without_verdict(self):
        state = initial_state()
        state["job_listing"].raw_html = ""
        with patch.dict(os.environ, {"NXS_MARKET_SEARCH": "off"}), patch(
            "nxs.graph.archetype.log_to_supabase", new_callable=AsyncMock,
        ) as persist:
            result = await NXS_Archetype.ainvoke(state)
        self.assertEqual(result["audit_retry_count"], 1)
        self.assertEqual(result["current_agent"], "Auditor")
        self.assertEqual(result["audit_report"].decision, "reject")
        self.assertIsNone(result["executive_verdict"])
        self.assertIsNone(result["market_match_score"])
        statuses = [call.args[2] for call in persist.call_args_list]
        self.assertEqual(statuses.count("SCOUTING"), 2)
        self.assertEqual(statuses.count("RETRY_TRIGGERED"), 1)
        self.assertTrue(all(call.args[0] == state["job_id"] for call in persist.call_args_list))

    async def test_graph_closed_listing_stops_immediately(self):
        state = initial_state()
        state["job_listing"].raw_html = "<p>This job is closed.</p>"
        with patch.dict(os.environ, {"NXS_MARKET_SEARCH": "off"}), patch(
            "nxs.graph.archetype.log_to_supabase", new_callable=AsyncMock,
        ):
            result = await NXS_Archetype.ainvoke(state)
        self.assertEqual(result["current_agent"], "Auditor")
        self.assertEqual(result["audit_retry_count"], 0)

    async def test_logging_failure_stops_audit(self):
        state = await self.state()
        with patch("nxs.graph.archetype.log_to_supabase", new_callable=AsyncMock, side_effect=OSError("unavailable")):
            with self.assertRaises(OSError):
                await auditor_node(state)

    async def test_failed_report_write_logs_error_and_stops(self):
        state = await self.state()
        with patch("nxs.graph.archetype.log_to_supabase", new_callable=AsyncMock,
                   side_effect=[None, OSError("unavailable"), None]) as persist:
            with self.assertRaises(OSError):
                await auditor_node(state)
        self.assertEqual(persist.call_args.args[2], "ERROR")

    async def test_repaired_retry_can_pass(self):
        state = initial_state()
        state["job_listing"].company = ""
        state["job_listing"].raw_html = "<p>Build services.</p>"
        with patch.dict(os.environ, {"NXS_MARKET_SEARCH": "off"}), patch(
            "nxs.graph.archetype.log_to_supabase", new_callable=AsyncMock,
        ), patch("nxs.graph.archetype.run_scout", new_callable=AsyncMock,
                 side_effect=[{}, {"company": "Recovered company"}]):
            result = await NXS_Archetype.ainvoke(state)
        self.assertEqual(result["audit_retry_count"], 1)
        self.assertEqual(result["audit_report"].decision, "pass")
        self.assertEqual(result["current_agent"], "Executive")
        self.assertIsNone(result["executive_verdict"])
