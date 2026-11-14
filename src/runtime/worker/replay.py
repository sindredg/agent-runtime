"""Rebuild a job's conversation from its step log. Pure logic, no I/O.

The step log is the only state. A worker that takes a job over replays the
log once, then folds in each step it writes itself.
"""

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from runtime.store.steps import Step, StepKind


@dataclass(slots=True)
class Replay:
    """Where a job stands after some of its steps. `apply` folds in the next one."""

    # The conversation in Messages format: the prompt, then every assistant
    # message and the tool results that answer it.
    messages: list[dict[str, Any]]
    # tool_use blocks of the last assistant message that have no result yet.
    pending: list[dict[str, Any]] = field(default_factory=list)
    # stop_reason of the last assistant message, None before the first one.
    stop_reason: str | None = None
    # The payload of a `tool_intent` step that no `tool_result` has followed.
    # A worker that finds one here did not write it: the call was interrupted.
    interrupted: dict[str, Any] | None = None
    # When that `tool_intent` step was written, by the database clock.
    interrupted_at: datetime | None = None
    # What was done about that interrupted call: None, "requested" (a person
    # was asked) or "given" (a person approved running it again).
    approval: str | None = None
    # Why the last assistant message cannot be used, as its `model_response`
    # step recorded it. None when it is fine.
    problem: str | None = None
    # `seq` of the last step folded in, 0 before the first.
    last_seq: int = 0
    # The id of every tool call the model has made in this job. A call's
    # operation id is built from its id, so no later call may use one again.
    used_ids: set[str] = field(default_factory=set)

    @property
    def needs_model_call(self) -> bool:
        """True at the start, and when every tool call of the last message is answered."""
        if self.stop_reason is None:
            return True
        return self.stop_reason == "tool_use" and not self.pending and self._answered

    @property
    def _answered(self) -> bool:
        return bool(self.messages) and self.messages[-1]["role"] == "user"

    def apply(self, step: Step) -> None:
        """Fold one more step into the state.

        Only `model_response` and `tool_result` steps shape the conversation.
        The results of one assistant message go into one user message, as
        the API requires. A `model_request` and an `error` change nothing: a
        request without a response is simply sent again.
        """
        self.last_seq = step.seq
        payload = step.payload
        if step.kind is StepKind.MODEL_RESPONSE:
            content = payload["content"]
            self.messages.append({"role": "assistant", "content": content})
            self.problem = payload.get("problem")
            # A response with a problem is stored as it came and may not have
            # the shape of a message. No call is taken from it.
            self.pending = (
                []
                if self.problem
                else [block for block in content if block.get("type") == "tool_use"]
            )
            self.used_ids.update(block["id"] for block in self.pending)
            self.stop_reason = payload["stop_reason"]
            self.interrupted, self.interrupted_at, self.approval = None, None, None
        elif step.kind is StepKind.TOOL_INTENT:
            # Every run of a tool has its own intent. A new one starts with
            # no approval, also when it reruns an approved call.
            self.interrupted, self.approval = payload, None
            self.interrupted_at = step.created_at
        elif step.kind is StepKind.TOOL_RESULT:
            tool_use_id = payload["tool_use_id"]
            if self.messages[-1]["role"] != "user" or len(self.messages) == 1:
                self.messages.append({"role": "user", "content": []})
            self.messages[-1]["content"].append(
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": payload["content"],
                    "is_error": payload["is_error"],
                }
            )
            self.pending = [block for block in self.pending if block["id"] != tool_use_id]
            self.interrupted, self.interrupted_at, self.approval = None, None, None
        elif step.kind is StepKind.APPROVAL_REQUESTED:
            self.approval = "requested"
        elif step.kind is StepKind.APPROVAL_GIVEN:
            self.approval = "given"


def replay(prompt: str, steps: Iterable[Step]) -> Replay:
    """The state of a job with this prompt after `steps`."""
    state = Replay(messages=[{"role": "user", "content": prompt}])
    for step in steps:
        state.apply(step)
    return state
