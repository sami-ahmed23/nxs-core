"""Deterministic scoring. Missing evidence lowers the ceiling; nothing is inferred."""

from __future__ import annotations

import re

from nxs.schemas.assessment import (
    COMPONENT_WEIGHTS, SCORE_FLOOR, SCORE_SPAN, AssessmentReport, ScoreComponent,
)
from nxs.schemas.models import PlacementState

# Only sterling is scored. No exchange rate is applied, so other currencies stay unknown.
_AMOUNT = r"£\s?(\d{1,3}(?:,\d{3})+|\d{2,3})\s?([kK])?"
_RANGE_RE = re.compile(_AMOUNT + r"\s*(?:[-–—]|to)\s*" + _AMOUNT)
_SINGLE_RE = re.compile(_AMOUNT)


def _amount(digits: str, suffix: str | None) -> int:
    value = int(digits.replace(",", ""))
    return value * 1000 if suffix else value


def parse_salary(text: str | None) -> tuple[int, int] | None:
    """Return an inclusive sterling range, or None when the figure is unusable."""
    if not text:
        return None
    if re.search(r"[$€]", text):
        return None
    match = _RANGE_RE.search(text)
    if match:
        low = _amount(match.group(1), match.group(2))
        high = _amount(match.group(3), match.group(4))
        if low > high:
            low, high = high, low
    else:
        match = _SINGLE_RE.search(text)
        if not match:
            return None
        low = high = _amount(match.group(1), match.group(2))
    # Four-figure annual pay is treated as unusable rather than guessed at.
    return (low, high) if low >= 10000 else None


def _midpoint(span: tuple[int, int]) -> float:
    return (span[0] + span[1]) / 2


def run_assessor(state: PlacementState) -> AssessmentReport:
    """Score skill coverage, audit integrity and salary alignment from captured evidence."""
    job = state["job_listing"]
    profile = state.get("candidate_profile")
    analysis = state.get("analyst_report")
    audit = state.get("audit_report")
    if profile is None or analysis is None or audit is None:
        raise ValueError("Assessor requires a profile, an Analyst report and an audit")
    if audit.decision != "pass":
        raise ValueError("Only a passed audit may be scored")

    components: list[ScoreComponent] = []
    limitations: list[str] = []

    # Skill coverage: extracted mentions present in the profile, not proven requirements.
    weight = COMPONENT_WEIGHTS["skill_coverage"]
    matched, missing = len(analysis.skill_matches), len(analysis.skill_gaps)
    if matched + missing == 0:
        components.append(ScoreComponent(
            name="skill_coverage", evidence_status="unknown", awarded=0, available=weight,
            detail="No technologies were extracted from the listing, so fit cannot be measured.",
        ))
        limitations.append("Skill coverage is unscored: the capture yielded no technologies.")
    else:
        components.append(ScoreComponent(
            name="skill_coverage", evidence_status="supported",
            awarded=round(weight * matched / (matched + missing)), available=weight,
            detail=f"{matched} of {matched + missing} extracted technologies appear in the profile.",
        ))

    # Audit integrity: input completeness measured by the Auditor, never job authenticity.
    weight = COMPONENT_WEIGHTS["audit_integrity"]
    warnings = [item.code for item in audit.evidence if item.severity == "warning"]
    components.append(ScoreComponent(
        name="audit_integrity", evidence_status="supported",
        awarded=round(weight * audit.confidence_score), available=weight,
        detail=f"Input completeness {audit.confidence_score:.2f}"
               + (f"; warnings: {', '.join(warnings)}." if warnings else "; no warnings."),
    ))

    # Salary alignment: the listing's own figure against the candidate's target.
    weight = COMPONENT_WEIGHTS["salary_alignment"]
    listed, target = parse_salary(job.salary), parse_salary(profile.target_salary)
    if listed is None or target is None or _midpoint(target) <= 0:
        reason = ("The listing states no usable sterling salary."
                  if listed is None else "The profile target salary is unusable.")
        components.append(ScoreComponent(
            name="salary_alignment", evidence_status="unknown", awarded=0,
            available=weight, detail=reason,
        ))
        limitations.append("Salary alignment is unscored: " + reason)
    else:
        ratio = min(_midpoint(listed) / _midpoint(target), 1.0)
        components.append(ScoreComponent(
            name="salary_alignment", evidence_status="supported",
            awarded=round(weight * ratio), available=weight,
            detail=f"Listed £{listed[0]:,}-£{listed[1]:,} against a target of "
                   f"£{target[0]:,}-£{target[1]:,}.",
        ))

    supported = [item for item in components if item.evidence_status == "supported"]
    available = sum(item.available for item in supported)
    limitations.append(
        "Market demand is reported as context only. The Analyst emits prose and citations, "
        "not a structured demand figure, so scoring it would require inference."
    )
    if available < SCORE_SPAN:
        limitations.append(
            f"Missing evidence caps this listing at {SCORE_FLOOR + available}; "
            "the gap is not redistributed across the remaining components."
        )
    return AssessmentReport(
        nexus_score=SCORE_FLOOR + sum(item.awarded for item in components),
        score_ceiling=SCORE_FLOOR + available,
        evidence_coverage=available / SCORE_SPAN,
        components=components,
        tech_demand_context=analysis.market.tech_demand.summary,
        limitations=limitations,
    )
