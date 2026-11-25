"""The pass line of the long-job suite, checked on made-up logs.

The runs themselves need the long stack. What they judge a job by is checked
here: each rule gets a log that passes and one that breaks only that rule.

The made-up job has three windows. Its requests have the keys (0, 0) to
(0, 2), a summary request at (0, 3), then (1, 3) and (1, 4), a summary
request at (1, 5), then (2, 5) and (2, 6). Turns 1 and 4 open an issue.
"""

import copy
import importlib.util
import json
import subprocess
import sys
import time
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from _pytest.junitxml import shorten_message

from fake_provider.scenario import NAME_PATTERN, parse_scenario

RULES = Path(__file__).resolve().parents[1] / "long" / "rules.py"
JOB = "0a0a"
OTHER = "0b0b"
PROMPT = {"role": "user", "content": "Read every part."}
SUMMARY = "The task and what was read."
CONTINUATION = "Go on from the summary."
INSTRUCTION = "Write a summary."
TOOLS = [{"name": "read_file"}, {"name": "create_issue"}]
WINDOWS = ((0, 3), (3, 5), (5, 7))
# One window more: three summaries, as the suite's own job has.
FOUR_WINDOWS = ((0, 3), (3, 5), (5, 7), (7, 9))
ISSUE_TURNS = (1, 4)
NONE = {"type": "none"}
TESTS = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def rules() -> ModuleType:
    spec = importlib.util.spec_from_file_location("long_rules", RULES)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered before it runs: the dataclasses in it look their module up.
    sys.modules["long_rules"] = module
    spec.loader.exec_module(module)
    return module


def packaged() -> dict[str, Any]:
    text = (resources.files("fake_provider") / "scenarios" / "long-window.json").read_text()
    found: dict[str, Any] = json.loads(text)
    return found


def call_id(turn: int) -> str:
    return f"toolu_{turn:02d}"


class Log:
    """The steps of a job, the provider's entries for it and the tracker's list."""

    def __init__(
        self,
        job_id: str = JOB,
        first_issue: int = 7,
        *,
        windows: tuple[tuple[int, int], ...] = WINDOWS,
        summary: str = SUMMARY,
        continuation: str = CONTINUATION,
        instruction: str = INSTRUCTION,
    ) -> None:
        self.id = job_id
        self.steps: list[dict[str, Any]] = []
        self.entries: list[dict[str, Any]] = []
        self.tracker: list[dict[str, Any]] = []
        number = first_issue
        for window, (start, end) in enumerate(windows):
            opening = f"{summary}\n\n[summary {window}] [turn {start}]\n\n{continuation}"
            messages = [
                copy.deepcopy(PROMPT)
                if window == 0
                else {"role": "user", "content": [self.text(opening)]}
            ]
            for turn in range(start, end):
                self.request(messages, f"msg_{turn:02d}")
                if turn == windows[-1][1] - 1:
                    self.step("model_response", id=f"msg_{turn:02d}", content=[self.text(turn)])
                    break
                name = "create_issue" if turn in ISSUE_TURNS else "read_file"
                content = [
                    self.text(turn),
                    {"type": "tool_use", "id": call_id(turn), "name": name, "input": {}},
                ]
                self.step("model_response", id=f"msg_{turn:02d}", content=content)
                result = "read."
                if name == "create_issue":
                    result = f"created issue {number}: part {turn}"
                    operation = f"{job_id}:{call_id(turn)}"
                    self.tracker.append({"id": number, "operation_id": operation})
                    number += 1
                self.step("tool_result", tool_use_id=call_id(turn), content=result, is_error=False)
                block = {
                    "type": "tool_result",
                    "tool_use_id": call_id(turn),
                    "content": result,
                    "is_error": False,
                }
                messages = [
                    *messages,
                    {"role": "assistant", "content": content},
                    {"role": "user", "content": [block]},
                ]
            else:
                last = messages[-1]
                asked = {
                    "role": "user",
                    "content": [*last["content"], {"type": "text", "text": instruction}],
                }
                self.request([*messages[:-1], asked], f"msg_summary_{window + 1:02d}", summary=True)
                text = f"{summary}\n\n[summary {window + 1}] [turn {end}]"
                self.step("summary", id=f"msg_summary_{window + 1:02d}", content=[self.text(text)])

    @staticmethod
    def text(of: object) -> dict[str, Any]:
        return {"type": "text", "text": str(of)}

    def step(self, kind: str, epoch: int = 1, **payload: Any) -> None:
        self.steps.append(
            {"seq": len(self.steps) + 1, "kind": kind, "lease_epoch": epoch, "payload": payload}
        )

    def request(self, messages: list[dict[str, Any]], answer: str, summary: bool = False) -> None:
        body: dict[str, Any] = {
            "model": "long-window",
            "max_tokens": 100,
            "tools": copy.deepcopy(TOOLS),
            "messages": copy.deepcopy(messages),
            "metadata": {"user_id": self.id},
        }
        payload: dict[str, Any] = {
            "max_tokens": 100,
            "messages": len(messages),
            "tools": [tool["name"] for tool in TOOLS],
        }
        if summary:
            body["tool_choice"] = dict(NONE)
            payload["summary"] = True
        self.step("model_request", **payload)
        self.entries.append(
            {
                "seq": len(self.entries) + 1,
                "user_id": self.id,
                "tool_choice": body.get("tool_choice"),
                "status": 200,
                "response_id": answer,
                "input_estimate": 50,
                "body": body,
            }
        )

    def seen(
        self, rules: ModuleType, *, questions: Any = (), log_length: int = 9, **job: Any
    ) -> Any:
        view = {"id": self.id, "status": "succeeded", "error": None, "takeovers": 0, **job}
        return rules.Seen(
            job=view,
            steps=self.steps,
            entries=self.entries,
            issues=rules.issues_of(self.id, self.tracker),
            questions=list(questions),
            log_length=log_length,
        )

    def taken_over_after(self, seq: int) -> None:
        """Every step after `seq` is the next worker's."""
        for step in self.steps:
            step["lease_epoch"] = 2 if step["seq"] > seq else 1

    def first(self, kind: str, **payload: Any) -> dict[str, Any]:
        """The first step of a kind whose payload holds these values."""
        return next(
            step
            for step in self.steps
            if step["kind"] == kind
            and all(step["payload"].get(name) == value for name, value in payload.items())
        )

    def at(self, rules: ModuleType, key: tuple[int, int], summary: bool = False) -> dict[str, Any]:
        """The entry at a key."""
        found: dict[str, Any] = rules.by_key(self.entries)[key, summary]
        return found


# What the stack is started with


def test_no_setting_of_the_stack_comes_from_the_inherited_environment(rules: ModuleType) -> None:
    inherited = {
        "PATH": "/usr/bin",
        "DOCKER_GID": "999",
        "LONG_SCENARIO_DIR": "/srv/other",
        "SUMMARY_TRIGGER": "1",
        "FAILPOINT": "after_claim",
        "FAILPOINT_SUMMARY": "1",
        "FAKE_TRACKER_FIRST_ISSUE": "900000",
    }
    assert set(rules.SETTINGS) < inherited.keys()

    plain = rules.compose_env(inherited, scenario_dir="/srv/session")
    assert plain == {
        "PATH": "/usr/bin",
        "DOCKER_GID": "999",
        "LONG_SCENARIO_DIR": "/srv/session",
        "SUMMARY_TRIGGER": "",
        "FAILPOINT": "",
        "FAILPOINT_SUMMARY": "",
        "FAKE_TRACKER_FIRST_ISSUE": "",
    }
    # No inherited value of the five is anywhere in the result.
    assert not {inherited[name] for name in rules.SETTINGS} & set(plain.values())

    chosen = rules.compose_env(
        inherited,
        scenario_dir="/srv/session",
        trigger=2800,
        failpoint="after_summary",
        failpoint_summary=True,
        first_issue="200000",
    )
    assert [chosen[name] for name in rules.SETTINGS] == [
        "/srv/session",
        "2800",
        "after_summary",
        "1",
        "200000",
    ]


def test_the_stacks_scenario_is_the_packaged_one_with_a_wait_on_every_answer(
    rules: ModuleType,
) -> None:
    original = packaged()
    written = rules.scenario_files(original)[rules.MODEL]

    assert written != original
    assert {turn["delay_seconds"] for turn in written["turns"]} == {rules.DELAY}
    assert written["summary"]["delay_seconds"] == rules.DELAY
    # Without the wait it is the packaged scenario: no request differs.
    for turn in written["turns"]:
        del turn["delay_seconds"]
    del written["summary"]["delay_seconds"]
    assert written == original
    # The packaged scenario itself was not changed.
    assert original == packaged()


def test_each_bad_summary_has_a_scenario_the_fake_provider_loads(rules: ModuleType) -> None:
    original = packaged()
    files = rules.scenario_files(original)

    assert sorted(files) == sorted(
        [
            "long-window",
            "long-window-bad-tool-use",
            "long-window-bad-refusal",
            "long-window-bad-empty",
            "long-window-bad-whitespace",
            "long-window-bad-max-tokens",
            "long-window-bad-stop-sequence",
        ]
    )
    for name, scenario in files.items():
        # The fake refuses a name with an underscore, and a key it does not know.
        assert NAME_PATTERN.fullmatch(name)
        loaded = parse_scenario(json.dumps(scenario))
        assert len(loaded.turns) == len(original["turns"])
    for fault in rules.FAULTS:
        bad = files[rules.bad_name(fault)]
        # The first summary gets the fault, under its own name, and no answer waits.
        section = {**original["summary"], "bad": {"1": fault}}
        if fault == "tool_use":
            # Its call would open an issue if it ran, under the id the provider gives it.
            section["bad_call"] = rules.BAD_CALL
            assert rules.BAD_CALL["name"] == "create_issue"
            assert rules.bad_call_id(fault) == "toolu_fake_long-window-bad-tool-use_summary_01"
        assert bad == {**original, "summary": section}
        assert parse_scenario(json.dumps(bad)).summary.bad == {1: fault}  # type: ignore[union-attr]


def test_the_jobs_files_are_one_short_line_for_each_file_the_scenario_reads(
    rules: ModuleType,
) -> None:
    files = rules.job_files(packaged())
    # 44 turns: 39 read a file, 4 open an issue, and the last one only answers.
    assert len(files) == 39
    assert files["notes/01.txt"] == "notes/01.txt: read.\n"
    assert all(text == f"{path}: read.\n" for path, text in files.items())


# The key of a request


def test_a_requests_key_is_its_window_and_its_turn_in_the_job(rules: ModuleType) -> None:
    log = Log()
    keys = [rules.key(entry["body"]) for entry in log.entries]
    assert keys == [(0, 0), (0, 1), (0, 2), (0, 3), (1, 3), (1, 4), (1, 5), (2, 5), (2, 6)]
    assert rules.summary_keys(log.entries) == [(0, 3), (1, 5)]
    assert rules.repeated_keys(log.entries) == []


def test_a_resent_summary_request_and_a_refused_request_have_keys_too(rules: ModuleType) -> None:
    log = Log()
    resent = copy.deepcopy(log.at(rules, (0, 3), summary=True))
    refused = copy.deepcopy(log.at(rules, (1, 4)))
    # A refused entry has no `turn`, no answer and no scenario: only its body.
    refused = {"seq": 99, "status": 400, "error": "refused", "body": refused["body"]}
    log.entries.insert(4, resent)
    log.entries.append(refused)

    assert rules.key(resent["body"]) == (0, 3)
    assert rules.key(refused["body"]) == (1, 4)
    assert rules.summary_keys(log.entries) == [(0, 3), (0, 3), (1, 5)]
    assert rules.repeated_keys(log.entries) == [(0, 3), (1, 4)]


# The job's end and its summaries


def test_a_job_that_did_not_succeed_is_named_with_its_error(rules: ModuleType) -> None:
    assert rules.not_succeeded({"status": "succeeded", "error": None}) is None
    found = rules.not_succeeded({"status": "failed", "error": "model call failed"})
    assert found == "status failed, error 'model call failed'"
    assert rules.not_succeeded({"status": "running", "error": None}) is not None


def test_the_number_of_summary_steps_is_held_to_the_expected_one(rules: ModuleType) -> None:
    log = Log()
    assert rules.summary_step_count(log.steps, 2) is None
    # More is told from fewer: a job that summarised again, and one that stopped early.
    assert rules.summary_step_count(log.steps, 1) == (True, "2 summary steps, expected 1")
    assert rules.summary_step_count(log.steps, 3) == (False, "2 summary steps, expected 3")
    log.step("summary", id="msg_summary_01", content=[])
    assert rules.summary_step_count(log.steps, 2) == (True, "3 summary steps, expected 2")


def test_each_of_the_first_windows_has_an_answered_summary_request(rules: ModuleType) -> None:
    log = Log()
    assert rules.summary_window_count(log.entries, 2) is None
    # A window more than expected has one too.
    longer = Log(windows=FOUR_WINDOWS)
    assert rules.summary_window_count(longer.entries, 3) is None
    assert rules.summary_window_count(longer.entries, 2) == (
        "windows with an answered summary request [0, 1, 2], expected 2 from 0"
    )
    # The second window's summary request was refused.
    log.at(rules, (1, 5), summary=True)["status"] = 400
    found = rules.summary_window_count(log.entries, 2)
    assert found == "windows with an answered summary request [0], expected 2 from 0"


def test_a_summary_request_is_the_request_before_it_its_answer_and_the_instruction(
    rules: ModuleType,
) -> None:
    log = Log()
    assert rules.summary_request_problems(log.steps, log.entries, INSTRUCTION) == []


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        ("tool_choice", "(0, 3): tool_choice is {'type': 'auto'}"),
        ("tools", "(0, 3): its tools is not that of the request before it"),
        ("system", "(0, 3): its system is not that of the request before it"),
        ("history", "(0, 3): its messages are not the request before it and its answer"),
        ("result", "(0, 3): its last message does not hold one result for each call"),
        ("instruction", "(0, 3): its last block is not the instruction"),
        ("no answer", "(0, 3): no model_response step has the id msg_02"),
        ("no request", "(0, 3): the job has no answered request at (0, 2)"),
        ("answer", "(0, 3): its messages are not the request before it and its answer"),
        ("result id", "(0, 3): its last message does not hold one result for each call"),
        ("result content", "(0, 3): a result in its last message is not the stored result"),
        ("result error", "(0, 3): a result in its last message is not the stored result"),
    ],
)
def test_a_summary_request_that_differs_in_one_part_is_found(
    rules: ModuleType, change: str, problem: str
) -> None:
    log = Log()
    body = log.at(rules, (0, 3), summary=True)["body"]
    if change == "tool_choice":
        body["tool_choice"] = {"type": "auto"}
    elif change == "tools":
        body["tools"] = body["tools"][:1]
    elif change == "system":
        body["system"] = "another"
    elif change == "history":
        body["messages"][1]["content"][0]["text"] = "another"
    elif change == "result":
        del body["messages"][-1]["content"][0]
    elif change == "instruction":
        body["messages"][-1]["content"][-1]["text"] = "Write a poem."
    elif change == "no answer":
        log.steps = [s for s in log.steps if s["payload"].get("id") != "msg_02"]
    elif change == "no request":
        log.entries = [e for e in log.entries if rules.key(e["body"]) != (0, 2)]
    elif change == "answer":
        # The assistant message before the results is not the stored answer.
        body["messages"][-2]["content"][0]["text"] = "another"
    elif change == "result id":
        body["messages"][-1]["content"][0]["tool_use_id"] = "toolu_99"
    elif change == "result content":
        body["messages"][-1]["content"][0]["content"] = "something else"
    elif change == "result error":
        body["messages"][-1]["content"][0]["is_error"] = True

    assert rules.summary_request_problems(log.steps, log.entries, INSTRUCTION) == [problem]


# The opening of a window


def test_each_window_opens_with_the_summary_as_sent_and_the_continuation(
    rules: ModuleType,
) -> None:
    log = Log()
    assert rules.opening_problems(log.entries, SUMMARY, CONTINUATION) == []
    # A request sent again after a takeover opens the window the same way.
    log.entries.insert(5, copy.deepcopy(log.at(rules, (1, 3))))
    assert rules.opening_problems(log.entries, SUMMARY, CONTINUATION) == []


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        ("cut", "after the summary at (0, 3): the opening message differs"),
        ("another turn", "after the summary at (0, 3): the opening message differs"),
        ("no continuation", "after the summary at (0, 3): the opening message differs"),
        ("whole history", "after the summary at (0, 3): the next request holds 7 messages"),
        ("nothing after", "after the summary at (1, 5): no request follows"),
    ],
)
def test_a_window_that_opens_another_way_is_found(
    rules: ModuleType, change: str, problem: str
) -> None:
    log = Log()
    first = log.at(rules, (1, 3))["body"]["messages"]
    text = first[0]["content"][0]["text"]
    if change == "cut":
        # The text cut in half, with the line of the marker kept whole: the
        # request then still has the key (1, 3).
        marker = f"[summary 1] [turn 3]\n\n{CONTINUATION}"
        first[0]["content"][0]["text"] = text[: len(SUMMARY) // 2] + "\n\n" + marker
        assert rules.key({"messages": first}) == (1, 3)
    elif change == "another turn":
        first[0]["content"][0]["text"] = text.replace("[turn 3]", "[turn 2]")
    elif change == "no continuation":
        first[0]["content"][0]["text"] = text.removesuffix(CONTINUATION)
    elif change == "whole history":
        log.at(rules, (1, 3))["body"]["messages"] = copy.deepcopy(
            log.at(rules, (0, 3), summary=True)["body"]["messages"]
        )
    elif change == "nothing after":
        log.entries = log.entries[:7]

    assert rules.opening_problems(log.entries, SUMMARY, CONTINUATION) == [problem]


# The tracker


def test_the_tracker_holds_one_issue_for_each_call_the_script_makes(rules: ModuleType) -> None:
    log = Log()
    issues = rules.issues_of(JOB, log.tracker)
    assert issues == {f"{JOB}:toolu_01": [7], f"{JOB}:toolu_04": [8]}
    assert rules.issue_problems(JOB, log.steps, issues, {}) == []
    # Another job's issues are not this job's.
    other = Log(OTHER, first_issue=20)
    assert rules.issues_of(JOB, [*log.tracker, *other.tracker]) == issues
    # The calls come from the uninterrupted job's steps, whose ids are the same.
    assert rules.issue_problems(OTHER, log.steps, rules.issues_of(OTHER, other.tracker), {}) == []


def test_an_issue_too_many_too_few_or_of_an_unscripted_call_is_found(rules: ModuleType) -> None:
    log = Log()
    first, second = f"{JOB}:toolu_01", f"{JOB}:toolu_04"

    twice = {first: [7, 9], second: [8]}
    assert rules.issue_problems(JOB, log.steps, twice, {}) == [f"{first}: 2 issues, expected 1"]
    # A rerun that a person approved may have opened a second issue.
    assert rules.issue_problems(JOB, log.steps, twice, {first: 1}) == []
    assert rules.issue_problems(JOB, log.steps, {first: [7], second: [8]}, {first: 1}) == [
        f"{first}: 1 issues, expected 2"
    ]
    assert rules.issue_problems(JOB, log.steps, {first: [7]}, {}) == [
        f"{second}: 0 issues, expected 1"
    ]
    unscripted = {first: [7], second: [8], f"{JOB}:toolu_summary_01": [9]}
    assert rules.issue_problems(JOB, log.steps, unscripted, {}) == [
        f"{JOB}:toolu_summary_01: 1 issues for a call the script does not make"
    ]


# The provider's log against the request steps


def test_every_entry_belongs_to_a_request_step_and_every_step_was_sent(rules: ModuleType) -> None:
    log = Log()
    found = rules.matched(log.entries, log.steps)
    assert (len(found.pairs), found.without_step, found.unsent) == (9, [], 0)
    assert all(step["kind"] == "model_request" for step, _ in found.pairs)
    assert rules.tool_name_problems(found.pairs) == []


def test_a_resend_has_its_own_step_and_an_unsent_request_is_counted(rules: ModuleType) -> None:
    log = Log()
    # A worker wrote the request step of (0, 1), the first with three messages,
    # and stopped before it sent it. The next worker wrote the step again and
    # sent it.
    position = next(i for i, s in enumerate(log.steps) if s["payload"].get("messages") == 3)
    log.steps.insert(position, copy.deepcopy(log.steps[position]))
    found = rules.matched(log.entries, log.steps)
    assert (len(found.pairs), found.without_step, found.unsent) == (9, [], 1)

    # A request that was sent twice has two steps and two entries.
    sent_twice = Log()
    step = next(s for s in sent_twice.steps if s["payload"].get("messages") == 3)
    sent_twice.steps.insert(sent_twice.steps.index(step), copy.deepcopy(step))
    sent_twice.entries.insert(2, copy.deepcopy(sent_twice.at(rules, (0, 1))))
    found = rules.matched(sent_twice.entries, sent_twice.steps)
    assert (len(found.pairs), found.without_step, found.unsent) == (10, [], 0)


def test_an_entry_that_no_step_announced_is_found(rules: ModuleType) -> None:
    log = Log()
    # The last request sent twice, written once.
    log.entries.append({**copy.deepcopy(log.at(rules, (2, 6))), "seq": 77})
    found = rules.matched(log.entries, log.steps)
    assert (len(found.pairs), found.without_step, found.unsent) == (9, [77], 0)

    # A request with a number of messages that no step names.
    longer = Log()
    longer.at(rules, (0, 1))["body"]["messages"].append({"role": "user", "content": "one more"})
    found = rules.matched(longer.entries, longer.steps)
    assert (found.without_step, found.unsent) == ([2], 1)

    # A request with another `max_tokens` than any step names.
    other = Log()
    other.at(rules, (0, 1))["body"]["max_tokens"] = 99
    found = rules.matched(other.entries, other.steps)
    assert (found.without_step, found.unsent) == ([2], 1)


def test_steps_whose_request_never_arrived_are_held_to_the_number_allowed(
    rules: ModuleType,
) -> None:
    log = Log()
    step = next(s for s in log.steps if s["payload"].get("messages") == 3)
    log.steps.insert(log.steps.index(step), copy.deepcopy(step))
    found = rules.matched(log.entries, log.steps)

    assert rules.unsent_over(found, 1) is None
    assert rules.unsent_over(found, 0) == "1 steps, at most 0 expected"
    none = rules.matched(Log().entries, Log().steps)
    assert rules.unsent_over(none, 0) is None
    # A worker stopped between the step and the call leaves exactly one.
    assert rules.unsent_over(none, 1) is None
    assert rules.unsent_over(none, 1, exactly=True) == "0 steps, exactly 1 expected"
    assert rules.unsent_over(found, 1, exactly=True) is None
    assert rules.unsent_over(found, 0, exactly=True) == "1 steps, exactly 0 expected"


def test_a_log_at_the_providers_cap_may_have_dropped_requests(rules: ModuleType) -> None:
    assert rules.log_full(9_999) is None
    assert rules.log_full(10_000) == "10000 entries, the cap is 10000"


def test_a_takeover_of_the_job_nobody_interrupted_is_named(rules: ModuleType) -> None:
    assert rules.taken_over({"takeovers": 0}) is None
    assert rules.taken_over({"takeovers": 2}) == "2 takeovers"


def test_approved_reruns_are_counted_by_operation(rules: ModuleType) -> None:
    asked = [{"operation_id": "a:1"}, {"operation_id": "a:2"}, {"operation_id": "a:1"}]
    assert rules.approvals([]) == {}
    assert rules.approvals(asked) == {"a:1": 2, "a:2": 1}


def test_a_step_that_names_other_tools_than_its_request_offered_is_found(rules: ModuleType) -> None:
    log = Log()
    log.at(rules, (0, 1))["body"]["tools"] = [{"name": "read_file"}]
    found = rules.matched(log.entries, log.steps)
    assert rules.tool_name_problems(found.pairs) == [
        "step 4: ['read_file', 'create_issue'], sent ['read_file']"
    ]


def test_the_lines_of_a_worker_that_stopped_at_a_failpoint_are_counted(rules: ModuleType) -> None:
    line = '{"level": "warning", "message": "failpoint reached, stopping the worker"}'
    assert rules.failpoint_lines('{"message": "claimed job"}\n{"message": "job finished"}') == 0
    assert rules.failpoint_lines(f'{{"message": "claimed job"}}\n{line}\n{line}') == 2


# A job that failed


def refused(rules: ModuleType, log: Log, **entry: Any) -> list[dict[str, Any]]:
    """The log cut after (1, 4), whose request the provider refused for the window."""
    entries = log.entries[:6]
    entries[-1] = {
        "seq": 6,
        "status": 400,
        "error": "prompt is too long: 3526 tokens > 3400 maximum",
        "input_estimate": 3526,
        "body": entries[-1]["body"],
        **entry,
    }
    return entries


def test_a_job_refused_for_the_window_on_a_scripted_turn_is_told_from_other_failures(
    rules: ModuleType,
) -> None:
    log = Log()
    failed = {"status": "failed", "error": "model call failed: provider answered 400"}
    assert rules.how_it_failed({"status": "succeeded"}, log.entries, 3400, 7) is None

    at_the_window = rules.how_it_failed(failed, refused(rules, log), 3400, 7)
    assert at_the_window is not None
    assert at_the_window[0] is True
    assert at_the_window[1].startswith(
        "last request: status 400, estimate 3526, window 3400, turn 4 of 7; error"
    )


@pytest.mark.parametrize(
    "entry",
    [
        # Past the end of the script: turn 4 of a script of 4 turns.
        {"turns": 4},
        {"error": "scenario long-window has 4 turns; the request asks for turn 5"},
        # Inside the window.
        {"input_estimate": 3400},
        {"input_estimate": None},
        {"status": 529},
        # Answered: the job failed for something the provider did not refuse.
        {"status": 200, "error": None},
    ],
)
def test_another_failure_is_not_a_refusal_for_the_window(
    rules: ModuleType, entry: dict[str, Any]
) -> None:
    log = Log()
    failed = {"status": "failed", "error": "the token budget does not cover the next model call"}
    turns = entry.pop("turns", 7)
    found = rules.how_it_failed(failed, refused(rules, log, **entry), 3400, turns)
    assert found is not None
    assert found[0] is False
    # A job that failed before any request.
    assert rules.how_it_failed(failed, [], 3400, 7) == (
        False,
        "the fake got no request; error 'the token budget does not cover the next model call'",
    )


# A job against the uninterrupted one


def test_a_job_with_other_issue_numbers_sends_what_the_uninterrupted_job_sent(
    rules: ModuleType,
) -> None:
    reference = Log()
    job = Log(OTHER, first_issue=41)
    assert job.entries != reference.entries

    found = rules.compared(reference.seen(rules), job.seen(rules))
    assert found.same
    # Against itself too.
    assert rules.compared(reference.seen(rules), reference.seen(rules)).same


def test_a_request_sent_again_is_compared_too(rules: ModuleType) -> None:
    reference = Log()
    job = Log(OTHER, first_issue=41)
    job.entries.insert(6, copy.deepcopy(job.at(rules, (1, 4))))
    assert rules.compared(reference.seen(rules), job.seen(rules)).same

    # The copy differs in one block: found, whether it came after the original or before.
    for position in (6, 5):
        job = Log(OTHER, first_issue=41)
        again = copy.deepcopy(job.at(rules, (1, 4)))
        again["body"]["messages"][-1]["content"][0]["content"] = "read!"
        job.entries.insert(position, again)
        found = rules.compared(reference.seen(rules), job.seen(rules))
        assert found.differs == [(1, 4)]
        assert (found.lacking, found.missing, found.out_of_order) == ([], [], [])


@pytest.mark.parametrize("part", ["system", "tools", "tool_choice", "messages"])
def test_a_request_that_differs_in_one_part_is_found_at_its_key(
    rules: ModuleType, part: str
) -> None:
    reference = Log()
    job = Log(OTHER, first_issue=41)
    body = job.at(rules, (1, 4))["body"]
    if part == "messages":
        body["messages"][-1]["content"][0]["content"] = "read!"
    else:
        body[part] = "another" if part == "system" else [{"name": "read_file"}]
    if part == "tool_choice":
        # With a `tool_choice` it is a summary request, where the other job went on.
        body["tool_choice"] = dict(NONE)

    found = rules.compared(reference.seen(rules), job.seen(rules))
    assert found.differs == [(1, 4)]
    assert (found.lacking, found.out_of_order, found.numbers) == ([], [], [])
    # The other job's request at that key is then missing, if it is another kind.
    assert found.missing == ([(1, 4)] if part == "tool_choice" else [])
    assert not found.same


def test_a_request_at_a_key_the_uninterrupted_job_lacks_is_found(rules: ModuleType) -> None:
    reference = Log()
    job = Log(OTHER, first_issue=41)
    # The whole history sent in the second window, with no marker: it reads
    # as window 0, turn 4, which the first window never reached.
    history = copy.deepcopy(job.at(rules, (0, 3), summary=True)["body"]["messages"])
    del history[-1]["content"][-1]
    history += copy.deepcopy(job.at(rules, (1, 4))["body"]["messages"][1:])
    late = copy.deepcopy(job.at(rules, (1, 3)))
    late["body"]["messages"] = history
    job.entries.insert(5, late)

    found = rules.compared(reference.seen(rules), job.seen(rules))
    assert found.lacking == [(0, 4)]
    assert found.out_of_order == [(0, 4)]
    assert (found.differs, found.missing, found.numbers) == ([], [], [])
    assert not found.same


def test_a_request_of_the_uninterrupted_job_that_was_never_sent_is_found(rules: ModuleType) -> None:
    reference = Log()
    job = Log(OTHER, first_issue=41)
    job.entries = job.entries[:-1]

    found = rules.compared(reference.seen(rules), job.seen(rules))
    assert found.missing == [(2, 6)]
    assert (found.differs, found.lacking, found.out_of_order, found.numbers) == ([], [], [], [])


def test_a_key_that_goes_back_is_found(rules: ModuleType) -> None:
    reference = Log()
    job = Log(OTHER, first_issue=41)
    job.entries[1], job.entries[2] = job.entries[2], job.entries[1]

    found = rules.compared(reference.seen(rules), job.seen(rules))
    assert found.out_of_order == [(0, 1)]
    assert (found.differs, found.lacking, found.missing, found.numbers) == ([], [], [], [])


def test_only_the_number_of_a_new_issue_is_masked_and_it_is_the_trackers(rules: ModuleType) -> None:
    reference = Log()
    job = Log(OTHER, first_issue=41)
    operation = f"{OTHER}:toolu_01"

    # The request holds a number the tracker does not list for that call.
    job.tracker[0]["id"] = 40
    found = rules.compared(reference.seen(rules), job.seen(rules))
    assert found.numbers == [f"{operation}: 41 in the request, the tracker lists [40]"]
    assert (found.differs, found.lacking, found.missing, found.out_of_order) == ([], [], [], [])

    # The tracker lists no issue for the call at all.
    del job.tracker[0]
    found = rules.compared(reference.seen(rules), job.seen(rules))
    assert found.numbers == [f"{operation}: 41 in the request, the tracker lists ()"]

    # The request holds the first of two numbers the tracker lists, not the last.
    twice = Log(OTHER, first_issue=41)
    twice.tracker.append({"id": 99, "operation_id": operation})
    found = rules.compared(reference.seen(rules), twice.seen(rules))
    assert found.numbers == [f"{operation}: 41 in the request, the tracker lists [41, 99]"]

    # After an approved rerun the request holds the last number the tracker lists.
    rerun = Log(OTHER, first_issue=41)
    rerun.tracker.insert(0, {"id": 39, "operation_id": operation})
    assert rules.compared(reference.seen(rules), rerun.seen(rules)).same


def test_a_number_outside_a_new_issues_result_is_compared_as_it_is(rules: ModuleType) -> None:
    reference = Log()
    for change in ("title", "another tool"):
        job = Log(OTHER, first_issue=41)
        for entry in job.entries:
            for message in entry["body"]["messages"]:
                for block in message["content"] if isinstance(message["content"], list) else []:
                    if block.get("type") != "tool_result":
                        continue
                    if change == "title" and block["tool_use_id"] == "toolu_01":
                        # The text after the number is not masked.
                        block["content"] = block["content"].replace("part 1", "part 9")
                    if change == "another tool" and block["tool_use_id"] == "toolu_00":
                        # A result of `read_file` that only looks like a new issue.
                        block["content"] = "created issue 41: part 0"
        found = rules.compared(reference.seen(rules), job.seen(rules))
        assert found.differs
        assert found.numbers == []


# The message of a failed test

NAMED = (
    "the job's last request was refused for the context window on a scripted turn",
    "the job does not have exactly three summary steps",
    "a window does not open with the summary text and the continuation line",
    "a worker with another trigger sent other requests than the first worker would have",
    "the window opened at the trigger and the job was not failed for it",
)


def kept(lines: list[str]) -> str:
    """What pytest keeps of the message of `assert not failures, "\\n".join(lines)`."""
    return shorten_message(message_of(lines))


def message_of(lines: list[str]) -> str:
    """The message as pytest writes it: the exception's name, and two spaces before a later line."""
    return "AssertionError: " + "\n  ".join(lines) + "\nassert not ['...']"


def test_the_lines_a_switched_off_run_names_come_first_and_are_all_kept(rules: ModuleType) -> None:
    # Five lines of 150 characters are 778 with the exception's name, the
    # line breaks and the two spaces before each later line, and pytest
    # keeps 500. So with five named lines each is cut to 93; with one to
    # three, as every test of the suite has, to 150.
    others = [f"another clause: job {n}, key (1, {n}), 3 of 4" for n in range(100)]
    named = [f"{text}: 4 steps, expected 3; job 0a0a, key (0, 12)" + " x" * 80 for text in NAMED]
    # The named lines last, and each too long to fit five times.
    lines = [*others, *named]
    assert not any(text in kept(lines) for text in NAMED)

    ordered = rules.ordered(lines, NAMED)
    message = kept(ordered)
    assert len("AssertionError: " + "\n  ".join(ordered[:5])) <= 500
    # The five named texts sit inside the first 500 characters of the message.
    assert all(text in message_of(ordered)[:500] for text in NAMED)
    assert all(text in message for text in NAMED)
    assert [line.startswith(text) for line, text in zip(ordered[:5], NAMED, strict=True)] == [
        True
    ] * 5
    # Nothing else is lost or moved.
    assert ordered[5:] == others
    assert len(ordered) == len(lines)


def test_one_or_two_named_lines_keep_150_characters_and_only_the_first_of_each_moves(
    rules: ModuleType,
) -> None:
    first = f"{NAMED[2]}: after the summary at (0, 12): the opening message differs" + " y" * 60
    second = f"{NAMED[2]}: after the summary at (1, 23): the opening message differs"
    lines = ["another clause: 1", first, "another clause: 2", second]

    ordered = rules.ordered(lines, NAMED)
    assert ordered == [first[:150], "another clause: 1", "another clause: 2", second]
    assert len(first) > 150
    # With no named line the lines stay as they are.
    plain = ["another clause: 1", "another clause: 2"]
    assert rules.ordered(plain, NAMED) == plain
    assert rules.ordered([], NAMED) == []


# Stops and takeovers


def test_a_worker_stopped_right_after_the_first_summary_is_told_from_a_later_stop(
    rules: ModuleType,
) -> None:
    log = Log()
    summary = log.first("summary")
    log.taken_over_after(summary["seq"])
    assert rules.stopped_after_first_summary(log.steps) is None
    assert rules.carried_on({"takeovers": 1}, log.steps, summary=True) is None

    # Stopped a step later: the first worker wrote the opening request too.
    log.taken_over_after(summary["seq"] + 1)
    found = rules.stopped_after_first_summary(log.steps)
    assert (
        found
        == "the first worker's last step is model_request at seq 12, the first summary is at seq 11"
    )
    # Stopped after the second summary: a summary, and not the first.
    second = [step for step in log.steps if step["kind"] == "summary"][1]
    log.taken_over_after(second["seq"])
    assert rules.stopped_after_first_summary(log.steps) == (
        f"the first worker's last step is summary at seq {second['seq']},"
        " the first summary is at seq 11"
    )
    # Never stopped: one worker wrote everything.
    log.taken_over_after(len(log.steps))
    assert rules.stopped_after_first_summary(log.steps) is not None
    assert rules.stopped_after_first_summary([]) is not None


def test_a_worker_stopped_at_a_summary_request_left_it_for_the_next_worker_to_send(
    rules: ModuleType,
) -> None:
    log = Log()
    request = log.first("model_request", summary=True)
    # The first worker wrote the step and stopped. The next wrote it again and sent it.
    log.steps.insert(log.steps.index(request) + 1, copy.deepcopy(request))
    for seq, step in enumerate(log.steps, start=1):
        step["seq"] = seq
    log.taken_over_after(request["seq"])
    assert rules.stopped_at_summary_request(log.steps, log.entries) is None
    assert rules.unsent_over(rules.matched(log.entries, log.steps), 1, exactly=True) is None

    # Stopped at an ordinary request instead.
    earlier = Log()
    earlier.taken_over_after(earlier.first("model_request", messages=3)["seq"])
    assert rules.stopped_at_summary_request(earlier.steps, earlier.entries) == (
        "the first worker's last step is model_request at seq 4, not a summary request"
    )
    # The provider got no request after the stop.
    assert rules.stopped_at_summary_request(log.steps, log.entries[:3]) == (
        "the provider got 3 requests, and none after the stop"
    )
    # The next request the provider got after the stop is not the summary request.
    others = [entry for entry in log.entries if not rules.is_summary(entry)]
    assert rules.stopped_at_summary_request(log.steps, others) == (
        "the provider's request 4, the first after the stop, asks for no summary"
    )
    # A first worker that did send it looks the same here. The count of
    # steps that were never sent tells the two apart.
    sent = Log()
    sent.taken_over_after(sent.first("model_request", summary=True)["seq"])
    assert rules.stopped_at_summary_request(sent.steps, sent.entries) is None
    found = rules.unsent_over(rules.matched(sent.entries, sent.steps), 1, exactly=True)
    assert found == "0 steps, exactly 1 expected"


def test_a_job_is_carried_on_when_a_later_worker_sent_a_request(rules: ModuleType) -> None:
    log = Log()
    log.taken_over_after(log.first("summary")["seq"])
    assert rules.carried_on({"takeovers": 1}, log.steps, summary=True) is None
    assert rules.carried_on({"takeovers": 1}, log.steps, summary=False) is None
    assert rules.carried_on({"takeovers": 0}, log.steps, summary=True) is not None

    # The later worker only wrote the job's last answer: no request, no summary.
    log.taken_over_after(len(log.steps) - 1)
    assert rules.carried_on({"takeovers": 1}, log.steps, summary=False) == (
        "1 takeovers; steps of a later worker: 1, of the kinds needed [] of ['model_request']"
    )
    # A request of the later worker, and no summary where one is needed.
    log.taken_over_after(len(log.steps) - 2)
    assert rules.carried_on({"takeovers": 1}, log.steps, summary=False) is None
    assert rules.carried_on({"takeovers": 1}, log.steps, summary=True) is not None


# Kills at planned requests


def test_the_plan_of_the_suites_seed_meets_its_three_rules(rules: ModuleType) -> None:
    plan = rules.plan(293)
    assert plan == [
        [(1, 9), (3, 1)],
        [(1, 2), (3, 1), (3, 3)],
        [(3, 1)],
        [(0, 4), (0, 9), (3, 2)],
        [(1, 7), (2, 6), (3, 1)],
    ]
    assert rules.plan(293) == plan
    assert rules.plan_problems(plan) == []
    # No kill is planned in a window's last three requests: the windows hold 13, 12, 12 and 10.
    assert all(m <= (10, 9, 9, 7)[k] for kills in plan for k, m in kills)
    assert all(1 <= len(kills) <= 3 for kills in plan)


def test_a_plan_that_shows_too_little_or_cannot_land_is_refused(rules: ModuleType) -> None:
    good = rules.plan(293)

    # Most seeds give a job with no kill right after a summary.
    assert rules.plan_problems(rules.plan(1)) != []
    no_boundary = [*good[:2], [(0, 1)], *good[3:]]
    assert rules.plan_problems(no_boundary) == [
        "job 2: no kill in the first two requests after a summary"
    ]
    only_boundaries = [[(1, 1)], [(2, 2)], [(3, 1)], [(1, 2)], [(0, 9), (2, 1)]]
    assert rules.plan_problems(only_boundaries) == [
        "no job has a kill at the fourth request or later after a summary"
    ]
    backwards = [[(3, 1), (1, 9)], *good[1:]]
    assert rules.plan_problems(backwards) == [
        "job 0: [(3, 1), (1, 9)] is not one to three pairs in rising order"
    ]
    twice = [[(1, 2), (1, 2)], *good[1:]]
    assert rules.plan_problems(twice)[0].startswith("job 0: [(1, 2), (1, 2)] is not")
    # The third request after a summary is neither right after it nor late in the window.
    third = [[(1, 3)], *good[1:]]
    assert rules.plan_problems(third) == [
        "job 0: no kill in the first two requests after a summary"
    ]
    early = [[(1, 1), (2, 3)], [(2, 2)], [(3, 1), (3, 3)], [(1, 2)], [(0, 9), (2, 1)]]
    assert rules.plan_problems(early) == [
        "no job has a kill at the fourth request or later after a summary"
    ]
    assert rules.plan_problems([[(1, 1), (2, 4)]] * 5) == []
    # No kill, and four.
    assert rules.plan_problems([[], *good[1:]])[0] == (
        "job 0: [] is not one to three pairs in rising order"
    )
    four = [[(0, 1), (1, 1), (2, 1), (3, 1)], *good[1:]]
    assert rules.plan_problems(four) == [
        "job 0: [(0, 1), (1, 1), (2, 1), (3, 1)] is not one to three pairs in rising order"
    ]
    # The summary request of the first window is its thirteenth request.
    too_late = [[(0, 11), (1, 1)], *good[1:]]
    assert rules.plan_problems(too_late) == ["job 0: (0, 11) is outside the windows"]


def test_a_planned_request_is_counted_from_its_summary_with_resends(rules: ModuleType) -> None:
    log = Log()
    assert rules.planned_seq(log.steps, 0, 1) == 1
    assert rules.planned_seq(log.steps, 0, 2) == 4
    # The fourth request step of the first window is its summary request.
    assert rules.planned_seq(log.steps, 0, 4) == log.first("model_request", summary=True)["seq"]
    assert rules.planned_seq(log.steps, 0, 5) is None
    # After the first summary: the opening request, then the next.
    assert rules.planned_seq(log.steps, 1, 1) == log.first("summary")["seq"] + 1
    assert rules.planned_seq(log.steps, 2, 3) is None
    assert rules.planned_seq(log.steps[:5], 1, 1) is None
    # A request sent again is a step of its own, so the one after it moves on.
    step = log.first("model_request", messages=3)
    log.steps.insert(log.steps.index(step), copy.deepcopy(step))
    for seq, moved in enumerate(log.steps, start=1):
        moved["seq"] = seq
    assert (rules.planned_seq(log.steps, 0, 2), rules.planned_seq(log.steps, 0, 3)) == (4, 5)


def test_a_kill_reached_its_request_when_the_killed_worker_wrote_nothing_after_it(
    rules: ModuleType,
) -> None:
    log = Log()
    target = log.first("model_request", messages=3)["seq"]
    log.taken_over_after(target)
    assert rules.kill_reached(log.steps, target) is True

    # The worker got its answer and wrote a step more: the kill came late.
    log.taken_over_after(target + 1)
    assert rules.kill_reached(log.steps, target) is False
    # A kill of the second worker, at a request it wrote: the third wrote the rest.
    later = log.first("model_request", summary=True)["seq"]
    for step in log.steps:
        step["lease_epoch"] = 1 if step["seq"] <= target else 2 if step["seq"] <= later else 3
    assert (rules.kill_reached(log.steps, target), rules.kill_reached(log.steps, later)) == (
        True,
        True,
    )
    assert rules.kill_reached(log.steps, later - 3) is False
    # Not a request step, and a step that is not there.
    assert rules.kill_reached(log.steps, target + 1) is False
    assert rules.kill_reached(log.steps, 999) is False


# A bad summary answer


def bad_summary(rules: ModuleType) -> Any:
    """A job whose first summary answer could not be used: stored with its problem, then failed."""
    log = Log()
    summary = log.first("summary")
    summary["payload"]["problem"] = "it calls a tool"
    log.steps = log.steps[: summary["seq"]]
    log.step("error", message="the summary is not usable: it calls a tool")
    log.entries = log.entries[:4]
    log.tracker = log.tracker[:1]
    return log


def test_a_bad_summary_fails_the_job_at_once_and_runs_nothing(rules: ModuleType) -> None:
    log = bad_summary(rules)
    job = log.seen(rules, status="failed", error="the summary is not usable: it calls a tool")
    assert rules.bad_summary_problems(job, "toolu_summary_01") == []


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        ("no problem", "1 summary steps, with a problem: [False]"),
        ("succeeded", "status succeeded"),
        ("asked again", "2 summary requests at the provider"),
        ("a tool ran", "steps after the summary: ['tool_intent', 'error']"),
        ("a result stored", "steps after the summary: ['tool_result', 'error']"),
        ("two summaries", "2 summary steps, with a problem: [True, True]"),
        ("an issue", f"the tracker holds [9] for {JOB}:toolu_summary_01"),
    ],
)
def test_a_bad_summary_that_did_not_end_the_job_cleanly_is_found(
    rules: ModuleType, change: str, problem: str
) -> None:
    log = bad_summary(rules)
    view: dict[str, Any] = {
        "status": "failed",
        "error": "the summary is not usable: it calls a tool",
    }
    if change == "no problem":
        del log.first("summary")["payload"]["problem"]
    elif change == "succeeded":
        view = {"status": "succeeded", "error": None}
    elif change == "asked again":
        log.entries.append(copy.deepcopy(log.entries[-1]))
    elif change == "a tool ran":
        log.steps.insert(-1, {"seq": 11.5, "kind": "tool_intent", "lease_epoch": 1, "payload": {}})
    elif change == "a result stored":
        log.steps.insert(-1, {"seq": 11.5, "kind": "tool_result", "lease_epoch": 1, "payload": {}})
    elif change == "two summaries":
        log.steps.insert(4, copy.deepcopy(log.first("summary")))
    elif change == "an issue":
        log.tracker.append({"id": 9, "operation_id": f"{JOB}:toolu_summary_01"})

    assert rules.bad_summary_problems(log.seen(rules, **view), "toolu_summary_01") == [problem]


# Other issue numbers


def test_requests_of_the_same_size_at_every_key_and_numbers_from_the_new_first(
    rules: ModuleType,
) -> None:
    reference, job = Log(), Log(OTHER, first_issue=200_000)
    assert rules.estimate_differences(reference.entries, job.entries) == []
    assert rules.numbers_below(rules.issues_of(OTHER, job.tracker), 200_000) == []

    job.at(rules, (1, 4))["input_estimate"] = 51
    assert rules.estimate_differences(reference.entries, job.entries) == [(1, 4)]
    # A refused request has no size to compare.
    job.at(rules, (1, 4))["status"] = 400
    assert rules.estimate_differences(reference.entries, job.entries) == []
    # The old tracker was still in use: its numbers are under the new first one.
    old = rules.issues_of(JOB, reference.tracker)
    assert rules.numbers_below(old, 200_000) == [7, 8]


# No trigger, and a window that opens at the trigger


def test_a_job_that_must_not_summarise_is_found_when_it_did(rules: ModuleType) -> None:
    log = Log()
    assert rules.summarised(log.seen(rules)) == "2 summary steps, 2 summary requests"
    steps_only = Log()
    steps_only.entries = [e for e in steps_only.entries if not rules.is_summary(e)]
    assert rules.summarised(steps_only.seen(rules)) == "2 summary steps, 0 summary requests"
    log.steps = [step for step in log.steps if step["kind"] != "summary"]
    assert rules.summarised(log.seen(rules)) == "0 summary steps, 2 summary requests"
    log.entries = [entry for entry in log.entries if not rules.is_summary(entry)]
    assert rules.summarised(log.seen(rules)) is None


def test_the_tokens_of_a_response_are_its_input_and_output(rules: ModuleType) -> None:
    usage = {"input_tokens": 2900, "output_tokens": 99}
    assert rules.response_tokens({"payload": {"usage": usage}}) == 2999
    assert rules.response_tokens({"payload": {}}) == 0


def oversize(rules: ModuleType, size: int = 293) -> Any:
    """A job cut after the first request of its second window, which the provider counted."""
    log = Log()
    log.entries = log.entries[:5]
    for entry in log.entries:
        entry["usage"] = {"input_tokens": 120, "output_tokens": 40}
    log.entries[4]["usage"]["input_tokens"] = size
    log.steps = log.steps[: log.first("summary")["seq"] + 2]
    return log


def test_the_size_of_a_windows_first_request_is_read_from_the_providers_count(
    rules: ModuleType,
) -> None:
    log = oversize(rules)
    assert rules.opening_input(log.entries) == 293
    # Not before the request after the summary request is answered.
    assert rules.opening_input(log.entries[:4]) is None
    del log.entries[4]["usage"]
    assert rules.opening_input(log.entries) is None
    assert rules.opening_input([]) is None


def test_a_job_whose_window_opens_at_the_trigger_is_failed_for_exactly_that(
    rules: ModuleType,
) -> None:
    log = oversize(rules)
    error = (
        "the summary leaves the next window at or over the trigger: 293 input tokens, trigger 293"
    )
    assert rules.oversize_error(293, 293) == error
    assert rules.oversize_problems(log.seen(rules, status="failed", error=error), 293, 293) == []

    # It went on and summarised again, to its end.
    whole = Log()
    assert rules.oversize_problems(whole.seen(rules), 293, 293) == [
        "status succeeded, error 'None'",
        "2 summary steps, 2 summary requests",
    ]
    # A second summary step alone, and a second summary request alone.
    stored = oversize(rules)
    stored.steps.append(copy.deepcopy(stored.first("summary")))
    failed = {"status": "failed", "error": error}
    assert rules.oversize_problems(stored.seen(rules, **failed), 293, 293) == [
        "2 summary steps, 1 summary requests"
    ]
    asked = oversize(rules)
    asked.entries.append(copy.deepcopy(asked.at(rules, (0, 3), summary=True)))
    assert rules.oversize_problems(asked.seen(rules, **failed), 293, 293) == [
        "1 summary steps, 2 summary requests"
    ]
    other = error.replace("trigger 293", "trigger 1")
    assert rules.oversize_problems(log.seen(rules, status="failed", error=other), 293, 293) == [
        f"status failed, error {other!r}"
    ]


def test_the_differences_of_a_job_are_counted_by_kind(rules: ModuleType) -> None:
    reference, job = Log(), Log(OTHER, first_issue=41)
    assert rules.compared(reference.seen(rules), job.seen(rules)).summary() == (
        "0 differ, 0 lacking, 0 missing, 0 out of order, 0 wrong numbers"
    )
    job.entries = job.entries[:-1]
    job.at(rules, (1, 4))["body"]["system"] = "another"
    assert rules.compared(reference.seen(rules), job.seen(rules)).summary() == (
        "1 differ, 0 lacking, 1 missing, 0 out of order, 0 wrong numbers, the first (1, 4)"
    )


# The lines of a test: each check under its own fixed text


@pytest.fixture
def suite(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """The suite's test module, imported as pytest imports it. It starts no stack."""
    monkeypatch.syspath_prepend(str(TESTS))
    return importlib.import_module("long.test_long_job")


def job_of(suite: ModuleType, job_id: str = OTHER, first_issue: int = 41) -> Log:
    """A made-up job with the suite's own texts and three summaries."""
    return Log(
        job_id,
        first_issue,
        windows=FOUR_WINDOWS,
        summary=suite.SUMMARY_TEXT,
        continuation=suite.CONTINUATION,
        instruction=suite.INSTRUCTION,
    )


def texts(suite: ModuleType, lines: list[str]) -> list[str]:
    """The name of the fixed text each line starts with."""
    names = {
        value: name
        for name, value in vars(suite).items()
        if name.isupper() and isinstance(value, str)
    }
    return [
        next((name for value, name in names.items() if line.startswith(f"{value}: ")), line)
        for line in lines
    ]


def test_a_job_that_holds_every_clause_gets_no_line(suite: ModuleType) -> None:
    reference = job_of(suite, JOB, 7).seen(suite.rules)
    job = job_of(suite).seen(suite.rules)

    assert suite.clauses(job, reference, unsent=0) == []
    assert suite.clauses(reference, reference, unsent=0) == []
    assert suite.uninterrupted(reference, 3400, 9) == []
    # The texts a switched-off run names, word for word, each under its own name.
    assert NAMED[:3] == (suite.WINDOW_REFUSAL, suite.SUMMARY_STEPS, suite.OPENING)
    assert suite.MORE_SUMMARIES == (
        "the job stored more summary steps than the three its windows need"
    )
    assert suite.LACKING == "a request at a key the uninterrupted run lacks"


@pytest.mark.parametrize(
    ("change", "lines"),
    [
        ("failed", ["NOT_SUCCEEDED"]),
        ("running", ["NOT_FINAL", "NOT_SUCCEEDED"]),
        ("a fourth summary", ["MORE_SUMMARIES"]),
        ("a summary step missing", ["SUMMARY_STEPS"]),
        ("a summary refused", ["SUMMARY_WINDOWS", "OPENING"]),
        ("a summary with another instruction", ["SUMMARY_REQUEST", "DIFFERS"]),
        ("the whole history", ["LACKING", "ORDER", "NO_STEP"]),
        ("the last request missing", ["MISSING", "UNSENT"]),
        ("the opening cut", ["DIFFERS", "OPENING"]),
        ("another number", ["NUMBERS"]),
        ("an issue twice", ["ISSUES"]),
        ("a question", ["QUESTION", "ISSUES"]),
        ("a full log", ["LOG_FULL"]),
        ("other tool names", ["TOOL_NAMES"]),
    ],
)
def test_each_broken_clause_gets_a_line_under_its_own_text(
    suite: ModuleType, change: str, lines: list[str]
) -> None:
    rules = suite.rules
    reference = job_of(suite, JOB, 7).seen(rules)
    log = job_of(suite)
    view: dict[str, Any] = {}
    operation = f"{OTHER}:toolu_01"
    if change in ("failed", "running"):
        view = {"status": change, "error": "an error" if change == "failed" else None}
    elif change == "a fourth summary":
        log.step("summary", id="msg_summary_09", content=[])
    elif change == "a summary step missing":
        log.steps.remove([step for step in log.steps if step["kind"] == "summary"][-1])
    elif change == "a summary refused":
        log.at(rules, (1, 5), summary=True)["status"] = 400
    elif change == "a summary with another instruction":
        asked = log.at(rules, (0, 3), summary=True)["body"]["messages"][-1]
        asked["content"][-1]["text"] = "Write a poem."
    elif change == "the whole history":
        history = copy.deepcopy(log.at(rules, (0, 3), summary=True)["body"]["messages"])
        del history[-1]["content"][-1]
        history += copy.deepcopy(log.at(rules, (1, 4))["body"]["messages"][1:])
        late = copy.deepcopy(log.at(rules, (1, 3)))
        late["body"]["messages"] = history
        log.entries.insert(5, late)
    elif change == "the last request missing":
        log.entries = log.entries[:-1]
    elif change == "the opening cut":
        block = log.at(rules, (1, 3))["body"]["messages"][0]["content"][0]
        cut, marker = suite.SUMMARY_TEXT[:40], f"[summary 1] [turn 3]\n\n{suite.CONTINUATION}"
        block["text"] = f"{cut}\n\n{marker}"
    elif change == "another number":
        log.tracker[0]["id"] = 40
    elif change == "an issue twice":
        log.tracker.insert(0, {"id": 39, "operation_id": operation})
    elif change == "a question":
        view = {"questions": [{"question": 9, "operation_id": operation, "reason": "interrupted"}]}
    elif change == "a full log":
        view = {"log_length": 10_000}
    elif change == "other tool names":
        log.steps[3]["payload"]["tools"] = ["read_file"]

    found = suite.clauses(log.seen(rules, **view), reference, unsent=0)
    assert texts(suite, found) == lines


def test_a_request_step_that_was_never_sent_is_allowed_after_a_stop(suite: ModuleType) -> None:
    reference = job_of(suite, JOB, 7).seen(suite.rules)
    log = job_of(suite)
    step = next(s for s in log.steps if s["payload"].get("messages") == 3)
    log.steps.insert(log.steps.index(step), copy.deepcopy(step))

    assert suite.clauses(log.seen(suite.rules), reference, unsent=1) == []
    assert texts(suite, suite.clauses(log.seen(suite.rules), reference, unsent=0)) == ["UNSENT"]


def test_the_uninterrupted_job_has_three_clauses_of_its_own(suite: ModuleType) -> None:
    rules = suite.rules
    log = job_of(suite, JOB, 7)
    failed: dict[str, Any] = {
        "status": "failed",
        "error": "model call failed: provider answered 400",
    }

    cut = job_of(suite, JOB, 7)
    cut.entries = refused(rules, cut)
    at_the_window = suite.uninterrupted(cut.seen(rules, **failed), 3400, 9)
    assert texts(suite, at_the_window) == ["WINDOW_REFUSAL"]
    assert at_the_window[0].startswith(NAMED[0] + ": last request: status 400, estimate 3526")
    # Past the end of a script of four turns: another failure.
    assert texts(suite, suite.uninterrupted(cut.seen(rules, **failed), 3400, 4)) == [
        "OTHER_FAILURE"
    ]
    assert texts(suite, suite.uninterrupted(log.seen(rules, takeovers=1), 3400, 9)) == [
        "TAKEN_OVER"
    ]
    log.entries.append(copy.deepcopy(log.entries[-1]))
    assert texts(suite, suite.uninterrupted(log.seen(rules), 3400, 9)) == ["REPEATED"]


# The end of a run: the clock and the record


def test_a_run_during_which_the_clock_was_set_is_void_and_says_so(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.syspath_prepend(str(TESTS))
    harness = importlib.import_module("long.conftest")
    stack = harness.Stack(tmp_path)

    def run(name: str, wall_back: float = 0.0, **more: Any) -> Any:
        return harness.Running(
            name, datetime.now(UTC), time.time() - wall_back, time.monotonic(), **more
        )

    steady: list[str] = []
    stack.close(run("steady"), steady)
    failed = ["a clause: 1"]
    stack.close(run("failed"), failed)
    # The wall clock ran 5 s ahead of the monotonic clock since the run began.
    moved: list[str] = []
    stack.close(run("moved", 5.0), moved)
    # Or it was set back 3 s while the job ran, before the run's own measurement.
    earlier: list[str] = ["a clause: 1"]
    stack.close(run("earlier", drift_before=-3.0), earlier)
    # Under the limit of a second: not void.
    small: list[str] = []
    stack.close(run("small", 0.5), small)
    stack.hang_up()

    assert (steady, failed, small) == ([], ["a clause: 1"], [])
    assert moved == [
        "void run: the wall clock moved +5.0 s against the monotonic clock during this run."
        " Leases run on the wall clock."
    ]
    assert earlier[1].startswith("void run: the wall clock moved -3.0 s")
    records = [json.loads(line) for line in (tmp_path / "results.jsonl").read_text().splitlines()]
    assert [(record["test"], record["verdict"]) for record in records] == [
        ("steady", "passed"),
        ("failed", "failed"),
        ("moved", "void"),
        ("earlier", "void"),
        ("small", "passed"),
    ]
    assert records[2]["void"] == "the wall clock moved +5.0 s against the monotonic clock"
    assert "void" not in records[0]
    assert {record["provider"] for record in records} == {"fake provider"}
    assert {record["workers"] for record in records} == {2}


def test_the_lines_of_one_job_among_several_carry_its_name(suite: ModuleType) -> None:
    lines = [f"{suite.LACKING}: 1 keys, the first (0, 15)", f"{suite.NOT_SUCCEEDED}: status failed"]
    assert suite.tagged(lines, "job 3") == [
        f"{suite.LACKING}: job 3: 1 keys, the first (0, 15)",
        f"{suite.NOT_SUCCEEDED}: job 3: status failed",
    ]
    # The line still starts with its fixed text, and the named ones come first.
    assert suite.rules.ordered(suite.tagged(lines[::-1], "job 3"), suite.NAMED)[0].startswith(
        suite.LACKING
    )


def test_the_suite_names_the_seven_texts_of_the_switched_off_runs(suite: ModuleType) -> None:
    assert suite.NAMED == (
        "the job's last request was refused for the context window on a scripted turn",
        "the job stored more summary steps than the three its windows need",
        "the job does not have exactly three summary steps",
        "a request at a key the uninterrupted run lacks",
        "a window does not open with the summary text and the continuation line",
        "a worker with another trigger sent other requests than the first worker would have",
        "the window opened at the trigger and the job was not failed for it",
    )
    assert suite.rules.plan_problems(suite.rules.plan(suite.SEED)) == []
    # A missed kill says the word and the seed.
    assert suite.INCONCLUSIVE.startswith("INCONCLUSIVE")


def test_every_text_a_switched_off_run_of_the_long_stack_names_is_a_named_text(
    suite: ModuleType,
) -> None:
    """Only a named text is moved to the top of a failure's message, where the run is judged."""
    spec = importlib.util.spec_from_file_location(
        "switched_off", TESTS.parent / "scripts/switched_off.py"
    )
    assert spec is not None and spec.loader is not None
    switched_off = importlib.util.module_from_spec(spec)
    sys.modules["switched_off"] = switched_off
    spec.loader.exec_module(switched_off)
    named = [
        expect.fails_with
        for entry in switched_off.ENTRIES.values()
        if entry.stack == "long"
        for expect in entry.expect
        if expect.fails_with is not None
    ]
    assert len(named) == 6
    assert set(named) <= set(suite.NAMED)
    # The text of a job with too many summaries is the one named for the stop after a summary.
    assert suite.MORE_SUMMARIES in named
    assert suite.SUMMARY_STEPS not in named


def test_the_scripted_calls_are_those_of_the_uninterrupted_job_not_the_jobs_own(
    suite: ModuleType,
) -> None:
    reference = job_of(suite, JOB, 7).seen(suite.rules)
    log = job_of(suite)
    # In the job's own log the first issue's call reads a file instead.
    answer = log.first("model_response", id="msg_01")
    answer["payload"]["content"][1]["name"] = "read_file"
    job = log.seen(suite.rules)

    assert suite.rules.issue_problems(job.id, job.steps, job.issues, {}) == [
        f"{OTHER}:toolu_01: 1 issues for a call the script does not make"
    ]
    assert suite.clauses(job, reference, unsent=0) == []


def test_a_run_that_expects_exactly_one_unsent_request_fails_with_none(suite: ModuleType) -> None:
    reference = job_of(suite, JOB, 7).seen(suite.rules)
    job = job_of(suite).seen(suite.rules)
    assert suite.clauses(job, reference, unsent=1) == []
    assert texts(suite, suite.clauses(job, reference, unsent=1, exactly=True)) == ["UNSENT"]


def test_the_last_lines_of_a_run_are_its_unfinished_checks_and_the_failpoint_count(
    suite: ModuleType,
) -> None:
    fired = '{"message": "failpoint reached, stopping the worker"}'
    assert suite.closing_lines(True, "", 0) == []
    assert suite.closing_lines(True, fired, 1) == []
    assert texts(suite, suite.closing_lines(False, "", 0)) == ["UNCHECKED"]
    assert texts(suite, suite.closing_lines(True, fired, 0)) == ["FAILPOINT"]
    assert texts(suite, suite.closing_lines(True, "", 1)) == ["FAILPOINT_ONCE"]
    assert texts(suite, suite.closing_lines(True, f"{fired}\n{fired}", 1)) == ["FAILPOINT_ONCE"]
    assert texts(suite, suite.closing_lines(False, fired, 0)) == ["UNCHECKED", "FAILPOINT"]


def test_a_forced_takeover_whose_kill_failed_or_came_late_gets_its_own_lines(
    suite: ModuleType,
) -> None:
    log = job_of(suite)
    seq = log.first("summary")["seq"]
    log.taken_over_after(seq + 1)
    job = log.seen(suite.rules)

    assert suite.stops("job A", job, {"seq": seq, "latency": 0.3}) == []
    # The worker got its answer and wrote on: the kill came late.
    assert texts(suite, suite.stops("job A", job, {"seq": seq - 3, "latency": 0.3})) == [
        "INCONCLUSIVE"
    ]
    failed = {"seq": seq, "problem": "`docker kill` of 0123456789ab failed"}
    assert texts(suite, suite.stops("job A", job, failed)) == ["KILL_FAILED"]
    # The job never came to the step the kill waits for.
    assert texts(suite, suite.stops("job A", job, None)) == ["KILL_FAILED", "INCONCLUSIVE"]
    assert suite.stops("job A", job, None)[1].startswith("INCONCLUSIVE: a planned kill did not")


# What a run's test compares


def test_the_failpoint_lines_of_a_run_are_held_to_the_number_expected(rules: ModuleType) -> None:
    line = '{"message": "failpoint reached, stopping the worker"}'
    assert rules.fired_problem("", 0) is None
    assert rules.fired_problem(line, 1) is None
    assert rules.fired_problem(line, 0) == "1 lines in the workers' logs, expected 0"
    assert rules.fired_problem("", 1) == "0 lines in the workers' logs, expected 1"
    assert rules.fired_problem(f"{line}\n{line}", 1) == "2 lines in the workers' logs, expected 1"


def test_workers_killed_at_a_step_were_stopped_before_the_next_answer(rules: ModuleType) -> None:
    log = Log()
    summary = log.first("summary")["seq"]
    # Killed when the summary was the last step: the worker wrote nothing more.
    log.taken_over_after(summary)
    assert rules.stopped_before_an_answer(log.steps, summary) is True
    # It wrote the next request before the kill landed, and nothing after that.
    log.taken_over_after(summary + 1)
    assert rules.stopped_before_an_answer(log.steps, summary) is True
    assert rules.stopped_before_an_answer(log.steps, summary + 1) is True
    # It got that request's answer: the kill came late.
    log.taken_over_after(summary + 2)
    assert rules.stopped_before_an_answer(log.steps, summary) is False
    assert rules.stopped_before_an_answer(log.steps, summary + 1) is False
    # A step after a step that is no request, and a step that is not there.
    log.taken_over_after(summary)
    assert rules.stopped_before_an_answer(log.steps, summary - 2) is False
    assert rules.stopped_before_an_answer(log.steps, 999) is False


def test_a_kill_counts_as_reached_only_when_it_was_sent_and_came_in_time(
    rules: ModuleType,
) -> None:
    log = Log()
    target = log.first("model_request", messages=3)["seq"]
    log.taken_over_after(target)
    sent = {"job": 0, "k": 0, "m": 2, "seq": target, "latency": 0.3}
    assert rules.reached(log.steps, sent) is True
    # Seen too late and never sent: no latency, whatever the log shows.
    assert rules.reached(log.steps, {"job": 0, "k": 0, "m": 2, "seq": target}) is False
    # A kill that failed has no latency either, and one whose request never came no step.
    assert rules.reached(log.steps, {**sent, "latency": None}) is False
    assert rules.reached(log.steps, {"job": 0, "k": 3, "m": 1, "seq": None}) is False
    # Sent, and the worker had already written on.
    log.taken_over_after(target + 1)
    assert rules.reached(log.steps, sent) is False

    kills = [{**sent, "reached": True}, {"job": 1, "k": 3, "m": 1, "reached": False}]
    assert rules.missed(kills) == [(1, 3, 1)]
    assert rules.missed(kills[:1]) == []


def test_a_job_has_at_least_as_many_takeovers_as_kills_planned(rules: ModuleType) -> None:
    assert rules.too_few_takeovers({"takeovers": 2}, 2) is None
    assert rules.too_few_takeovers({"takeovers": 3}, 2) is None
    assert rules.too_few_takeovers({"takeovers": 1}, 2) == "1 takeovers, 2 kills planned"


def test_the_issue_numbers_show_which_tracker_was_in_use(rules: ModuleType) -> None:
    new = rules.issues_of(OTHER, Log(OTHER, first_issue=200_000).tracker)
    old = rules.issues_of(JOB, Log().tracker)
    assert rules.numbering_problem(new, 200_000) is None
    assert rules.numbering_problem(old, 200_000) == "2 issues, under 200000: [7, 8]"
    # No issue at all shows nothing about the tracker.
    assert rules.numbering_problem({}, 200_000) == "0 issues, under 200000: []"


def test_the_summary_keys_of_a_job_are_the_uninterrupted_jobs_or_one_turn_earlier(
    rules: ModuleType,
) -> None:
    reference, job = Log(), Log(OTHER, first_issue=41)
    assert rules.summary_keys_problem(reference.entries, job.entries) is None
    assert rules.not_moved(reference.entries, job.entries) == (
        "first summary at [(0, 3)], the uninterrupted run [(0, 3)]"
    )
    # A job whose first window is a turn shorter.
    earlier = Log(OTHER, first_issue=41, windows=((0, 2), (2, 5), (5, 7)))
    assert rules.not_moved(reference.entries, earlier.entries) is None
    assert rules.summary_keys_problem(reference.entries, earlier.entries) == (
        "[(0, 2), (1, 5)], the uninterrupted run [(0, 3), (1, 5)]"
    )
    # Two turns earlier is not one, and a job with no summary did not move one.
    two = Log(OTHER, first_issue=41, windows=((0, 1), (1, 5), (5, 7)))
    assert rules.not_moved(reference.entries, two.entries) is not None
    none = [entry for entry in job.entries if not rules.is_summary(entry)]
    assert rules.not_moved(reference.entries, none) is not None
    assert rules.not_moved(none, earlier.entries) is not None


def test_the_trigger_read_back_from_every_new_worker_is_the_one_wanted(rules: ModuleType) -> None:
    assert rules.wrong_triggers(["2800", "2800"], 2800) is None
    assert rules.wrong_triggers(["", ""], None) is None
    assert rules.wrong_triggers(["2800", "3000"], 2800) == (
        "read back ['2800', '3000'], expected '2800'"
    )
    assert rules.wrong_triggers(["3000", "3000"], None) == "read back ['3000', '3000'], expected ''"
    # No worker answered, and the takeover never happened.
    assert rules.wrong_triggers([], 2800) == "read back [], expected '2800'"
    assert rules.wrong_triggers(None, 2800) == "read back None, expected '2800'"


def test_the_job_with_trigger_1_shows_a_windows_size_and_fails_for_it(rules: ModuleType) -> None:
    log = oversize(rules)
    error = "the summary leaves the next window at or over the trigger: 293 input tokens, trigger 1"
    probe = log.seen(rules, status="failed", error=error)
    assert rules.probe_problems(probe, 293, too_long=False) == []

    running = log.seen(rules, status="running", error=None)
    assert rules.probe_problems(running, 293, too_long=True) == [
        "not final 30 s after its size, 293, was read: status running",
        "status running, size 293, not the error for trigger 1",
    ]
    other = log.seen(rules, status="failed", error=error.replace("trigger 1", "trigger 293"))
    assert rules.probe_problems(other, 293, too_long=False) == [
        "status failed, size 293, not the error for trigger 1"
    ]
    # No request after a summary request: counts only, nothing of the job's spec.
    spec = {"prompt": "a prompt that no line may carry"}
    none = log.seen(rules, status="failed", error="model call failed", spec=spec)
    assert rules.probe_problems(none, None, too_long=False) == [
        "no request after a summary request: status failed, 5 requests"
    ]


def test_a_runs_worker_log_is_the_last_saved_file_of_each_container_from_its_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.syspath_prepend(str(TESTS))
    harness = importlib.import_module("long.conftest")
    stack = harness.Stack(tmp_path)
    stack.hang_up()
    (tmp_path / "logs").mkdir()
    started = datetime(2026, 11, 25, 12, 0, 0, tzinfo=UTC)
    before = "2026-11-25T11:59:59.999999999Z before the run"
    first = "2026-11-25T12:00:00.000000001Z at its start"
    later = "2026-11-25T12:00:05.5Z later"
    files = {
        # Saved twice: the later file holds the container's whole log.
        "a-run-02-worker-aaaaaaaaaaaa.log": f"{before}\n{first}",
        "a-run-03-worker-aaaaaaaaaaaa.log": f"{before}\n{first}\n{later}",
        # A container that was made again has a file of its own.
        "a-run-02-worker-bbbbbbbbbbbb.log": "2026-11-25T12:00:01Z of the other container\nno time",
        # Saved in another run: not this run's.
        "session-01-worker-cccccccccccc.log": "2026-11-25T12:00:02Z of another run",
    }
    for name, text in files.items():
        (tmp_path / "logs" / name).write_text(text)
    run = harness.Running("a-run", started, 0.0, 0.0, logs=list(files)[:3])

    assert stack.worker_log(run).splitlines() == [
        first,
        later,
        "2026-11-25T12:00:01Z of the other container",
    ]
    assert harness.stamp(later) == datetime(2026, 11, 25, 12, 0, 5, 500000, tzinfo=UTC)
    assert harness.stamp("no time") is None


def test_the_named_lines_are_in_the_message_a_real_failure_leaves_in_its_junit_file(
    rules: ModuleType, tmp_path: Path
) -> None:
    """As a switched-off run reads it: pytest's own junit file, through the script's reader."""
    others = [f"another clause: job {n}, key (1, {n}), 3 of 4" for n in range(100)]
    named = [f"{text}: 4 steps, expected 3; job 0a0a, key (0, 12)" + " x" * 80 for text in NAMED]
    for name, lines in (
        ("ordered", rules.ordered([*others, *named], NAMED)),
        ("as_they_came", [*others, *named]),
    ):
        (tmp_path / f"{name}.json").write_text(json.dumps(lines))
    (tmp_path / "test_made_up.py").write_text(
        "import json\nfrom pathlib import Path\n\nimport pytest\n\n\n"
        "@pytest.mark.parametrize('name', ['ordered', 'as_they_came'])\n"
        "def test_lines(name):\n"
        "    failures = json.loads((Path(__file__).parent / f'{name}.json').read_text())\n"
        "    assert not failures, '\\n'.join(failures)\n"
    )
    junit = tmp_path / "junit.xml"
    subprocess.run(  # noqa: S603 - the interpreter that runs this test, on a made-up test
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--rootdir", str(tmp_path),
         "-c", "/dev/null", f"--junitxml={junit}", str(tmp_path / "test_made_up.py")],
        capture_output=True, text=True, check=False, timeout=120, cwd=tmp_path,
    )  # fmt: skip
    spec = importlib.util.spec_from_file_location(
        "switched_off", TESTS.parent / "scripts/switched_off.py"
    )
    assert spec is not None and spec.loader is not None
    switched_off = importlib.util.module_from_spec(spec)
    sys.modules["switched_off"] = switched_off
    spec.loader.exec_module(switched_off)
    found = {
        node.rsplit("[", 1)[1]: text for node, (_, text) in switched_off.outcomes(junit).items()
    }

    assert set(found) == {"ordered]", "as_they_came]"}
    assert all(text in found["ordered]"] for text in NAMED)
    assert not any(text in found["as_they_came]"] for text in NAMED)
