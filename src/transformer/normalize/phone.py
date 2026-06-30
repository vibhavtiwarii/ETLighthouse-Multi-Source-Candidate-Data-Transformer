"""Pure normalization helpers for phone numbers.

No file I/O, no printing, no side effects. ``normalize_phone`` takes a raw
string and returns a normalized E.164 phone number string, or ``None`` if
the value cannot be parsed into a valid number.
"""

from typing import Optional

import phonenumbers


def normalize_phone(raw: str, country_hint: Optional[str] = None) -> Optional[str]:
    """Normalize a raw phone number string to E.164 format.

    Strategy:
      1. Try parsing with the supplied ``country_hint`` (may be ``None``,
         which only works for numbers that already include a country code,
         e.g. a leading "+").
      2. If that raises or yields an invalid number, retry assuming the
         hint is ``"US"`` as a last-resort default.
      3. If that also fails, return ``None``. This function never raises.

    Examples:
        >>> normalize_phone("+1 415-555-2671", "US")
        '+14155552671'
        >>> normalize_phone("(415) 555-2671", "US")
        '+14155552671'
        >>> normalize_phone("not a phone number")
        None
        >>> normalize_phone("020 7946 0958", "GB")
        '+442079460958'
    """
    if not raw or not raw.strip():
        return None

    # Attempt 1: parse using the caller-provided hint (or None).
    try:
        parsed = phonenumbers.parse(raw, country_hint)
        if phonenumbers.is_valid_number(parsed):
            return phonenumbers.format_number(
                parsed, phonenumbers.PhoneNumberFormat.E164
            )
    except phonenumbers.NumberParseException:
        pass

    # Attempt 2: last-resort default region of "US".
    try:
        parsed = phonenumbers.parse(raw, "US")
        if phonenumbers.is_valid_number(parsed):
            return phonenumbers.format_number(
                parsed, phonenumbers.PhoneNumberFormat.E164
            )
    except phonenumbers.NumberParseException:
        pass

    return None
