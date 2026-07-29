"""Scaffolding smoke test: verifies the test harness and package layout work.

Confirms that pytest discovery, asyncio mode, and the `app` package import
path are all wired correctly before real modules land.
"""

import importlib


def test_app_packages_importable() -> None:
    """Every app subpackage from the design layout imports cleanly."""
    for pkg in (
        "app",
        "app.bots",
        "app.core",
        "app.kb",
        "app.ai",
        "app.rag",
        "app.security",
        "app.analytics",
        "app.monitoring",
        "app.api",
    ):
        assert importlib.import_module(pkg) is not None


async def test_asyncio_mode_auto_runs_async_tests() -> None:
    """pytest-asyncio auto mode executes bare async test functions."""
    assert True
