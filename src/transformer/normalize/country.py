"""Pure normalization helpers for country names.

No file I/O happens per-call: the ISO 3166 lookup table is loaded once at
import time. ``normalize_country`` takes a raw country string and returns
its ISO 3166-1 alpha-2 code, or ``None`` if no exact match is found.

This is deliberately strict (exact match only, no fuzzy matching) because
a wrong country guess could silently corrupt downstream normalization
(e.g. picking the wrong default region for phone number parsing), which
is worse than simply leaving the field empty.
"""

import json
from pathlib import Path
from typing import Optional

# Loaded once at module import, not on every call.
_DATA_PATH = Path(__file__).resolve().parents[3] / "data" / "iso3166_countries.json"
with open(_DATA_PATH, "r", encoding="utf-8") as _f:
    _COUNTRY_LOOKUP: dict = json.load(_f)


def normalize_country(raw: str) -> Optional[str]:
    """Normalize a raw country string to its ISO 3166-1 alpha-2 code.

    Matching is case-insensitive and whitespace-trimmed, but otherwise
    exact -- no fuzzy matching is performed for countries.

    Examples:
        >>> normalize_country("United States")
        'US'
        >>> normalize_country("  uk ")
        'GB'
        >>> normalize_country("Deutschland")
        'DE'
        >>> normalize_country("Narnia")
        None
    """
    if raw is None:
        return None

    cleaned = raw.strip().lower()
    if not cleaned:
        return None

    return _COUNTRY_LOOKUP.get(cleaned)
