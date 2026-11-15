"""Issue #9: render a scored listing as Substack-ready Markdown for The Metric.

Only constructs Substack's editor supports are emitted: headings, bold, bullet lists,
block quotes, links and a horizontal rule. No tables and no HTML. Every figure comes from
the Assessor and Executive reports; nothing is added here.

Usage: python -m nxs.editorial <job_id> [--out briefs]
"""

from __future__ import annotations

import argparse
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from nxs.agents.executive import headline_for
from nxs.schemas.assessment import SCORE_CEILING, AssessmentReport
from nxs.schemas.executive import ExecutiveReport
from nxs.schemas.models import JobListing

_LABELS = {
    "skill_coverage": "Skill coverage",
    "audit_integrity": "Input completeness",
    "seniority_fit": "Seniority fit",
    "salary_alignment": "Salary alignment",
}
_VERDICT_STATUSES = ("PROCEED", "OBSERVE", "DISCARD")

# Characters that would otherwise turn captured text into formatting or links.
_INLINE_SPECIAL = re.compile(r"([\\`*_\[\]<>|])")
_LINE_START = re.compile(r"^(\s*)([#>+\-]|\d+\.)(?=\s)")


def escape(text: str) -> str:
    """Neutralise Markdown syntax in text that came from a listing, not from this module."""
    text = " ".join(text.split())
    text = _INLINE_SPECIAL.sub(r"\\\1", text)
    return _LINE_START.sub(lambda m: m.group(1) + "\\" + m.group(2), text)


def _link(url: str) -> str:
    url = url.strip()
    parts = urlsplit(url)
    # LinkedIn appends hundreds of characters of tracking state; the job ID alone is the listing.
    if parts.hostname and parts.hostname.endswith("linkedin.com") and parts.path.startswith("/jobs/view/"):
        url = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    # Parentheses and spaces would end a Markdown link early.
    return url.replace(" ", "%20").replace("(", "%28").replace(")", "%29")


def _unique(lines: list[str]) -> list[str]:
    seen: set[str] = set()
    return [line for line in lines if not (line in seen or seen.add(line))]


def _bullets(lines: list[str], empty: str) -> list[str]:
    lines = _unique(lines)
    return [f"- {escape(line)}" for line in lines] if lines else [f"- {empty}"]


# The Executive writes its lines in fixed formats (see nxs/agents/executive.py). Editorial
# regroups them for a reader; it never rewords a figure or changes a judgement.
_MARKET_PREFIXES = ("Technology demand. ", "Salary benchmark. ")
_AUDIT_WARNING = re.compile(r"^Audit warning ([A-Z_]+)\. (.*)$")
# Warnings about what the capture lacked, not about the job itself.
_DATA_LIMIT_CODES = {"SALARY_UNKNOWN", "MARKET_EVIDENCE_INCOMPLETE", "TECHNOLOGIES_UNKNOWN"}
# Demand prose lists one technology per run of sentences; split it into one bullet each.
_TECH_BOUNDARY = re.compile(r"(?<=\.)\s+(?=[^.,]+, UK permanent advertised jobs)")


def _regroup(report: ExecutiveReport) -> tuple[list[str], list[str], list[str], list[str]]:
    """Split the Executive's lines into reasons for, reasons against, market context and limits."""
    reasons_for, market = [], []
    for line in report.supporting:
        prefix = next((p for p in _MARKET_PREFIXES if line.startswith(p)), None)
        if prefix:
            market.extend(_TECH_BOUNDARY.split(line[len(prefix):]))
        elif not line.startswith(_LABELS["audit_integrity"] + " "):
            # Input completeness describes the capture, so it appears in the breakdown only.
            reasons_for.append(line)

    reasons_against, limits = [], list(report.evidence_limits)
    for line in report.against:
        warning = _AUDIT_WARNING.match(line)
        if warning and warning.group(1) in _DATA_LIMIT_CODES:
            limits.append(warning.group(2))
        elif warning:
            reasons_against.append(f"{warning.group(1).replace('_', ' ').capitalize()}: {warning.group(2)}")
        else:
            reasons_against.append(line)
    return reasons_for, reasons_against, market, limits


def render_brief(job: JobListing, assessment: AssessmentReport, report: ExecutiveReport) -> str:
    """Return the brief as Markdown. The reports must describe the same scored listing."""
    if assessment.nexus_score != report.nexus_score or assessment.score_ceiling != report.score_ceiling:
        raise ValueError("Assessment and Executive reports describe different scores")

    badge = (f"> **NXS VERDICT: {report.verdict.upper()}** · Nexus Score "
             f"**{report.nexus_score}**/{SCORE_CEILING} · Grade {report.grade}")
    summary = (f"Scored {report.nexus_score} of a possible {report.score_ceiling} on the "
               f"evidence captured. {headline_for(report.verdict, report.out_of_reach, report.score_ceiling)}")

    breakdown = [
        f"- **{_LABELS[c.name]}:** "
        + (f"{c.awarded}/{c.available}. " if c.evidence_status == "supported" else "not assessed. ")
        + escape(c.detail)
        for c in assessment.components
    ]
    reasons_for, reasons_against, market, limits = _regroup(report)
    market_section = ["## Market context", "", *_bullets(market, ""), ""] if market else []

    lines = [
        f"# {escape(job.role)}",
        "",
        f"**{escape(job.company)}** · [View the listing]({_link(job.url)})",
        "",
        badge,
        "",
        "## Verdict",
        "",
        summary,
        "",
        "## Why this job",
        "",
        *_bullets(reasons_for, "No component scored in favour."),
        "",
        "## Why not",
        "",
        *_bullets(reasons_against, "No component scored against."),
        "",
        "## Score breakdown",
        "",
        *breakdown,
        "",
        *market_section,
        "## Evidence limits",
        "",
        *_bullets(limits, "None recorded."),
        "",
        "---",
        "",
        "*Nexus Intelligence · The Metric. Every figure comes from the captured listing and "
        "cited public sources; missing evidence lowers the achievable score rather than "
        "being estimated.*",
        "",
    ]
    return "\n".join(lines)


def load_brief_inputs(client, job_id: str) -> tuple[JobListing, AssessmentReport, ExecutiveReport]:
    """Read the listing and its latest score and verdict logs, validating each one."""
    rows = (client.table("job_listings").select("company, role, salary, tech_stack, url, raw_html")
            .eq("id", job_id).limit(1).execute().data)
    if not rows:
        raise LookupError(f"No job_listings row for {job_id}")
    row = rows[0]
    job = JobListing(company=row["company"], role=row["role"], salary=row.get("salary"),
                     tech_stack=row.get("tech_stack") or [], url=row.get("url") or "",
                     raw_html=row.get("raw_html") or "")

    def latest(agent: str, statuses: tuple[str, ...]) -> str:
        logs = (client.table("agent_logs").select("reasoning").eq("job_id", job_id)
                .eq("agent_name", agent).in_("status", list(statuses))
                .order("created_at", desc=True).limit(1).execute().data)
        if not logs:
            raise LookupError(f"No {agent} result is logged for {job_id}; run the pipeline first")
        return logs[0]["reasoning"]

    assessment = AssessmentReport.model_validate_json(latest("Assessor", ("SCORE_COMPLETE",)))
    report = ExecutiveReport.model_validate_json(latest("Executive", _VERDICT_STATUSES))
    return job, assessment, report


def brief_filename(job: JobListing, today: datetime | None = None) -> str:
    day = (today or datetime.now(timezone.utc)).strftime("%Y-%m-%d")
    slug = re.sub(r"[^a-z0-9]+", "-", f"{job.company} {job.role}".lower()).strip("-")[:80]
    return f"{day}-{slug}.md"


def main(argv: list[str] | None = None) -> Path:
    parser = argparse.ArgumentParser(description="Render a scored listing as a Substack brief.")
    parser.add_argument("job_id")
    parser.add_argument("--out", default="briefs")
    args = parser.parse_args(argv)

    from nxs.db.client import get_client  # Imported late so tests never need credentials.

    job, assessment, report = load_brief_inputs(get_client(), args.job_id)
    path = Path(args.out) / brief_filename(job)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_brief(job, assessment, report), encoding="utf-8")
    print(f"Brief written to {path}")
    return path


if __name__ == "__main__":
    main()
