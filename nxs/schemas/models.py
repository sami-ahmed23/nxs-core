from __future__ import annotations

from typing import Literal, TypedDict

from pydantic import BaseModel


class JobListing(BaseModel):
    """Raw job data ingested by Scout via the Chrome extension POST to /api/ingest."""

    company: str
    role: str
    salary: str | None = None
    tech_stack: list[str]
    url: str
    raw_html: str

    def to_supabase(self) -> dict:
        """Serialise to a flat dict for Supabase JSONB storage."""
        return self.model_dump()


class CandidateProfile(BaseModel):
    """Static profile of the candidate. Loaded once at runtime from config."""

    skills: list[str]
    experience_level: str
    target_salary: str
    geo_preferences: list[str]

    def to_supabase(self) -> dict:
        return self.model_dump()


class AuditReport(BaseModel):
    """Output of the Auditor agent. Determines whether the pipeline retries or proceeds."""

    red_flags: list[str]
    retry_required: bool
    confidence_score: float  # 0.0 (low confidence) to 1.0 (high confidence)
    notes: str | None = None

    def to_supabase(self) -> dict:
        return self.model_dump()


class PlacementState(TypedDict):
    """LangGraph state machine schema. Passed between all agent nodes."""

    current_agent: str
    job_listing: JobListing
    candidate_profile: CandidateProfile
    audit_report: AuditReport | None
    market_match_score: int | None
    executive_verdict: Literal["Proceed", "Observe", "Discard"] | None
