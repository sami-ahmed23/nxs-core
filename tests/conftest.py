"""Shared test guards. No test may write a verdict to the live job_listings table."""

from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture(autouse=True)
def block_live_write_back():
    # A test that asserts on the write-back patches it again inside its own block, which wins.
    with patch("nxs.graph.archetype.persist_result", new_callable=AsyncMock) as write_back:
        yield write_back
