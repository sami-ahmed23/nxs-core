"""Evidence contracts for Issue #5. Sources are search snippets, not verified pages."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class SearchSource(StrictModel):
    source_id: str
    topic: Literal["salary", "demand"]
    url: HttpUrl
    title: str = Field(min_length=1)
    snippet: str = Field(min_length=1)
    query: str
    retrieved_at: datetime


class Citation(StrictModel):
    source_id: str = Field(min_length=1)
    quote: str = Field(min_length=1)


class MarketFinding(StrictModel):
    evidence_status: Literal["supported", "unknown"]
    summary: str = Field(min_length=1)
    citations: list[Citation]

    @model_validator(mode="after")
    def check_evidence(self) -> MarketFinding:
        if self.evidence_status == "supported" and not self.citations:
            raise ValueError("Supported findings require source citations")
        if self.evidence_status == "unknown" and self.citations:
            raise ValueError("Unknown findings must not claim supporting citations")
        return self


class MarketAnalysis(StrictModel):
    salary_benchmark: MarketFinding
    tech_demand: MarketFinding


class AnalystReport(StrictModel):
    # These compare extracted mentions, not proven mandatory job requirements.
    skill_matches: list[str]
    skill_gaps: list[str]
    market: MarketAnalysis
    sources: list[SearchSource]
    limitations: list[str]

    @model_validator(mode="after")
    def check_citations(self) -> AnalystReport:
        sources = {source.source_id: source for source in self.sources}
        if len(sources) != len(self.sources):
            raise ValueError("Source IDs must be unique")
        for topic, finding in (
            ("salary", self.market.salary_benchmark),
            ("demand", self.market.tech_demand),
        ):
            for citation in finding.citations:
                source = sources.get(citation.source_id)
                if source is None or source.topic != topic:
                    raise ValueError("Citation must refer to a retrieved source for this topic")
                quote = " ".join(citation.quote.split())
                snippet = " ".join(source.snippet.split())
                if quote not in snippet:
                    raise ValueError("Citation quote must occur in the retrieved snippet")
        return self
