"""Write-back of final results to job_listings. The Executive is the only writer."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from nxs.schemas.executive import ExecutiveReport


def _update(job_id: str, payload: dict) -> None:
    # Imported here to match the logger's avoidance of the listener import cycle.
    from nxs.db.client import get_client

    get_client().table("job_listings").update(payload).eq("id", job_id).execute()


async def persist_result(job_id: str, report: ExecutiveReport) -> None:
    """Set the score, verdict and board status without blocking the graph's event loop.

    Failures propagate so a verdict cannot be reported as persisted when it is not.
    """
    payload = {
        "nexus_score": report.nexus_score,
        "verdict": report.verdict,
        "status": report.verdict.upper(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        await asyncio.to_thread(_update, job_id, payload)
    except Exception as exc:
        raise RuntimeError("Could not persist the final result to Supabase") from exc
