from datetime import date, datetime, timezone
import unittest
from unittest.mock import AsyncMock, patch

from nxs.agents.market import collect_public_market, parse_table, role_category
from nxs.agents.analyst import run_analyst
from nxs.schemas.analysis import AnalystReport
from nxs.profile import CANDIDATE_PROFILE
from test_prep import JOB_ID, initial_state


def table(category="Python", *, jobs="100", previous="80", salaries="50", median="£30,000", end="7 Oct 2026"):
    label = f"Permanent jobs citing {category}"
    return f'''<table class="summary">
    <tr><th>UK</th><th>6 months to<br>{end}</th><th>Same period 2025</th></tr>
    <tr><td>{label}</td><td>{jobs}</td><td>{previous}</td></tr>
    <tr><td>As % of all permanent jobs in the UK</td><td>5%</td><td>6%</td></tr>
    <tr><td>Number of salaries quoted</td><td>{salaries}</td><td>40</td></tr>
    <tr><td>Median annual salary (50<sup>th</sup> Percentile)</td><td>{median}</td><td>£28,000</td></tr>
    </table>'''


def fetch(category, day):
    metrics, evidence = parse_table(table(category), category, date(2026, 10, 7))
    return metrics, evidence, "https://www.itjobswatch.co.uk/jobs/uk/python.do", datetime.now(timezone.utc)


class MarketTests(unittest.TestCase):
    def test_reads_current_column_and_real_reporting_period(self):
        result, evidence = parse_table(table(), "Python", date(2026, 10, 7))
        self.assertEqual(result["median"], 30000)
        self.assertEqual(result["jobs"], 100)
        self.assertEqual(result["previous_jobs"], 80)
        self.assertEqual(result["period_end"], "2026-10-07")
        self.assertIn("£30,000", evidence)

    def test_stale_future_wrong_category_and_missing_table_rejected(self):
        for html in [table(end="1 Jan 2025"), table(end="8 Oct 2026"), table("Java"), "<html>blocked</html>"]:
            with self.subTest(html=html), self.assertRaises(ValueError):
                parse_table(html, "Python", date(2026, 10, 7))

    def test_missing_values_not_converted_to_zero(self):
        result, _ = parse_table(table(median="-", previous="-"), "Python", date(2026, 10, 7))
        self.assertIsNone(result["median"])
        self.assertIsNone(result["previous_jobs"])

    def test_category_mapping_preserves_graduate_scope(self):
        self.assertEqual(role_category("Graduate Software Engineer"), "Graduate Software Engineer")
        self.assertEqual(role_category("Software Engineer Intern"), "Software Engineer")
        self.assertIsNone(role_category("Unrecognised role"))

    def test_count_growth_and_share_decline_are_separate(self):
        job = initial_state()["job_listing"].model_copy(update={"role": "Software Engineer Intern", "tech_stack": ["Python"]})
        with patch("nxs.agents.market._fetch", side_effect=fetch):
            market, sources, limitations = collect_public_market(job)
        report = AnalystReport(skill_matches=[], skill_gaps=[], market=market, sources=sources, limitations=limitations)
        self.assertEqual(report.market.salary_benchmark.evidence_status, "supported")
        self.assertIn("+25.0%", market.tech_demand.summary)
        self.assertIn("-1.000 percentage points", market.tech_demand.summary)
        self.assertIn("not a validated internship", market.salary_benchmark.summary)
        self.assertTrue(any("placement-specific salary" in item for item in limitations))

    def test_zero_previous_count_has_no_division_or_infinite_growth(self):
        def zero(category, day):
            metrics, evidence = parse_table(table(category, previous="0"), category, date(2026, 10, 7))
            return metrics, evidence, "https://example.com", datetime.now(timezone.utc)
        job = initial_state()["job_listing"].model_copy(update={"tech_stack": ["Python"]})
        with patch("nxs.agents.market._fetch", side_effect=zero):
            market, _, _ = collect_public_market(job)
        self.assertNotIn("Advert count change", market.tech_demand.summary)

    def test_low_sample_salary_unknown(self):
        def low(category, day):
            metrics, evidence = parse_table(table(category, salaries="2"), category, date(2026, 10, 7))
            return metrics, evidence, "https://example.com", datetime.now(timezone.utc)
        job = initial_state()["job_listing"].model_copy(update={"role": "Software Engineer"})
        with patch("nxs.agents.market._fetch", side_effect=low):
            market, _, _ = collect_public_market(job)
        self.assertEqual(market.salary_benchmark.evidence_status, "unknown")

    def test_provider_failure_is_explicit_unknown(self):
        job = initial_state()["job_listing"].model_copy(update={"role": "Software Engineer", "tech_stack": ["Python"]})
        with patch("nxs.agents.market._fetch", side_effect=ValueError("changed markup")):
            market, sources, limitations = collect_public_market(job)
        self.assertEqual(market.tech_demand.evidence_status, "unknown")
        self.assertEqual(market.salary_benchmark.evidence_status, "unknown")
        self.assertEqual(sources, [])
        self.assertTrue(any("invalid" in item for item in limitations))


class PublicModeTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_mode_without_api_keys(self):
        import os
        job = initial_state()["job_listing"].model_copy(update={"role": "Software Engineer", "tech_stack": ["Python", "SQL"]})
        with patch.dict(os.environ, {"NXS_MARKET_SEARCH": "public", "OPENAI_API_KEY": "", "SERPER_API_KEY": ""}):
            with patch("nxs.agents.market._fetch", side_effect=fetch):
                with patch("nxs.agents.analyst.log_to_supabase", new_callable=AsyncMock):
                    report = await run_analyst(JOB_ID, job, CANDIDATE_PROFILE)
        self.assertEqual(report.skill_matches, ["Python"])
        self.assertEqual(report.skill_gaps, ["SQL"])
        self.assertEqual(report.market.tech_demand.evidence_status, "supported")
        self.assertEqual(len(report.sources), 3)
