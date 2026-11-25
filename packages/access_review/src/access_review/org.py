"""The organisation: its model, how it is built from a seed, and the job it gives.

Everything is built in one fixed order from the streams of `rng`, so one
seed and one shape give one organisation.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from access_review.ids import IdDrawer, draw_code
from access_review.render import (
    FILE_BYTES,
    INDEX_PATH,
    MIN_CLOSING,
    SYSTEM,
    corpus_path,
    file_body,
    render_approval,
    render_file,
    render_grant,
    render_index,
    render_leaver,
    render_prompt,
)
from access_review.rng import DECIDED, LABELS, Shape, Source, Stream, draw_day, fill_label, rows

PERIOD_START = date(2026, 1, 1)
ROSTER_DATES = (date(2026, 3, 31), date(2026, 6, 30))
REVIEW_DATE = date(2026, 9, 30)
GRANTED_FIRST = date(2025, 1, 1)
GRANTED_LAST = date(2025, 12, 31)

SYSTEMS = 60
SYSTEMS_PER_FILE = 16
APPROVAL_SYSTEMS_PER_FILE = 5
PEOPLE_PER_TEAM = 32
BACKGROUND_LEAVERS = 3
ROLES = ("reader", "editor", "operator", "admin")
LOCAL = 12
CARRIED = 8

LATE_LEAVER = "late_leaver"
LATE_APPROVAL = "late_approval"
LOCAL_NOTICE = "local_notice"
LOCAL_ROSTER = "local_roster"
LOCAL_APPROVAL = "local_approval"
CARRIED_KIND = "carried"
KINDS = (LATE_LEAVER, LATE_APPROVAL, LOCAL_NOTICE, LOCAL_ROSTER, LOCAL_APPROVAL, CARRIED_KIND)
LATE_KINDS = (LATE_LEAVER, LATE_APPROVAL)
LOCAL_KINDS = (LOCAL_NOTICE, LOCAL_ROSTER, LOCAL_APPROVAL)

# Invented words. They share no word with each other, the roles, the
# templates or any fixed text of a job.
GIVEN = (
    "Fonul", "Banum", "Bavuth", "Badur", "Bakod", "Bazox", "Benok", "Bevist", "Fokom",
    "Bekisk", "Bezin", "Biveth", "Bikad", "Bizax", "Borust", "Bosusk", "Botun", "Bulom",
    "Buroth", "Bumor", "Dalik", "Damel", "Dasesk", "Fodun", "Delam", "Derath", "Dekux",
    "Dezuk", "Divol", "Didosk", "Dikon", "Dizim", "Donith", "Dovir", "Fozoth", "Dokex",
    "Dozek", "Dunast", "Duval", "Dudask", "Dutum", "Faluth", "Farur", "Famod",
)  # fmt: skip
FAMILY = (
    "Kanod", "Kavox", "Kazil", "Kenisk", "Kedem", "Keketh", "Kezer", "Kinad", "Kivax",
    "Kidak", "Kisust", "Kolusk", "Perost", "Komom", "Pilim", "Kusest", "Kutel", "Malesk",
    "Pavath", "Masath", "Peluk", "Mazud", "Menux", "Mevuk", "Medost", "Mekol", "Mezosk",
    "Mivim", "Modast", "Mokal", "Mozask", "Munan", "Murum", "Mumuth", "Musur", "Nalox",
    "Pirith", "Namist", "Nasil", "Natisk", "Nelin", "Nilax", "Nirak", "Nivust",
)  # fmt: skip
TEAM_WORDS = (
    "Radisk", "Reneth", "Rekax", "Rezak", "Rilust", "Rirul", "Risun", "Roloth", "Roror",
    "Rulest", "Rurel", "Rumesk", "Rusen", "Rutam", "Savud", "Sadux", "Sazost", "Senol",
    "Sevosk", "Sekim", "Sezith", "Sikek", "Sizast", "Sovask", "Sotuth", "Sulur", "Surod",
    "Sumox", "Susok", "Sutist", "Tarisk", "Teler", "Terad", "Tekust", "Tezul", "Tinusk",
    "Tivun", "Tidom", "Tikoth", "Tokest", "Tozel", "Tukath", "Tuzar", "Vamuk",
)  # fmt: skip
SYSTEM_WORDS = (
    "Branuk", "Bredir", "Brikask", "Brosod", "Brusisk", "Dratax", "Drekun", "Drikix",
    "Droken", "Drusux", "Krason", "Krelith", "Krisan", "Krodox", "Krudin", "Tradax",
    "Tremun", "Trimix", "Trosam", "Truduk", "Gradim", "Gredek", "Grimum", "Gromok",
    "Grumem", "Pramak", "Predoth", "Pridest", "Prulux", "Skalon", "Skerek", "Skinum",
    "Skonok", "Skunem", "Svanak", "Svelom", "Svilik", "Svolam", "Svozuk", "Svuzim",
    "Thazek", "Thetum", "Thitok", "Thuleth",
)  # fmt: skip


@dataclass(frozen=True)
class System:
    id: str
    name: str
    approval_required: bool


@dataclass(frozen=True)
class Team:
    id: str
    name: str


@dataclass(frozen=True)
class Person:
    id: str
    name: str
    team: Team
    last_day: date | None


@dataclass(frozen=True)
class Grant:
    id: str
    person: Person
    system: System
    role: str
    granted: date


@dataclass(frozen=True)
class Roster:
    team: Team
    date: date
    people: tuple[Person, ...]


@dataclass(frozen=True)
class Leaver:
    person: Person
    last_day: date
    template: int


@dataclass(frozen=True)
class Approval:
    person: Person
    system: System
    date: date
    template: int


@dataclass(frozen=True)
class CorpusFile:
    """What one team's file is rendered from, in the order it is written."""

    row: int
    team: Team
    systems: tuple[System, ...]
    rosters: tuple[Roster, ...]
    grants: tuple[Grant, ...]
    leavers: tuple[Leaver, ...]
    approvals: tuple[Approval, ...]


@dataclass(frozen=True)
class Org:
    systems: tuple[System, ...]
    teams: tuple[Team, ...]
    people: tuple[Person, ...]
    files: tuple[CorpusFile, ...]


@dataclass(frozen=True)
class Planted:
    """A grant placed on purpose, and the row of the file that decides it.

    `fact_row` is None for the late kinds, `row` for the local ones and
    `row - lead_rows` for `carried`.
    """

    kind: str
    grant: Grant
    row: int
    fact_row: int | None


@dataclass(frozen=True)
class Notice:
    """A record in the prompt, and its line."""

    record: Leaver | Approval
    text: str


@dataclass(frozen=True)
class Review:
    shape: Shape
    # The index first, then the corpus in index order.
    files: dict[str, str]
    system: str
    prompt: str
    notices: tuple[Notice, ...]
    reference_code: str
    org: Org
    planted: tuple[Planted, ...]


def _name(stream: Stream, words: Sequence[str]) -> str:
    first = stream.pick(words)
    second = stream.pick([word for word in words if word != first])
    return f"{first} {second}"


def _on_roster(person: Person, day: date) -> bool:
    return person.last_day is None or person.last_day >= day


def _placed(shape: Shape, placement: Stream) -> list[tuple[str, int, int | None]]:
    """Kind, row and fact row of every planted grant, in build order."""
    n, lead = shape.files, shape.lead_rows
    decided = rows(n, lead, DECIDED)
    late_approval = [run * 4 + placement.below(4) for run in range(DECIDED // 4)]
    out: list[tuple[str, int, int | None]] = [
        (LATE_APPROVAL if i in late_approval else LATE_LEAVER, row, None)
        for i, row in enumerate(decided)
    ]
    out += [(LOCAL_KINDS[i % 3], row, row) for i, row in enumerate(rows(n, 0, LOCAL))]
    out += [(CARRIED_KIND, row, row - lead) for row in rows(n, lead, CARRIED)]
    return out


def _last_day(kind: str, placement: Source) -> date | None:
    if kind in (LATE_APPROVAL, LOCAL_APPROVAL):
        return None
    if kind == LOCAL_ROSTER:
        # On the first roster only: from its date to the day before the second.
        return draw_day(placement, ROSTER_DATES[0], ROSTER_DATES[1] - timedelta(days=1))
    # On both rosters: after the last one, up to the review date.
    return draw_day(placement, ROSTER_DATES[-1] + timedelta(days=1), REVIEW_DATE)


def build(
    seed: bytes, shape: Shape
) -> tuple[Org, tuple[Planted, ...], tuple[Leaver | Approval, ...], str]:
    """The organisation, its planted grants, the prompt's records and the code."""
    n = shape.files
    streams = {label: Stream(seed, shape, label) for label in LABELS}

    systems_stream = streams["systems"]
    system_ids = IdDrawer(systems_stream, "SY", 6)
    systems = tuple(
        System(system_ids.next(), _name(systems_stream, SYSTEM_WORDS), i % 3 == 0)
        for i in range(SYSTEMS)
    )
    needs = [system for system in systems if system.approval_required]
    free = [system for system in systems if not system.approval_required]
    file_systems = [
        tuple(
            sorted(
                systems_stream.shuffle(needs)[:APPROVAL_SYSTEMS_PER_FILE]
                + systems_stream.shuffle(free)[: SYSTEMS_PER_FILE - APPROVAL_SYSTEMS_PER_FILE],
                key=lambda system: system.id,
            )
        )
        for _ in range(n)
    ]

    teams_stream = streams["teams"]
    team_ids = IdDrawer(teams_stream, "TM", 8)
    teams = tuple(Team(team_ids.next(), _name(teams_stream, TEAM_WORDS)) for _ in range(n))

    people_stream = streams["people"]
    person_ids = IdDrawer(people_stream, "PN", 8)
    drawn = [
        [
            (
                person_ids.next(),
                f"{people_stream.pick(GIVEN)} {people_stream.pick(FAMILY)}",
            )
            for _ in range(PEOPLE_PER_TEAM)
        ]
        for _ in range(n)
    ]

    placement = streams["placement"]
    placed = _placed(shape, placement)
    planted_at: list[list[int]] = [[] for _ in range(n)]
    for index, (_, row, _) in enumerate(placed):
        planted_at[row].append(index)

    # Per team: three background leavers, then one person per planted grant,
    # then the people who never left.
    people: list[list[Person]] = []
    background: list[list[Leaver]] = []
    planted_people: dict[int, Person] = {}
    clean: list[list[Person]] = []
    for row in range(n):
        team = teams[row]
        ids_names = drawn[row]
        leavers: list[Leaver] = []
        members: list[Person] = []
        for b in range(BACKGROUND_LEAVERS):
            if row == 0:
                day = (PERIOD_START, REVIEW_DATE, REVIEW_DATE)[b]
                template = b
            else:
                day = draw_day(placement, PERIOD_START, REVIEW_DATE)
                template = placement.below(3)
            person = Person(*ids_names[b], team, day)
            members.append(person)
            leavers.append(Leaver(person, day, template))
        for offset, index in enumerate(planted_at[row]):
            person = Person(
                *ids_names[BACKGROUND_LEAVERS + offset],
                team,
                _last_day(placed[index][0], placement),
            )
            members.append(person)
            planted_people[index] = person
        first_clean = BACKGROUND_LEAVERS + len(planted_at[row])
        never_left = [Person(*pair, team, None) for pair in ids_names[first_clean:]]
        members.extend(never_left)
        people.append(members)
        background.append(leavers)
        clean.append(never_left)

    grant_ids = IdDrawer(streams["grant ids"], "GR", 10, apart=True)
    file_grants: list[list[Grant]] = [[] for _ in range(n)]
    file_leavers: list[list[Leaver]] = [list(background[row]) for row in range(n)]
    late_records: list[Leaver | Approval] = []
    planted: list[Planted] = []
    for index, (kind, row, fact_row) in enumerate(placed):
        person = planted_people[index]
        needs_approval = kind in (LATE_APPROVAL, LOCAL_APPROVAL)
        system = placement.pick(
            [system for system in file_systems[row] if system.approval_required == needs_approval]
        )
        grant = Grant(
            grant_ids.next(),
            person,
            system,
            placement.pick(ROLES),
            draw_day(placement, GRANTED_FIRST, GRANTED_LAST),
        )
        file_grants[row].append(grant)
        planted.append(Planted(kind, grant, row, fact_row))
        template = placement.below(3)
        if kind == LATE_APPROVAL:
            day = draw_day(placement, PERIOD_START, REVIEW_DATE)
            late_records.append(Approval(person, system, day, template))
        elif person.last_day is not None and kind != LOCAL_ROSTER:
            record = Leaver(person, person.last_day, template)
            if kind == LATE_LEAVER:
                late_records.append(record)
            else:
                file_leavers[row if fact_row is None else fact_row].append(record)

    files = [
        _fill(
            row,
            n,
            teams[row],
            file_systems[row],
            people[row],
            clean[row],
            file_grants[row],
            file_leavers[row],
            Stream(seed, shape, fill_label(row)),
            grant_ids,
        )
        for row in range(n)
    ]
    order = streams["notices"].shuffle(late_records)
    code = draw_code(streams["code"])
    org = Org(
        systems,
        teams,
        tuple(person for members in people for person in members),
        tuple(files),
    )
    return org, tuple(planted), tuple(order), code


def _fill(
    row: int,
    n: int,
    team: Team,
    systems: tuple[System, ...],
    members: list[Person],
    never_left: list[Person],
    grants: list[Grant],
    leavers: list[Leaver],
    stream: Stream,
    grant_ids: IdDrawer,
) -> CorpusFile:
    """Add clean grants from the file's own stream until the file is full."""
    rosters = tuple(
        Roster(
            team,
            day,
            tuple(
                sorted(
                    (person for person in members if _on_roster(person, day)),
                    key=lambda person: person.id,
                )
            ),
        )
        for day in ROSTER_DATES
    )
    grants = list(grants)
    approvals: list[Approval] = []
    held: dict[str, list[str]] = {person.id: [] for person in never_left}

    def add(person: Person, system: System, approval: Approval | None) -> None:
        grants.append(
            Grant(
                grant_ids.next(),
                person,
                system,
                stream.pick(ROLES),
                draw_day(stream, GRANTED_FIRST, GRANTED_LAST),
            )
        )
        held[person.id].append(system.id)
        if approval is not None:
            approvals.append(approval)

    if row == 0:
        # Fixed records: every template of both kinds occurs in the files,
        # and their dates reach both ends of the period.
        needing = [system for system in systems if system.approval_required][:3]
        for template, system, day in zip(
            (0, 1, 2), needing, (PERIOD_START, REVIEW_DATE, REVIEW_DATE), strict=True
        ):
            person = stream.pick(never_left)
            add(person, system, Approval(person, system, day, template))

    draft = CorpusFile(row, team, systems, rosters, tuple(grants), tuple(leavers), tuple(approvals))
    used = sum(len(line) + 1 for line in file_body(draft, n))
    while True:
        open_people = [person for person in never_left if len(held[person.id]) < len(systems)]
        if not open_people:
            break
        person = stream.pick(open_people)
        system = stream.pick([system for system in systems if system.id not in held[person.id]])
        role = stream.pick(ROLES)
        granted = draw_day(stream, GRANTED_FIRST, GRANTED_LAST)
        approval = None
        if system.approval_required:
            approval = Approval(
                person, system, draw_day(stream, PERIOD_START, REVIEW_DATE), stream.below(3)
            )
        # Every grant id has the same length, so a stand-in measures the line.
        cost = len(render_grant(Grant("GR" + "0" * 10, person, system, role, granted))) + 1
        if approval is not None:
            cost += len(render_approval(approval)) + 1
        if used + cost + MIN_CLOSING > FILE_BYTES:
            break
        grants.append(Grant(grant_ids.next(), person, system, role, granted))
        held[person.id].append(system.id)
        if approval is not None:
            approvals.append(approval)
        used += cost
    return CorpusFile(
        row,
        team,
        systems,
        rosters,
        tuple(sorted(grants, key=lambda grant: grant.id)),
        tuple(sorted(leavers, key=lambda record: record.person.id)),
        tuple(sorted(approvals, key=lambda record: (record.person.id, record.system.id))),
    )


def generate(seed: bytes, shape: Shape) -> Review:
    """The job of one seed and one shape, with what it was built from."""
    org, planted, records, code = build(seed, shape)
    notices = tuple(
        Notice(
            record,
            render_leaver(record) if isinstance(record, Leaver) else render_approval(record),
        )
        for record in records
    )
    files = {INDEX_PATH: render_index(shape.files)}
    for file in org.files:
        files[corpus_path(file.row)] = render_file(file, shape.files)
    return Review(
        shape=shape,
        files=files,
        system=SYSTEM,
        prompt=render_prompt([notice.text for notice in notices], code),
        notices=notices,
        reference_code=code,
        org=org,
        planted=planted,
    )


def spec(review: Review, model: str) -> dict[str, Any]:
    """The job file of a review, as `POST /jobs` takes it."""
    return {
        "prompt": review.prompt,
        "system": review.system,
        "model": model,
        "files": dict(review.files),
    }
