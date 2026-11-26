"""Say whether a pull request changes anything outside `docs/`, for CI's stack jobs.

Reads the changed paths, one per line, as `git diff --name-only --no-renames`
prints them, and prints `code=true` or `code=false` for `$GITHUB_OUTPUT`.
Only a list of one or more paths, every one under `docs/`, gives false. An
empty list, a name git quoted, or any other path gives true: a doubt runs
every job. `README.md` is not docs here: the image and the package read it.
"""

import sys

DOCS = "docs/"


def code(paths: list[str]) -> bool:
    """True unless there is a path and every path is under `docs/`."""
    return not paths or not all(path.startswith(DOCS) for path in paths)


def main() -> int:
    paths = [line for line in sys.stdin.read().splitlines() if line]
    print(f"code={'true' if code(paths) else 'false'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
