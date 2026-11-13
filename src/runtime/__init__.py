"""Runtime for long-running AI agent jobs."""

from importlib.metadata import version

# Read from the installed package so the number lives only in pyproject.toml.
__version__ = version("agent-runtime")
