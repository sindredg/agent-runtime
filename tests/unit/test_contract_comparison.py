"""The comparison the contract test makes between the fake provider and the Claude API.

The contract test itself needs a key and is run by hand. What it compares is
checked here on made-up responses. One of them repeats what the API did in
the run of 2026-11-14: it called the tool without writing text first, and
its `tool_use` block had a `caller` field that the fake does not return.
"""

import copy
import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

CONTRACT = Path(__file__).resolve().parents[1] / "contract" / "test_messages_contract.py"


@pytest.fixture(scope="module")
def contract() -> ModuleType:
    spec = importlib.util.spec_from_file_location("contract_test", CONTRACT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def message(stop_reason: str, *content: dict[str, Any], **more: Any) -> dict[str, Any]:
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "m",
        "content": list(content),
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 5},
        **more,
    }


TEXT = {"type": "text", "text": "I will read the file."}
CALL = {"type": "tool_use", "id": "toolu_1", "name": "read_file", "input": {"path": "hello.txt"}}

# The fake writes a sentence before its tool call.
FAKE = [message("tool_use", TEXT, CALL), message("end_turn", TEXT)]
# The API went straight to the call. Its messages, blocks and usage have
# fields the fake leaves out. Only their presence matters here, not their values.
REAL = [
    message(
        "tool_use",
        {**CALL, "caller": None},
        usage={"input_tokens": 10, "output_tokens": 5, "another_count": 0},
        another_field=None,
    ),
    message("end_turn", {**TEXT, "another_field": None}),
]


def test_a_fake_that_writes_text_where_the_model_did_not_is_no_difference(
    contract: ModuleType,
) -> None:
    """Whether a model writes before it calls a tool is its choice, not the API's shape."""
    assert contract.differences(FAKE, REAL) == []


def changed(turn: int, **fields: Any) -> list[dict[str, Any]]:
    fake = copy.deepcopy(FAKE)
    fake[turn].update(fields)
    return fake


@pytest.mark.parametrize(
    ("fake", "found"),
    [
        (changed(0, invented="x"), "invented"),
        (changed(1, usage={"input_tokens": 1, "output_tokens": 1, "made_up_tokens": 1}), "made_up"),
        (changed(0, stop_reason="end_turn"), "stop_reason"),
        (changed(1, role="user"), "role"),
        (changed(1, content=[{"type": "image", "source": "x"}]), "image"),
        (changed(0, content=[TEXT, {**CALL, "extra": 1}]), "extra"),
    ],
    ids=[
        "top-level-field",
        "usage-field",
        "stop-reason",
        "role",
        "block-type-the-api-never-sent",
        "block-field",
    ],
)
def test_what_the_fake_returns_and_the_api_does_not_is_reported(
    contract: ModuleType, fake: list[dict[str, Any]], found: str
) -> None:
    differences = contract.differences(fake, REAL)
    assert len(differences) == 1
    assert found in differences[0]


def test_a_field_on_one_of_two_fake_blocks_of_a_type_is_still_reported(
    contract: ModuleType,
) -> None:
    """The fake has a text block in each turn. A field on only one of them counts."""
    fake = changed(1, content=[{**TEXT, "extra": 1}])
    differences = contract.differences(fake, REAL)
    assert len(differences) == 1
    assert "extra" in differences[0]


def test_a_field_on_only_some_api_blocks_of_a_type_is_not_one_the_fake_may_rely_on(
    contract: ModuleType,
) -> None:
    real = copy.deepcopy(REAL)
    real[0]["content"].insert(0, {**TEXT, "sometimes": 1})
    fake = changed(1, content=[{**TEXT, "sometimes": 1}])
    differences = contract.differences(fake, real)
    assert len(differences) == 1
    assert "sometimes" in differences[0]
