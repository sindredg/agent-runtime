"""The package installs and has the expected subpackages."""

import importlib
from importlib.metadata import version

import pytest

import runtime

SUBPACKAGES = ["api", "worker", "store", "provider", "sandbox", "tools"]


def test_version_matches_package_metadata() -> None:
    assert runtime.__version__ == version("agent-runtime")


@pytest.mark.parametrize("name", SUBPACKAGES)
def test_subpackage_imports(name: str) -> None:
    importlib.import_module(f"runtime.{name}")
