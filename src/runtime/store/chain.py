"""The step log's hash chain, recomputed apart from the database (ADR 0013).

Migration 0007 makes the chain: an insert trigger hashes each step and moves
the job's head. This module computes the same hashes in Python, from what the
database stores, and reports every step and head that does not match. It
shares no code with the trigger, so a fault in one is not repeated in the
other.

The payload is read as `payload::text`, the text Postgres hashed, and
`created_at` as whole microseconds, so no JSON or time zone formatting is
redone here.
"""

import hashlib
import struct
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

_STEPS = text(
    """
    SELECT seq, lease_epoch,
           (extract(epoch FROM created_at) * 1000000)::bigint AS created_us,
           kind, payload::text AS payload, hash
      FROM steps WHERE job_id = :job_id ORDER BY seq
    """
)
_HEAD = text("SELECT head_seq, head_hash FROM jobs WHERE id = :job_id")


def step_hash(
    prev: bytes,
    *,
    job_id: UUID,
    seq: int,
    lease_epoch: int,
    created_us: int,
    kind: str,
    payload: str,
) -> bytes:
    """SHA-256 over the previous hash and the step's fields, in the trigger's order.

    `prev` is the hash of the step before, or the job id's 16 bytes for the
    first step. Integers are big-endian and signed, as Postgres sends them.
    """
    kind_bytes = kind.encode()
    return hashlib.sha256(
        prev
        + job_id.bytes
        + struct.pack(">iiqi", seq, lease_epoch, created_us, len(kind_bytes))
        + kind_bytes
        + payload.encode()
    ).digest()


@dataclass(frozen=True, slots=True)
class Mismatch:
    """One thing in a job's log that the chain does not account for.

    `seq` is the step it was found at, or None for the head.
    """

    seq: int | None
    reason: str


class UnknownJob(LookupError):
    """There is no job with this id."""


async def verify_job(conn: AsyncConnection, job_id: UUID) -> list[Mismatch]:
    """Recompute the job's chain and compare it with what is stored. Empty means it holds.

    Each step is hashed from the stored hash of the step before it, so a
    changed step is named alone and the steps after it still verify. A step
    that is missing or moved shows as a gap in `seq` or a hash that differs.
    A log cut after its last step shows at the head.
    """
    head = (await conn.execute(_HEAD, {"job_id": job_id})).one_or_none()
    if head is None:
        raise UnknownJob(job_id)
    found: list[Mismatch] = []
    prev = job_id.bytes
    last_seq = 0
    for row in await conn.execute(_STEPS, {"job_id": job_id}):
        if row.seq != last_seq + 1:
            found.append(Mismatch(row.seq, f"follows step {last_seq}"))
        want = step_hash(
            prev,
            job_id=job_id,
            seq=row.seq,
            lease_epoch=row.lease_epoch,
            created_us=row.created_us,
            kind=row.kind,
            payload=row.payload,
        )
        if want != row.hash:
            found.append(Mismatch(row.seq, "hash differs"))
        prev = row.hash
        last_seq = row.seq
    if head.head_seq != last_seq:
        found.append(Mismatch(None, f"head is step {head.head_seq}, the log ends at {last_seq}"))
    elif head.head_hash != (prev if last_seq else None):
        found.append(Mismatch(None, "head hash differs from the last step's"))
    return found
