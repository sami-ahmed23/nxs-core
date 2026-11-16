import os
import unittest
from datetime import date
from unittest.mock import AsyncMock, patch

from markdown_it import MarkdownIt

from nxs.graph.archetype import NXS_Archetype
from nxs.schemas.models import JobListing
from nxs.weekly import ListingRecord, load_records, main, record_from_logs, render_weekly, week_bounds

JOB_ID = "72464349-bbe1-4d6d-aefa-e634eddf3011"
WEEK = date(2026, 11, 15)


async def pipeline_logs(html, role="Industrial Placement Developer"):
    """Run the real graph offline and return exactly what it would write to agent_logs."""
    state = {
        "job_id": JOB_ID, "current_agent": "Scout", "audit_retry_count": 0,
        "job_listing": JobListing(company="Example", role=role, tech_stack=[],
                                  url="https://example.com/job", raw_html=html),
        "candidate_profile": None, "analyst_report": None, "audit_report": None,
        "assessment_report": None, "nexus_score": None,
        "executive_report": None, "executive_verdict": None,
    }
    with patch.dict(os.environ, {"NXS_MARKET_SEARCH": "off"}), patch(
        "nxs.graph.archetype.log_to_supabase", new_callable=AsyncMock,
    ) as persist:
        await NXS_Archetype.ainvoke(state)
    return [{"agent_name": c.args[1], "status": c.args[2], "reasoning": c.args[3],
             "created_at": f"2026-11-15T10:00:{i:02d}"} for i, c in enumerate(persist.call_args_list)]


OPEN = "<p>About the job: build services in Python, Rust and SQL. Salary £30,000 per annum.</p>"
CLOSED = "<p>About the job: Python services.</p><p>This job is closed.</p>"


def record(**kwargs):
    data = {"company": "A", "role": "R", "technologies": ["Python"], "seniority": "placement",
            "audit_decision": "pass"}
    return ListingRecord(**(data | kwargs))


class RecordTests(unittest.IsolatedAsyncioTestCase):
    async def test_open_listing_facts_come_from_the_real_log_formats(self):
        rec = record_from_logs({"company": "Example", "role": "Dev"}, await pipeline_logs(OPEN))
        self.assertEqual(sorted(rec.technologies), ["Python", "Rust", "SQL"])
        self.assertEqual(rec.seniority, "placement")
        self.assertEqual((rec.salary_low, rec.salary_high), (30000, 30000))
        self.assertEqual(rec.audit_decision, "pass")

    async def test_closed_listing_is_counted_with_its_flag(self):
        rec = record_from_logs({"company": "Example", "role": "Dev"}, await pipeline_logs(CLOSED))
        self.assertEqual(rec.audit_decision, "reject")
        self.assertIn("CLOSED_LISTING", rec.quality_flags)

    async def test_technologies_do_not_depend_on_the_profile(self):
        # Python is in the profile and Rust is not; both must count as asked for.
        rec = record_from_logs({"company": "Example", "role": "Dev"}, await pipeline_logs(OPEN))
        self.assertIn("Python", rec.technologies)
        self.assertIn("Rust", rec.technologies)

    def test_listing_without_analysis_is_skipped(self):
        logs = [{"agent_name": "Scout", "status": "SCOUTING", "reasoning": "x", "created_at": "1"}]
        self.assertIsNone(record_from_logs({"company": "A", "role": "R"}, logs))


class RenderTests(unittest.TestCase):
    def test_small_samples_show_counts_without_shares(self):
        brief = render_weekly([record(), record(technologies=["Python", "SQL"])], WEEK)
        self.assertIn("**Python**: 2 of 2 listings", brief)
        self.assertNotIn("%", brief.split("## Method and limits")[0])

    def test_too_few_salaries_report_no_median(self):
        brief = render_weekly([record(salary_low=25000, salary_high=25000), record()], WEEK)
        self.assertIn("Fewer than 3 stated a salary, so no median is reported.", brief)
        self.assertNotIn("Median stated salary", brief)

    def test_shares_and_median_appear_with_enough_listings(self):
        records = [record(salary_low=20000, salary_high=30000), record(salary_low=30000, salary_high=30000),
                   record(salary_low=40000, salary_high=40000), record(), record(), record()]
        brief = render_weekly(records, WEEK)
        self.assertIn("**Python**: 6 of 6 (100%) listings", brief)
        self.assertIn("3 of 6 (50%) listings stated a usable sterling salary", brief)
        self.assertIn("Median stated salary, taking the midpoint of each range: £30,000.", brief)

    def test_no_personal_fit_judgement_leaks_in(self):
        brief = render_weekly([record(), record(audit_decision="reject",
                                                quality_flags=["CLOSED_LISTING"])], WEEK)
        for personal in ["Nexus Score", "Proceed", "Observe", "Discard", "profile"]:
            self.assertNotIn(personal, brief)
        self.assertIn("Stated as closed when captured: 1 of 2", brief)
        self.assertIn("Passed the audit: 1 of 2 audited listings.", brief)

    def test_seniority_follows_the_ladder(self):
        brief = render_weekly([record(seniority="senior"), record(seniority=None), record(seniority="intern")], WEEK)
        mix = brief.split("## Seniority mix\n\n")[1].split("\n\n")[0].splitlines()
        self.assertEqual([line.split(":")[0] for line in mix], ["- Intern", "- Senior", "- Not stated"])

    def test_demand_context_matches_top_skills(self):
        line = "Python, UK permanent advertised jobs, six months to 2026-11-13: 6,438 adverts."
        brief = render_weekly([record(demand_lines=[line])], WEEK)
        self.assertIn("## Wider market context", brief)
        self.assertIn("6,438 adverts", brief)

    def test_renders_in_substack_safe_markdown(self):
        tokens = MarkdownIt("commonmark").parse(render_weekly([record()], WEEK))
        kinds = {t.type for t in tokens}
        self.assertIn("bullet_list_open", kinds)
        self.assertIn("blockquote_open", kinds)
        self.assertFalse(kinds & {"table_open", "html_block", "html_inline"})

    def test_empty_week_says_so(self):
        self.assertIn("No listings were captured this week", render_weekly([], WEEK))

    def test_week_runs_seven_days_inclusive(self):
        self.assertEqual(week_bounds(WEEK), ("2026-11-09T00:00:00+00:00", "2026-11-16T00:00:00+00:00"))
        self.assertIn("09 November to 15 November 2026", render_weekly([record()], WEEK))


class FakeQuery:
    def __init__(self, rows):
        self.rows = rows

    def __getattr__(self, name):
        return lambda *args, **kwargs: self

    def execute(self):
        return type("Result", (), {"data": self.rows})()


class LoadTests(unittest.IsolatedAsyncioTestCase):
    async def test_loads_records_and_skips_unprocessed_listings(self):
        logs = await pipeline_logs(OPEN)
        listings = [{"id": "1", "company": "Example", "role": "Dev", "created_at": "x"},
                    {"id": "2", "company": "June", "role": "Old", "created_at": "x"}]
        log_sets = [logs, []]

        class Client:
            def table(self, name):
                return FakeQuery(listings if name == "job_listings" else log_sets.pop(0))

        records = load_records(Client(), WEEK)
        self.assertEqual([r.company for r in records], ["Example"])

    def test_cli_writes_a_dated_brief(self):
        out = "/tmp/claude-0/weekly-test"
        with patch("nxs.db.client.get_client"), patch("nxs.weekly.load_records", return_value=[record()]):
            path = main(["--week-ending", "2026-11-15", "--out", out])
        self.assertEqual(path.name, "2026-11-15-weekly-market-brief.md")
        self.assertTrue(path.read_text(encoding="utf-8").startswith("# UK placement market"))
        path.unlink()


if __name__ == "__main__":
    unittest.main()
