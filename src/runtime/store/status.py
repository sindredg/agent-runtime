"""Job statuses and the moves between them. Pure logic, no database."""

from enum import StrEnum


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING = "waiting"
    NEEDS_APPROVAL = "needs_approval"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


_S = JobStatus

# Status -> the statuses it may move to. A final status moves nowhere.
TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    _S.QUEUED: frozenset({_S.RUNNING, _S.CANCELLED, _S.FAILED}),
    _S.RUNNING: frozenset({_S.WAITING, _S.NEEDS_APPROVAL, _S.SUCCEEDED, _S.FAILED, _S.CANCELLED}),
    _S.WAITING: frozenset({_S.RUNNING, _S.CANCELLED, _S.FAILED}),
    _S.NEEDS_APPROVAL: frozenset({_S.QUEUED, _S.CANCELLED}),
    _S.SUCCEEDED: frozenset(),
    _S.FAILED: frozenset(),
    _S.CANCELLED: frozenset(),
}

FINAL_STATUSES = frozenset(status for status, moves in TRANSITIONS.items() if not moves)


def can_move(current: JobStatus, new: JobStatus) -> bool:
    return new in TRANSITIONS[current]


def sources_of(new: JobStatus) -> frozenset[JobStatus]:
    """The statuses a job may be in just before it moves to `new`."""
    return frozenset(status for status, moves in TRANSITIONS.items() if new in moves)
