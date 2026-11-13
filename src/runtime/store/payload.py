"""What a JSON column accepts.

Postgres `jsonb` cannot hold some values that Python's JSON encoder lets
through: the NUL character and unpaired surrogates in a string, and the float
values NaN and infinity. Left unchecked they fail inside the INSERT, which
aborts the caller's transaction. Checking first turns them into an error the
caller can handle.

Object keys must be strings. The JSON encoder would turn other keys into
strings, and the value read back would not equal the value written.

One more `jsonb` behaviour, not an error: numbers have no int or float type.
A float that the JSON encoder writes without a fraction, such as 1e22
(written `1e+22`), is read back as an int. 2.0 is written `2.0` and stays a
float.

Not checked here: nesting deep enough to hit Python's recursion limit and
integers of more than 4300 digits. Both fail in the driver before anything is
sent, with an error that is not InvalidPayload.
"""

import math
import re
from typing import Any

# NUL, or a surrogate code point. A str can hold a surrogate on its own;
# valid text never does.
_UNSTORABLE = re.compile("[\x00\ud800-\udfff]")


class InvalidPayload(ValueError):
    """The value cannot be stored in a `jsonb` column and read back unchanged."""


def check_json(value: Any) -> None:
    """Raise InvalidPayload if `value` holds something `jsonb` rejects."""
    if isinstance(value, str):
        if _UNSTORABLE.search(value):
            raise InvalidPayload("a string contains NUL or an unpaired surrogate")
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise InvalidPayload(f"{value} is not a JSON number")
    elif isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise InvalidPayload(f"object key {key!r} is not a string")
            check_json(key)
            check_json(item)
    elif isinstance(value, list | tuple):
        for item in value:
            check_json(item)
