"""A synthetic organisation for an access review, rendered as one job.

`generate` builds the job of a seed and a shape; `answer_key` reads its two
keys from the text; `problems` runs the checks; `commitment` hashes the seed,
the keys and the digest of the job.
"""

from access_review.checks import problems
from access_review.commit import commitment, job_digest, parse_seed, preimage, read_seed
from access_review.ids import GRANT_ID, REFERENCE_CODE, IdDrawer
from access_review.org import Planted, Review, generate, spec
from access_review.parse import (
    Key,
    ParseError,
    answer_key,
    parse_files,
    parse_notices,
    reasons,
    violations,
)
from access_review.render import SYSTEM, TITLE_FORMAT, WHEN_FOUND, render_approval, render_leaver
from access_review.rng import FULL, SHORT, VERSION, Shape, Stream

__all__ = [
    "FULL",
    "GRANT_ID",
    "REFERENCE_CODE",
    "SHORT",
    "SYSTEM",
    "TITLE_FORMAT",
    "VERSION",
    "WHEN_FOUND",
    "IdDrawer",
    "Key",
    "ParseError",
    "Planted",
    "Review",
    "Shape",
    "Stream",
    "answer_key",
    "commitment",
    "generate",
    "job_digest",
    "parse_files",
    "parse_notices",
    "parse_seed",
    "preimage",
    "problems",
    "read_seed",
    "reasons",
    "render_approval",
    "render_leaver",
    "spec",
    "violations",
]
