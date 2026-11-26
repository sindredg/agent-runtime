"""The checks a seed's job is put through before its hash is printed.

Each check returns lines of the form `<check>.<clause>: <counts>`. A line
holds counts only: never an id, a name, a notice or the code, so the lines
of a secret seed can be shown.
"""

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from access_review.ids import REFERENCE_CODE, wildcard_forms
from access_review.org import (
    CARRIED_KIND,
    LATE_APPROVAL,
    LATE_KINDS,
    LATE_LEAVER,
    LOCAL_APPROVAL,
    LOCAL_KINDS,
    LOCAL_NOTICE,
    LOCAL_ROSTER,
    Leaver,
    Review,
)
from access_review.parse import (
    Evidence,
    FileRecords,
    ParseError,
    Records,
    corpus_row,
    parse_files,
    reasons,
    violations,
    with_notices,
)
from access_review.render import (
    APPROVAL_TEMPLATES,
    CODE_SENTENCE,
    FILE_BYTES,
    INDEX_PATH,
    LEAVER_TEMPLATES,
    SYSTEM,
    TITLE_FORMAT,
    WHEN_FOUND,
    mask,
    render_approval,
    render_leaver,
)
from access_review.rng import DECIDED, rows

INDEX_LIMIT = 65_536
_PATH_TEXT = re.compile(r"access/[0-9]{4}\.txt")
_TOKEN = re.compile(r"[A-Za-z0-9]+")
# Every two tokens with one space between them, overlapping.
_PAIR = re.compile(r"(?<![A-Za-z0-9])(?=([A-Za-z0-9]+ [A-Za-z0-9]+)(?![A-Za-z0-9]))")


@dataclass(frozen=True)
class Parsed:
    """The records of the files alone, and of the files plus the prompt."""

    files: Records
    full: Records


def parse_all(review: Review) -> Parsed:
    files = parse_files(review.files)
    return Parsed(files, with_notices(files, review.prompt))


def _files(review: Review, parsed: Parsed | None) -> Records:
    """The files' records; the prompt is not read."""
    return parse_files(review.files) if parsed is None else parsed.files


def _line(name: str, *counts: int) -> str:
    return f"{name}: {' '.join(str(count) for count in counts)}"


def _corpus(review: Review) -> list[tuple[str, str]]:
    return [(path, text) for path, text in review.files.items() if path != INDEX_PATH]


def _ids(review: Review, kinds: Sequence[str]) -> list[str]:
    return [planted.grant.id for planted in review.planted if planted.kind in kinds]


def _rows_holding(review: Review, grant: str) -> list[int]:
    """The row of each corpus file that holds `grant`, once per occurrence."""
    return [
        row
        for path, text in _corpus(review)
        if (row := corpus_row(path)) is not None
        for _ in range(text.count(grant))
    ]


def _mismatch(found: Sequence[int], wanted: Sequence[int]) -> int:
    return sum(a != b for a, b in zip(found, wanted, strict=False)) + abs(len(found) - len(wanted))


def _tokens(text: str) -> list[str]:
    return _TOKEN.findall(text)


def _pairs(text: str) -> list[str]:
    return [match.group(1) for match in _PAIR.finditer(text)]


def _index_rows(review: Review) -> list[str]:
    return review.files.get(INDEX_PATH, "").split("\n")[:-1]


def parse(review: Review) -> list[str]:
    try:
        parse_all(review)
    except ParseError:
        return [_line("parse.line", 1)]
    return []


def _file_view(file: FileRecords) -> tuple[Any, ...]:
    return (
        (file.team.id, file.team.name),
        tuple(
            ((line.system.id, line.system.name), line.approval_required) for line in file.systems
        ),
        tuple(
            (roster.date, tuple((ref.id, ref.name) for ref in roster.people))
            for roster in file.rosters
        ),
        tuple(
            (g.id, (g.person.id, g.person.name), (g.system.id, g.system.name), g.role, g.granted)
            for g in file.grants
        ),
        tuple(((r.person.id, r.person.name), r.last_day, r.template) for r in file.leavers),
        tuple(
            ((r.person.id, r.person.name), (r.system.id, r.system.name), r.date, r.template)
            for r in file.approvals
        ),
    )


def round_trip(review: Review, parsed: Parsed | None = None) -> list[str]:
    """`records`: the files read back are what they were rendered from. `key`."""
    records = _files(review, parsed)
    found = {file.row: _file_view(file) for file in records.files}
    differ = 0
    for file in review.org.files:
        wanted = (
            (file.team.id, file.team.name),
            tuple(((s.id, s.name), s.approval_required) for s in file.systems),
            tuple(
                (roster.date, tuple((p.id, p.name) for p in roster.people))
                for roster in file.rosters
            ),
            tuple(
                (
                    g.id,
                    (g.person.id, g.person.name),
                    (g.system.id, g.system.name),
                    g.role,
                    g.granted,
                )
                for g in file.grants
            ),
            tuple(((r.person.id, r.person.name), r.last_day, r.template) for r in file.leavers),
            tuple(
                ((r.person.id, r.person.name), (r.system.id, r.system.name), r.date, r.template)
                for r in file.approvals
            ),
        )
        differ += found.pop(file.row, None) != wanted
    differ += len(found)
    out = [_line("round_trip.records", differ)] if differ else []
    key_files = set(violations(records))
    wanted_key = set(_ids(review, (*LOCAL_KINDS, CARRIED_KIND, LATE_APPROVAL)))
    if key_files != wanted_key:
        out.append(_line("round_trip.key", len(key_files ^ wanted_key)))
    return out


def keys_differ(review: Review, parsed: Parsed | None = None) -> list[str]:
    """The prompt's records make the 12 late leavers and clear the 4 late approvals."""
    parsed = parsed or parse_all(review)
    key_files, key_full = violations(parsed.files), violations(parsed.full)
    made = [grant for grant in key_full if grant not in key_files]
    cleared = [grant for grant in key_files if grant not in key_full]
    if made != sorted(_ids(review, (LATE_LEAVER,))) or cleared != sorted(
        _ids(review, (LATE_APPROVAL,))
    ):
        return [_line("keys_differ.differ", len(made), len(cleared))]
    return []


def expected_evidence(review: Review, full: bool) -> dict[str, tuple[Evidence, ...]]:
    """What each planted grant is decided by; every other grant has none."""
    out: dict[str, tuple[Evidence, ...]] = {}
    for planted in review.planted:
        kind, row = planted.kind, planted.row
        found: tuple[Evidence, ...]
        if kind == LATE_LEAVER:
            found = (Evidence("notice", None),) if full else ()
        elif kind == LATE_APPROVAL:
            found = () if full else (Evidence("approval", row),)
        elif kind == LOCAL_APPROVAL:
            found = (Evidence("approval", row),)
        elif kind == LOCAL_NOTICE:
            found = (Evidence("notice", row),)
        elif kind == LOCAL_ROSTER:
            found = (Evidence("roster", row),)
        else:
            found = (Evidence("notice", planted.fact_row),)
        out[planted.grant.id] = found
    return out


def one_reason(review: Review, parsed: Parsed | None = None) -> list[str]:
    """`reason`: each grant has exactly its expected evidence, over both record sets."""
    parsed = parsed or parse_all(review)
    counts = []
    for records, full in ((parsed.files, False), (parsed.full, True)):
        wanted = expected_evidence(review, full)
        got = reasons(records)
        counts.append(sum(got[grant] != wanted.get(grant, ()) for grant in got))
        counts[-1] += sum(grant not in got for grant in wanted)
    return [_line("one_reason.reason", *counts)] if any(counts) else []


def rosters(review: Review, parsed: Parsed | None = None) -> list[str]:
    """`presence`: a file's leaver is on each roster of their team's own file
    dated on or before their last day, and on none after it."""
    files = _files(review, parsed).files
    team_file = {file.team.id: file for file in files}
    person_team = {person.id: person.team.id for person in review.org.people}
    wrong = 0
    for file in files:
        for leaver in file.leavers:
            own = team_file.get(person_team.get(leaver.person.id, ""))
            if own is None:
                wrong += 1
                continue
            for roster in own.rosters:
                listed = sum(ref.id == leaver.person.id for ref in roster.people)
                wrong += listed != (1 if leaver.last_day >= roster.date else 0)
    return [_line("rosters.presence", wrong)] if wrong else []


def decided_once(review: Review, parsed: Parsed | None = None) -> list[str]:
    """`once`: each decided id is once in the corpus. `elsewhere`: in no notice,
    index row or `system`."""
    decided = _ids(review, LATE_KINDS)
    texts = [text for _, text in _corpus(review)]
    out = []
    not_once = sum(sum(text.count(grant) for text in texts) != 1 for grant in decided)
    if not_once:
        out.append(_line("decided_once.once", not_once))
    other = [notice.text for notice in review.notices]
    other += [review.prompt, review.system, *_index_rows(review)]
    elsewhere = sum(any(grant in text for text in other) for grant in decided)
    if elsewhere:
        out.append(_line("decided_once.elsewhere", elsewhere))
    return out


def decided_rows(review: Review, parsed: Parsed | None = None) -> list[str]:
    """`formula`: the files holding the decided ids, found in the text, are at
    `rows(n, lead_rows, 16)`."""
    shape = review.shape
    found = sorted(
        row for grant in _ids(review, LATE_KINDS) for row in _rows_holding(review, grant)
    )
    wrong = _mismatch(found, rows(shape.files, shape.lead_rows, DECIDED))
    return [_line("decided_rows.formula", wrong)] if wrong else []


def no_pointers(review: Review, parsed: Parsed | None = None) -> list[str]:
    """`notice`: no notice holds a corpus path, a team id or a team name.
    `index`: no index row holds an id or a name of a person, grant, team or system."""
    org = review.org
    team_ids = {team.id for team in org.teams}
    team_names = {team.name for team in org.teams}
    out = []
    pointing = sum(
        bool(_PATH_TEXT.search(notice.text))
        or any(token in team_ids for token in _tokens(notice.text))
        or any(pair in team_names for pair in _pairs(notice.text))
        for notice in review.notices
    )
    if pointing:
        out.append(_line("no_pointers.notice", pointing))
    grants = [grant for file in org.files for grant in file.grants]
    kinds = (
        ({person.id for person in org.people}, {person.name for person in org.people}),
        ({grant.id for grant in grants}, set[str]()),
        (team_ids, team_names),
        ({system.id for system in org.systems}, {system.name for system in org.systems}),
    )
    holding = 0
    for row in _index_rows(review):
        tokens, pairs = _tokens(row), _pairs(row)
        holding += any(
            any(token in ids for token in tokens) or any(pair in names for pair in pairs)
            for ids, names in kinds
        )
    if holding:
        out.append(_line("no_pointers.index", holding))
    return out


def notice_form(
    review: Review,
    leaver_templates: Sequence[str] = LEAVER_TEMPLATES,
    approval_templates: Sequence[str] = APPROVAL_TEMPLATES,
    parsed: Parsed | None = None,
) -> list[str]:
    """`same_function`, `known_template` and `date_in_range`, read from `review.notices`."""
    files = _files(review, parsed).files
    leavers = [record for file in files for record in file.leavers]
    approvals = [record for file in files for record in file.approvals]
    masks = {
        "leaver": {mask(record.text) for record in leavers},
        "approval": {mask(record.text) for record in approvals},
    }
    days = {
        "leaver": [record.last_day for record in leavers],
        "approval": [record.date for record in approvals],
    }
    prompt_lines = review.prompt.split("\n")
    same = known = in_range = 0
    for notice in review.notices:
        record = notice.record
        kind = "leaver" if isinstance(record, Leaver) else "approval"
        if isinstance(record, Leaver):
            text, day = render_leaver(record, leaver_templates), record.last_day
        else:
            text, day = render_approval(record, approval_templates), record.date
        same += notice.text != text or notice.text not in prompt_lines
        known += mask(notice.text) not in masks[kind]
        kind_days = days[kind]
        in_range += not kind_days or not min(kind_days) <= day <= max(kind_days)
    out = []
    for clause, count in (("same_function", same), ("known_template", known)):
        if count:
            out.append(_line(f"notice_form.{clause}", count))
    if in_range:
        out.append(_line("notice_form.date_in_range", in_range))
    return out


def code(review: Review, parsed: Parsed | None = None) -> list[str]:
    """`form`, `value`, `shape` and `in_prompt` of the reference code."""
    value = review.reference_code
    places = [*review.files.values(), review.system]
    out = []
    if not REFERENCE_CODE.fullmatch(value):
        out.append(_line("code.form", 1))
    holding = sum(value in text or value.replace("-", "") in text for text in places)
    if holding:
        out.append(_line("code.value", holding))
    shaped = sum(bool(REFERENCE_CODE.search(text)) for text in places)
    if shaped:
        out.append(_line("code.shape", shaped))
    last = review.prompt.split("\n\n")[-1]
    if review.prompt.count(value) != 1 or last != CODE_SENTENCE.format(code=value):
        out.append(_line("code.in_prompt", review.prompt.count(value)))
    return out


def system_text(review: Review, parsed: Parsed | None = None) -> list[str]:
    """`equal`: `system` is SYSTEM."""
    return [] if review.system == SYSTEM else [_line("system_text.equal", 1)]


def system(review: Review, parsed: Parsed | None = None) -> list[str]:
    """`holds`: `system` names the index path and holds the title format."""
    missing = (INDEX_PATH not in review.system) + (TITLE_FORMAT not in review.system)
    return [_line("system.holds", missing)] if missing else []


def texts_clean(review: Review, texts: Sequence[str]) -> list[str]:
    """`clean`: no text holds a notice line, a decided id or the code."""
    value = review.reference_code
    needles = [notice.text for notice in review.notices]
    needles += [*_ids(review, LATE_KINDS), value, value.replace("-", "")]
    dirty = sum(any(needle in text for needle in needles) for text in texts)
    return [_line("texts_clean.clean", dirty)] if dirty else []


def file_decided(review: Review, parsed: Parsed | None = None) -> list[str]:
    """`local_rows`, `carried_distance` and `when_found`."""
    files = _files(review, parsed).files
    shape = review.shape
    out = []
    found = sorted(
        row for grant in _ids(review, LOCAL_KINDS) for row in _rows_holding(review, grant)
    )
    wrong = _mismatch(found, rows(shape.files, 0, len(_ids(review, LOCAL_KINDS))))
    if wrong:
        out.append(_line("file_decided.local_rows", wrong))
    grant_rows = {grant.id: file.row for file in files for grant in file.grants}
    notice_rows: dict[str, list[int]] = {}
    for file in files:
        for leaver in file.leavers:
            notice_rows.setdefault(leaver.person.id, []).append(file.row)
    away = 0
    for planted in review.planted:
        if planted.kind == CARRIED_KIND:
            row = grant_rows.get(planted.grant.id)
            at = notice_rows.get(planted.grant.person.id, [])
            away += row is None or not at or any(row - notice != shape.lead_rows for notice in at)
    if away:
        out.append(_line("file_decided.carried_distance", away))
    if WHEN_FOUND not in review.system:
        out.append(_line("file_decided.when_found", 1))
    return out


def sizes(
    review: Review, index_limit: int = INDEX_LIMIT, parsed: Parsed | None = None
) -> list[str]:
    """`count`, `bytes`, `ascii`, `index_bytes`, `index_rows` and `distinct`."""
    corpus = _corpus(review)
    index = review.files.get(INDEX_PATH, "")
    out = []
    checks = (
        ("count", abs(len(corpus) - review.shape.files)),
        ("bytes", sum(len(text.encode()) != FILE_BYTES for _, text in corpus)),
        ("ascii", sum(not text.isascii() for text in review.files.values())),
        ("index_bytes", int(len(index.encode()) > index_limit)),
        ("index_rows", _mismatch_paths(_index_rows(review), [path for path, _ in corpus])),
        ("distinct", len(corpus) - len({text for _, text in corpus})),
    )
    for clause, count in checks:
        if count:
            out.append(_line(f"sizes.{clause}", count))
    return out


def _mismatch_paths(found: list[str], wanted: list[str]) -> int:
    repeated = len(found) - len(set(found))
    return (
        repeated
        + sum(a != b for a, b in zip(found, wanted, strict=False))
        + abs(len(found) - len(wanted))
    )


def ids(review: Review, parsed: Parsed | None = None) -> list[str]:
    """`unique` and `apart`, read from the parsed files."""
    files = _files(review, parsed).files
    grant_lines: dict[str, int] = {}
    person_files: dict[str, list[int]] = {}
    person_names: dict[str, list[str]] = {}
    system_names: dict[str, list[str]] = {}
    system_marks: dict[str, list[bool]] = {}
    team_files: dict[str, int] = {}
    for file in files:
        team_files[file.team.id] = team_files.get(file.team.id, 0) + 1
        for line in file.systems:
            system_names.setdefault(line.system.id, []).append(line.system.name)
            system_marks.setdefault(line.system.id, []).append(line.approval_required)
        listed = [ref for roster in file.rosters for ref in roster.people]
        listed += [grant.person for grant in file.grants]
        for ref in listed:
            rows_of = person_files.setdefault(ref.id, [])
            if file.row not in rows_of:
                rows_of.append(file.row)
        named = listed + [record.person for record in file.leavers]
        named += [record.person for record in file.approvals]
        for ref in named:
            person_names.setdefault(ref.id, []).append(ref.name)
        for grant in file.grants:
            grant_lines[grant.id] = grant_lines.get(grant.id, 0) + 1
            system_names.setdefault(grant.system.id, []).append(grant.system.name)
        for record in file.approvals:
            system_names.setdefault(record.system.id, []).append(record.system.name)
    not_unique = (
        sum(count != 1 for count in grant_lines.values())
        + sum(len(found) != 1 for found in person_files.values())
        + sum(len(set(names)) != 1 for names in person_names.values())
        + sum(len(set(names)) != 1 for names in system_names.values())
        + sum(len(set(marks)) != 1 for marks in system_marks.values())
        + sum(count != 1 for count in team_files.values())
    )
    out = [_line("ids.unique", not_unique)] if not_unique else []
    forms: dict[str, list[str]] = {}
    for grant_id in grant_lines:
        for form in wildcard_forms(grant_id):
            forms.setdefault(form, []).append(grant_id)
    near = sum(len(found) > 1 for found in forms.values())
    if near:
        out.append(_line("ids.apart", near))
    return out


def problems(review: Review) -> list[str]:
    """Every check, in a fixed order. A file that does not parse ends the list."""
    try:
        parsed = parse_all(review)
    except ParseError:
        return [_line("parse.line", 1)]
    checks: tuple[Callable[[Review, Parsed], list[str]], ...] = (
        round_trip,
        keys_differ,
        one_reason,
        rosters,
        decided_once,
        decided_rows,
        no_pointers,
    )
    out = [line for check in checks for line in check(review, parsed)]
    out += notice_form(review, LEAVER_TEMPLATES, APPROVAL_TEMPLATES, parsed)
    out += code(review) + system_text(review) + system(review)
    out += texts_clean(review, (review.system,))
    out += file_decided(review, parsed) + sizes(review) + ids(review, parsed)
    return out
