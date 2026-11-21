"""Check that each tracked diagram parses as SVG."""

import sys
from pathlib import Path
from xml.etree import ElementTree


class DiagramTree(ElementTree.TreeBuilder):
    def doctype(self, name: str, pubid: str | None, system: str | None) -> None:
        raise ValueError("a diagram cannot declare a document type")


def main(paths: list[str]) -> int:
    failed = False
    for name in paths:
        try:
            parser = ElementTree.XMLParser(target=DiagramTree())  # noqa: S314
            root = ElementTree.parse(Path(name), parser).getroot()  # noqa: S314
            if root.tag != "{http://www.w3.org/2000/svg}svg":
                raise ValueError("expected an SVG root element")
        except (ElementTree.ParseError, ValueError, OSError) as error:
            print(f"{name}: {error}", file=sys.stderr)
            failed = True
    return int(failed)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
