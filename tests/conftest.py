"""Shared fixtures: an isolated database per test, a TestClient on it."""

from __future__ import annotations

import importlib
import sys

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("NOISE_DB", str(tmp_path / "noise.db"))
    # app.main resolves NOISE_DB per request, so reimporting is not needed,
    # but it keeps every test honest about module-level state.
    if "app.main" in sys.modules:
        module = importlib.reload(sys.modules["app.main"])
    else:
        module = importlib.import_module("app.main")
    with TestClient(module.app) as test_client:
        yield test_client


def minutes(**groups: int) -> list[int]:
    """Build 1440 readings: ``minutes(l50=600, l62=600, l70=240)``."""
    values: list[int] = []
    for key, count in groups.items():
        values.extend([int(key[1:])] * count)
    assert len(values) == 1440, f"got {len(values)} minutes"
    return values
