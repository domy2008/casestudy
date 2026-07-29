"""Smoke test verifying the project scaffolding imports cleanly."""

import importlib

import pytest

PACKAGES = [
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
]


@pytest.mark.parametrize("package", PACKAGES)
def test_package_imports(package: str) -> None:
    """Every application package is importable from the project root."""
    assert importlib.import_module(package) is not None
