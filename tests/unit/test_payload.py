"""check_json accepts what jsonb accepts and names what it rejects."""

from typing import Any

import pytest

from runtime.store.payload import InvalidPayload, check_json


@pytest.mark.parametrize(
    "value",
    [
        {},
        {"a": [1, 2.5, "x", None, True, {"b": []}]},
        {"backslash": "\\u0000"},
        {"big": 10**40},
        {"astral": "\U0001f600"},
        {"tuple": (1, "x")},
    ],
)
def test_accepts(value: Any) -> None:
    check_json(value)


@pytest.mark.parametrize(
    "value",
    [
        "\x00",
        {"a": "x\x00"},
        {"\x00": 1},
        [[["\x00"]]],
        float("nan"),
        {"a": float("inf")},
        [float("-inf")],
        "\ud800",
        {"a": "x\udfff"},
        {"\udc00": 1},
        {"n": (float("nan"),)},
        ("\x00",),
        {1: "int key"},
        {float("nan"): 1},
        {None: 1},
    ],
)
def test_rejects(value: Any) -> None:
    with pytest.raises(InvalidPayload):
        check_json(value)
