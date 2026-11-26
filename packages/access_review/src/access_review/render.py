"""The text of a job: the corpus files, the index, `system` and the prompt.

`render_leaver` and `render_approval` are the only producers of a leaver
notice or an approval, in a file and in the prompt alike.
"""

import re
from collections.abc import Sequence
from typing import TYPE_CHECKING

from access_review.ids import PERSON_ID, SYSTEM_ID

if TYPE_CHECKING:
    from access_review.org import Approval, CorpusFile, Grant, Leaver, Person, System, Team

# Each corpus file is exactly this many bytes.
FILE_BYTES = 16_384
# The shortest closing line: `[end] =` and the line end.
MIN_CLOSING = 8
INDEX_PATH = "index.txt"

# `{person}`, `{system}` and `{date}` are filled in by the render functions.
LEAVER_TEMPLATES = (
    "Leaver notice: {person} left the company on {date}.",
    "The last working day of {person} was {date}.",
    "Exit recorded for {person}, effective {date}.",
)
APPROVAL_TEMPLATES = (
    "Approval: the owner of {system} approved access for {person} on {date}.",
    "On {date} the owner of {system} signed off access to it for {person}.",
    "Access for {person} to {system} was confirmed by the owner of that system, {date}.",
)

TITLE_FORMAT = "Start every issue title with the id of the grant, then a colon and a few words."
WHEN_FOUND = "Open each issue when you find it."
SYSTEM = "\n".join(
    (f"The file {INDEX_PATH} lists the files of this job.", TITLE_FORMAT, WHEN_FOUND)
)

TASK = (
    f"Review the access records. Read {INDEX_PATH}, then read every file it lists, in the"
    " order listed. Open one issue for each grant that is a violation."
)
RULES = "\n".join(
    (
        "Rules:",
        "1. A grant is a violation when its holder has left. A person has left when a leaver"
        " notice names them, in any file, or when they are on a roster of their team and"
        " missing from a later roster of that team.",
        '2. A grant on a system marked "approval required" is a violation unless an approval'
        " names its holder and that system. An approval is in the file of its grant.",
        "3. Nothing else is a violation.",
    )
)
NOTICES_HEAD = (
    "These records arrived after the files were written. They count like records in the files:"
)
CODE_SENTENCE = "Put the reference code {code} in the title of every issue."

MARKS = {True: "approval required", False: "no approval"}
_NAME = "[A-Z][a-z]+ [A-Z][a-z]+"
_PERSON = re.compile(rf"{_NAME} \({PERSON_ID.pattern}\)")
_SYSTEM = re.compile(rf"{_NAME} \({SYSTEM_ID.pattern}\)")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def corpus_path(row: int) -> str:
    return f"access/{row + 1:04d}.txt"


def person_text(person: Person) -> str:
    return f"{person.name} ({person.id})"


def system_text(system: System) -> str:
    return f"{system.name} ({system.id})"


def team_text(team: Team) -> str:
    return f"{team.name} ({team.id})"


def render_leaver(record: Leaver, templates: Sequence[str] = LEAVER_TEMPLATES) -> str:
    return templates[record.template].format(
        person=person_text(record.person), date=record.last_day.isoformat()
    )


def render_approval(record: Approval, templates: Sequence[str] = APPROVAL_TEMPLATES) -> str:
    return templates[record.template].format(
        person=person_text(record.person),
        system=system_text(record.system),
        date=record.date.isoformat(),
    )


def render_grant(grant: Grant) -> str:
    return (
        f"{grant.id}: {person_text(grant.person)}, {system_text(grant.system)},"
        f" {grant.role}, granted {grant.granted.isoformat()}"
    )


def mask(text: str) -> str:
    """`text` with each person, system and date replaced by a placeholder."""
    text = _PERSON.sub("<person>", text)
    text = _SYSTEM.sub("<system>", text)
    return _DATE.sub("<date>", text)


def file_body(file: CorpusFile, total: int) -> list[str]:
    """The lines of a corpus file before its closing line."""
    lines = [
        f"access records, file {file.row + 1:04d} of {total:04d}",
        f"team: {team_text(file.team)}",
        "[systems]",
        *(f"{system_text(system)}: {MARKS[system.approval_required]}" for system in file.systems),
    ]
    for roster in file.rosters:
        lines.append(f"[roster {roster.date.isoformat()}]")
        lines.extend(person_text(person) for person in roster.people)
    lines.append("[grants]")
    lines.extend(render_grant(grant) for grant in file.grants)
    lines.append("[leaver notices]")
    lines.extend(render_leaver(record) for record in file.leavers)
    lines.append("[approvals]")
    lines.extend(render_approval(record) for record in file.approvals)
    return lines


def closing_line(used: int) -> str:
    """The last line, `[end] ` and `=` up to the last byte but one."""
    if FILE_BYTES - used < MIN_CLOSING:
        raise ValueError("the file is too full for its closing line")
    return "[end] " + "=" * (FILE_BYTES - used - 7) + "\n"


def render_file(file: CorpusFile, total: int) -> str:
    body = "".join(line + "\n" for line in file_body(file, total))
    return body + closing_line(len(body.encode()))


def render_index(total: int) -> str:
    return "".join(corpus_path(row) + "\n" for row in range(total))


def render_prompt(notice_lines: Sequence[str], code: str) -> str:
    notices = "\n".join((NOTICES_HEAD, *notice_lines))
    return "\n\n".join((TASK, RULES, notices, CODE_SENTENCE.format(code=code)))
