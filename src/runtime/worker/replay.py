"""Rebuild a job's conversation from its step log. Pure logic, no I/O.

The step log is the only state. The worker asks this module what the
conversation is and what comes next, after every step it writes.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from runtime.store.steps import Step, StepKind


@dataclass(frozen=True, slots=True)
class Replay:
    # The conversation in Messages format: the prompt, then every assistant
    # message and the tool results that answer it.
    messages: list[dict[str, Any]]
    # tool_use blocks of the last assistant message that have no result yet.
    pending: list[dict[str, Any]]
    # stop_reason of the last assistant message, None before the first one.
    stop_reason: str | None

    @property
    def needs_model_call(self) -> bool:
        """True at the start, and when every tool call of the last message is answered."""
        if self.stop_reason is None:
            return True
        return self.stop_reason == "tool_use" and not self.pending and self._answered

    @property
    def _answered(self) -> bool:
        return bool(self.messages) and self.messages[-1]["role"] == "user"


def replay(prompt: str, steps: Sequence[Step]) -> Replay:
    """The conversation and the open tool calls after `steps`.

    Only `model_response` and `tool_result` steps shape the conversation.
    The results of one assistant message go into one user message, as the
    API requires. The other kinds record what was attempted.
    """
    messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
    pending: list[dict[str, Any]] = []
    stop_reason: str | None = None

    for step in steps:
        if step.kind is StepKind.MODEL_RESPONSE:
            content = step.payload["content"]
            messages.append({"role": "assistant", "content": content})
            pending = [block for block in content if block.get("type") == "tool_use"]
            stop_reason = step.payload["stop_reason"]
        elif step.kind is StepKind.TOOL_RESULT:
            tool_use_id = step.payload["tool_use_id"]
            if messages[-1]["role"] != "user" or len(messages) == 1:
                messages.append({"role": "user", "content": []})
            messages[-1]["content"].append(
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": step.payload["content"],
                    "is_error": step.payload["is_error"],
                }
            )
            pending = [block for block in pending if block["id"] != tool_use_id]

    return Replay(messages=messages, pending=pending, stop_reason=stop_reason)
