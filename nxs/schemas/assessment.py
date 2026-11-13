"""Scoring contracts for Issue #7: a Nexus Score bounded by the evidence available."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from nxs.schemas.analysis import StrictModel

SCORE_FLOOR = 300
SCORE_CEILING = 850
SCORE_SPAN = SCORE_CEILING - SCORE_FLOOR

# Points available above the floor. Unscored context is excluded by design.
COMPONENT_WEIGHTS: dict[str, int] = {
    "skill_coverage": 250,
    "audit_integrity": 150,
    "salary_alignment": 150,
}


class ScoreComponent(StrictModel):
    """One weighted input. Unknown evidence awards nothing and lowers the ceiling."""

    name: Literal["skill_coverage", "audit_integrity", "salary_alignment"]
    evidence_status: Literal["supported", "unknown"]
    awarded: int = Field(ge=0)
    available: int = Field(ge=0)
    detail: str = Field(min_length=1)

    @model_validator(mode="after")
    def check_award(self) -> ScoreComponent:
        if self.available != COMPONENT_WEIGHTS[self.name]:
            raise ValueError("Component weight must match the locked weighting model")
        if self.evidence_status == "unknown" and self.awarded:
            raise ValueError("Unknown evidence cannot award points")
        if self.awarded > self.available:
            raise ValueError("A component cannot award more than its weight")
        return self


class AssessmentReport(StrictModel):
    """Deterministic score. A missing input lowers the ceiling instead of being guessed."""

    nexus_score: int = Field(ge=SCORE_FLOOR, le=SCORE_CEILING)
    score_ceiling: int = Field(ge=SCORE_FLOOR, le=SCORE_CEILING)
    evidence_coverage: float = Field(ge=0, le=1)
    components: list[ScoreComponent]
    tech_demand_context: str = Field(min_length=1)
    limitations: list[str]

    @model_validator(mode="after")
    def check_arithmetic(self) -> AssessmentReport:
        names = [component.name for component in self.components]
        if sorted(names) != sorted(COMPONENT_WEIGHTS):
            raise ValueError("Every weighted component must be reported exactly once")
        supported = [item for item in self.components if item.evidence_status == "supported"]
        if self.nexus_score != SCORE_FLOOR + sum(item.awarded for item in self.components):
            raise ValueError("Nexus Score must equal the floor plus awarded points")
        if self.score_ceiling != SCORE_FLOOR + sum(item.available for item in supported):
            raise ValueError("The ceiling must equal the floor plus the available supported points")
        if self.nexus_score > self.score_ceiling:
            raise ValueError("A score cannot exceed its evidence ceiling")
        if abs(self.evidence_coverage - sum(item.available for item in supported) / SCORE_SPAN) > 1e-9:
            raise ValueError("Coverage must equal the supported share of the weighted span")
        return self

    def to_supabase(self) -> dict:
        return self.model_dump()
