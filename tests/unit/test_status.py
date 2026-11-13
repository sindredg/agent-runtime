"""Job status transitions."""

from hypothesis import given
from hypothesis import strategies as st

from runtime.store.status import FINAL_STATUSES, TRANSITIONS, JobStatus, can_move, sources_of

S = JobStatus
statuses = st.sampled_from(list(JobStatus))

# Written out a second time on purpose: a change to the table has to be made
# in both places.
EXPECTED_MOVES = {
    (S.QUEUED, S.RUNNING),
    (S.QUEUED, S.CANCELLED),
    (S.QUEUED, S.FAILED),
    (S.RUNNING, S.WAITING),
    (S.RUNNING, S.NEEDS_APPROVAL),
    (S.RUNNING, S.SUCCEEDED),
    (S.RUNNING, S.FAILED),
    (S.RUNNING, S.CANCELLED),
    (S.WAITING, S.RUNNING),
    (S.WAITING, S.CANCELLED),
    (S.WAITING, S.FAILED),
    (S.NEEDS_APPROVAL, S.QUEUED),
    (S.NEEDS_APPROVAL, S.CANCELLED),
}


def test_transition_table_is_exactly_the_expected_moves() -> None:
    actual = {
        (current, new) for current in JobStatus for new in JobStatus if can_move(current, new)
    }
    assert actual == EXPECTED_MOVES


def test_final_statuses() -> None:
    assert {S.SUCCEEDED, S.FAILED, S.CANCELLED} == FINAL_STATUSES


def test_every_status_has_a_row_in_the_table() -> None:
    assert set(TRANSITIONS) == set(JobStatus)


@given(current=statuses, new=statuses)
def test_sources_of_is_the_inverse_of_can_move(current: JobStatus, new: JobStatus) -> None:
    assert (current in sources_of(new)) == can_move(current, new)


@given(start=statuses)
def test_every_status_can_reach_a_final_status(start: JobStatus) -> None:
    seen = {start}
    frontier = [start]
    while frontier:
        for new in TRANSITIONS[frontier.pop()]:
            if new not in seen:
                seen.add(new)
                frontier.append(new)
    assert seen & FINAL_STATUSES
