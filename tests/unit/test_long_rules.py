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
        assert bad == {**original, "summary": {**original["summary"], "bad": {"1": fault}}}
        assert parse_scenario(json.dumps(bad)).summary.bad == {1: fault}  # type: ignore[union-attr]


def test_the_jobs_files_are_one_short_line_for_each_file_the_scenario_reads(
    rules: ModuleType,
) -> None:
    files = rules.job_files(packaged())
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
    log.step("summary", id="msg_summary_01", content=[])
    assert rules.summary_step_count(log.steps, 2) == "3 summary steps, expected 2"


def test_each_of_the_first_windows_has_an_answered_summary_request(rules: ModuleType) -> None:
    log = Log()
    assert rules.summary_window_count(log.entries, 2) is None
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
    assert rules.unsent_over(rules.matched(Log().entries, Log().steps), 0) is None


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


def test_a_request_sent_again_is_compared_too_and_changes_nothing(rules: ModuleType) -> None:
    reference = Log()
    job = Log(OTHER, first_issue=41)
    job.entries.insert(6, copy.deepcopy(job.at(rules, (1, 4))))
    assert rules.compared(reference.seen(rules), job.seen(rules)).same


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
    "the job does not have exactly N summary steps",
    "a window does not open with the summary text and the continuation line",
    "a worker with another trigger sent other requests than the first worker would have",
    "the window opened at the trigger and the job was not failed for it",
)


def kept(lines: list[str]) -> str:
    """What pytest keeps of the message of `assert not failures, "\\n".join(lines)`."""
    return shorten_message("AssertionError: " + "\n".join(lines) + "\nassert not ['...']")


def test_the_lines_a_switched_off_run_names_come_first_and_are_all_kept(rules: ModuleType) -> None:
    others = [f"another clause: job {n}, key (1, {n}), 3 of 4" for n in range(100)]
    named = [f"{text}: 4 steps, expected 3; job 0a0a, key (0, 12)" + " x" * 80 for text in NAMED]
    # The named lines last, and each too long to fit five times.
    lines = [*others, *named]
    assert not any(text in kept(lines) for text in NAMED)

    ordered = rules.ordered(lines, NAMED)
    message = kept(ordered)
    assert len("AssertionError: " + "\n".join(ordered[:5])) <= 500
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
    assert suite.LACKING == "a request at a key the uninterrupted run lacks"
    assert set(suite.NAMED) == {
        suite.WINDOW_REFUSAL,
        suite.SUMMARY_STEPS,
        suite.OPENING,
        suite.LACKING,
    }


@pytest.mark.parametrize(
    ("change", "lines"),
    [
        ("failed", ["NOT_SUCCEEDED"]),
        ("running", ["NOT_FINAL", "NOT_SUCCEEDED"]),
        ("a fourth summary", ["SUMMARY_STEPS"]),
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
