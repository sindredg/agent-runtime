"""The seed, the digest of a job, and the hash commitment to a seed and its keys.

No error text here quotes the seed or the seed file.
"""

import hashlib
import json
from pathlib import Path
from typing import Any

from access_review.org import Review
from access_review.parse import Key
from access_review.rng import VERSION, Shape

MIN_SEED_DIGITS = 32
_HEX = frozenset("0123456789abcdef")

NOT_READ = "The seed file cannot be read."
NOT_JSON = "The seed file is not JSON."
NO_SEED = "The seed file has no seed string."
BAD_SEED = "The seed is not an even number of hex digits, at least 32."


class SeedError(ValueError):
    """A seed or a seed file that is refused. The message is one of the fixed sentences."""


def parse_seed(hex_text: str) -> bytes:
    """The seed's bytes. White space around it and capitals are allowed."""
    text = hex_text.strip().lower()
    if len(text) < MIN_SEED_DIGITS or len(text) % 2 or not all(c in _HEX for c in text):
        raise SeedError(BAD_SEED)
    return bytes.fromhex(text)


def read_seed(path: Path) -> bytes:
    """The seed of a JSON file with a string `seed`; other keys are ignored."""
    try:
        raw = path.read_bytes()
    except OSError:
        raise SeedError(NOT_READ) from None
    try:
        data = json.loads(raw)
    except ValueError:
        raise SeedError(NOT_JSON) from None
    if not isinstance(data, dict) or not isinstance(data.get("seed"), str):
        raise SeedError(NO_SEED)
    return parse_seed(data["seed"])


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("ascii")


def job_digest(review: Review) -> str:
    """The SHA-256 of what the job is sent: its files, prompt and `system`."""
    job = {"files": review.files, "prompt": review.prompt, "system": review.system}
    return "sha256:" + hashlib.sha256(_canonical(job)).hexdigest()


def preimage(seed: bytes, shape: Shape, key: Key, job: str) -> bytes:
    """The bytes the commitment hashes."""
    return _canonical(
        {
            "generator": VERSION,
            "job": job,
            "key": {
                "cleared": sorted(key.cleared),
                "key_files": sorted(key.key_files),
                "key_full": sorted(key.key_full),
                "made": sorted(key.made),
                "reference_code": key.reference_code,
            },
            "seed": seed.hex(),
            "shape": shape.name,
        }
    )


def commitment(seed: bytes, shape: Shape, key: Key, job: str) -> str:
    return "sha256:" + hashlib.sha256(preimage(seed, shape, key, job)).hexdigest()


def inside_checkout(path: Path) -> bool:
    """Whether `path`, or a directory above it, holds `.git` or is `.git`.

    A worktree's `.git` is a file, so any `.git` counts.
    """
    resolved = path.resolve()
    if ".git" in resolved.parts:
        return True
    return any((directory / ".git").exists() for directory in (resolved, *resolved.parents))
