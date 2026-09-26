"""Durations written the way people write them: ``90m``, ``2h``, ``1h30m``."""

from __future__ import annotations

import re

# The same grammar as _DURATION below, without look-arounds, so that any JSON
# Schema validator can use it.
DURATION_PATTERN = r"^([0-9]+h([0-9]+m)?([0-9]+s)?|[0-9]+m([0-9]+s)?|[0-9]+s)$"

_DURATION = re.compile(r"(?:(?P<h>\d+)h)?(?:(?P<m>\d+)m)?(?:(?P<s>\d+)s)?")


def parse_duration(text: str) -> int:
    """Return the number of seconds in ``text``, such as ``"2h"`` or ``"25m"``.

    Raises ValueError with a message fit for the user.
    """
    match = _DURATION.fullmatch(text)
    if not text or match is None:
        raise ValueError(
            f"{text!r} is not a duration; use hours, minutes or seconds, "
            "such as '2h', '90m' or '1h30m'"
        )
    hours, minutes, seconds = (int(match.group(unit) or 0) for unit in "hms")
    total = hours * 3600 + minutes * 60 + seconds
    if total <= 0:
        raise ValueError(f"{text!r} is not a duration longer than zero")
    return total


def format_duration(seconds: int) -> str:
    """Format seconds compactly, for example ``5400`` as ``1h30m``."""
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    parts = [f"{hours}h" if hours else "", f"{minutes}m" if minutes else ""]
    parts.append(f"{secs}s" if secs or not (hours or minutes) else "")
    return "".join(parts)
