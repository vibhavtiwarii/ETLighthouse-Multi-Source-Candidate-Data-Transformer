#confidence.py

"""
src/transformer/merge/confidence.py
────────────────────────────────────────────────────────────────────────────────
Field-level confidence scoring for the merge stage.

Formula
-------
    confidence = min(1.0,
        source_weight(source, field_category)
        * method_certainty(method)
        * (1.0 + 0.15 * (corroborating_source_count - 1))
    )

Where:
  - source_weight  comes from config/source_weights.json, keyed by source name
    and field category ("identity" | "employment" | "skills").
  - method_certainty is a fixed lookup table — extraction techniques that
    require human judgment or probabilistic matching carry lower certainty.
  - corroborating_source_count is the number of *distinct* sources that
    produced the same normalized value for this field.  A count of 1 means
    only the current source asserts this value (no boost); each additional
    independent corroborator adds 15 % to the raw product, capped at 1.0.

The cap at 1.0 ensures that even a highly-corroborated fuzzy-match from a
low-trust source cannot exceed perfect confidence.
────────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import json
import pathlib
from functools import lru_cache
from typing import Dict

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

# Resolve config relative to this file's location so the module works
# regardless of the working directory the CLI is invoked from.
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]  # …/eightfold-transformer
_WEIGHTS_PATH = _REPO_ROOT / "config" / "source_weights.json"

# ---------------------------------------------------------------------------
# Method certainty table
# Covers every MethodName defined in raw_field.py PLUS "keyword_match", which
# is specified in the confidence formula but not in the MethodName Literal
# (method is typed as `str` on RawField, so any value is legal).
# Unknown methods fall back to 0.5 (same as the least-certain named methods).
# ---------------------------------------------------------------------------

_METHOD_CERTAINTY: Dict[str, float] = {
    "direct_copy":   1.0,
    "field_remap":   0.95,
    "api_fetch":     0.8,
    "regex_extract": 0.6,
    "keyword_match": 0.5,
    "fuzzy_match":   0.5,
}

_METHOD_CERTAINTY_DEFAULT: float = 0.5  # safe floor for unrecognised methods

# ---------------------------------------------------------------------------
# Field-category membership
# Used by callers to translate a canonical field_name into the category key
# expected by source_weights.json.
# ---------------------------------------------------------------------------

#: Maps canonical field names (and their dotted sub-field variants) to category.
FIELD_CATEGORY: Dict[str, str] = {
    # identity
    "full_name":        "identity",
    "emails":           "identity",
    "email":            "identity",
    "phones":           "identity",
    "phone":            "identity",
    "location":         "identity",
    "location.city":    "identity",
    "location.region":  "identity",
    "location.country": "identity",
    "links":            "identity",
    "links.linkedin":   "identity",
    "links.github":     "identity",
    "links.portfolio":  "identity",
    "links.other":      "identity",
    # employment
    "headline":           "employment",
    "years_experience":   "employment",
    "experience":         "employment",
    "experience.company": "employment",
    "experience.title":   "employment",
    "experience.start":   "employment",
    "experience.end":     "employment",
    "experience.summary": "employment",
    "education":              "employment",
    "education.institution":  "employment",
    "education.degree":       "employment",
    "education.field":        "employment",
    "education.end_year":     "employment",
    # skills
    "skills": "skills",
    "skill":  "skills",
}


def get_field_category(field_name: str) -> str:
    """
    Return the category string for *field_name*.

    Lookup order:
    1. Exact match in FIELD_CATEGORY.
    2. Prefix match (e.g. ``"experience.company"`` → ``"employment"``).
    3. Fall back to ``"identity"`` — the most conservative default, since
       identity fields already carry moderate source weights and we'd rather
       not silently inflate employment/skills confidence for unknown fields.
    """
    if field_name in FIELD_CATEGORY:
        return FIELD_CATEGORY[field_name]

    # Try dotted prefix
    prefix = field_name.split(".")[0]
    if prefix in FIELD_CATEGORY:
        return FIELD_CATEGORY[prefix]

    return "identity"


# ---------------------------------------------------------------------------
# Weight loading (cached — file is read once per process)
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _load_weights() -> Dict[str, Dict[str, float]]:
    """
    Load source_weights.json exactly once and cache it for the process lifetime.

    Raises
    ------
    FileNotFoundError
        If config/source_weights.json cannot be found relative to the repo root.
    json.JSONDecodeError
        If the file is malformed.
    """
    with _WEIGHTS_PATH.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _source_weight(source: str, field_category: str) -> float:
    """
    Return the weight for *source* in *field_category*.

    Unknown sources fall back to 0.3 (conservative — we don't know the
    reliability of an unregistered adapter).  Unknown categories fall back
    to the "identity" weight for that source.
    """
    weights = _load_weights()
    source_map = weights.get(source)
    if source_map is None:
        return 0.3
    return source_map.get(field_category, source_map.get("identity", 0.3))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compute_confidence(
    source: str,
    method: str,
    field_category: str,
    corroborating_source_count: int,
) -> float:
    """
    Compute a [0.0, 1.0] confidence score for a single field value assertion.

    Parameters
    ----------
    source : str
        The adapter that produced the value — one of ``"csv"``, ``"ats_json"``,
        ``"github"``, ``"notes"``, or any custom adapter string.
    method : str
        The extraction technique — one of ``"direct_copy"``, ``"field_remap"``,
        ``"api_fetch"``, ``"regex_extract"``, ``"keyword_match"``,
        ``"fuzzy_match"``, or any custom method string.
    field_category : str
        One of ``"identity"``, ``"employment"``, or ``"skills"``.  Use
        ``get_field_category(field_name)`` to derive this from a canonical
        field name.
    corroborating_source_count : int
        Number of *distinct* sources that produced the *same* normalised value
        for this field.  Must be >= 1 (the asserting source itself counts as 1;
        values < 1 are clamped to 1 to prevent nonsensical negative boosts).

    Returns
    -------
    float
        Confidence in [0.0, 1.0], inclusive.

    Examples
    --------
    >>> compute_confidence("ats_json", "direct_copy", "identity", 1)
    0.95       # 0.95 * 1.0 * (1 + 0.15*0) = 0.95
    >>> compute_confidence("ats_json", "direct_copy", "identity", 2)
    1.0        # 0.95 * 1.0 * 1.15 = 1.0925 → clamped to 1.0
    >>> compute_confidence("github", "api_fetch", "employment", 1)
    0.08       # 0.1 * 0.8 * 1.0 = 0.08
    """
    # Clamp corroborating_source_count to a sensible minimum of 1.
    count = max(1, corroborating_source_count)

    sw = _source_weight(source, field_category)
    mc = _METHOD_CERTAINTY.get(method, _METHOD_CERTAINTY_DEFAULT)
    corroboration_multiplier = 1.0 + 0.15 * (count - 1)

    raw = sw * mc * corroboration_multiplier
    return min(1.0, raw)
