"""Synthesis of every agent's output into one verdict. Nothing here is generated prose."""

from __future__ import annotations

from nxs.schemas.executive import DISCARD_BELOW, ExecutiveReport, grade_for, verdict_for
from nxs.schemas.models import PlacementState

# A component carrying this share of its weight counts for the listing, or against it.
SUPPORTING_RATIO = 0.75
AGAINST_RATIO = 0.5

_LABELS = {
    "skill_coverage": "Skill coverage",
    "audit_integrity": "Input completeness",
    "seniority_fit": "Seniority fit",
    "salary_alignment": "Salary alignment",
}


def run_executive(state: PlacementState) -> ExecutiveReport:
    """Weigh the Assessor's components, the audit findings and the market evidence."""
    assessment = state.get("assessment_report")
    audit = state.get("audit_report")
    analysis = state.get("analyst_report")
    if assessment is None or audit is None or analysis is None:
        raise ValueError("Executive requires an assessment, an audit and an Analyst report")

    supporting: list[str] = []
    against: list[str] = []
    limits: list[str] = []

    for component in assessment.components:
        label = _LABELS[component.name]
        if component.evidence_status == "unknown":
            limits.append(f"{label} could not be assessed. {component.detail}")
            continue
        ratio = component.awarded / component.available
        line = f"{label} {component.awarded}/{component.available}. {component.detail}"
        if ratio >= SUPPORTING_RATIO:
            supporting.append(line)
        elif ratio < AGAINST_RATIO:
            against.append(line)

    # Audit warnings are carried through rather than summarised away.
    for item in audit.evidence:
        if item.severity == "warning":
            against.append(f"Audit warning {item.code}. {item.detail}")

    for finding, topic in ((analysis.market.salary_benchmark, "Salary benchmark"),
                           (analysis.market.tech_demand, "Technology demand")):
        if finding.evidence_status == "supported":
            supporting.append(f"{topic}. {finding.summary}")
        else:
            limits.append(f"{topic} is unknown. {finding.summary}")
    limits.extend(assessment.limitations)
    limits.extend(analysis.limitations)

    score, ceiling = assessment.nexus_score, assessment.score_ceiling
    seniority = next(c for c in assessment.components if c.name == "seniority_fit")
    out_of_reach = seniority.evidence_status == "supported" and seniority.awarded == 0
    verdict = verdict_for(score, ceiling, out_of_reach)
    grade = grade_for(score)

    headline = {
        "Proceed": "Worth applying to.",
        "Observe": "Monitor rather than act.",
        "Discard": "Not worth pursuing.",
    }[verdict]
    if out_of_reach:
        headline = ("Not worth pursuing. The listing is pitched beyond the stated seniority "
                    "target, and skill overlap does not offset that.")
    elif verdict == "Observe" and ceiling < DISCARD_BELOW:
        # Distinguish a thin capture from a judged rejection.
        headline = ("Monitor rather than act. The capture was too thin to reject on, "
                    f"so the listing could not score above {ceiling}.")

    reasoning = (
        f"{state['job_listing'].role} at {state['job_listing'].company}. "
        f"Nexus Score {score} of a possible {ceiling}, grade {grade}. {verdict}. {headline} "
        + (f"For: {' '.join(supporting)} " if supporting else "No component scored in favour. ")
        + (f"Against: {' '.join(against)} " if against else "No component scored against. ")
        + (f"Evidence limits: {' '.join(limits)}" if limits else "No evidence limits recorded.")
    )

    if verdict == "Discard" and not against:
        # The threshold rule guarantees evidence exists; state it rather than assert a bare verdict.
        against.append(
            f"Scored {score} against a reachable ceiling of {ceiling}, below the {DISCARD_BELOW} bar."
        )
    return ExecutiveReport(
        verdict=verdict, grade=grade, nexus_score=score, score_ceiling=ceiling,
        out_of_reach=out_of_reach, reasoning=reasoning, supporting=supporting, against=against, evidence_limits=limits,
    )
