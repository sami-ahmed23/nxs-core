from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime, timezone
from typing import Literal
from uuid import UUID

from pydantic import BaseModel


class AgentLog(BaseModel):
    """Validated payload for the existing agent_logs table."""

    job_id: UUID
    agent_name: Literal["Scout", "Analyst", "Auditor", "Assessor", "Executive"]
    status: Literal[
        "SCOUTING", "SCOUT_COMPLETE", "ANALYSING", "ANALYST_COMPLETE",
        "AUDITING", "RED_FLAG_DETECTED", "RETRY_TRIGGERED", "AUDIT_PASSED",
        "SCORING", "SCORE_COMPLETE", "VERDICT_PENDING", "PROCEED",
        "OBSERVE", "DISCARD", "ERROR",
    ]
    reasoning: str


def _insert_log(payload: dict) -> None:
    # Import here to avoid the database package's listener import cycle.
    from nxs.db.client import get_client

    get_client().table("agent_logs").insert(payload).execute()


async def log_to_supabase(
    job_id: str, agent_name: str, status: str, reasoning: str,
) -> None:
    """Persist a validated entry without blocking the graph's event loop.

    Write failures propagate so a node cannot silently complete without its log.
    """
    entry = AgentLog(
        job_id=job_id, agent_name=agent_name, status=status, reasoning=reasoning,
    )
    try:
        await asyncio.to_thread(_insert_log, entry.model_dump(mode="json"))
    except Exception as exc:
        raise RuntimeError("Could not persist agent log to Supabase") from exc


def get_logger(agent_name: str) -> logging.Logger:
    """
    Returns a structured logger for the given agent.
    Each agent calls this at the top of its node function.

    Usage:
        log = get_logger("Scout")
        log.info("Ingestion complete")
    """
    logger = logging.getLogger(f"nxs.{agent_name}")

    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(_NXSFormatter(agent_name))
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        logger.propagate = False

    return logger


class _NXSFormatter(logging.Formatter):
    """Clinical log format: [timestamp] [AGENT] LEVEL - message"""

    def __init__(self, agent_name: str) -> None:
        super().__init__()
        self.agent_name = agent_name

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return f"[{ts}] [{self.agent_name}] {record.levelname} - {record.getMessage()}"
