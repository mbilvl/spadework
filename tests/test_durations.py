import re

import pytest

from spadework.durations import DURATION_PATTERN, format_duration, parse_duration


@pytest.mark.parametrize(
    ("text", "seconds"),
    [
        ("90m", 5400),
        ("2h", 7200),
        ("25m", 1500),
        ("1h30m", 5400),
        ("45s", 45),
        ("1h2m3s", 3723),
    ],
)
def test_parse_duration(text: str, seconds: int) -> None:
    assert parse_duration(text) == seconds
    assert re.fullmatch(DURATION_PATTERN, text)


@pytest.mark.parametrize("text", ["", "2", "2 h", "h", "1.5h", "30m2h", "-1h", " 2h"])
def test_parse_duration_rejects_what_is_not_a_duration(text: str) -> None:
    with pytest.raises(ValueError, match="is not a duration; use hours"):
        parse_duration(text)
    assert not re.fullmatch(DURATION_PATTERN, text)


def test_parse_duration_rejects_zero() -> None:
    with pytest.raises(ValueError, match="longer than zero"):
        parse_duration("0m")


@pytest.mark.parametrize(
    ("seconds", "text"),
    [(7200, "2h"), (5400, "1h30m"), (1500, "25m"), (61, "1m1s"), (0, "0s")],
)
def test_format_duration(seconds: int, text: str) -> None:
    assert format_duration(seconds) == text
