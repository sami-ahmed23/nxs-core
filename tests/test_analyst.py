import os
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock, patch

from pydantic import ValidationError

from nxs.agents.analyst import _match_skills, _search, _summarise, run_analyst
from nxs.graph.archetype import NXS_Archetype, analyst_node
from nxs.db.listener import start_listener
from nxs.profile import CANDIDATE_PROFILE
from nxs.schemas.analysis import AnalystReport, Citation, MarketAnalysis, MarketFinding, SearchSource
from test_prep import JOB_ID, initial_state


def unknown():
    return MarketFinding(evidence_status="unknown", summary="Insufficient evidence.", citations=[])


def market():
    return MarketAnalysis(salary_benchmark=unknown(), tech_demand=unknown())


def search_result():
    return {"organic": [{
        "title": "Placement salary survey",
        "link": "https://example.com/survey",
        "snippet": "UK developer placements in 2026 pay £25,000 annually.",
    }]}


def source():
    return SearchSource(
        source_id="salary-1", topic="salary", url="https://example.com/survey",
        title="Placement salary survey", snippet=search_result()["organic"][0]["snippet"],
        query="UK placements salary", retrieved_at=datetime.now(timezone.utc),
    )


def supported_market(source_id="salary-1", quote=None):
    return MarketAnalysis(
        salary_benchmark=MarketFinding(
            evidence_status="supported", summary="The survey snippet reports £25,000 annually.",
            citations=[Citation(source_id=source_id, quote=quote or source().snippet)],
        ), tech_demand=unknown(),
    )


class EvidenceTests(unittest.TestCase):
    def test_supported_finding_requires_citation(self):
        with self.assertRaises(ValidationError):
            MarketFinding(evidence_status="supported", summary="Salary known.", citations=[])

    def test_unknown_cannot_claim_evidence(self):
        with self.assertRaises(ValidationError):
            MarketFinding(evidence_status="unknown", summary="Unknown.", citations=[Citation(source_id="x", quote="x")])

    def test_invented_source_or_quote_is_rejected(self):
        for analysis in [supported_market("invented"), supported_market(quote="£50,000 annually")]:
            with self.subTest(analysis=analysis):
                with self.assertRaises(ValidationError):
                    AnalystReport(skill_matches=[], skill_gaps=[], market=analysis, sources=[source()], limitations=[])

    def test_topic_mismatch_is_rejected(self):
        evidence = source().model_copy(update={"topic": "demand"})
        with self.assertRaises(ValidationError):
            AnalystReport(skill_matches=[], skill_gaps=[], market=supported_market(), sources=[evidence], limitations=[])

    def test_real_quote_is_accepted(self):
        report = AnalystReport(skill_matches=[], skill_gaps=[], market=supported_market(), sources=[source()], limitations=[])
        self.assertEqual(report.market.salary_benchmark.evidence_status, "supported")

    def test_skill_matching_is_case_insensitive_and_deduplicated(self):
        job = initial_state()["job_listing"].model_copy(update={"tech_stack": ["python", " Python ", "SQL", "", "C++"]})
        self.assertEqual(_match_skills(job, CANDIDATE_PROFILE), (["python"], ["SQL", "C++"]))


class AnalystTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"NXS_MARKET_SEARCH": "serper", "SERPER_API_KEY": "test-only"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.logging = patch("nxs.agents.analyst.log_to_supabase", new_callable=AsyncMock)
        self.persist = self.logging.start()
        self.addCleanup(self.logging.stop)
        self.job = initial_state()["job_listing"].model_copy(update={"tech_stack": ["Python", "SQL"]})

    async def test_search_failure_is_explicit_unknown_without_model_call(self):
        with patch("nxs.agents.analyst._search", side_effect=TimeoutError):
            with patch("nxs.agents.analyst._summarise") as summarise:
                report = await run_analyst(JOB_ID, self.job, CANDIDATE_PROFILE)
        summarise.assert_not_called()
        self.assertEqual(report.sources, [])
        self.assertEqual(report.market.salary_benchmark.evidence_status, "unknown")
        self.assertEqual(report.market.tech_demand.evidence_status, "unknown")
        self.assertEqual(sum("search failed" in item for item in report.limitations), 2)
        self.assertEqual([call.args[2] for call in self.persist.call_args_list].count("ERROR"), 2)

    async def test_empty_results_are_unknown(self):
        with patch("nxs.agents.analyst._search", return_value={"organic": []}):
            with patch("nxs.agents.analyst._summarise") as summarise:
                report = await run_analyst(JOB_ID, self.job, CANDIDATE_PROFILE)
        summarise.assert_not_called()
        self.assertEqual(report.market.salary_benchmark.evidence_status, "unknown")

    async def test_malformed_sources_are_excluded(self):
        with patch("nxs.agents.analyst._search", return_value={"organic": [None, {"link": "file:///secret"}]}):
            report = await run_analyst(JOB_ID, self.job, CANDIDATE_PROFILE)
        self.assertEqual(report.sources, [])
        self.assertTrue(any("unusable" in item for item in report.limitations))

    async def test_partial_search_failure_preserves_available_evidence(self):
        with patch("nxs.agents.analyst._search", side_effect=[search_result(), TimeoutError()]):
            with patch("nxs.agents.analyst._summarise", return_value=supported_market()):
                report = await run_analyst(JOB_ID, self.job, CANDIDATE_PROFILE)
        self.assertEqual(report.market.salary_benchmark.evidence_status, "supported")
        self.assertEqual(report.market.tech_demand.evidence_status, "unknown")
        self.assertEqual(report.skill_matches, ["Python"])
        self.assertEqual(report.skill_gaps, ["SQL"])

    async def test_hallucinated_citation_rejects_report(self):
        with patch("nxs.agents.analyst._search", return_value=search_result()):
            with patch("nxs.agents.analyst._summarise", return_value=supported_market("invented")):
                with self.assertRaises(ValidationError):
                    await run_analyst(JOB_ID, self.job, CANDIDATE_PROFILE)

    async def test_missing_keys_fail_before_search(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "", "SERPER_API_KEY": ""}):
            with patch("nxs.agents.analyst._search") as search:
                with self.assertRaisesRegex(ValueError, "Configure"):
                    await run_analyst(JOB_ID, self.job, CANDIDATE_PROFILE)
        search.assert_not_called()

    async def test_missing_keys_stop_listener_before_database_access(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "", "SERPER_API_KEY": ""}):
            with patch("nxs.db.listener.acreate_client", new_callable=AsyncMock) as connect:
                with self.assertRaisesRegex(ValueError, "Configure"):
                    await start_listener()
        connect.assert_not_called()

    async def test_invalid_analysis_stops_graph_before_auditor(self):
        with patch("nxs.graph.archetype.log_to_supabase", new_callable=AsyncMock) as persist:
            with patch("nxs.graph.archetype.run_analyst", new_callable=AsyncMock, side_effect=ValueError("Invalid analysis output")):
                with self.assertRaises(ValueError):
                    await NXS_Archetype.ainvoke(initial_state())
        statuses = [call.args[2] for call in persist.call_args_list]
        self.assertEqual(statuses, ["SCOUTING", "SCOUT_COMPLETE", "ANALYSING", "ERROR"])

    async def test_missing_profile_logs_failure(self):
        with patch("nxs.graph.archetype.log_to_supabase", new_callable=AsyncMock) as persist:
            with self.assertRaisesRegex(ValueError, "candidate profile"):
                await analyst_node(initial_state())
        self.assertEqual(persist.call_args.args[2], "ERROR")


class ProviderAdapterTests(unittest.TestCase):
    """Mock only the optional search HTTP boundary."""

    def test_real_serper_tool_processes_response_and_uses_uk_locale(self):
        response = Mock()
        response.json.return_value = search_result()
        with patch.dict(os.environ, {"SERPER_API_KEY": "test-only"}):
            with patch("requests.post", return_value=response) as request:
                result = _search("UK Python placement salary")
        self.assertEqual(result["organic"][0]["link"], "https://example.com/survey")
        self.assertEqual(request.call_args.kwargs["json"]["gl"], "gb")
        self.assertEqual(request.call_args.kwargs["timeout"], 10)

    def test_deterministic_summary_does_not_invent_market_trends(self):
        evidence = source().model_copy(update={"topic": "demand", "source_id": "demand-1", "snippet": "UK Python placements are advertised."})
        job = initial_state()["job_listing"].model_copy(update={"tech_stack": ["Python"]})
        result = _summarise(job, [evidence])
        self.assertEqual(result.salary_benchmark.evidence_status, "unknown")
        self.assertEqual(result.tech_demand.evidence_status, "supported")
        self.assertIn("mentions only", result.tech_demand.summary)
        self.assertEqual(result.tech_demand.citations[0].quote, evidence.snippet)

    def test_unrelated_search_results_do_not_support_demand(self):
        result = _summarise(initial_state()["job_listing"], [source()])
        self.assertEqual(result.tech_demand.evidence_status, "unknown")


class LocalModeTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_mode_needs_no_keys_and_never_calls_external_search(self):
        with patch.dict(os.environ, {"NXS_MARKET_SEARCH": "off", "OPENAI_API_KEY": "", "SERPER_API_KEY": ""}):
            with patch("nxs.agents.analyst._search") as search:
                with patch("nxs.agents.analyst.log_to_supabase", new_callable=AsyncMock):
                    job = initial_state()["job_listing"].model_copy(update={"tech_stack": ["Python", "SQL"]})
                    report = await run_analyst(JOB_ID, job, CANDIDATE_PROFILE)
        search.assert_not_called()
        self.assertEqual(report.skill_matches, ["Python"])
        self.assertEqual(report.skill_gaps, ["SQL"])
        self.assertEqual(report.sources, [])
        self.assertEqual(report.market.salary_benchmark.evidence_status, "unknown")
        self.assertEqual(report.market.tech_demand.evidence_status, "unknown")

    async def test_typo_in_mode_is_rejected(self):
        with patch.dict(os.environ, {"NXS_MARKET_SEARCH": "serpr"}):
            with self.assertRaisesRegex(ValueError, "off or serper"):
                await run_analyst(JOB_ID, initial_state()["job_listing"], CANDIDATE_PROFILE)


if __name__ == "__main__":
    unittest.main()
