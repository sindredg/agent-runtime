"""Scenario files: the scripted answers the fake provider gives.

A scenario is a list of turns. Turn N is the assistant message the provider
returns when the request already holds N assistant messages, so sending the
same conversation again always gets the same answer. After a summary, turns
are counted on from the turn its marker names (`fake_provider.summary`).

A scenario with a `summary` section answers a request with `tool_choice`
`none` with a summary. Without one, such a request is refused.

A scenario with a `context_window` refuses a request whose input is over it,
so a job can be played that outgrows its window.
"""

import math
import re
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# A scenario's name is its file name without `.json`, and the model id that selects it.
NAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]*")
# The longest wait a turn may ask for.
MAX_DELAY_SECONDS = 300


class _Strict(BaseModel):
    # A misspelt key in a scenario file is an error, not a silently ignored field.
    model_config = ConfigDict(extra="forbid", frozen=True)


class TextBlock(_Strict):
    type: Literal["text"]
    text: str = Field(min_length=1)


def _has_non_finite(value: Any) -> bool:
    if isinstance(value, float):
        return not math.isfinite(value)
    if isinstance(value, dict):
        return any(_has_non_finite(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_non_finite(item) for item in value)
    return False


class ToolUseBlock(_Strict):
    type: Literal["tool_use"]
    name: str = Field(min_length=1)
    input: dict[str, Any]

    @field_validator("input")
    @classmethod
    def _input_is_json(cls, value: dict[str, Any]) -> dict[str, Any]:
        # Python's parser accepts NaN and Infinity. They are not JSON, and
        # the answer built from them could not be sent.
        if _has_non_finite(value):
            raise ValueError("a tool input holds NaN or infinity, which is not JSON")
        return value


Block = Annotated[TextBlock | ToolUseBlock, Field(discriminator="type")]
StopReason = Literal["end_turn", "tool_use", "max_tokens", "refusal"]


class StopDetails(_Strict):
    """Why the model declined, sent as the API sends `stop_details`."""

    category: str | None = None
    explanation: str | None = None


class Turn(_Strict):
    content: list[Block] = Field(min_length=1)
    # Left out, it follows from the content: `tool_use` if the turn calls a
    # tool, otherwise `end_turn`.
    stop_reason: StopReason | None = None
    # Only with the stop reason `refusal`.
    stop_details: StopDetails | None = None
    # Reported in `usage.output_tokens_details`. Added to the output tokens,
    # which include thinking in the API too.
    thinking_tokens: int | None = Field(default=None, ge=0, strict=True)
    # Seconds to wait before the answer's content is sent. It makes a job
    # slow enough to be interrupted in the middle of a model call.
    delay_seconds: float = Field(default=0, ge=0, le=MAX_DELAY_SECONDS, allow_inf_nan=False)

    @property
    def calls_tool(self) -> bool:
        return any(isinstance(block, ToolUseBlock) for block in self.content)

    @property
    def resolved_stop_reason(self) -> StopReason:
        if self.stop_reason is not None:
            return self.stop_reason
        return "tool_use" if self.calls_tool else "end_turn"

    @model_validator(mode="after")
    def _stop_reason_matches_content(self) -> Turn:
        if self.stop_reason == "tool_use" and not self.calls_tool:
            raise ValueError("stop_reason is tool_use but the turn has no tool_use block")
        if self.stop_reason == "end_turn" and self.calls_tool:
            raise ValueError("stop_reason is end_turn but the turn has a tool_use block")
        if self.stop_details is not None and self.stop_reason != "refusal":
            raise ValueError("stop_details needs the stop_reason refusal")
        return self


# The bad answers a summary can be scripted to give: a tool call, a refusal,
# empty text, text that is only white space, or a stop other than end_turn.
SummaryFault = Literal["tool_use", "refusal", "empty", "whitespace", "max_tokens", "stop_sequence"]


class Summary(_Strict):
    """The answer to a summary request: the request with `tool_choice` `none`."""

    # The text of every summary. The fake adds its markers after it.
    text: str = Field(min_length=1)
    # Seconds to wait before the answer's content is sent, as for a turn.
    delay_seconds: float = Field(default=0, ge=0, le=MAX_DELAY_SECONDS, allow_inf_nan=False)
    # A bad answer for a summary, by its number in the job, from 1.
    bad: dict[Annotated[int, Field(ge=1)], SummaryFault] = Field(default_factory=dict)


class Scenario(_Strict):
    description: str = ""
    turns: list[Turn] = Field(min_length=1)
    summary: Summary | None = None
    # The context window, in the fake's estimated tokens. A request over it
    # is refused as the API refuses one. Left out, there is no window.
    context_window: int | None = Field(default=None, ge=1, strict=True)


def parse_scenario(text: str) -> Scenario:
    """Parse and validate the JSON text of one scenario file."""
    return Scenario.model_validate_json(text)


def load_scenarios(directory: Path) -> dict[str, Scenario]:
    """Load every `*.json` file in `directory`, keyed by scenario name."""
    scenarios = {}
    for path in sorted(directory.glob("*.json")):
        if not NAME_PATTERN.fullmatch(path.stem):
            raise ValueError(f"{path.name}: a scenario name is lower-case letters, digits and '-'")
        try:
            scenarios[path.stem] = parse_scenario(path.read_text(encoding="utf-8"))
        except ValueError as error:
            raise ValueError(f"{path.name}: {error}") from error
    return scenarios
