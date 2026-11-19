"""The comparison the contract test makes between the fake provider and the Claude API.

The contract test itself needs a key and is run by hand. What it compares is
checked here on made-up responses. One of them repeats what the API did in
the run of 2026-11-14: it called the tool without writing text first, and
its `tool_use` block had a `caller` field that the fake does not return.
"""

import copy
import importlib.util
import re
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from _pytest._io.saferepr import saferepr

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


# The other direction: what the API returns and the fake does not.

THINKING = {"type": "thinking", "thinking": "", "signature": "sig"}


def test_what_the_api_returns_and_the_fake_does_not_is_listed(contract: ModuleType) -> None:
    """REAL has three fields the fake leaves out: on a message, on usage, on a block."""
    assert contract.api_only(FAKE, REAL) == [
        "message field another_field",
        "text block field another_field",
        "tool_use block field caller",
        "usage field another_count",
    ]


def test_the_same_responses_list_nothing(contract: ModuleType) -> None:
    assert contract.api_only(REAL, REAL) == []


@pytest.mark.parametrize(
    ("real", "found"),
    [
        ([message("tool_use", THINKING, CALL), FAKE[1]], "block type thinking"),
        (
            [message("tool_use", CALL, {**CALL, "id": "toolu_2"}), FAKE[1]],
            "more than one tool_use block in a response",
        ),
        ([FAKE[0], message("refusal", TEXT)], "stop reason refusal"),
    ],
    ids=["block-type", "two-calls", "stop-reason"],
)
def test_each_kind_of_api_only_entry_is_named(
    contract: ModuleType, real: list[dict[str, Any]], found: str
) -> None:
    assert contract.api_only(FAKE, real) == [found]


def test_an_entry_that_is_not_declared_fails_the_contract_test(contract: ModuleType) -> None:
    """The check the keyed run makes, on a response with a block type nobody declared."""
    real = [message("tool_use", {"type": "made_up", "x": 1}, CALL), FAKE[1]]
    exchanges = (FAKE, real)
    with pytest.raises(AssertionError):
        contract.test_what_only_the_api_returns_is_declared(exchanges)


def test_every_declared_entry_has_its_line_on_the_contract_test_page(contract: ModuleType) -> None:
    page = (CONTRACT.parents[2] / "docs" / "contract-test.md").read_text()
    section = page.split("### What the fake does not model", 1)[1].split("\n#", 1)[0]
    listed = {line.split("`")[1] for line in section.splitlines() if line.startswith("| `")}
    assert listed == set(contract.NOT_MODELLED)


# The key in a failure report


def test_no_function_of_the_contract_test_takes_the_key() -> None:
    """pytest prints the arguments of the call that failed. The run's log is public."""
    source = CONTRACT.read_text(encoding="utf-8")
    taking = re.findall(r"def \w+\([^)]*\b(?:api_)?key\b[^)]*\)", source)
    assert taking == []


def test_the_key_is_not_shown_where_pytest_prints_a_value(
    contract: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-a-real-key-x9")
    monkeypatch.setenv("CONTRACT_MODEL", "some-model")
    _, key, _ = contract.target("claude")
    # It is still the key for a header.
    assert key == "not-a-real-key-x9"
    assert "x9" not in repr(key)
    assert "x9" not in saferepr(key)
    assert "x9" not in saferepr(("https://api.example", key, "some-model"))
