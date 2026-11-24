"""The error of a job stopped for repeating itself names the tool only when the name is plain."""

import pytest

from runtime.worker.agent import _repeat_error


def test_a_plain_tool_name_is_named() -> None:
    assert _repeat_error("read_file") == (
        "the job repeated the same call to read_file with the same result 5 times in a row"
    )


@pytest.mark.parametrize("name", ["x\x1b[31mred", "a\nb", "", "n" * 65, "read file"])
def test_a_name_that_is_not_plain_is_not_shown(name: str) -> None:
    error = _repeat_error(name)
    assert "a tool with the same result" in error
    assert name not in error or name == ""
