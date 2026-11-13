from __future__ import annotations

from typing import Literal, TypedDict

from pydantic import BaseModel, Field, model_validator

from nxs.schemas.analysis import AnalystReport
from nxs.schemas.assessment import AssessmentReport


class JobListing(BaseModel):
    """Raw job data ingested by Scout via the Chrome extension POST to /api/ingest."""

    company: str
    role: str
    salary: str | None = None
    tech_stack: list[str]
    seniority: str | None = None  # extracted ladder level, None when the listing is silent
    url: str
    raw_html: str

    def to_supabase(self) -> dict:
        """Serialise to a flat dict for Supabase JSONB storage."""
        return self.model_dump()


class CandidateProfile(BaseModel):
    """Static profile of the candidate. Loaded once at runtime from config."""

    skills: list[str]
    experience_level: str
    seniority_target: list[str]  # acceptable ladder levels, most junior first
    target_salary: str
    geo_preferences: list[str]

    def to_supabase(self) -> dict:
        return self.model_dump()


class AuditEvidence(BaseModel):
    code: str
    severity: Literal["warning", "blocking"]
    field: str
    detail: str


class AuditReport(BaseModel):
    """Output of the Auditor agent. Determines whether the pipeline retries or proceeds."""

    red_flags: list[str]
    retry_required: bool
    confidence_score: float = Field(ge=0, le=1)
    notes: str | None = None
    decision: Literal["pass", "retry", "reject"]
    evidence: list[AuditEvidence]

    @model_validator(mode="after")
    def consistent_decision(self):
        if self.retry_required != (self.decision == "retry"):
            raise ValueError("retry_required must agree with the audit decision")
        if self.decision == "pass" and any(item.severity == "blocking" for item in self.evidence):
            raise ValueError("A blocking finding cannot pass audit")
        if self.decision != "pass" and not any(item.severity == "blocking" for item in self.evidence):
            raise ValueError("Retry or rejection requires blocking evidence")
        return self

    def to_supabase(self) -> dict:
        return self.model_dump()


class PlacementState(TypedDict):
    """LangGraph state machine schema. Passed between all agent nodes."""

    job_id: str
    current_agent: str
    job_listing: JobListing
    candidate_profile: CandidateProfile | None  # loaded by Scout (Issue #4)
    analyst_report: AnalystReport | None
    audit_report: AuditReport | None
    audit_retry_count: int
    assessment_report: AssessmentReport | None
    nexus_score: int | None  # 300-850, set by Assessor (Issue #7)
    executive_verdict: Literal["Proceed", "Observe", "Discard"] | None
