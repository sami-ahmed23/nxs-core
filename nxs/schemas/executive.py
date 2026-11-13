"""Verdict contracts for Issue #8: a verdict that only condemns on evidence."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from nxs.schemas.analysis import StrictModel
from nxs.schemas.assessment import SCORE_CEILING, SCORE_FLOOR

PROCEED_AT = 700
DISCARD_BELOW = 600

# Bands are the published Nexus Score grades. Lower bound, grade.
GRADE_BANDS: list[tuple[int, str]] = [
    (800, "A+"), (750, "A"), (700, "B+"), (650, "B"), (600, "C"), (SCORE_FLOOR, "F"),
]


def grade_for(score: int) -> str:
    for lower, grade in GRADE_BANDS:
        if score >= lower:
            return grade
    raise ValueError("Score is below the published floor")


def verdict_for(score: int, ceiling: int, out_of_reach: bool = False) -> str:
    """Proceed on merit, discard only when the listing could have cleared the bar.

    A level the candidate cannot reach is decisive: no score offsets it.
    """
    if out_of_reach:
        return "Discard"
    if score >= PROCEED_AT:
        return "Proceed"
    # A shortfall caused by missing evidence is not grounds for rejection.
    if score < DISCARD_BELOW and ceiling >= DISCARD_BELOW:
        return "Discard"
    return "Observe"


class ExecutiveReport(StrictModel):
    """Final synthesis. Reasoning is assembled from agent output, never generated."""

    verdict: Literal["Proceed", "Observe", "Discard"]
    grade: Literal["A+", "A", "B+", "B", "C", "F"]
    nexus_score: int = Field(ge=SCORE_FLOOR, le=SCORE_CEILING)
    score_ceiling: int = Field(ge=SCORE_FLOOR, le=SCORE_CEILING)
    out_of_reach: bool
    reasoning: str = Field(min_length=1)
    supporting: list[str]
    against: list[str]
    evidence_limits: list[str]

    @model_validator(mode="after")
    def check_verdict(self) -> ExecutiveReport:
        if self.nexus_score > self.score_ceiling:
            raise ValueError("A score cannot exceed its evidence ceiling")
        if self.grade != grade_for(self.nexus_score):
            raise ValueError("Grade must match the published Nexus Score bands")
        if self.verdict != verdict_for(self.nexus_score, self.score_ceiling, self.out_of_reach):
            raise ValueError("Verdict must follow the locked threshold rule")
        if self.verdict == "Discard" and not self.against:
            raise ValueError("A discard must state what it is based on")
        return self

    def to_supabase(self) -> dict:
        return self.model_dump()
