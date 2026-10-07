import unittest
from unittest.mock import AsyncMock, patch

from pydantic import ValidationError

from nxs.db.listener import on_new_job
from nxs.graph.archetype import NXS_Archetype, scout_node
from nxs.logger import log_to_supabase
from nxs.schemas.models import JobListing
from nxs.schemas.analysis import AnalystReport, MarketAnalysis, MarketFinding


JOB_ID = "72464349-bbe1-4d6d-aefa-e634eddf3011"


def initial_state():
    return {
        "job_id": JOB_ID,
        "current_agent": "Scout",
        "job_listing": JobListing(
            company="Example", role="Placement developer", salary=None,
            tech_stack=[], url="https://example.com/job",
            raw_html="<p>About the job: Python and SQL. Salary £25,000.</p>",
        ),
        "candidate_profile": None, "audit_report": None,
        "analyst_report": None,
        "market_match_score": None, "executive_verdict": None,
    }


class PrepTests(unittest.IsolatedAsyncioTestCase):
    async def test_listener_retains_row_identity(self):
        record = initial_state()["job_listing"].model_dump() | {"id": JOB_ID}
        with patch("nxs.db.listener.NXS_Archetype.ainvoke", new_callable=AsyncMock) as invoke:
            await on_new_job(record)
        self.assertEqual(invoke.call_args.args[0]["job_id"], JOB_ID)

    async def test_log_payload_matches_live_table(self):
        with patch("nxs.logger._insert_log") as insert:
            await log_to_supabase(JOB_ID, "Scout", "SCOUTING", "Starting extraction.")
        self.assertEqual(insert.call_args.args[0], {
            "job_id": JOB_ID, "agent_name": "Scout",
            "status": "SCOUTING", "reasoning": "Starting extraction.",
        })

    async def test_invalid_identity_or_status_never_writes(self):
        for job_id, status in [("not-a-uuid", "SCOUTING"), (JOB_ID, "INVENTED")]:
            with self.subTest(job_id=job_id, status=status):
                with patch("nxs.logger._insert_log") as insert:
                    with self.assertRaises(ValidationError):
                        await log_to_supabase(job_id, "Scout", status, "Starting.")
                    insert.assert_not_called()

    async def test_write_failure_propagates(self):
        with patch("nxs.logger._insert_log", side_effect=OSError("unavailable")):
            with self.assertRaisesRegex(RuntimeError, "Could not persist"):
                await log_to_supabase(JOB_ID, "Scout", "SCOUTING", "Starting.")

    async def test_graph_retains_identity_and_logs_scout(self):
        unknown = MarketFinding(evidence_status="unknown", summary="No evidence in this test.", citations=[])
        report = AnalystReport(
            skill_matches=["Python"], skill_gaps=["SQL"],
            market=MarketAnalysis(salary_benchmark=unknown, tech_demand=unknown),
            sources=[], limitations=[],
        )
        with patch("nxs.graph.archetype.log_to_supabase", new_callable=AsyncMock) as persist:
            with patch("nxs.graph.archetype.run_analyst", new_callable=AsyncMock, return_value=report):
                result = await NXS_Archetype.ainvoke(initial_state())
        self.assertEqual(result["job_id"], JOB_ID)
        self.assertEqual(result["job_listing"].tech_stack, ["Python", "SQL"])
        self.assertIsNotNone(result["candidate_profile"])
        self.assertEqual(result["analyst_report"], report)
        self.assertEqual([call.args[2] for call in persist.call_args_list], ["SCOUTING", "SCOUT_COMPLETE", "ANALYSING", "ANALYST_COMPLETE"])
        self.assertTrue(all(call.args[0] == JOB_ID for call in persist.call_args_list))

    async def test_scout_failure_logs_error_and_stops(self):
        with patch("nxs.graph.archetype.log_to_supabase", new_callable=AsyncMock) as persist:
            with patch("nxs.graph.archetype.run_scout", new_callable=AsyncMock, side_effect=ValueError("bad input")):
                with self.assertRaises(ValueError):
                    await scout_node(initial_state())
        self.assertEqual([call.args[2] for call in persist.call_args_list], ["SCOUTING", "ERROR"])


if __name__ == "__main__":
    unittest.main()
