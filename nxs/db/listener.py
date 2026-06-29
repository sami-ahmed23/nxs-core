from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone

from dotenv import load_dotenv
from supabase import AsyncClient, acreate_client

from nxs.graph.archetype import NXS_Archetype
from nxs.logger import get_logger
from nxs.schemas.models import JobListing, PlacementState

load_dotenv()

log = get_logger("DB Bridge")

POLL_INTERVAL = 3  # seconds


async def on_new_job(record: dict) -> None:
    """Processes a new job_listings row and invokes the pipeline."""
    log.info(f"New job captured: {record.get('company')} - {record.get('role')}")

    try:
        job = JobListing(
            company=record["company"],
            role=record["role"],
            salary=record.get("salary"),
            tech_stack=record.get("tech_stack") or [],
            url=record.get("url", ""),
            raw_html=record.get("raw_html", ""),
        )

        initial_state: PlacementState = {
            "current_agent": "Scout",
            "job_listing": job,
            "candidate_profile": None,  # loaded inside Scout node (Issue #4)
            "audit_report": None,
            "market_match_score": None,
            "executive_verdict": None,
        }

        log.info("Invoking NXS_Archetype pipeline")
        await NXS_Archetype.ainvoke(initial_state)
        log.info("Pipeline complete")

    except Exception as e:
        log.error(f"Pipeline failed: {e}")


async def start_listener() -> None:
    """Polls job_listings every POLL_INTERVAL seconds for new rows."""
    url = os.environ["SUPABASE_URL"]
    key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]

    client: AsyncClient = await acreate_client(url, key)

    # Anchor to now so we only process jobs captured after the engine starts.
    last_seen = datetime.now(timezone.utc).isoformat()

    log.info("DB Bridge polling job_listings - engine is live")

    while True:
        await asyncio.sleep(POLL_INTERVAL)

        try:
            response = (
                await client.table("job_listings")
                .select("*")
                .gt("created_at", last_seen)
                .order("created_at")
                .execute()
            )

            rows = response.data or []

            for row in rows:
                last_seen = row["created_at"]
                await on_new_job(row)

        except Exception as e:
            log.error(f"Poll error: {e}")
