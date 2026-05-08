"""Pytest configuration and fixtures."""

import pytest


@pytest.fixture
def anyio_backend():
    """Use asyncio as the async backend."""
    return "asyncio"
