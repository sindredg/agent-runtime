"""Read a job's text back into records, apply the rules, and give the keys.

The parser checks the form of each line only, never what a line refers
to; `checks` does that. The keys come from the text alone, never from
the model the text was built from.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date
from typing import TYPE_CHECKING

from access_review.ids import GRANT_ID, PERSON_ID, SYSTEM_ID, TEAM_ID
from access_review.render import (
    APPROVAL_TEMPLATES,
    INDEX_PATH,
    LEAVER_TEMPLATES,
    NOTICES_HEAD,
)

if TYPE_CHECKING:
    from access_review.org import Review

_NAME = "([A-Z][a-z]+ [A-Z][a-z]+)"
_DATE = "([0-9]{4}-[0-9]{2}-[0-9]{2})"
_PERSON = rf"{_NAME} \(({PERSON_ID.pattern})\)"
_SYSTEM = rf"{_NAME} \(({SYSTEM_ID.pattern})\)"
_ROLE = "(reader|editor|operator|admin)"

_PATH = re.compile(r"access/([0-9]{4})\.txt")
_HEADER = re.compile(r"access records, file [0-9]{4} of [0-9]{4}")
_TEAM = re.compile(rf"team: {_NAME} \(({TEAM_ID.pattern})\)")
_SYSTEM_LINE = re.compile(rf"{_SYSTEM}: (approval required|no approval)")
_ROSTER_HEAD = re.compile(rf"\[roster {_DATE}\]")
_PERSON_LINE = re.compile(_PERSON)
_GRANT_LINE = re.compile(rf"({GRANT_ID.pattern}): {_PERSON}, {_SYSTEM}, {_ROLE}, granted {_DATE}")
_END = re.compile(r"\[end\] =+")
_HEADS = {"[systems]": "systems", "[grants]": "grants", "[leaver notices]": "leavers"}
_HEADS["[approvals]"] = "approvals"


def _pattern(template: str) -> re.Pattern[str]:
    """A template as a pattern; its groups are in the order person, system, date."""
    text = re.escape(template)
    for name, group in (("person", _PERSON), ("system", _SYSTEM), ("date", _DATE)):
        text = text.replace(re.escape("{" + name + "}"), f"(?P<{name}>{group})")
    return re.compile(text)


LEAVER_PATTERNS = tuple(_pattern(template) for template in LEAVER_TEMPLATES)
APPROVAL_PATTERNS = tuple(_pattern(template) for template in APPROVAL_TEMPLATES)


class ParseError(Exception):
    """A line no pattern of its section matches: the file's row and the line number.

    Row is None for the prompt. It carries no text of the line.
    """

    def __init__(self, row: int | None, line: int) -> None:
        super().__init__(row, line)
        self.row = row
        self.line = line


@dataclass(frozen=True)
class Ref:
    """An id and the name written next to it."""

    id: str
    name: str


@dataclass(frozen=True)
class SystemLine:
    system: Ref
    approval_required: bool


@dataclass(frozen=True)
class RosterLines:
    date: date
    people: tuple[Ref, ...]


@dataclass(frozen=True)
class GrantLine:
    id: str
    person: Ref
    system: Ref
    role: str
    granted: date


@dataclass(frozen=True)
class LeaverLine:
    """A leaver notice. `row` is its file's, None in the prompt."""

    row: int | None
    person: Ref
    last_day: date
    template: int
    text: str


@dataclass(frozen=True)
class ApprovalLine:
    row: int | None
    person: Ref
    system: Ref
    date: date
    template: int
    text: str


@dataclass(frozen=True)
class FileRecords:
    row: int
    team: Ref
    systems: tuple[SystemLine, ...]
    rosters: tuple[RosterLines, ...]
    grants: tuple[GrantLine, ...]
    leavers: tuple[LeaverLine, ...]
    approvals: tuple[ApprovalLine, ...]


@dataclass(frozen=True)
class Records:
    """The files' records, and the prompt's when they are added."""

    files: tuple[FileRecords, ...]
    leavers: tuple[LeaverLine, ...] = ()
    approvals: tuple[ApprovalLine, ...] = ()


@dataclass(frozen=True)
class Evidence:
    """Why a grant is a violation: `notice`, `roster` or `approval`, and a row."""

    kind: str
    row: int | None


@dataclass(frozen=True)
class Key:
    key_files: tuple[str, ...]
    key_full: tuple[str, ...]
    made: tuple[str, ...]
    cleared: tuple[str, ...]
    reference_code: str


def _day(text: str, row: int | None, line: int) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise ParseError(row, line) from None


_SYSTEM_REF = re.compile(_SYSTEM)


def _named(match: re.Match[str], key: str) -> Ref:
    """The id and name inside a template's `person` or `system` group."""
    inner = (_PERSON_LINE if key == "person" else _SYSTEM_REF).fullmatch(match.group(key))
    if inner is None:  # The group's own pattern matched it already.
        raise ValueError(key)
    return Ref(inner.group(2), inner.group(1))


def _leaver(line: str, row: int | None, number: int) -> LeaverLine | None:
    for template, pattern in enumerate(LEAVER_PATTERNS):
        match = pattern.fullmatch(line)
        if match:
            day = _day(match.group("date"), row, number)
            return LeaverLine(row, _named(match, "person"), day, template, line)
    return None


def _approval(line: str, row: int | None, number: int) -> ApprovalLine | None:
    for template, pattern in enumerate(APPROVAL_PATTERNS):
        match = pattern.fullmatch(line)
        if match:
            day = _day(match.group("date"), row, number)
            person, system = _named(match, "person"), _named(match, "system")
            return ApprovalLine(row, person, system, day, template, line)
    return None


def corpus_row(path: str) -> int | None:
    """The row of a corpus path, or None for any other path."""
    match = _PATH.fullmatch(path)
    return None if match is None else int(match.group(1)) - 1


def parse_file(row: int, text: str) -> FileRecords:
    """One corpus file. Raises ParseError on a line of the wrong form."""
    lines = text.split("\n")
    if lines[-1] != "":
        raise ParseError(row, len(lines))
    lines = lines[:-1]
    if not lines or not _HEADER.fullmatch(lines[0]):
        raise ParseError(row, 1)
    team = _TEAM.fullmatch(lines[1]) if len(lines) > 1 else None
    if team is None:
        raise ParseError(row, 2)
    if not _END.fullmatch(lines[-1]):
        raise ParseError(row, len(lines))
    systems: list[SystemLine] = []
    rosters: list[tuple[date, list[Ref]]] = []
    grants: list[GrantLine] = []
    leavers: list[LeaverLine] = []
    approvals: list[ApprovalLine] = []
    section = ""
    for number, line in enumerate(lines[2:-1], start=3):
        head = _HEADS.get(line)
        roster = _ROSTER_HEAD.fullmatch(line)
        if head is not None:
            section = head
        elif roster:
            section = "roster"
            rosters.append((_day(roster.group(1), row, number), []))
        elif section == "systems" and (match := _SYSTEM_LINE.fullmatch(line)):
            systems.append(
                SystemLine(
                    Ref(match.group(2), match.group(1)), match.group(3) == "approval required"
                )
            )
        elif section == "roster" and (match := _PERSON_LINE.fullmatch(line)):
            rosters[-1][1].append(Ref(match.group(2), match.group(1)))
        elif section == "grants" and (match := _GRANT_LINE.fullmatch(line)):
            grants.append(
                GrantLine(
                    match.group(1),
                    Ref(match.group(3), match.group(2)),
                    Ref(match.group(5), match.group(4)),
                    match.group(6),
                    _day(match.group(7), row, number),
                )
            )
        elif section == "leavers" and (leaver := _leaver(line, row, number)):
            leavers.append(leaver)
        elif section == "approvals" and (approval := _approval(line, row, number)):
            approvals.append(approval)
        else:
            raise ParseError(row, number)
    return FileRecords(
        row,
        Ref(team.group(2), team.group(1)),
        tuple(systems),
        tuple(RosterLines(day, tuple(people)) for day, people in rosters),
        tuple(grants),
        tuple(leavers),
        tuple(approvals),
    )


def parse_files(files: Mapping[str, str]) -> Records:
    """Every corpus file, in the order of `files`; the row comes from the path.

    The index is not read here. A path that is neither the index nor a
    corpus path raises ParseError with row None and line 0.
    """
    parsed: list[FileRecords] = []
    for path, text in files.items():
        if path == INDEX_PATH:
            continue
        row = corpus_row(path)
        if row is None:
            raise ParseError(None, 0)
        parsed.append(parse_file(row, text))
    return Records(tuple(parsed))


def parse_notices(prompt: str) -> tuple[tuple[LeaverLine, ...], tuple[ApprovalLine, ...]]:
    """The records in the prompt: the lines after NOTICES_HEAD, to the next blank line."""
    lines = prompt.split("\n")
    if NOTICES_HEAD not in lines:
        raise ParseError(None, 0)
    start = lines.index(NOTICES_HEAD) + 1
    leavers: list[LeaverLine] = []
    approvals: list[ApprovalLine] = []
    for number, line in enumerate(lines[start:], start=start + 1):
        if not line:
            break
        if leaver := _leaver(line, None, number):
            leavers.append(leaver)
        elif approval := _approval(line, None, number):
            approvals.append(approval)
        else:
            raise ParseError(None, number)
    return tuple(leavers), tuple(approvals)


def with_notices(records: Records, prompt: str) -> Records:
    leavers, approvals = parse_notices(prompt)
    return replace(records, leavers=leavers, approvals=approvals)


def _left_by_roster(person: str, rosters: Sequence[RosterLines]) -> bool:
    """On a roster of the team and missing from a later one."""
    present = [
        any(ref.id == person for ref in roster.people)
        for roster in sorted(rosters, key=lambda roster: roster.date)
    ]
    return any(
        present[i] and not present[j]
        for i in range(len(present))
        for j in range(i + 1, len(present))
    )


def reasons(records: Records) -> dict[str, tuple[Evidence, ...]]:
    """Every grant id and why it is a violation; an empty tuple when it is not.

    A system's mark is read from the grant's own file only.
    """
    notices: dict[str, list[int | None]] = {}
    for file in records.files:
        for leaver in file.leavers:
            notices.setdefault(leaver.person.id, []).append(file.row)
    for leaver in records.leavers:
        notices.setdefault(leaver.person.id, []).append(None)
    prompt_approvals = {(record.person.id, record.system.id) for record in records.approvals}
    out: dict[str, tuple[Evidence, ...]] = {}
    for file in records.files:
        marks = {line.system.id: line.approval_required for line in file.systems}
        approved = {(record.person.id, record.system.id) for record in file.approvals}
        for grant in file.grants:
            found = [Evidence("notice", row) for row in notices.get(grant.person.id, [])]
            if _left_by_roster(grant.person.id, file.rosters):
                found.append(Evidence("roster", file.row))
            pair = (grant.person.id, grant.system.id)
            needed = marks.get(grant.system.id, False)
            if needed and pair not in approved and pair not in prompt_approvals:
                found.append(Evidence("approval", file.row))
            out[grant.id] = tuple(found)
    return out


def violations(records: Records) -> tuple[str, ...]:
    """The grant ids with any evidence, sorted."""
    return tuple(sorted(grant for grant, found in reasons(records).items() if found))


def answer_key(review: Review) -> Key:
    """Both keys, from the job's text alone."""
    files = parse_files(review.files)
    key_files = violations(files)
    key_full = violations(with_notices(files, review.prompt))
    in_files, in_full = set(key_files), set(key_full)
    return Key(
        key_files=key_files,
        key_full=key_full,
        made=tuple(grant for grant in key_full if grant not in in_files),
        cleared=tuple(grant for grant in key_files if grant not in in_full),
        reference_code=review.reference_code,
    )
