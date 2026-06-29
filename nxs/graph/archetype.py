from __future__ import annotations

from langgraph.graph import END, StateGraph

from nxs.schemas.models import PlacementState


# Each node receives the full PlacementState and returns a partial update.
# Logic is implemented per-agent in Issues #4 through #8.


def scout_node(state: PlacementState) -> dict:
    """Issue #4 - Raw data ingestion and parsing."""
    return {"current_agent": "Scout"}


def analyst_node(state: PlacementState) -> dict:
    """Issue #5 - SerperTool market cross-referencing."""
    return {"current_agent": "Analyst"}


def auditor_node(state: PlacementState) -> dict:
    """Issue #6 - Red flag detection and retry logic."""
    return {"current_agent": "Auditor"}


def assessor_node(state: PlacementState) -> dict:
    """Issue #7 - Mathematical fit scoring (1-100)."""
    return {"current_agent": "Assessor"}


def executive_node(state: PlacementState) -> dict:
    """Issue #8 - Clinical verdict synthesis."""
    return {"current_agent": "Executive"}


def route_after_auditor(state: PlacementState) -> str:
    """Conditional edge: retry back to Scout if AuditReport flags it, else proceed."""
    audit = state.get("audit_report")
    if audit and audit.retry_required:
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
        {"Scout": "Scout", "Assessor": "Assessor"},
    )
    graph.add_edge("Assessor", "Executive")
    graph.add_edge("Executive", END)

    return graph


NXS_Archetype = build_graph().compile()
