"""The step hash in Python: the recomputation that `runtime verify` relies on (ADR 0013).

The integration tests compare it with the trigger's hash. These check that
two different rows never give the same input.
"""

from uuid import UUID

import pytest

from runtime.store.chain import step_hash

JOB = UUID("00000000-0000-7000-8000-000000000001")
CREATED = 1_800_000_000_000_000


def hash_of(
    prev: bytes = JOB.bytes,
    *,
    job_id: UUID = JOB,
    seq: int = 1,
    lease_epoch: int = 1,
    created_us: int = CREATED,
    kind: str = "model_request",
    payload: str = "{}",
) -> bytes:
    return step_hash(
        prev,
        job_id=job_id,
        seq=seq,
        lease_epoch=lease_epoch,
        created_us=created_us,
        kind=kind,
        payload=payload,
    )


def test_the_hash_is_sha256_long_and_repeatable() -> None:
    assert len(hash_of()) == 32
    assert hash_of() == hash_of()


@pytest.mark.parametrize(
    "other",
    [
        hash_of(bytes(32)),
        hash_of(job_id=UUID("00000000-0000-7000-8000-000000000002")),
        hash_of(seq=2),
        hash_of(lease_epoch=2),
        hash_of(created_us=CREATED + 1),
        hash_of(kind="model_response"),
        hash_of(payload='{"a": 1}'),
    ],
)
def test_every_field_and_the_previous_hash_count(other: bytes) -> None:
    assert other != hash_of()


def test_moving_bytes_from_the_kind_to_the_payload_changes_the_hash() -> None:
    """The kind carries its length, so the boundary between it and the payload is fixed."""
    assert hash_of(kind="error", payload="{}") != hash_of(kind="erro", payload="r{}")


def test_values_are_packed_as_postgres_sends_them() -> None:
    """int4send and int8send: big-endian and signed. A time before 1970 is negative."""
    assert hash_of(created_us=-1) != hash_of(created_us=2**62)
    with pytest.raises(Exception, match="format requires"):
        hash_of(seq=2**31)
