"""Ids of grants, people, systems and teams, and the reference code.

An id is a prefix and symbols of a 32-symbol alphabet: digits and capitals
without I, L, O and U. Letters and digits only, so an id is one token.
"""

import re

from access_review.rng import Source

ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_SYMBOL = "[0-9A-HJKMNP-TV-Z]"

GRANT_ID = re.compile(f"GR{_SYMBOL}{{10}}")
PERSON_ID = re.compile(f"PN{_SYMBOL}{{8}}")
SYSTEM_ID = re.compile(f"SY{_SYMBOL}{{6}}")
TEAM_ID = re.compile(f"TM{_SYMBOL}{{8}}")
REFERENCE_CODE = re.compile(f"{_SYMBOL}{{4}}-{_SYMBOL}{{4}}-{_SYMBOL}{{4}}-{_SYMBOL}{{4}}")

CODE_SYMBOLS = 16


def wildcard_forms(body: str) -> list[str]:
    """`body` with one symbol at a time replaced by `*`."""
    return [body[:i] + "*" + body[i + 1 :] for i in range(len(body))]


class IdDrawer:
    """Draws ids of one kind; never the same id twice.

    With `apart`, an id one changed symbol from an earlier one is drawn
    again too. It keeps the wildcard forms of each id it gave, not pairs.
    """

    def __init__(self, stream: Source, prefix: str, symbols: int, apart: bool = False) -> None:
        self._stream = stream
        self._prefix = prefix
        self._symbols = symbols
        self._apart = apart
        self._given: set[str] = set()
        self._forms: set[str] = set()

    def next(self) -> str:
        while True:
            body = "".join(
                ALPHABET[self._stream.below(len(ALPHABET))] for _ in range(self._symbols)
            )
            if body in self._given:
                continue
            forms = wildcard_forms(body)
            if self._apart and any(form in self._forms for form in forms):
                continue
            self._given.add(body)
            if self._apart:
                self._forms.update(forms)
            return self._prefix + body


def draw_code(stream: Source) -> str:
    """The reference code: 16 symbols, 80 bits, as `XXXX-XXXX-XXXX-XXXX`."""
    body = IdDrawer(stream, "", CODE_SYMBOLS).next()
    return "-".join(body[i : i + 4] for i in range(0, CODE_SYMBOLS, 4))
