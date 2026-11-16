"""Weekly market brief for The Metric: what UK placement listings asked for this week.

Every figure here describes the listings, never the candidate. Nexus Scores and verdicts
are fit judgements against one profile, so they are deliberately excluded.

Usage: python -m nxs.weekly [--week-ending YYYY-MM-DD] [--out briefs]
"""

from __future__ import annotations

import argparse
import re
import statistics
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from pydantic import BaseModel, Field

from nxs.agents.assessor import parse_salary
from nxs.agents.scout import SENIORITY_LADDER
from nxs.editorial import _TECH_BOUNDARY, escape
from nxs.schemas.analysis import AnalystReport
from nxs.schemas.models import AuditReport

# Below these sample sizes a share or a median says more about chance than the market.
MIN_FOR_SHARES = 5
MIN_FOR_MEDIAN = 3
TOP_SKILLS = 10

# Scout's SCOUT_COMPLETE log, in the fixed format written by nxs/graph/archetype.py.
_SCOUT_LOG = re.compile(r"Salary: (?P<salary>.+?)\. Seniority: (?P<seniority>[a-z]+)\. Candidate profile loaded\.$")
_QUALITY_CODES = {
    "CLOSED_LISTING": "Stated as closed when captured",
    "TALENT_POOL_WORDING": "Talent-pool or speculative wording",
}


class ListingRecord(BaseModel):
    """Profile-independent facts about one captured listing."""

    company: str
    role: str
    technologies: list[str]
    seniority: str | None = None
    salary_low: int | None = None
    salary_high: int | None = None
    audit_decision: str | None = None
    quality_flags: list[str] = Field(default_factory=list)
    demand_lines: list[str] = Field(default_factory=list)


def record_from_logs(row: dict, logs: list[dict]) -> ListingRecord | None:
    """Build a record from a listing and its agent logs, or None if Analyst never finished."""
    latest = {}
    for log in sorted(logs, key=lambda item: item["created_at"]):
        latest[(log["agent_name"], log["status"])] = log["reasoning"]

    analyst_json = latest.get(("Analyst", "ANALYST_COMPLETE"))
    if analyst_json is None:
        return None
    analysis = AnalystReport.model_validate_json(analyst_json)
    # Matches plus gaps is every extracted technology, whatever the profile holds.
    seen: dict[str, str] = {}
    for tech in analysis.skill_matches + analysis.skill_gaps:
        seen.setdefault(tech.casefold(), tech)

    seniority, salary = None, None
    scout = _SCOUT_LOG.search(latest.get(("Scout", "SCOUT_COMPLETE"), ""))
    if scout:
        seniority = scout["seniority"] if scout["seniority"] in SENIORITY_LADDER else None
        salary = parse_salary(scout["salary"])

    audit = None
    for status in ("AUDIT_PASSED", "RETRY_TRIGGERED", "RED_FLAG_DETECTED"):
        if ("Auditor", status) in latest:
            audit = AuditReport.model_validate_json(latest[("Auditor", status)])
            break
    demand = analysis.market.tech_demand
    return ListingRecord(
        company=row["company"], role=row["role"], technologies=list(seen.values()),
        seniority=seniority,
        salary_low=salary[0] if salary else None, salary_high=salary[1] if salary else None,
        audit_decision=audit.decision if audit else None,
        quality_flags=[item.code for item in audit.evidence if item.code in _QUALITY_CODES] if audit else [],
        demand_lines=_TECH_BOUNDARY.split(demand.summary) if demand.evidence_status == "supported" else [],
    )


def _share(count: int, total: int) -> str:
    return f"{count} of {total}" + (f" ({round(100 * count / total)}%)" if total >= MIN_FOR_SHARES else "")


def render_weekly(records: list[ListingRecord], week_ending: date) -> str:
    """Return the weekly brief as Markdown in the same Substack-safe subset as Editorial."""
    total = len(records)
    start = week_ending - timedelta(days=6)
    period = f"{start.strftime('%d %B')} to {week_ending.strftime('%d %B %Y')}"
    title = f"# UK placement market: {period}"
    if total == 0:
        return f"{title}\n\nNo listings were captured this week, so there is nothing to report.\n"

    skills = Counter(tech for record in records for tech in record.technologies)
    levels = Counter(record.seniority or "not stated" for record in records)
    salaried = [record for record in records if record.salary_low is not None]
    audited = [record for record in records if record.audit_decision]
    passed = sum(1 for record in audited if record.audit_decision == "pass")
    flags = Counter(flag for record in records for flag in record.quality_flags)

    skill_lines = [f"- **{escape(tech)}**: {_share(count, total)} listings"
                   for tech, count in skills.most_common(TOP_SKILLS)] or ["- No known technologies were extracted."]
    order = {level: index for index, level in enumerate(SENIORITY_LADDER + ["not stated"])}
    level_lines = [f"- {level.capitalize()}: {_share(count, total)}"
                   for level, count in sorted(levels.items(), key=lambda item: order[item[0]])]

    salary_lines = [f"- {_share(len(salaried), total)} listings stated a usable sterling salary."]
    if len(salaried) >= MIN_FOR_MEDIAN:
        median = statistics.median((r.salary_low + r.salary_high) / 2 for r in salaried)
        salary_lines.append(f"- Median stated salary, taking the midpoint of each range: £{round(median):,}.")
    elif salaried:
        salary_lines.append(f"- Fewer than {MIN_FOR_MEDIAN} stated a salary, so no median is reported.")

    quality_lines = [f"- Passed the audit: {_share(passed, len(audited))} audited listings."] if audited else []
    quality_lines += [f"- {_QUALITY_CODES[code]}: {_share(count, total)}" for code, count in flags.most_common()]
    quality_lines = quality_lines or ["- No audit results were recorded."]

    # Public demand figures for the most requested skills, taken from the latest listing citing each.
    demand: dict[str, str] = {}
    for record in records:
        for line in record.demand_lines:
            demand[line.split(",", 1)[0].casefold()] = line
    demand_lines = [f"- {escape(demand[tech.casefold()])}" for tech, _ in skills.most_common(3)
                    if tech.casefold() in demand]

    headline = (f"> **{total} listings captured** · {_share(len(salaried), total)} stated a salary"
                + (f" · {_share(passed, len(audited))} passed audit" if audited else ""))
    sections = [
        title, "", headline, "",
        "## Most requested skills", "", *skill_lines, "",
        "## Seniority mix", "", *level_lines, "",
        "## Salary transparency", "", *salary_lines, "",
        "## Listing quality", "", *quality_lines, "",
    ]
    if demand_lines:
        sections += ["## Wider market context", "", *demand_lines, ""]
    sections += [
        "## Method and limits", "",
        f"- This is a sample of {total} listings captured by one person, not a survey of the market.",
        "- Skills come from a fixed list of known technologies, so anything outside it is not counted.",
        "- Seniority is taken from the role title, or from programme wording when the title is silent.",
        f"- Shares appear only from {MIN_FOR_SHARES} listings and a median only from {MIN_FOR_MEDIAN} stated salaries.",
        "", "---", "",
        "*Nexus Intelligence · The Metric. Figures describe the listings captured, never a "
        "candidate. Wider market figures come from cited public statistics.*", "",
    ]
    return "\n".join(sections)


def week_bounds(week_ending: date) -> tuple[str, str]:
    """Return ISO timestamps covering the seven days ending on week_ending, inclusive."""
    start = datetime.combine(week_ending - timedelta(days=6), time.min, timezone.utc)
    end = datetime.combine(week_ending + timedelta(days=1), time.min, timezone.utc)
    return start.isoformat(), end.isoformat()


def load_records(client, week_ending: date) -> list[ListingRecord]:
    start, end = week_bounds(week_ending)
    rows = (client.table("job_listings").select("id, company, role, created_at")
            .gte("created_at", start).lt("created_at", end).execute().data)
    records = []
    for row in rows:
        logs = (client.table("agent_logs").select("agent_name, status, reasoning, created_at")
                .eq("job_id", row["id"]).execute().data)
        record = record_from_logs(row, logs)
        if record is not None:
            records.append(record)
    return records


def main(argv: list[str] | None = None) -> Path:
    parser = argparse.ArgumentParser(description="Render the weekly UK placement market brief.")
    parser.add_argument("--week-ending", type=date.fromisoformat,
                        default=datetime.now(timezone.utc).date())
    parser.add_argument("--out", default="briefs")
    args = parser.parse_args(argv)

    from nxs.db.client import get_client  # Imported late so tests never need credentials.

    records = load_records(get_client(), args.week_ending)
    path = Path(args.out) / f"{args.week_ending.isoformat()}-weekly-market-brief.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_weekly(records, args.week_ending), encoding="utf-8")
    print(f"Weekly brief for {len(records)} listings written to {path}")
    return path


if __name__ == "__main__":
    main()
