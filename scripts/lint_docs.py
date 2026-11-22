"""Check that each Markdown file given on the command line can be skimmed.

A page fails when it is too long, when a paragraph, list item or table cell
is too long, when it has no diagram, when a link inside the repository leads
nowhere, when a link to a line does not name a commit, or when it has an em
dash. The limits are the constants below.

Decision records that were accepted before the limits keep their text, so
only their length, links and diagram are checked.
"""

import re
import sys
from collections.abc import Iterator
from pathlib import Path

MAX_LINES = 150
# Four lines of 80 characters, counted without link targets.
MAX_BLOCK = 320
MAX_CELL = 160
MAX_CELL_SENTENCES = 2

# Accepted before the limits: the text stays as it was written.
KEPT_TEXT = re.compile(r"docs/adr/00(0[1-9]|1[0-2])-[^/]*\.md$")
# Records of a choice with no mechanism to draw.
NO_DIAGRAM = re.compile(r"docs/adr/000[17]-[^/]*\.md$")

_FENCE = re.compile(r"^\s*```")
_ITEM = re.compile(r"^\s*([-*]|\d+\.)\s+")
_CODE = re.compile(r"`[^`]*`")
_TARGET = re.compile(r"\]\([^)]*\)")
_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
_SVG_IMAGE = re.compile(r"^!\[[^\]]*\]\(([^)\s]+\.svg)\)$")
_SENTENCE_END = re.compile(r"[.!?](?=\s|$)")
_HEADING = re.compile(r"^#{1,6}\s+(.*)$")
# A line anchor, and the permalink form that keeps it on its line: a file at
# a full commit id. Any other line link points elsewhere after the next change.
_LINE = re.compile(r"#L\d+(-L\d+)?$")
_PERMALINK = re.compile(r"https://github\.com/[^/]+/[^/]+/blob/[0-9a-f]{40}/")


def _shown(text: str) -> str:
    """The text as a reader sees it: no link targets, code masked."""
    masked = _CODE.sub(lambda match: "x" * len(match.group()), text)
    return _TARGET.sub("]", masked)


def _prose(lines: list[str]) -> Iterator[tuple[int, str]]:
    """The lines outside code fences, with their line numbers."""
    fenced = False
    for number, line in enumerate(lines, start=1):
        if _FENCE.match(line):
            fenced = not fenced
        elif not fenced:
            yield number, line


def _blocks(lines: list[str]) -> Iterator[tuple[int, str]]:
    """Paragraphs and list items, each joined into one line."""
    start = 0
    parts: list[str] = []
    for number, line in [*_prose(lines), (0, "")]:
        text = line.strip()
        ends = not text or text.startswith(("#", "|")) or _ITEM.match(line)
        if ends and parts:
            yield start, " ".join(parts)
            parts = []
        if text and not text.startswith(("#", "|")):
            if not parts:
                start = number
            parts.append(_ITEM.sub("", line).strip())


def _cells(lines: list[str]) -> Iterator[tuple[int, str]]:
    for number, line in _prose(lines):
        if line.lstrip().startswith("|"):
            for cell in _shown(line).strip().strip("|").split("|"):
                yield number, cell.strip()


def anchors(text: str) -> set[str]:
    """The anchors GitHub gives the headings of a page."""
    found: set[str] = set()
    for _, line in _prose(text.splitlines()):
        heading = _HEADING.match(line)
        if heading:
            slug = re.sub(r"[^\w\- ]", "", heading.group(1).strip().lower())
            found.add(slug.replace(" ", "-"))
    return found


def _broken_links(path: Path, lines: list[str]) -> Iterator[str]:
    # Code spans are examples, not links.
    prose = _CODE.sub("", "\n".join(line for _, line in _prose(lines)))
    for target in _LINK.findall(prose):
        if _LINE.search(target) and not _PERMALINK.match(target):
            yield f"link to {target}: a line link moves with the code; link the file at a commit"
            continue
        if re.match(r"[a-z][a-z0-9+.-]*:", target):
            continue
        name, _, anchor = target.partition("#")
        other = (path.parent / name).resolve() if name else path
        if not other.exists():
            yield f"link to {target}: no such file"
        elif (
            anchor
            and other.suffix == ".md"
            and anchor not in anchors(other.read_text(encoding="utf-8"))
        ):
            yield f"link to {target}: no such heading"


def check(path: Path) -> list[str]:
    """Return the problems of one page, each as a line of text."""
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    name = path.resolve().as_posix()
    problems = list(_broken_links(path, lines))
    if len(lines) > MAX_LINES:
        problems.append(f"{len(lines)} lines, at most {MAX_LINES}: split or cut")
    if "```mermaid" not in text and not NO_DIAGRAM.search(name):
        images = (_SVG_IMAGE.fullmatch(line) for _, line in _prose(lines))
        if not any(image and (path.parent / image[1]).is_file() for image in images):
            problems.append("no Mermaid diagram or embedded SVG")
    if "—" in text:
        problems.append("an em dash")
    if KEPT_TEXT.search(name):
        return problems
    for number, block in _blocks(lines):
        length = len(_shown(block))
        if length > MAX_BLOCK:
            problems.append(
                f"line {number}: a paragraph of {length} characters, at most {MAX_BLOCK}"
            )
    for number, cell in _cells(lines):
        sentences = len(_SENTENCE_END.findall(cell))
        if len(cell) > MAX_CELL or sentences > MAX_CELL_SENTENCES:
            problems.append(
                f"line {number}: a table cell of {len(cell)} characters and"
                f" {sentences} sentences, at most {MAX_CELL} and {MAX_CELL_SENTENCES}"
            )
    return problems


def main(arguments: list[str]) -> int:
    failed = False
    for name in arguments:
        for problem in check(Path(name)):
            print(f"{name}: {problem}", file=sys.stderr)
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
