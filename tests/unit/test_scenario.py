"""Scenario files: what parses, what is refused, and the packaged ones."""

import json
from importlib import resources
from pathlib import Path
from typing import Any

import pytest

from fake_provider.scenario import load_scenarios, parse_scenario

TEXT = {"type": "text", "text": "hi"}
TOOL = {"type": "tool_use", "name": "read_file", "input": {"path": "a.txt"}}


def scenario(*turns: dict[str, Any], **extra: Any) -> str:
    return json.dumps({"turns": list(turns), **extra})


def test_parses_text_and_tool_use_turns() -> None:
    parsed = parse_scenario(scenario({"content": [TEXT, TOOL]}, {"content": [TEXT]}))
    assert len(parsed.turns) == 2
    assert parsed.turns[0].resolved_stop_reason == "tool_use"
    assert parsed.turns[1].resolved_stop_reason == "end_turn"


def test_explicit_stop_reason_is_kept() -> None:
    parsed = parse_scenario(scenario({"content": [TEXT], "stop_reason": "max_tokens"}))
    assert parsed.turns[0].resolved_stop_reason == "max_tokens"


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        "[]",
        "{}",
        scenario(),
        scenario({"content": []}),
        scenario({"content": [{"type": "text", "text": ""}]}),
        scenario({"content": [{"type": "image", "source": "x"}]}),
        scenario({"content": [{"type": "tool_use", "name": "", "input": {}}]}),
        scenario({"content": [{"type": "tool_use", "name": "t", "input": "not an object"}]}),
        scenario({"content": [{"type": "tool_use", "name": "t"}]}),
        scenario({"content": [TEXT], "stop_reason": "tool_use"}),
        scenario({"content": [TOOL], "stop_reason": "end_turn"}),
        scenario({"content": [TEXT], "stop_reason": "refusal"}),
        scenario({"content": [TEXT], "delay": 5}),
        scenario({"content": [{**TEXT, "txet": "typo"}]}),
        scenario({"content": [TEXT]}, turn=1),
        # Python's parser reads these. They are not JSON.
        scenario({"content": [TOOL]}).replace('"a.txt"', "NaN"),
        scenario({"content": [TOOL]}).replace('"a.txt"', "[Infinity]"),
        scenario({"content": [TOOL]}).replace('"a.txt"', '{"n": 1e999}'),
    ],
)
def test_refuses(text: str) -> None:
    with pytest.raises(ValueError):
        parse_scenario(text)


def test_load_scenarios_keys_by_file_name(tmp_path: Path) -> None:
    (tmp_path / "one.json").write_text(scenario({"content": [TEXT]}))
    (tmp_path / "two-b.json").write_text(scenario({"content": [TOOL]}))
    (tmp_path / "notes.txt").write_text("ignored")
    assert sorted(load_scenarios(tmp_path)) == ["one", "two-b"]


@pytest.mark.parametrize("name", ["Upper.json", "with space.json", "-dash.json", "a_b.json"])
def test_load_scenarios_refuses_a_bad_name(tmp_path: Path, name: str) -> None:
    (tmp_path / name).write_text(scenario({"content": [TEXT]}))
    with pytest.raises(ValueError, match="scenario name"):
        load_scenarios(tmp_path)


def test_load_scenarios_names_the_broken_file(tmp_path: Path) -> None:
    (tmp_path / "broken.json").write_text(scenario())
    with pytest.raises(ValueError, match=r"broken\.json"):
        load_scenarios(tmp_path)


def test_packaged_scenarios_load_and_include_hello() -> None:
    with resources.as_file(resources.files("fake_provider") / "scenarios") as directory:
        loaded = load_scenarios(directory)
    assert "hello" in loaded
    assert [turn.resolved_stop_reason for turn in loaded["hello"].turns] == ["tool_use", "end_turn"]
