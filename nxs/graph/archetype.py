from __future__ import annotations

from langgraph.graph import END, StateGraph

from nxs.agents.scout import run_scout
from nxs.agents.analyst import run_analyst
from nxs.agents.auditor import run_auditor, MAX_AUDIT_RETRIES
from nxs.agents.assessor import run_assessor
from nxs.agents.executive import run_executive
from nxs.logger import get_logger, log_to_supabase
from nxs.profile import CANDIDATE_PROFILE
from nxs.schemas.models import JobListing, PlacementState


async def scout_node(state: PlacementState) -> dict:
    """Extract listing fields, load the candidate profile and persist Scout logs."""
    job_id = state["job_id"]
    await log_to_supabase(job_id, "Scout", "SCOUTING", "Extracting listing fields from captured HTML.")
    try:
        update = await _enrich_listing(state)
        job = update["job_listing"]
        await log_to_supabase(
            job_id, "Scout", "SCOUT_COMPLETE",
            f"Extracted technologies: {', '.join(job.tech_stack) or 'none identified'}. "
            f"Salary: {job.salary or 'unknown'}. Seniority: {job.seniority or 'unstated'}. "
            "Candidate profile loaded.",
        )
        return update
    except Exception:
        try:
            await log_to_supabase(job_id, "Scout", "ERROR", "Scout could not complete extraction or persist its result log.")
        except Exception:
            get_logger("Scout").error("Could not persist Scout failure log")
        raise


async def _enrich_listing(state: PlacementState) -> dict:
    job = state["job_listing"]
    parsed = await run_scout(job)

    enriched_job = JobListing(
        company=parsed.get("company") or job.company,
        role=parsed.get("role") or job.role,
        salary=parsed.get("salary") or job.salary,
        tech_stack=parsed.get("tech_stack") or job.tech_stack,
        seniority=parsed.get("seniority") or job.seniority,
        url=job.url,
        raw_html=job.raw_html,
    )

    return {
        "current_agent": "Scout",
        "job_listing": enriched_job,
        "candidate_profile": CANDIDATE_PROFILE,
    }


async def analyst_node(state: PlacementState) -> dict:
    """Issue #5: match candidate skills and research source-backed market evidence."""
    job_id = state["job_id"]
    await log_to_supabase(job_id, "Analyst", "ANALYSING", "Comparing candidate skills and researching UK market evidence.")
    try:
        profile = state.get("candidate_profile")
        if profile is None:
            raise ValueError("Analyst requires a candidate profile from Scout")
        report = await run_analyst(job_id, state["job_listing"], profile)
        await log_to_supabase(job_id, "Analyst", "ANALYST_COMPLETE", report.model_dump_json())
        return {"current_agent": "Analyst", "analyst_report": report}
    except Exception:
        try:
            await log_to_supabase(job_id, "Analyst", "ERROR", "Analyst failed. Check search configuration, service availability and output validation; no analysis was accepted.")
        except Exception:
            get_logger("Analyst").error("Could not persist Analyst failure log")
        raise


async def auditor_node(state: PlacementState) -> dict:
    """Audit captured evidence and log a bounded routing decision."""
    job_id = state["job_id"]
    await log_to_supabase(job_id, "Auditor", "AUDITING", "Checking captured listing quality and Analyst consistency.")
    try:
        report = run_auditor(state)
        if report.evidence:
            await log_to_supabase(job_id, "Auditor", "RED_FLAG_DETECTED", report.model_dump_json())
        count = state.get("audit_retry_count", 0)
        if report.decision == "retry":
            count += 1
            await log_to_supabase(job_id, "Auditor", "RETRY_TRIGGERED", report.model_dump_json())
        elif report.decision == "pass":
            await log_to_supabase(job_id, "Auditor", "AUDIT_PASSED", report.model_dump_json())
        return {"current_agent": "Auditor", "audit_report": report, "audit_retry_count": count}
    except Exception:
        try:
            await log_to_supabase(job_id, "Auditor", "ERROR", "Audit failed; processing stopped without accepting the listing.")
        except Exception:
            get_logger("Auditor").error("Could not persist Auditor failure log")
        raise


async def assessor_node(state: PlacementState) -> dict:
    """Score the passed audit and persist the Nexus Score with its evidence ceiling."""
    job_id = state["job_id"]
    await log_to_supabase(job_id, "Assessor", "SCORING", "Weighting skill coverage, audit integrity, seniority fit and salary alignment.")
    try:
        report = run_assessor(state)
        await log_to_supabase(job_id, "Assessor", "SCORE_COMPLETE", report.model_dump_json())
        return {"current_agent": "Assessor", "assessment_report": report,
                "nexus_score": report.nexus_score}
    except Exception:
        try:
            await log_to_supabase(job_id, "Assessor", "ERROR", "Scoring failed; no Nexus Score was accepted for this listing.")
        except Exception:
            get_logger("Assessor").error("Could not persist Assessor failure log")
        raise


async def persist_result(job_id: str, report) -> None:
    """Write the verdict back. Imported lazily: nxs.db imports this module via the listener."""
    from nxs.db.results import persist_result as _persist

    await _persist(job_id, report)


async def executive_node(state: PlacementState) -> dict:
    """Synthesise the verdict, persist it to job_listings and log the outcome."""
    job_id = state["job_id"]
    await log_to_supabase(job_id, "Executive", "VERDICT_PENDING", "Synthesising agent output into a verdict.")
    try:
        report = run_executive(state)
        await persist_result(job_id, report)
        await log_to_supabase(job_id, "Executive", report.verdict.upper(), report.model_dump_json())
        return {"current_agent": "Executive", "executive_report": report,
                "executive_verdict": report.verdict}
    except Exception:
        try:
            await log_to_supabase(job_id, "Executive", "ERROR", "Verdict synthesis or write-back failed; no verdict was recorded.")
        except Exception:
            get_logger("Executive").error("Could not persist Executive failure log")
        raise


def route_after_auditor(state: PlacementState) -> str:
    """Stop rejected audits, reparse incomplete inputs once, or proceed."""
    audit = state.get("audit_report")
    if audit is None:
        raise ValueError("Auditor must produce a report before routing")
    if audit.decision == "reject":
        return END
    if audit.retry_required:
        if state.get("audit_retry_count", 0) > MAX_AUDIT_RETRIES:
            raise ValueError("Audit retry budget exceeded")
        return "Scout"
    return "Assessor"


def build_graph() -> StateGraph:
    graph = StateGraph(PlacementState)

    graph.add_node("Scout", scout_node)
    graph.add_node("Analyst", analyst_node)
    graph.add_node("Auditor", auditor_node)
    graph.add_node("Assessor", assessor_node)
    graph.add_node("Executive", executive_node)

    graph.set_entry_point("Scout")
    graph.add_edge("Scout", "Analyst")
    graph.add_edge("Analyst", "Auditor")
    graph.add_conditional_edges(
        "Auditor",
        route_after_auditor,
        {"Scout": "Scout", "Assessor": "Assessor", END: END},
    )
    graph.add_edge("Assessor", "Executive")
    graph.add_edge("Executive", END)

    return graph


NXS_Archetype = build_graph().compile()
