"""Pure normalization helpers for dates.

No file I/O, no printing, no side effects. ``normalize_date`` takes a raw
date-ish string and returns a ``"YYYY-MM"`` string, the literal string
``"present"``, or ``None`` if the value cannot be parsed at all.
"""

from typing import Optional

from dateutil import parser as dateutil_parser

_PRESENT_TOKENS = {"present", "current"}


def normalize_date(raw: str) -> Optional[str]:
    """Normalize a raw date string to ``"YYYY-MM"`` format.

    "present" / "current" (any casing) are special-cased to the literal
    string ``"present"`` rather than being parsed as a calendar date, since
    they represent an open-ended/ongoing range, not an actual point in time.

    Examples:
        >>> normalize_date("March 2019")
        '2019-03'
        >>> normalize_date("Present")
        'present'
        >>> normalize_date("CURRENT")
        'present'
        >>> normalize_date("06/2021")
        '2021-06'
        >>> normalize_date("not a date at all !!!")
        None
    """
    if raw is None:
        return None

    cleaned = raw.strip()
    if not cleaned:
        return None

    if cleaned.lower() in _PRESENT_TOKENS:
        return "present"

    try:
        parsed = dateutil_parser.parse(cleaned, fuzzy=True)
    except (ValueError, OverflowError, TypeError):
        return None

    return parsed.strftime("%Y-%m")
