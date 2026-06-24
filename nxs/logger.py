from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone


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
