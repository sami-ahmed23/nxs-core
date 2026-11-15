import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from markdown_it import MarkdownIt

from nxs.agents.analyst import run_analyst
from nxs.agents.assessor import run_assessor
from nxs.agents.auditor import run_auditor
from nxs.agents.executive import run_executive
from nxs.editorial import brief_filename, escape, load_brief_inputs, main, render_brief
from nxs.profile import CANDIDATE_PROFILE
from nxs.schemas.models import JobListing

JOB_ID = "72464349-bbe1-4d6d-aefa-e634eddf3011"
PARSER = MarkdownIt("commonmark")


def tokens(markdown):
    return PARSER.parse(markdown)


async def scored(company="Example", role="Placement developer", salary="£30,000",
                 seniority="placement", tech=("Python", "TypeScript"),
                 html="<p>About the job: build services using Python and TypeScript.</p>"):
    state = {
        "job_id": JOB_ID, "current_agent": "Scout", "audit_retry_count": 0,
        "job_listing": JobListing(company=company, role=role, salary=salary,
                                  tech_stack=list(tech), seniority=seniority,
                                  url="https://example.com/job", raw_html=html),
        "candidate_profile": CANDIDATE_PROFILE, "analyst_report": None, "audit_report": None,
        "assessment_report": None, "nexus_score": None,
        "executive_report": None, "executive_verdict": None,
    }
    with patch.dict(os.environ, {"NXS_MARKET_SEARCH": "off"}):
        state["analyst_report"] = await run_analyst(JOB_ID, state["job_listing"], CANDIDATE_PROFILE)
    state["audit_report"] = run_auditor(state)
    state["assessment_report"] = run_assessor(state)
    report = run_executive(state)
    return state["job_listing"], state["assessment_report"], report


class RenderTests(unittest.IsolatedAsyncioTestCase):
    async def test_structure_renders_as_substack_supports(self):
        brief = render_brief(*await scored())
        kinds = [t.type for t in tokens(brief)]
        headings = [(t.tag, tokens(brief)[i + 1].content)
                    for i, t in enumerate(tokens(brief)) if t.type == "heading_open"]
        self.assertEqual(headings[0], ("h1", "Placement developer"))
        self.assertEqual([title for tag, title in headings if tag == "h2"],
                         ["Verdict", "Why this job", "Why not", "Score breakdown", "Evidence limits"])
        self.assertIn("blockquote_open", kinds)
        self.assertIn("bullet_list_open", kinds)
        self.assertIn("hr", kinds)
        # Substack's editor has no tables and strips raw HTML.
        self.assertNotIn("table_open", kinds)
        self.assertFalse(any(t.type in ("html_block", "html_inline") for t in tokens(brief)))

    async def test_verdict_badge_leads_the_brief(self):
        job, assessment, report = await scored()
        badge = render_brief(job, assessment, report).split("\n")[4]
        self.assertTrue(badge.startswith("> **NXS VERDICT: PROCEED**"))
        self.assertIn(f"**{report.nexus_score}**/850", badge)
        self.assertIn(f"Grade {report.grade}", badge)

    async def test_listing_link_renders(self):
        brief = render_brief(*await scored())
        links = [c.attrGet("href") for t in tokens(brief) if t.type == "inline"
                 for c in (t.children or []) if c.type == "link_open"]
        self.assertEqual(links, ["https://example.com/job"])

    async def test_figures_match_the_reports(self):
        job, assessment, report = await scored(salary=None)
        brief = render_brief(job, assessment, report)
        self.assertIn(f"Scored {report.nexus_score} of a possible {report.score_ceiling}", brief)
        self.assertIn("**Salary alignment:** not assessed.", brief)
        for component in assessment.components:
            if component.evidence_status == "supported":
                self.assertIn(f"{component.awarded}/{component.available}.", brief)

    async def test_out_of_reach_brief_says_why(self):
        brief = render_brief(*await scored(seniority="senior"))
        self.assertIn("NXS VERDICT: DISCARD", brief)
        self.assertIn("pitched beyond the stated seniority target", brief)

    async def test_captured_text_cannot_inject_formatting(self):
        job, assessment, report = await scored(company="Acme_Ltd *Global*",
                                               role="# Data [Engineer](http://x.test) <b>")
        html = PARSER.render(render_brief(job, assessment, report))
        title, company_line = html.split("\n")[0], html.split("\n")[1]
        # The role renders literally: no link, no raw tag, still a single heading.
        self.assertEqual(title, '<h1># Data [Engineer](http://x.test) &lt;b&gt;</h1>')
        # The company renders literally inside its bold, with no italics injected.
        self.assertTrue(company_line.startswith("<p><strong>Acme_Ltd *Global*</strong>"))
        self.assertNotIn("http://x.test", html.replace("[Engineer](http://x.test)", ""))

    async def test_mismatched_reports_are_refused(self):
        job, assessment, report = await scored()
        _, other, _ = await scored(salary=None)
        with self.assertRaises(ValueError):
            render_brief(job, other, report)


def section(brief, title):
    body = brief.split(f"## {title}\n\n", 1)[1]
    return body.split("\n\n## ", 1)[0].split("\n\n---", 1)[0]


class RegroupTests(unittest.IsolatedAsyncioTestCase):
    DEMAND = ("Python, UK permanent advertised jobs, six months to 2026-11-13: 6,438 adverts. "
              "Same period last year: 4,423. PyTorch, UK permanent advertised jobs, six months to "
              "2026-11-13: 208 adverts. Same period last year: 148.")

    def logged_report(self):
        # Lines as the Executive logged them for the JPMorganChase internship on 13-11-2026.
        from nxs.schemas.executive import ExecutiveReport
        return ExecutiveReport(
            verdict="Observe", grade="C", nexus_score=600, score_ceiling=750, out_of_reach=False,
            reasoning="Logged paragraph.",
            supporting=["Input completeness 150/150. Input completeness 1.00; warnings: SKILL_GAPS.",
                        "Seniority fit 100/100. Listing states intern against a target of intern.",
                        "Technology demand. " + self.DEMAND],
            against=["Skill coverage 50/200. 2 of 8 extracted technologies appear in the profile.",
                     "Audit warning SKILL_GAPS. Extracted mentions absent from the profile: pandas, numpy.",
                     "Audit warning SALARY_UNKNOWN. The listing salary is unknown; no pay judgement was made."],
            evidence_limits=["Salary alignment could not be assessed."],
        )

    def test_reader_facing_grouping(self):
        from nxs.editorial import _regroup
        reasons_for, against, market, limits = _regroup(self.logged_report())
        # Input completeness describes the capture, not a reason to apply.
        self.assertEqual(reasons_for, ["Seniority fit 100/100. Listing states intern against a target of intern."])
        self.assertIn("Skill gaps: Extracted mentions absent from the profile: pandas, numpy.", against)
        self.assertFalse(any("SALARY_UNKNOWN" in line for line in against))
        self.assertIn("The listing salary is unknown; no pay judgement was made.", limits)
        self.assertEqual(len(market), 2)
        self.assertTrue(market[1].startswith("PyTorch, UK permanent advertised jobs"))

    def test_no_figure_is_lost_or_changed(self):
        from nxs.editorial import _regroup
        report = self.logged_report()
        regrouped = " ".join(" ".join(group) for group in _regroup(report))
        for figure in ["6,438", "4,423", "208", "148", "50/200", "100/100", "pandas, numpy"]:
            self.assertIn(figure, regrouped)

    async def test_live_pipeline_lines_still_match_the_formats(self):
        # Guards against the Executive changing its line format underneath Editorial.
        brief = render_brief(*await scored())
        self.assertNotIn("Input completeness", section(brief, "Why this job"))
        self.assertNotIn("Audit warning", brief)


class EscapeTests(unittest.TestCase):
    def test_line_start_markers_are_neutralised(self):
        for text in ["# heading", "- item", "> quote", "1. step"]:
            with self.subTest(text=text):
                self.assertFalse(any(t.type in ("heading_open", "bullet_list_open",
                                                "blockquote_open", "ordered_list_open")
                                     for t in tokens(escape(text))))

    def test_linkedin_tracking_is_stripped_and_other_links_kept(self):
        from nxs.editorial import _link
        self.assertEqual(_link("https://www.linkedin.com/jobs/view/4488145253/?eBP=Cw&refId=x%3D"),
                         "https://www.linkedin.com/jobs/view/4488145253/")
        self.assertEqual(_link("https://jobs.example.com/apply?id=7 (UK)"),
                         "https://jobs.example.com/apply?id=7%20%28UK%29")

    def test_ordinary_text_is_untouched(self):
        self.assertEqual(escape("Python, SQL and TypeScript (2027)."),
                         "Python, SQL and TypeScript (2027).")


class FakeQuery:
    def __init__(self, rows):
        self.rows = rows

    def __getattr__(self, name):
        return lambda *args, **kwargs: self

    def execute(self):
        return type("Result", (), {"data": self.rows})()


class FakeClient:
    def __init__(self, tables):
        self.tables = tables
        self.calls = []

    def table(self, name):
        self.calls.append(name)
        rows = self.tables[name]
        return FakeQuery(rows.pop(0) if name == "agent_logs" else rows)


class LoadingTests(unittest.IsolatedAsyncioTestCase):
    async def test_loads_and_validates_logged_reports(self):
        job, assessment, report = await scored()
        client = FakeClient({
            "job_listings": [job.model_dump()],
            "agent_logs": [[{"reasoning": assessment.model_dump_json()}],
                           [{"reasoning": report.model_dump_json()}]],
        })
        loaded = load_brief_inputs(client, JOB_ID)
        self.assertEqual(loaded[0].role, job.role)
        self.assertEqual(loaded[1], assessment)
        self.assertEqual(loaded[2], report)

    def test_missing_listing_or_unscored_listing_is_refused(self):
        with self.assertRaises(LookupError):
            load_brief_inputs(FakeClient({"job_listings": []}), JOB_ID)
        with self.assertRaises(LookupError):
            load_brief_inputs(FakeClient({
                "job_listings": [{"company": "A", "role": "B", "url": "https://a.test"}],
                "agent_logs": [[]],
            }), JOB_ID)

    async def test_cli_writes_a_dated_file(self):
        job, assessment, report = await scored()
        out = "/tmp/claude-0/editorial-test"
        with patch("nxs.db.client.get_client"), patch(
            "nxs.editorial.load_brief_inputs", return_value=(job, assessment, report),
        ):
            path = main([JOB_ID, "--out", out])
        self.assertTrue(path.read_text(encoding="utf-8").startswith("# Placement developer"))
        path.unlink()

    def test_filename_is_dated_and_safe(self):
        job = JobListing(company="JPMorganChase", role="2027 ML (NLP) - Internship",
                         tech_stack=[], url="https://a.test", raw_html="")
        name = brief_filename(job, datetime(2026, 11, 15, tzinfo=timezone.utc))
        self.assertEqual(name, "2026-11-15-jpmorganchase-2027-ml-nlp-internship.md")


if __name__ == "__main__":
    unittest.main()
