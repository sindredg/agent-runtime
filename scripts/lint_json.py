"""Check that each file given on the command line is valid JSON.

Stricter than Python's parser in two ways. An object that repeats a key is an
error, because a parser keeps only the last value and the mistake goes
unseen. NaN and Infinity are errors, because they are not JSON.
"""

import json
import sys
from pathlib import Path
from typing import Any


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [key for key, _ in pairs]
    for key in keys:
        if keys.count(key) > 1:
            raise ValueError(f"duplicate key {key!r}")
    return dict(pairs)


def _not_json(constant: str) -> None:
    raise ValueError(f"{constant} is not JSON")


def check(path: Path) -> str | None:
    """Return the problem with the file, or None if it is valid."""
    try:
        json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_no_duplicate_keys,
            parse_constant=_not_json,
        )
    except ValueError as error:
        return str(error)
    return None


def main(arguments: list[str]) -> int:
    failed = False
    for name in arguments:
        problem = check(Path(name))
        if problem is not None:
            print(f"{name}: {problem}", file=sys.stderr)
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
