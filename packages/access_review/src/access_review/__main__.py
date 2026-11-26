"""`python -m access_review commitment`: print the job digest and the commitment of a seed.

Exit 2: the arguments, the seed file, the seed or the preimage path are
refused, with a fixed sentence on stderr. Exit 1: a check failed (its lines
on stderr) or something raised (its type name on stderr). Stdout is empty
unless the exit is 0.
"""

import argparse
import sys
from pathlib import Path
from typing import NoReturn

from access_review.checks import problems
from access_review.commit import (
    SeedError,
    commitment,
    inside_checkout,
    job_digest,
    preimage,
    read_seed,
)
from access_review.org import generate
from access_review.parse import answer_key
from access_review.rng import SHAPES, VERSION

USAGE = (
    "usage: python -m access_review commitment --shape {full,short} --seed-file PATH"
    " [--write-preimage PATH]"
)
BAD_ARGUMENTS = "The arguments are not valid."
IN_CHECKOUT = "The preimage path is inside a checkout."


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        # The default message quotes the argument; this one quotes nothing.
        self.exit(2, f"{BAD_ARGUMENTS}\n{USAGE}\n")


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="python -m access_review", usage=USAGE)
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("commitment", usage=USAGE)
    command.add_argument("--shape", required=True, choices=sorted(SHAPES))
    command.add_argument("--seed-file", required=True, type=Path)
    command.add_argument("--write-preimage", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.write_preimage is not None and inside_checkout(args.write_preimage):
        print(IN_CHECKOUT, file=sys.stderr)
        return 2
    try:
        seed = read_seed(args.seed_file)
    except SeedError as error:
        print(str(error), file=sys.stderr)
        return 2
    shape = SHAPES[args.shape]
    try:
        review = generate(seed, shape)
        found = problems(review)
        if found:
            print("\n".join(found), file=sys.stderr)
            return 1
        job = job_digest(review)
        key = answer_key(review)
        if args.write_preimage is not None:
            args.write_preimage.write_bytes(preimage(seed, shape, key, job))
        lines = (
            f"generator {VERSION}",
            f"shape {shape.name}",
            f"job {job}",
            f"commitment {commitment(seed, shape, key, job)}",
        )
    except Exception as error:
        # The type only: a message could quote the seed or a record.
        print(type(error).__name__, file=sys.stderr)
        return 1
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
