"""Which tool calls wait for a person before they run (ADR 0011).

A tool's definition can say that its calls need approval. A job's spec can
name further tools for that job. Either is enough, so a spec can add to the
gate and never take from it.
"""

from collections.abc import Collection, Iterable

from runtime.tools.base import AnyTool, ToolRegistry

# What the model is told about a call a person refused.
DENIED = "A person denied this call. It was not run."
DENIED_RERUN = (
    "A person denied running this call again. It was interrupted before, and may or may"
    " not have run then."
)


def gate_reason(tool: AnyTool | None, name: str, named: Collection[str]) -> str | None:
    """Why a call of this tool waits for a person before it runs. None: it does not.

    `named` are the tools the job's spec asks approval for.
    """
    if tool is not None and tool.needs_approval:
        return "the tool's calls need a person's approval before they run"
    if name in named:
        return "the job's spec asks for a person's approval before this tool runs"
    return None


def not_offered(named: Iterable[str], tools: ToolRegistry) -> list[str]:
    """The names in a spec's list that are not tools of this worker, each once, in order.

    A name that matches no tool gates nothing. It is most likely a misspelt
    one, and the tool it was meant for would run without a question.
    """
    return [name for name in dict.fromkeys(named) if name not in tools]
