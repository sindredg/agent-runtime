"""The summary of a long conversation (ADR 0033). Pure logic, no I/O.

When the last response's tokens reach the job's trigger, the next model
call asks for a summary instead. Its answer is stored as one `summary` step,
and the next context window starts from that step's message alone.

Both texts are frozen with ADR 0033: a change to either changes what a job
that is taken over sends, and what a real-model run was judged on.
"""

from collections.abc import Sequence
from typing import Any

# Appended as a text block to the last user message of a summary request.
SUMMARY_INSTRUCTION = (
    "Your earlier messages are about to be removed to make room. Write a summary that"
    " lets you continue this task with nothing else: the task and every instruction you"
    " were given, the facts you found that later steps need, what you have done, and"
    " what is left to do. Plain text. Do not call a tool."
)
# Follows the summary text, after a blank line, in the message that opens
# the next window.
CONTINUATION = (
    "That was your own summary of this job so far, written before your earlier messages"
    " were removed. Continue the task from where it stops."
)
# The model may not call a tool in its answer. The tools stay in the request.
TOOL_CHOICE_NONE: dict[str, Any] = {"type": "none"}
# Every other request of a job with a trigger: at most one tool call a
# response, so the trigger is checked after each call's result (issue 258).
TOOL_CHOICE_ONE_CALL: dict[str, Any] = {"type": "auto", "disable_parallel_tool_use": True}


def summary_messages(messages: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """The conversation with the instruction after the last block of its last user message.

    That message holds the results of the last response's tool calls, so
    the instruction comes after the last `tool_result`. The conversation
    itself is not changed: the instruction is never part of it.
    """
    *earlier, last = messages
    content = last["content"]
    blocks = [{"type": "text", "text": content}] if isinstance(content, str) else list(content)
    instruction = {"type": "text", "text": SUMMARY_INSTRUCTION}
    return [*earlier, {"role": "user", "content": [*blocks, instruction]}]


def summary_text(content: Sequence[dict[str, Any]]) -> str:
    """The text blocks of the answer, joined with a blank line. Thinking is not carried over."""
    return "\n\n".join(
        block["text"]
        for block in content
        if block.get("type") == "text" and isinstance(block.get("text"), str)
    )


def opening_message(text: str) -> dict[str, Any]:
    """The user message that opens the next window: the summary, a blank line, the continuation."""
    return {"role": "user", "content": [{"type": "text", "text": f"{text}\n\n{CONTINUATION}"}]}


def summary_problem(stop_reason: str, content: Sequence[dict[str, Any]]) -> str | None:
    """Why an answer to a summary request cannot open the next window, or None.

    A usable summary ends its turn, has text that is not only white space,
    and calls no tool. The shape of the content is checked before this.
    """
    if any(block.get("type") == "tool_use" for block in content):
        return "it calls a tool"
    if stop_reason != "end_turn":
        return f"it stopped with stop_reason {stop_reason}"
    if not summary_text(content).strip():
        return "it has no text"
    return None
