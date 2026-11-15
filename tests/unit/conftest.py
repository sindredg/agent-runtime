"""Fixtures shared by the unit tests."""

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

CHAOS_CONFTEST = Path(__file__).resolve().parents[1] / "chaos" / "conftest.py"


@pytest.fixture(scope="module")
def chaos() -> ModuleType:
    """The chaos tests' conftest, loaded as a plain module. Nothing in it starts the stack."""
    spec = importlib.util.spec_from_file_location("chaos_conftest", CHAOS_CONFTEST)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
