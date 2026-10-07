"""Bounded UK market research and evidence-backed placement matching."""

from __future__ import annotations

import asyncio
import os
import re

import requests
from datetime import datetime, timezone

from dotenv import load_dotenv
from pydantic import ValidationError

from nxs.logger import log_to_supabase
from nxs.agents.market import collect_public_market
from nxs.schemas.analysis import AnalystReport, Citation, MarketAnalysis, MarketFinding, SearchSource
from nxs.schemas.models import CandidateProfile, JobListing


def _search(query: str) -> dict:
    response = requests.post(
        "https://google.serper.dev/search",
        headers={"X-API-KEY": os.environ["SERPER_API_KEY"]},
        json={"q": query, "gl": "gb", "hl": "en", "num": 5},
        timeout=10,
    )
    response.raise_for_status()
    result = response.json()
    if not isinstance(result, dict) or not isinstance(result.get("organic", []), list):
        raise ValueError("Serper returned an invalid search response")
    return result


def _summarise(job: JobListing, sources: list[SearchSource]) -> MarketAnalysis:
    """Retain source observations without inventing benchmarks or demand trends."""
    salary = _unknown("Search snippets do not establish a verified, comparable salary benchmark.")
    demand = _unknown("Search snippets do not establish a market-wide technology-demand trend.")
    citations = []
    for source in sources:
        if source.topic != "demand":
            continue
        if any(
            re.search(rf"(?<!\w){re.escape(skill)}(?!\w)", source.snippet, re.IGNORECASE)
            for skill in job.tech_stack if skill.strip()
        ):
            citations.append(Citation(source_id=source.source_id, quote=source.snippet))
    if citations:
        demand = MarketFinding(
            evidence_status="supported",
            summary="Retrieved search snippets mention technologies in this listing. "
                    "This is evidence of mentions only, not a measured hiring-demand trend.",
            citations=citations,
        )
    return MarketAnalysis(salary_benchmark=salary, tech_demand=demand)


def _unknown(reason: str) -> MarketFinding:
    return MarketFinding(evidence_status="unknown", summary=reason, citations=[])


def _match_skills(job: JobListing, profile: CandidateProfile) -> tuple[list[str], list[str]]:
    candidate_skills = {skill.strip().casefold() for skill in profile.skills}
    matches, gaps = [], []
    seen = set()
    for skill in job.tech_stack:
        normalised = skill.strip().casefold()
        if not normalised or normalised in seen:
            continue
        seen.add(normalised)
        (matches if normalised in candidate_skills else gaps).append(skill.strip())
    return matches, gaps


async def run_analyst(job_id: str, job: JobListing, profile: CandidateProfile) -> AnalystReport:
    """Compare skills and retrieve public market evidence without model calls."""
    validate_analyst_configuration()

    matches, gaps = _match_skills(job, profile)
    if os.environ.get("NXS_MARKET_SEARCH", "public").strip().lower() == "public":
        await log_to_supabase(job_id, "Analyst", "ANALYSING", "Reading free public UK role salary and technology vacancy tables.")
        market, sources, limitations = await asyncio.to_thread(collect_public_market, job)
        limitations.append("Skill gaps compare extracted mentions with the configured profile, not verified mandatory requirements.")
        if not job.tech_stack:
            limitations.append("Scout extracted no technologies; technology demand and skill alignment cannot be established.")
        return AnalystReport(skill_matches=matches, skill_gaps=gaps, market=market, sources=sources, limitations=limitations)
    year = datetime.now(timezone.utc).year
    queries = {
        "salary": f"UK {job.role[:150]} placement internship salary benchmark {year}",
        "demand": f"UK {job.role[:150]} {' '.join(job.tech_stack[:6])} hiring demand {year}",
    }
    sources = []
    search_enabled = os.environ.get("NXS_MARKET_SEARCH", "off").strip().lower() == "serper"
    limitations = [
        "Skills compare extracted mentions with the candidate profile; mentions are not proven requirements.",
        "Evidence comes from search snippets; source pages and factual entailment are not independently verified.",
        "Location, seniority and salary compatibility are unknown without structured listing fields and comparable pay periods.",
    ]
    if not search_enabled:
        limitations.append("Market search is disabled in zero-cost local mode; no external evidence was retrieved.")
    for topic, query in (queries.items() if search_enabled else []):
        await log_to_supabase(job_id, "Analyst", "ANALYSING", f"Searching UK {topic} evidence: {query}")
        try:
            result = await asyncio.to_thread(_search, query)
        except Exception:
            limitations.append(f"The {topic} search failed; this topic remains unknown.")
            await log_to_supabase(job_id, "Analyst", "ERROR", f"The {topic} search failed. No evidence was inferred.")
            continue
        for item in result.get("organic", [])[:5]:
            try:
                source = SearchSource(
                    source_id=f"{topic}-{len(sources) + 1}", topic=topic,
                    url=item["link"], title=item["title"], snippet=item.get("snippet", ""),
                    query=query, retrieved_at=datetime.now(timezone.utc),
                )
                sources.append(source)
            except (ValidationError, KeyError, TypeError, AttributeError):
                limitations.append(f"An unusable {topic} search result was excluded.")

    market = MarketAnalysis(
        salary_benchmark=_unknown("No usable salary evidence was retrieved."),
        tech_demand=_unknown("No usable technology-demand evidence was retrieved."),
    )
    if sources:
        await log_to_supabase(job_id, "Analyst", "ANALYSING", f"Checking technology mentions in {len(sources)} retrieved snippets without a model.")
        market = _summarise(job, sources)
        # A topic without retrieved evidence must remain unknown.
        if not any(source.topic == "salary" for source in sources):
            market.salary_benchmark = _unknown("No usable salary evidence was retrieved.")
        if not any(source.topic == "demand" for source in sources):
            market.tech_demand = _unknown("No usable technology-demand evidence was retrieved.")

    if not job.tech_stack:
        limitations.append("No technologies were extracted; empty match/gap lists do not indicate a good fit.")
    return AnalystReport(
        skill_matches=matches, skill_gaps=gaps, market=market,
        sources=sources, limitations=limitations,
    )


def validate_analyst_configuration() -> None:
    """Public and offline modes need no keys; Serper is an optional legacy mode."""
    load_dotenv()
    mode = os.environ.get("NXS_MARKET_SEARCH", "public").strip().lower()
    if mode not in {"public", "off", "serper"}:
        raise ValueError("NXS_MARKET_SEARCH must be public, off or serper")
    if mode == "serper" and not os.environ.get("SERPER_API_KEY", "").strip():
        raise ValueError("Configure SERPER_API_KEY or set NXS_MARKET_SEARCH=off")
