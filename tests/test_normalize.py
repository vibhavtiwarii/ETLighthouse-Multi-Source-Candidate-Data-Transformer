"""Tests normalization functions.""" 
"""
tests/test_normalize.py
────────────────────────────────────────────────────────────────────────────────
Parametrized unit tests for the four Phase-4 normaliser functions:
    normalize_phone   → E.164 string or None
    normalize_date    → "YYYY-MM" / "present" or None
    normalize_skill   → canonical skill name string (never None — falls back)
    normalize_country → ISO 3166-1 alpha-2 string or None

Each function gets at least 3 cases, always including one garbage-input case
that asserts the function returns None (or a safe fallback) instead of raising.
────────────────────────────────────────────────────────────────────────────────
"""

import pytest

from src.transformer.normalize.phone import normalize_phone
from src.transformer.normalize.date import normalize_date
from src.transformer.normalize.skills import normalize_skill
from src.transformer.normalize.country import normalize_country


# ─────────────────────────────────────────────────────────────────────────────
# normalize_phone
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw, hint, expected", [
    # US number with formatting — should normalise to E.164
    ("+1 415-555-0192", "US", "+14155550192"),
    # UK number with hint — should normalise correctly
    ("020 7946 0312", "GB", "+442079460312"),
    # Already E.164 — should pass through unchanged
    ("+14155550192", None, "+14155550192"),
    # Garbage input — must return None, not raise
    ("not a phone number at all !!!", None, None),
    # Empty string — must return None, not raise
    ("", "US", None),
    # Whitespace-only — must return None, not raise
    ("   ", None, None),
])
def test_normalize_phone(raw, hint, expected):
    result = normalize_phone(raw, hint)
    assert result == expected


# ─────────────────────────────────────────────────────────────────────────────
# normalize_date
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw, expected", [
    # Month + year prose form
    ("March 2019", "2019-03"),
    # MM/YYYY numeric form
    ("06/2021", "2021-06"),
    # "present" keyword — any casing
    ("Present", "present"),
    ("CURRENT", "present"),
    ("current", "present"),
    # Garbage input — must return None, not raise
    ("not a date at all !!!", None),
    # Empty string — must return None, not raise
    ("", None),
    # None value — must return None, not raise
    (None, None),
])
def test_normalize_date(raw, expected):
    result = normalize_date(raw)
    assert result == expected


# ─────────────────────────────────────────────────────────────────────────────
# normalize_skill
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw, expected_contains", [
    # Common alias — "js" should map to JavaScript canonical form
    ("js", "JavaScript"),
    # Exact canonical name — should come back unchanged (or same canonical)
    ("Python", "Python"),
    # Typo close enough for fuzzy match — "pythn" → Python
    ("pythn", "Python"),
    # Completely unknown skill — must NOT raise, must return a non-empty string
    # (the function capitalises and returns the input as a best-effort fallback)
    ("underwater basket weaving", "Underwater basket weaving"),
    # Empty string — must NOT raise, returns empty string
    ("", ""),
])
def test_normalize_skill(raw, expected_contains):
    result = normalize_skill(raw)
    # normalize_skill never returns None — it always returns a string
    assert isinstance(result, str)
    assert result == expected_contains


def test_normalize_skill_garbage_does_not_raise():
    """Garbage input must return a string fallback, never raise."""
    result = normalize_skill("!@#$%^&*()")
    assert isinstance(result, str)  # capitalised fallback, not an exception


# ─────────────────────────────────────────────────────────────────────────────
# normalize_country
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw, expected", [
    # Full English name
    ("United States", "US"),
    # Common abbreviation handled by the lookup table
    ("uk", "GB"),
    # Case-insensitive + surrounding whitespace
    ("  germany  ", "DE"),
    # ISO code already — pass-through if in lookup, else None
    # (exact behaviour depends on iso3166_countries.json contents)
    # Fictional country — must return None, not raise
    ("Narnia", None),
    # Empty string — must return None, not raise
    ("", None),
    # None — must return None, not raise
    (None, None),
])
def test_normalize_country(raw, expected):
    result = normalize_country(raw)
    assert result == expected