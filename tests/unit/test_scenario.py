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
        scenario({"content": [TEXT], "stop_reason": "pause_turn"}),
        # `stop_details` belongs to a refusal, and has two known fields.
        scenario({"content": [TEXT], "stop_details": {"category": "cyber"}}),
        scenario({"content": [TEXT], "stop_reason": "end_turn", "stop_details": {}}),
        scenario({"content": [TEXT], "stop_reason": "refusal", "stop_details": {"why": "x"}}),
        scenario({"content": [TEXT], "stop_reason": "refusal", "stop_details": {"category": 7}}),
        scenario({"content": [TEXT], "thinking_tokens": -1}),
        scenario({"content": [TEXT], "thinking_tokens": True}),
        scenario({"content": [TEXT], "thinking_tokens": "5"}),
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


# A refusal and a thinking count


def test_a_turn_can_be_a_refusal_with_or_without_details() -> None:
    parsed = parse_scenario(
        scenario(
            {
                "content": [TEXT],
                "stop_reason": "refusal",
                "stop_details": {"category": "cyber", "explanation": "why"},
            },
            {"content": [TEXT], "stop_reason": "refusal"},
        )
    )
    first, second = parsed.turns
    assert first.resolved_stop_reason == second.resolved_stop_reason == "refusal"
    assert first.stop_details is not None
    assert (first.stop_details.category, first.stop_details.explanation) == ("cyber", "why")
    assert second.stop_details is None


def test_a_turn_can_report_thinking_tokens() -> None:
    parsed = parse_scenario(scenario({"content": [TEXT], "thinking_tokens": 12}))
    assert parsed.turns[0].thinking_tokens == 12


# Delay


def test_a_turn_has_no_delay_unless_it_says_so() -> None:
    scenario = parse_scenario('{"turns": [{"content": [{"type": "text", "text": "hi"}]}]}')
    assert scenario.turns[0].delay_seconds == 0


def test_a_turn_can_ask_for_a_delay() -> None:
    scenario = parse_scenario(
        '{"turns": [{"delay_seconds": 0.5, "content": [{"type": "text", "text": "hi"}]}]}'
    )
    assert scenario.turns[0].delay_seconds == 0.5


@pytest.mark.parametrize("delay", ["-1", "301", '"soon"', "NaN", "Infinity", "null"])
def test_a_delay_that_is_not_a_sensible_number_of_seconds_is_refused(delay: str) -> None:
    turn = '{"delay_seconds": ' + delay + ', "content": [{"type": "text", "text": "hi"}]}'
    text = '{"turns": [' + turn + "]}"
    with pytest.raises(ValueError, match="delay_seconds"):
        parse_scenario(text)


def test_the_packaged_chaos_scenarios_load() -> None:
    """The slow and stalled variants answer like ci-triage. Only their timing differs."""
    from importlib import resources

    with resources.as_file(resources.files("fake_provider") / "scenarios") as directory:
        scenarios = load_scenarios(directory)

    def content(name: str) -> list[object]:
        return [turn.content for turn in scenarios[name].turns]

    assert content("ci-triage-slow") == content("ci-triage")
    assert content("ci-triage-stall") == content("ci-triage")
    assert [turn.delay_seconds for turn in scenarios["ci-triage"].turns] == [0, 0, 0]
    assert all(turn.delay_seconds > 0 for turn in scenarios["ci-triage-slow"].turns)
    assert scenarios["ci-triage-stall"].turns[0].delay_seconds >= 5
    assert scenarios["crash-loop"].turns[0].content[-1].name == "crash_worker"  # type: ignore[union-attr]


def test_a_scenario_has_no_context_window_unless_it_sets_one() -> None:
    assert parse_scenario(scenario({"content": [TEXT]})).context_window is None
    assert parse_scenario(scenario({"content": [TEXT]}, context_window=1)).context_window == 1


@pytest.mark.parametrize("window", [0, -1, True, "5", 1.5])
def test_a_context_window_that_is_not_a_positive_whole_number_is_refused(window: Any) -> None:
    with pytest.raises(ValueError):
        parse_scenario(scenario({"content": [TEXT]}, context_window=window))
