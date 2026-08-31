"""
Shared pytest fixtures for the browser automation test suite.
"""

import asyncio

import pytest


@pytest.fixture(scope="session")
def event_loop():
    """Provide a single event loop for the entire test session.

    Using a session-scoped loop allows async fixtures and tests that share
    stateful resources (e.g., a Playwright browser instance) to run in the
    same loop without recreating it between tests.
    """
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()
