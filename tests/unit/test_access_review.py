"""The access-review generator on the committed seed, both shapes.

Each check passes on the committed seed and fails, on its own clause, on a
fixture built for it. A fixture is a text edit of the generated job, or a
record put through the package's render functions.
"""

import ast
import dataclasses
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Iterator
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

import access_review
from access_review import (
    FULL,
    GRANT_ID,
    REFERENCE_CODE,
    SHORT,
    SYSTEM,
    TITLE_FORMAT,
    VERSION,
    WHEN_FOUND,
    IdDrawer,
    ParseError,
    Review,
    Shape,
    answer_key,
    checks,
    generate,
    job_digest,
    parse_files,
    problems,
    read_seed,
    render_approval,
    render_leaver,
    spec,
)
from access_review import __main__ as command
from access_review.org import (
    FAMILY,
    GIVEN,
    LOCAL_ROSTER,
    PERIOD_START,
    REVIEW_DATE,
    ROLES,
    ROSTER_DATES,
    SYSTEM_WORDS,
    TEAM_WORDS,
    Leaver,
    Notice,
    _last_day,
)
from access_review.parse import Key
from access_review.render import (
    APPROVAL_TEMPLATES,
    CODE_SENTENCE,
    FILE_BYTES,
    INDEX_PATH,
    LEAVER_TEMPLATES,
    NOTICES_HEAD,
    RULES,
    TASK,
    corpus_path,
    person_text,
    system_text,
    team_text,
)
from access_review.rng import rows
from runtime.spec import MAX_FILES_BYTES, JobSpec
from runtime.tools import default_tools
from runtime.tools.trackers import FakeTracker
from runtime.worker.summary import CONTINUATION, SUMMARY_INSTRUCTION

# This seed is public, so everything generated from it is known, the answer key included.
SEED_FILE = Path(__file__).with_name("access_review_seed.json")
PACKAGE = Path(access_review.__file__).parent
LATE = ("late_leaver", "late_approval")
LOCAL = ("local_notice", "local_roster", "local_approval")


@pytest.fixture(scope="module")
def seed() -> bytes:
    return read_seed(SEED_FILE)


@pytest.fixture(scope="module", params=[FULL, SHORT], ids=["full", "short"])
def review(request: pytest.FixtureRequest, seed: bytes) -> Review:
    started = time.perf_counter()
    built = generate(seed, request.param)
    print(f"generate {request.param.name}: {time.perf_counter() - started:.2f} s")
    return built


# --- helpers that build fixtures ---------------------------------------------


def clauses(lines: list[str]) -> list[str]:
    return [line.split(":")[0] for line in lines]


def only(lines: list[str], clause: str) -> None:
    """The clause is the only one in the lines a check returned."""
    assert clauses(lines) == [clause], lines


def with_file(review: Review, path: str, text: str) -> Review:
    files = dict(review.files)
    files[path] = text
    return dataclasses.replace(review, files=files)


def corpus(review: Review) -> list[str]:
    return [path for path in review.files if path != INDEX_PATH]


def planted(review: Review, kind: str) -> list[access_review.Planted]:
    return [p for p in review.planted if p.kind == kind]


def grant_line(review: Review, grant: str) -> tuple[str, str]:
    """The path and the line of a grant id."""
    for path in corpus(review):
        for line in review.files[path].split("\n"):
            if line.startswith(grant + ":"):
                return path, line
    raise LookupError(grant)


def remove_line(text: str, line: str) -> str:
    assert text.count(line + "\n") == 1
    return text.replace(line + "\n", "", 1)


def insert_after(text: str, head: str, line: str) -> str:
    """`line` as the first line after the section head `head`."""
    assert text.count(head + "\n") == 1
    return text.replace(head + "\n", f"{head}\n{line}\n", 1)


def move_grant(review: Review, grant: str, to: str) -> Review:
    path, line = grant_line(review, grant)
    moved = with_file(review, path, remove_line(review.files[path], line))
    return with_file(moved, to, insert_after(moved.files[to], "[grants]", line))


def next_path(path: str) -> str:
    return corpus_path(int(path[7:11]))


def with_notice(review: Review, index: int, notice: Notice) -> Review:
    """Notice `index` replaced, in `notices` and in the prompt."""
    old = review.notices[index]
    assert review.prompt.count(old.text) == 1
    notices = list(review.notices)
    notices[index] = notice
    prompt = review.prompt.replace(old.text, notice.text)
    return dataclasses.replace(review, notices=tuple(notices), prompt=prompt)


def notice_text(review: Review, index: int, text: str) -> Review:
    return with_notice(review, index, Notice(review.notices[index].record, text))


def leaver_index(review: Review) -> int:
    return next(i for i, n in enumerate(review.notices) if isinstance(n.record, Leaver))


def quiet_row(review: Review) -> int:
    """A row above 0 with no planted grant and no carried notice."""
    busy = {p.row for p in review.planted} | {p.fact_row for p in review.planted}
    return next(row for row in range(1, review.shape.files) if row not in busy)


def edit_end(review: Review, path: str, new: Callable[[str], str]) -> Review:
    """The closing line of one file changed."""
    text = review.files[path]
    end = text.rindex("[end] ")
    return with_file(review, path, text[:end] + new(text[end:]))


# --- the committed seed passes every check -----------------------------------


ALL_CHECKS: list[Callable[[Review], list[str]]] = [
    checks.parse,
    checks.round_trip,
    checks.keys_differ,
    checks.one_reason,
    checks.rosters,
    checks.decided_once,
    checks.decided_rows,
    checks.no_pointers,
    checks.notice_form,
    checks.code,
    checks.system_text,
    checks.system,
    lambda review: checks.texts_clean(review, (review.system,)),
    checks.file_decided,
    checks.sizes,
    checks.ids,
]


@pytest.mark.parametrize("check", ALL_CHECKS, ids=lambda check: check.__name__)
def test_each_check_passes_on_the_committed_seed(review: Review, check: Any) -> None:
    assert check(review) == []


def test_problems_is_empty_on_the_committed_seed(review: Review) -> None:
    started = time.perf_counter()
    assert problems(review) == []
    print(f"problems {review.shape.name}: {time.perf_counter() - started:.2f} s")


def test_the_keys_have_the_designed_sizes(review: Review) -> None:
    key = answer_key(review)
    assert (len(key.key_files), len(key.key_full), len(key.made), len(key.cleared)) == (
        24,
        32,
        12,
        4,
    )
    assert key.made == tuple(sorted(p.grant.id for p in planted(review, "late_leaver")))
    assert key.cleared == tuple(sorted(p.grant.id for p in planted(review, "late_approval")))


def test_planted_rows_and_fact_rows(review: Review) -> None:
    shape = review.shape
    assert [len(planted(review, kind)) for kind in (*LATE, *LOCAL, "carried")] == [
        12,
        4,
        4,
        4,
        4,
        8,
    ]
    assert all(p.row != 0 and p.fact_row != 0 for p in review.planted)
    for p in review.planted:
        if p.kind in LATE:
            assert p.fact_row is None
        elif p.kind in LOCAL:
            assert p.fact_row == p.row
        else:
            assert p.fact_row == p.row - shape.lead_rows
    holders = [p.grant.person.id for p in review.planted]
    assert len(holders) == len(set(holders))
    held = [g.person.id for f in review.org.files for g in f.grants]
    assert all(held.count(holder) == 1 for holder in holders)


def test_the_rows_of_the_full_shape() -> None:
    """Row 183 holds a decided and a local grant; no other row is shared."""
    decided, local, carried = rows(293, 24, 16), rows(293, 0, 12), rows(293, 24, 8)
    notices = [row - 24 for row in carried]
    assert set(decided) & set(local) == {183}
    assert carried == [40, 74, 108, 141, 175, 208, 242, 276]
    every = [*decided, *local, *carried, *notices]
    assert len(set(every)) == len(every) - 1


# --- keys_differ -------------------------------------------------------------


def test_keys_differ_fails_on_a_later_roster(review: Review) -> None:
    """A third roster, dated 2026-08-31, without one late leaver."""
    leaver = planted(review, "late_leaver")[0]
    path = corpus_path(leaver.row)
    text = review.files[path]
    head = f"[roster {ROSTER_DATES[1].isoformat()}]"
    listed = text[text.index(head) + len(head) + 1 : text.index("[grants]")]
    later = listed.replace(person_text(leaver.grant.person) + "\n", "")
    assert later != listed
    fixture = with_file(
        review, path, text.replace("[grants]\n", f"[roster 2026-08-31]\n{later}[grants]\n")
    )
    assert checks.keys_differ(fixture) == ["keys_differ.differ: 11 4"]


def test_keys_differ_fails_when_a_late_approval_is_not_in_the_prompt(review: Review) -> None:
    approval = next(n for n in review.notices if not isinstance(n.record, Leaver))
    prompt = review.prompt.replace(approval.text + "\n", "").replace("\n" + approval.text, "")
    assert prompt != review.prompt
    fixture = dataclasses.replace(review, prompt=prompt)
    assert checks.keys_differ(fixture) == ["keys_differ.differ: 12 3"]


# --- round_trip --------------------------------------------------------------


def mark_flipped(review: Review) -> Review:
    """A system with clean grants only reads `no approval` in one file."""
    file = review.org.files[quiet_row(review)]
    system = next(s for s in file.systems if s.approval_required)
    path = corpus_path(file.row)
    line = f"{system_text(system)}: approval required"
    return with_file(review, path, review.files[path].replace(line, line[:-17] + "no approval"))


def test_round_trip_fails_on_a_flipped_mark(review: Review) -> None:
    only(checks.round_trip(mark_flipped(review)), "round_trip.records")


# --- decided_once ------------------------------------------------------------


def decided(review: Review) -> str:
    return planted(review, "late_leaver")[0].grant.id


def test_decided_once_fails_on_an_id_in_two_files(review: Review) -> None:
    path, line = grant_line(review, decided(review))
    other = next_path(path)
    fixture = with_file(review, other, insert_after(review.files[other], "[grants]", line))
    only(checks.decided_once(fixture), "decided_once.once")


def test_decided_once_fails_on_an_id_in_the_index(review: Review) -> None:
    index = review.files[INDEX_PATH].replace("\n", f" {decided(review)}\n", 1)
    only(checks.decided_once(with_file(review, INDEX_PATH, index)), "decided_once.elsewhere")


def test_decided_once_fails_on_an_id_in_a_notice(review: Review) -> None:
    fixture = notice_text(review, 0, f"{review.notices[0].text} {decided(review)}")
    only(checks.decided_once(fixture), "decided_once.elsewhere")


def test_decided_once_fails_on_an_id_in_system(review: Review) -> None:
    fixture = dataclasses.replace(review, system=f"{review.system}\n{decided(review)}")
    only(checks.decided_once(fixture), "decided_once.elsewhere")


def test_decided_once_fails_on_a_decided_id_in_no_file(review: Review) -> None:
    path, line = grant_line(review, decided(review))
    fixture = with_file(review, path, remove_line(review.files[path], line))
    only(checks.decided_once(fixture), "decided_once.once")


# --- decided_rows ------------------------------------------------------------


def test_decided_rows_fails_on_a_moved_grant(review: Review) -> None:
    path, _ = grant_line(review, decided(review))
    only(
        checks.decided_rows(move_grant(review, decided(review), next_path(path))),
        "decided_rows.formula",
    )


def test_the_decided_rows_are_apart() -> None:
    for shape in (FULL, SHORT):
        found = rows(shape.files, shape.lead_rows, 16)
        assert len(set(found)) == 16
        assert min(found) >= shape.lead_rows
    with pytest.raises(ValueError, match="not all different"):
        Shape("x", 20, 6)


# --- no_pointers -------------------------------------------------------------


def test_no_pointers_fails_on_a_path_in_a_notice(review: Review) -> None:
    fixture = notice_text(review, 0, f"{review.notices[0].text} access/0001.txt")
    only(checks.no_pointers(fixture), "no_pointers.notice")


@pytest.mark.parametrize("part", ["name", "id"])
def test_no_pointers_fails_on_a_team_in_a_notice(review: Review, part: str) -> None:
    team = review.org.teams[1]
    fixture = notice_text(review, 0, f"{review.notices[0].text} {getattr(team, part)}")
    only(checks.no_pointers(fixture), "no_pointers.notice")


@pytest.mark.parametrize(
    "kind",
    ["person name", "person id", "grant id", "team name", "team id", "system name", "system id"],
)
def test_no_pointers_fails_on_a_record_in_the_index(review: Review, kind: str) -> None:
    org = review.org
    value = {
        "person name": org.people[5].name,
        "person id": org.people[5].id,
        "grant id": org.files[1].grants[0].id,
        "team name": org.teams[2].name,
        "team id": org.teams[2].id,
        "system name": org.systems[3].name,
        "system id": org.systems[3].id,
    }[kind]
    index = review.files[INDEX_PATH].replace("\n", f" {value}\n", 1)
    only(checks.no_pointers(with_file(review, INDEX_PATH, index)), "no_pointers.index")


# --- notice_form -------------------------------------------------------------


def test_notice_form_fails_on_an_edited_date(review: Review) -> None:
    index = leaver_index(review)
    record = review.notices[index].record
    assert isinstance(record, Leaver)
    other = date(2026, 5, 5) if record.last_day != date(2026, 5, 5) else date(2026, 5, 6)
    text = review.notices[index].text.replace(record.last_day.isoformat(), other.isoformat())
    only(checks.notice_form(notice_text(review, index, text)), "notice_form.same_function")


def test_notice_form_fails_on_a_fourth_template(review: Review) -> None:
    templates = (*LEAVER_TEMPLATES, "Gone since {date}: {person}.")
    index = leaver_index(review)
    record = review.notices[index].record
    assert isinstance(record, Leaver)
    fourth = dataclasses.replace(record, template=3)
    fixture = with_notice(review, index, Notice(fourth, render_leaver(fourth, templates)))
    only(checks.notice_form(fixture, templates, APPROVAL_TEMPLATES), "notice_form.known_template")


def test_notice_form_fails_on_a_late_date(review: Review) -> None:
    index = leaver_index(review)
    record = review.notices[index].record
    assert isinstance(record, Leaver)
    late = dataclasses.replace(record, last_day=REVIEW_DATE + timedelta(days=1))
    fixture = with_notice(review, index, Notice(late, render_leaver(late)))
    only(checks.notice_form(fixture), "notice_form.date_in_range")


def test_notice_form_fails_on_a_notice_missing_from_the_prompt(review: Review) -> None:
    text = review.notices[0].text
    prompt = review.prompt.replace(text + "\n", "").replace("\n" + text, "")
    assert prompt != review.prompt
    only(
        checks.notice_form(dataclasses.replace(review, prompt=prompt)), "notice_form.same_function"
    )


# --- code --------------------------------------------------------------------


def with_code(review: Review, value: str) -> Review:
    """Another code, in the review and in the prompt's code sentence."""
    old = CODE_SENTENCE.format(code=review.reference_code)
    prompt = review.prompt.replace(old, CODE_SENTENCE.format(code=value))
    return dataclasses.replace(review, reference_code=value, prompt=prompt)


def other_code(review: Review) -> str:
    return (
        "AAAA-BBBB-CCCC-DDDD"
        if review.reference_code != "AAAA-BBBB-CCCC-DDDD"
        else "EEEE-FFFF-GGGG-HHHH"
    )


def test_the_code_is_not_a_grant_id(review: Review) -> None:
    assert REFERENCE_CODE.fullmatch(review.reference_code)
    assert GRANT_ID.fullmatch(review.reference_code) is None


def test_code_fails_on_a_short_code(review: Review) -> None:
    """The code less its last group, in the prompt too."""
    only(checks.code(with_code(review, review.reference_code[:14])), "code.form")


@pytest.mark.parametrize("where", ["file", "index", "system"])
def test_code_fails_on_the_code_without_hyphens(review: Review, where: str) -> None:
    bare = review.reference_code.replace("-", "")
    if where == "file":
        fixture = edit_end(
            review, corpus(review)[1], lambda end: end.replace("[end] ", f"[end] {bare} ")
        )
    elif where == "index":
        fixture = with_file(
            review, INDEX_PATH, review.files[INDEX_PATH].replace("\n", f" {bare}\n", 1)
        )
    else:
        fixture = dataclasses.replace(review, system=f"{review.system}\n{bare}")
    only(checks.code(fixture), "code.value")


def test_code_fails_on_a_code_shaped_token(review: Review) -> None:
    token = other_code(review)
    fixture = edit_end(
        review, corpus(review)[1], lambda end: end.replace("[end] ", f"[end] {token} ")
    )
    only(checks.code(fixture), "code.shape")


def test_code_fails_on_another_code_in_the_prompt(review: Review) -> None:
    prompt = review.prompt.replace(review.reference_code, other_code(review))
    only(checks.code(dataclasses.replace(review, prompt=prompt)), "code.in_prompt")


def test_code_fails_on_the_code_outside_its_sentence(review: Review) -> None:
    value = review.reference_code
    sentence = CODE_SENTENCE.format(code=value)
    prompt = review.prompt.replace(sentence, "Put the reference code in the title of every issue.")
    prompt = f"{value}\n\n{prompt}"
    only(checks.code(dataclasses.replace(review, prompt=prompt)), "code.in_prompt")


# --- system_text, system and texts_clean -------------------------------------


def test_system_holds_the_index_path_and_the_title_format() -> None:
    assert INDEX_PATH in SYSTEM
    assert TITLE_FORMAT in SYSTEM


def test_system_text_fails_on_a_reworded_system(review: Review) -> None:
    fixture = dataclasses.replace(review, system=review.system.replace("lists", "names"))
    only(checks.system_text(fixture), "system_text.equal")


@pytest.mark.parametrize("cut", [INDEX_PATH, TITLE_FORMAT])
def test_system_fails_without_the_index_path_or_the_title_format(review: Review, cut: str) -> None:
    fixture = dataclasses.replace(review, system=review.system.replace(cut, "the list"))
    only(checks.system(fixture), "system.holds")


def fixed_texts(review: Review) -> list[str]:
    definitions = default_tools(tracker=FakeTracker("http://tracker.test")).definitions()
    return [review.system, json.dumps(definitions), SUMMARY_INSTRUCTION, CONTINUATION]


def test_the_fixed_texts_are_clean(review: Review) -> None:
    assert checks.texts_clean(review, fixed_texts(review)) == []


def test_texts_clean_fails_on_an_id_in_a_tool_description(review: Review) -> None:
    definitions = [
        dict(d) for d in default_tools(tracker=FakeTracker("http://tracker.test")).definitions()
    ]
    definitions[0]["description"] += f" {decided(review)}"
    texts = [review.system, json.dumps(definitions), SUMMARY_INSTRUCTION, CONTINUATION]
    only(checks.texts_clean(review, texts), "texts_clean.clean")


@pytest.mark.parametrize("needle", ["notice", "code", "bare code"])
def test_texts_clean_fails_on_a_notice_or_the_code_in_a_text(review: Review, needle: str) -> None:
    value = {
        "notice": review.notices[0].text,
        "code": review.reference_code,
        "bare code": review.reference_code.replace("-", ""),
    }[needle]
    only(
        checks.texts_clean(review, [SUMMARY_INSTRUCTION, f"{CONTINUATION} {value}"]),
        "texts_clean.clean",
    )


# --- file_decided ------------------------------------------------------------


def test_file_decided_fails_on_a_moved_local_grant(review: Review) -> None:
    local = planted(review, "local_roster")[0].grant.id
    path, _ = grant_line(review, local)
    only(checks.file_decided(move_grant(review, local, next_path(path))), "file_decided.local_rows")


def test_file_decided_fails_on_a_carried_notice_in_the_grant_file(review: Review) -> None:
    carried = planted(review, "carried")[0]
    person = carried.grant.person
    source = corpus_path(carried.fact_row or 0)
    line = next(
        line for line in review.files[source].split("\n") if line.find(f"({person.id})") > 0
    )
    fixture = with_file(review, source, remove_line(review.files[source], line))
    target = corpus_path(carried.row)
    fixture = with_file(
        fixture, target, insert_after(fixture.files[target], "[leaver notices]", line)
    )
    only(checks.file_decided(fixture), "file_decided.carried_distance")


def test_file_decided_fails_without_when_found(review: Review) -> None:
    fixture = dataclasses.replace(review, system=review.system.replace(WHEN_FOUND, "").strip())
    only(checks.file_decided(fixture), "file_decided.when_found")


# --- sizes -------------------------------------------------------------------


def test_sizes_fails_on_a_missing_file(review: Review) -> None:
    last = corpus(review)[-1]
    files = {path: text for path, text in review.files.items() if path != last}
    files[INDEX_PATH] = files[INDEX_PATH].replace(last + "\n", "")
    only(checks.sizes(dataclasses.replace(review, files=files)), "sizes.count")


def test_sizes_fails_on_a_file_one_byte_short(review: Review) -> None:
    fixture = edit_end(review, corpus(review)[1], lambda end: end.replace("=", "", 1))
    only(checks.sizes(fixture), "sizes.bytes")


def test_sizes_fails_on_a_character_outside_ascii(review: Review) -> None:
    """Two ASCII characters replaced by one two-byte character: the bytes stay."""
    fixture = edit_end(review, corpus(review)[1], lambda end: end.replace("==", "é", 1))
    assert len(fixture.files[corpus(review)[1]].encode()) == FILE_BYTES
    only(checks.sizes(fixture), "sizes.ascii")


def test_sizes_fails_on_an_index_over_its_limit(review: Review) -> None:
    limit = len(review.files[INDEX_PATH].encode()) - 1
    only(checks.sizes(review, index_limit=limit), "sizes.index_bytes")


def test_sizes_fails_on_two_index_rows_swapped(review: Review) -> None:
    first, second = corpus(review)[1:3]
    index = review.files[INDEX_PATH].replace(first, "#").replace(second, first).replace("#", second)
    only(checks.sizes(with_file(review, INDEX_PATH, index)), "sizes.index_rows")


def test_sizes_fails_on_a_repeated_index_row(review: Review) -> None:
    first, second = corpus(review)[1:3]
    index = review.files[INDEX_PATH].replace(second + "\n", first + "\n")
    only(checks.sizes(with_file(review, INDEX_PATH, index)), "sizes.index_rows")


def test_sizes_fails_on_one_file_in_another(review: Review) -> None:
    first, second = corpus(review)[1:3]
    only(checks.sizes(with_file(review, second, review.files[first])), "sizes.distinct")


def test_the_job_spec_takes_the_job(review: Review) -> None:
    job = spec(review, "a-model")
    assert JobSpec.model_validate(job).files == review.files
    with pytest.raises(ValidationError, match="extra"):
        JobSpec.model_validate({**job, "unknown": 1})


def test_the_files_fit_the_spec_cap_with_the_largest_index(review: Review) -> None:
    paths = sum(len(path) for path in corpus(review)) + len(INDEX_PATH)
    largest = FILE_BYTES * review.shape.files + paths + checks.INDEX_LIMIT
    assert largest == {"full": 4_870_452, "short": 655_909}[review.shape.name]
    assert largest < MAX_FILES_BYTES
    assert len(review.files[INDEX_PATH]) <= checks.INDEX_LIMIT


# --- ids and parse -----------------------------------------------------------


def roster_line(review: Review, row: int) -> str:
    return person_text(review.org.files[row].rosters[0].people[0])


def test_ids_fails_on_a_person_in_two_teams(review: Review) -> None:
    line = roster_line(review, 1)
    other = corpus_path(2)
    head = f"[roster {ROSTER_DATES[0].isoformat()}]"
    only(
        checks.ids(with_file(review, other, insert_after(review.files[other], head, line))),
        "ids.unique",
    )


def test_ids_fails_on_a_grant_id_on_two_lines(review: Review) -> None:
    path = corpus_path(1)
    line = render_line(review, 1)
    only(
        checks.ids(with_file(review, path, insert_after(review.files[path], "[grants]", line))),
        "ids.unique",
    )


def render_line(review: Review, row: int) -> str:
    _, line = grant_line(review, review.org.files[row].grants[0].id)
    return line


def test_ids_fails_on_a_system_with_two_marks(review: Review) -> None:
    only(checks.ids(mark_flipped(review)), "ids.unique")


def test_ids_fails_on_a_system_with_two_names(review: Review) -> None:
    system = review.org.files[1].systems[0]
    path = corpus_path(1)
    line = f"{system_text(system)}: "
    renamed = f"{SYSTEM_WORDS[0]} {SYSTEM_WORDS[1]} ({system.id}): "
    if renamed == line:
        renamed = f"{SYSTEM_WORDS[2]} {SYSTEM_WORDS[1]} ({system.id}): "
    only(
        checks.ids(with_file(review, path, review.files[path].replace(line, renamed, 1))),
        "ids.unique",
    )


def test_ids_fails_on_a_person_with_two_names(review: Review) -> None:
    person = review.org.files[1].rosters[0].people[0]
    path = corpus_path(1)
    given, family = person.name.split()
    other = GIVEN[0] if given != GIVEN[0] else GIVEN[1]
    text = review.files[path].replace(person_text(person), f"{other} {family} ({person.id})", 1)
    only(checks.ids(with_file(review, path, text)), "ids.unique")


def test_ids_fails_on_a_team_heading_two_files(review: Review) -> None:
    path = corpus_path(2)
    text = review.files[path].replace(
        f"team: {team_text(review.org.teams[2])}", f"team: {team_text(review.org.teams[1])}"
    )
    only(checks.ids(with_file(review, path, text)), "ids.unique")


def test_ids_fails_on_neighbour_ids(review: Review) -> None:
    grants = review.org.files[1].grants
    target = grants[1].id
    neighbour = next(
        target[:-1] + symbol
        for symbol in "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
        if symbol != target[-1] and target[:-1] + symbol not in {g.id for g in grants}
    )
    path, line = grant_line(review, grants[0].id)
    text = review.files[path].replace(line, neighbour + line[len(neighbour) :])
    only(checks.ids(with_file(review, path, text)), "ids.apart")


@pytest.mark.parametrize("edit", ["short_id", "unknown_line"])
def test_parse_fails_on_a_line_of_no_form(review: Review, edit: str) -> None:
    """The error holds the row and the line number; the check line says 1."""
    path = corpus_path(1)
    text = review.files[path]
    lines = text.split("\n")
    number = lines.index("[grants]") + 2
    if edit == "short_id":
        lines[number - 1] = lines[number - 1][:2] + lines[number - 1][3:]
    else:
        lines.insert(number - 1, "a line of no known form")
    fixture = with_file(review, path, "\n".join(lines))
    with pytest.raises(ParseError) as error:
        parse_files(fixture.files)
    assert (error.value.row, error.value.line) == (1, number)
    assert error.value.args == (1, number)
    assert checks.parse(fixture) == ["parse.line: 1"]
    assert problems(fixture) == ["parse.line: 1"]


# --- one_reason and rosters --------------------------------------------------


def test_one_reason_fails_when_a_carried_grant_needs_approval(review: Review) -> None:
    carried = planted(review, "carried")[0]
    path = corpus_path(carried.row)
    line = f"{system_text(carried.grant.system)}: no approval"
    text = review.files[path].replace(line, line[:-11] + "approval required")
    only(checks.one_reason(with_file(review, path, text)), "one_reason.reason")


def test_rosters_fails_on_a_leaver_still_listed(review: Review) -> None:
    """Row 0's first leaver left on the first day of the period."""
    first = review.org.people[0]
    assert first.last_day == PERIOD_START
    path = corpus_path(0)
    head = f"[roster {ROSTER_DATES[0].isoformat()}]"
    fixture = with_file(review, path, insert_after(review.files[path], head, person_text(first)))
    only(checks.rosters(fixture), "rosters.presence")


def test_rosters_fails_on_a_leaver_missing_before_their_last_day(review: Review) -> None:
    """Row 0's second leaver left on the review date."""
    second = review.org.people[1]
    assert second.last_day == REVIEW_DATE
    path = corpus_path(0)
    text = review.files[path]
    head = f"[roster {ROSTER_DATES[0].isoformat()}]\n"
    start = text.index(head) + len(head)
    first_roster = text[start:]
    cut = first_roster.replace(person_text(second) + "\n", "", 1)
    only(checks.rosters(with_file(review, path, text[:start] + cut)), "rosters.presence")


def test_a_roster_absent_leaver_left_between_the_rosters(review: Review) -> None:
    for p in planted(review, LOCAL_ROSTER):
        day = p.grant.person.last_day
        assert day is not None
        assert ROSTER_DATES[0] <= day < ROSTER_DATES[1]


class Scripted:
    """A stream that answers from a list, or with a function of `n`."""

    def __init__(self, values: Iterable[int] | Callable[[int], int]) -> None:
        if callable(values):
            self._answer = values
            self._values: Iterator[int] = iter(())
        else:
            self._answer = self._next
            self._values = iter(values)

    def _next(self, n: int) -> int:
        return next(self._values)

    def below(self, n: int) -> int:
        value = self._answer(n)
        assert 0 <= value < n
        return value

    def left(self) -> list[int]:
        return list(self._values)


@pytest.mark.parametrize(
    ("kind", "first", "last"),
    [
        ("local_roster", date(2026, 3, 31), date(2026, 6, 29)),
        ("local_notice", date(2026, 7, 1), date(2026, 9, 30)),
        ("late_leaver", date(2026, 7, 1), date(2026, 9, 30)),
        ("carried", date(2026, 7, 1), date(2026, 9, 30)),
    ],
)
def test_a_planted_last_day_is_drawn_at_both_ends(kind: str, first: date, last: date) -> None:
    assert _last_day(kind, Scripted(lambda n: 0)) == first
    assert _last_day(kind, Scripted(lambda n: n - 1)) == last


# --- the drawer and the name tuples ------------------------------------------


def test_the_drawer_draws_a_repeated_id_again() -> None:
    stream = Scripted([1, 2, 1, 2, 3, 4])
    drawer = IdDrawer(stream, "XX", 2)
    assert (drawer.next(), drawer.next()) == ("XX12", "XX34")
    assert stream.left() == []


@pytest.mark.parametrize(("apart", "second"), [(True, "XX56"), (False, "XX13")])
def test_the_drawer_keeps_grant_ids_apart(apart: bool, second: str) -> None:
    stream = Scripted([1, 2, 1, 3, 5, 6])
    drawer = IdDrawer(stream, "XX", 2, apart=apart)
    assert (drawer.next(), drawer.next()) == ("XX12", second)


def words(texts: Iterable[str]) -> set[str]:
    return {word.lower() for text in texts for word in re.findall(r"[A-Za-z]+", text)}


def shared(names: dict[str, tuple[str, ...]], fixed: Iterable[str]) -> list[str]:
    """Words in two name tuples, or in a name tuple and a fixed text, lower-cased."""
    fixed_words = words(fixed)
    found = []
    seen: dict[str, str] = {}
    for group, tuple_ in names.items():
        for word in tuple_:
            low = word.lower()
            if low in fixed_words or seen.get(low, group) != group:
                found.append(low)
            seen[low] = group
    return found


def fixed_lines() -> list[str]:
    return [
        "access records, file of",
        "team:",
        "[systems]",
        "approval required",
        "no approval",
        "[roster]",
        "[grants]",
        "granted",
        "[leaver notices]",
        "[approvals]",
        "[end]",
    ]


NAMES: dict[str, tuple[str, ...]] = {
    "GIVEN": GIVEN,
    "FAMILY": FAMILY,
    "TEAM_WORDS": TEAM_WORDS,
    "SYSTEM_WORDS": SYSTEM_WORDS,
}


def test_the_name_tuples_share_no_word() -> None:
    fixed = [*ROLES, *LEAVER_TEMPLATES, *APPROVAL_TEMPLATES, *fixed_lines(), SYSTEM]
    fixed += [TASK, RULES, NOTICES_HEAD, CODE_SENTENCE]
    assert shared(NAMES, fixed) == []
    for tuple_ in NAMES.values():
        assert len(tuple_) >= 40
        assert len(set(tuple_)) == len(tuple_)
        assert all(re.fullmatch(r"[A-Z][a-z]+", word) for word in tuple_)


def test_the_shared_word_helper_finds_a_shared_word() -> None:
    assert shared({"a": ("Kanod", "Velo"), "b": ("Velo",)}, []) == ["velo"]
    assert shared({"a": ("Granted",)}, ["granted 2025"]) == ["granted"]


# --- determinism -------------------------------------------------------------


def test_two_builds_are_equal(review: Review, seed: bytes) -> None:
    again = generate(seed, review.shape)
    assert again == review
    assert list(again.files) == list(review.files)


def run_command(
    *arguments: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        [sys.executable, "-m", "access_review", "commitment", *arguments],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def test_the_job_does_not_depend_on_the_hash_seed() -> None:
    outputs = []
    for hash_seed in ("1", "2"):
        env = {**os.environ, "PYTHONHASHSEED": hash_seed}
        result = run_command("--shape", "short", "--seed-file", str(SEED_FILE), env=env)
        assert result.returncode == 0, result.stderr
        outputs.append([line for line in result.stdout.splitlines() if line.startswith("job ")])
    assert outputs[0] == outputs[1]
    assert len(outputs[0]) == 1


def test_another_seed_gives_another_job_and_code(seed: bytes) -> None:
    other = bytes(byte ^ 1 for byte in seed)
    mine, theirs = generate(seed, SHORT), generate(other, SHORT)
    assert job_digest(mine) != job_digest(theirs)
    assert mine.reference_code != theirs.reference_code


def test_the_two_shapes_share_no_code_and_no_id(seed: bytes) -> None:
    full, short = generate(seed, FULL), generate(seed, SHORT)
    assert full.reference_code != short.reference_code
    full_ids = {g.id for f in full.org.files for g in f.grants}
    assert not any(g.id in full_ids for f in short.org.files for g in f.grants)


ALLOWED_IMPORTS = {
    "argparse",
    "collections.abc",
    "dataclasses",
    "datetime",
    "hashlib",
    "json",
    "pathlib",
    "re",
    "sys",
    "typing",
}


def test_the_package_uses_only_the_listed_imports_and_no_clock() -> None:
    for path in sorted(PACKAGE.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules = [node.module or ""]
            else:
                modules = []
            for module in modules:
                assert module in ALLOWED_IMPORTS or module.split(".")[0] == "access_review", (
                    path.name,
                    module,
                )
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id != "hash", path.name
            if isinstance(node, ast.Attribute):
                assert node.attr not in ("now", "today"), path.name


# --- the command -------------------------------------------------------------


def recorded() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(SEED_FILE.read_text(encoding="utf-8"))
    return data


def expected_lines(shape: str) -> list[str]:
    entry = recorded()["shapes"][shape]
    return [
        f"generator {VERSION}",
        f"shape {shape}",
        f"job {entry['job']}",
        f"commitment {entry['commitment']}",
    ]


@pytest.mark.parametrize("shape", ["full", "short"])
def test_the_command_prints_the_recorded_hashes(
    shape: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert command.main(["commitment", "--shape", shape, "--seed-file", str(SEED_FILE)]) == 0
    out = capsys.readouterr()
    assert out.out.splitlines() == expected_lines(shape)
    assert out.err == ""


def test_the_command_runs_as_a_module() -> None:
    result = run_command("--shape", "full", "--seed-file", str(SEED_FILE))
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == expected_lines("full")


def test_the_commitment_is_built_as_documented(review: Review, seed: bytes) -> None:
    key: Key = answer_key(review)
    preimage = {
        "generator": VERSION,
        "job": job_digest(review),
        "key": {
            "cleared": sorted(key.cleared),
            "key_files": sorted(key.key_files),
            "key_full": sorted(key.key_full),
            "made": sorted(key.made),
            "reference_code": review.reference_code,
        },
        "seed": seed.hex(),
        "shape": review.shape.name,
    }
    data = json.dumps(preimage, sort_keys=True, separators=(",", ":")).encode("ascii")
    built = "sha256:" + hashlib.sha256(data).hexdigest()
    assert access_review.commitment(seed, review.shape, key, job_digest(review)) == built
    assert recorded()["shapes"][review.shape.name]["commitment"] == built
    job = {"files": review.files, "prompt": review.prompt, "system": review.system}
    job_bytes = json.dumps(job, sort_keys=True, separators=(",", ":")).encode("ascii")
    assert (
        recorded()["shapes"][review.shape.name]["job"]
        == "sha256:" + hashlib.sha256(job_bytes).hexdigest()
    )


def test_the_seed_file_names_the_generator_version() -> None:
    assert recorded()["generator"] == VERSION


SEED_TEXT = json.loads(SEED_FILE.read_text(encoding="utf-8"))["seed"]


@pytest.mark.parametrize(
    "content",
    [
        json.dumps({"seed": SEED_TEXT[:30]}),
        json.dumps({"seed": SEED_TEXT + "a"}),
        json.dumps({"seed": SEED_TEXT[:-1] + "g"}),
        None,
        '{"seed": "' + SEED_TEXT,
        json.dumps({"other": SEED_TEXT}),
    ],
    ids=["30-digits", "odd-count", "not-hex", "missing-file", "bad-json", "no-seed"],
)
def test_a_refused_seed_file_exits_2_and_quotes_nothing(
    content: str | None, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "seed.json"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    assert command.main(["commitment", "--shape", "short", "--seed-file", str(path)]) == 2
    out = capsys.readouterr()
    assert out.out == ""
    assert SEED_TEXT[:20] not in out.err
    assert SEED_TEXT[:20].upper() not in out.err
    assert str(path) not in out.err
    assert len(out.err.splitlines()) == 1


def test_capitals_give_the_same_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "seed.json"
    path.write_text(json.dumps({"seed": f"  {SEED_TEXT.upper()}\n"}), encoding="utf-8")
    assert command.main(["commitment", "--shape", "short", "--seed-file", str(path)]) == 0
    assert capsys.readouterr().out.splitlines() == expected_lines("short")


def test_a_failed_check_exits_1_with_its_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], seed: bytes
) -> None:
    broken = mark_flipped(generate(seed, SHORT))
    monkeypatch.setattr(command, "generate", lambda seed, shape: broken)
    assert command.main(["commitment", "--shape", "short", "--seed-file", str(SEED_FILE)]) == 1
    out = capsys.readouterr()
    assert out.out == ""
    assert "round_trip.records: 1" in out.err.splitlines()


def test_an_exception_exits_1_with_its_type_only(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail(seed: bytes, shape: Shape) -> Review:
        raise ValueError("marker text 8d1f")

    monkeypatch.setattr(command, "generate", fail)
    assert command.main(["commitment", "--shape", "short", "--seed-file", str(SEED_FILE)]) == 1
    out = capsys.readouterr()
    assert out.out == ""
    assert out.err == "ValueError\n"


def test_the_preimage_hashes_to_the_commitment(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "preimage.json"
    arguments = ["commitment", "--shape", "short", "--seed-file", str(SEED_FILE)]
    assert command.main([*arguments, "--write-preimage", str(path)]) == 0
    commitment = capsys.readouterr().out.splitlines()[3].split()[1]
    assert commitment == "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize("where", ["this-checkout", "git-file", "git-directory"])
def test_the_preimage_is_never_written_inside_a_checkout(
    where: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    if where == "this-checkout":
        path = Path(__file__).with_name("preimage-test.json")
    elif where == "git-file":
        (tmp_path / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
        path = tmp_path / "deeper" / "preimage.json"
    else:
        (tmp_path / ".git").mkdir()
        path = tmp_path / ".git" / "preimage.json"
    arguments = ["commitment", "--shape", "short", "--seed-file", str(SEED_FILE)]
    assert command.main([*arguments, "--write-preimage", str(path)]) == 2
    out = capsys.readouterr()
    assert out.out == ""
    assert out.err == f"{command.IN_CHECKOUT}\n"
    assert not path.exists()


# --- any seed ----------------------------------------------------------------


def test_problems_is_empty_for_other_seeds() -> None:
    started = time.perf_counter()
    cases = [(i, SHORT) for i in range(20)] + [(i, FULL) for i in range(2)]
    for i, shape in cases:
        seed = hashlib.sha256(b"access-review-test-%d" % i).digest()[:16]
        assert problems(generate(seed, shape)) == [], (i, shape.name)
    print(f"other seeds: {time.perf_counter() - started:.2f} s")


def test_render_functions_give_the_prompt_lines(review: Review) -> None:
    for notice in review.notices:
        record = notice.record
        text = render_leaver(record) if isinstance(record, Leaver) else render_approval(record)
        assert text == notice.text
        assert text in review.prompt.split("\n")


def test_a_planted_grant_has_no_fixed_place_in_its_file(review: Review) -> None:
    """Grant lines are sorted by id, so a planted grant is not marked by where it is."""
    places = [
        [grant.id for grant in review.org.files[p.row].grants].index(p.grant.id)
        for p in review.planted
    ]
    assert max(places) > 1
