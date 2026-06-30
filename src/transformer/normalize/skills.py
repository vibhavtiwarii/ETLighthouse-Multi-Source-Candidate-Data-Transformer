"""Pure normalization helpers for skill strings.

No file I/O happens per-call: the synonym dictionary is loaded once at
import time. ``normalize_skill`` takes a raw skill string and returns a
normalized, canonical skill name. Unknown skills are never dropped -- they
are returned capitalized as a best-effort fallback so downstream consumers
still see them, just unverified against the dictionary.
"""

import json
from pathlib import Path

from rapidfuzz import fuzz

# Loaded once at module import, not on every call.
_DATA_PATH = Path(__file__).resolve().parents[3] / "data" / "skill_synonyms.json"
with open(_DATA_PATH, "r", encoding="utf-8") as _f:
    _SKILL_SYNONYMS: dict = json.load(_f)

_FUZZY_MATCH_THRESHOLD = 85


def normalize_skill(raw: str) -> str:
    """Normalize a raw skill string to its canonical form.

    Lookup order:
      1. Lowercase + exact match against the synonym dictionary keys.
      2. Fuzzy match (rapidfuzz ``fuzz.ratio``) against all dictionary
         keys; accept the best match only if its score is >= 85.
      3. Otherwise, fall back to the original input, capitalized. The
         skill is kept (not dropped) since it may simply be missing from
         our dictionary rather than invalid.

    Examples:
        >>> normalize_skill("js")
        'JavaScript'
        >>> normalize_skill("Reactjs")
        'React'
        >>> normalize_skill("pythn")  # typo, fuzzy-matched
        'Python'
        >>> normalize_skill("underwater basket weaving")  # unknown, kept
        'Underwater basket weaving'
    """
    if raw is None:
        return ""

    cleaned = raw.strip()
    if not cleaned:
        return ""

    lowered = cleaned.lower()

    # 1. Exact match.
    if lowered in _SKILL_SYNONYMS:
        return _SKILL_SYNONYMS[lowered]

    # 2. Fuzzy match against all known keys.
    best_key = None
    best_score = 0
    for key in _SKILL_SYNONYMS:
        score = fuzz.ratio(lowered, key)
        if score > best_score:
            best_score = score
            best_key = key

    if best_key is not None and best_score >= _FUZZY_MATCH_THRESHOLD:
        return _SKILL_SYNONYMS[best_key]

    # 3. Unknown skill: keep it, just capitalized as a best-effort guess.
    return cleaned.capitalize()
