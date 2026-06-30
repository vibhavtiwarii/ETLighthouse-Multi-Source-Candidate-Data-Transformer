from __future__ import annotations

"""Parses recruiter note text files.""" 
"""
adapters/notes_adapter.py — adapter for free-text recruiter / candidate notes.

What this adapter extracts from a single ``.txt`` file
-------------------------------------------------------
1. **emails**  — first email address found via standard email regex.
                 method='regex_extract'
2. **phones**  — first phone-looking string (7+ digit sequence with optional
                 +, -, spaces, parens).
                 method='regex_extract'
3. **skills**  — every skill whose canonical name or any synonym appears
                 (case-insensitive) in the note text, loaded from
                 ``data/skill_synonyms.json``.
                 method='keyword_match'

All RawFields carry ``raw_text`` set to the full original note content so
every extraction is fully traceable.

Error policy
------------
Missing file, empty file, unreadable file → log WARNING, return [].
No exception propagates out of extract().
"""



import json
import logging
import re
from pathlib import Path

from src.transformer.adapters.base import SourceAdapter
from src.transformer.raw_field import RawField

logger = logging.getLogger(__name__)

_SOURCE = "notes"
_METHOD_REGEX = "regex_extract"
_METHOD_KEYWORD = "keyword_match"

# Matches standard email addresses.
_EMAIL_RE = re.compile(
    r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}",
    re.IGNORECASE,
)

# Matches phone-like strings: optional leading +, then sequences of digits,
# spaces, dashes, dots, and parens — must total at least 7 digits.
_PHONE_RE = re.compile(
    r"[\+\(]?[\d][\d\s\-\.\(\)]{5,}[\d]",
)

# Path to the skill synonyms file, relative to the project root.
_SYNONYMS_PATH = Path("data/skill_synonyms.json")


def _load_synonyms(synonyms_path: Path) -> dict[str, list[str]]:
    """
    Load ``skill_synonyms.json`` and return a dict of
    ``{canonical_name: [variant, variant, ...]}``.

    Returns an empty dict on any read/parse failure (logged as WARNING).
    """
    try:
        if not synonyms_path.exists():
            logger.warning(
                "NotesAdapter: skill_synonyms.json not found at %r — skill matching disabled",
                str(synonyms_path),
            )
            return {}
        with synonyms_path.open(encoding="utf-8") as fh:
            return json.load(fh)
    except Exception as exc:
        logger.warning(
            "NotesAdapter: failed to load skill_synonyms.json: %s — skill matching disabled",
            exc,
        )
        return {}


def _digit_count(s: str) -> int:
    """Return the number of digit characters in *s*."""
    return sum(1 for c in s if c.isdigit())


class NotesAdapter(SourceAdapter):
    """
    Reads a single plain-text ``.txt`` note file and extracts emails,
    phone numbers, and skill keyword matches.

    Parameters
    ----------
    synonyms_path:
        Override the default path to ``data/skill_synonyms.json``.
        Useful in tests.
    """

    def __init__(self, synonyms_path: Path | None = None) -> None:
        self._synonyms_path = synonyms_path or _SYNONYMS_PATH

    def extract(self, source_path_or_url: str) -> list[RawField]:
        try:
            path = Path(source_path_or_url)

            if not path.exists():
                logger.warning(
                    "NotesAdapter: file not found: %r — returning []",
                    source_path_or_url,
                )
                return []

            raw_text = path.read_text(encoding="utf-8")

            if not raw_text.strip():
                logger.warning(
                    "NotesAdapter: file %r is empty — returning []",
                    source_path_or_url,
                )
                return []

            fields: list[RawField] = []

            # ---- 1. Email extraction ---------------------------------
            email_match = _EMAIL_RE.search(raw_text)
            if email_match:
                fields.append(
                    RawField(
                        field_name="emails",
                        value=email_match.group(0),
                        source=_SOURCE,
                        method=_METHOD_REGEX,
                        raw_text=raw_text,
                    )
                )

            # ---- 2. Phone extraction ---------------------------------
            for phone_match in _PHONE_RE.finditer(raw_text):
                candidate = phone_match.group(0)
                if _digit_count(candidate) >= 7:
                    fields.append(
                        RawField(
                            field_name="phones",
                            value=candidate.strip(),
                            source=_SOURCE,
                            method=_METHOD_REGEX,
                            raw_text=raw_text,
                        )
                    )
                    break  # emit only the first valid phone match

            # ---- 3. Skill keyword matching ---------------------------
            synonyms = _load_synonyms(self._synonyms_path)
            text_lower = raw_text.lower()

            for canonical_name, variants in synonyms.items():
                # Build a combined search set: canonical name + all variants.
                all_terms = {canonical_name.lower()} | {v.lower() for v in variants}
                for term in all_terms:
                    # Whole-word match to avoid substring false positives
                    # (e.g. "C" matching inside "Microsoft").
                    pattern = re.compile(
                        r"(?<![a-zA-Z0-9_])" + re.escape(term) + r"(?![a-zA-Z0-9_])",
                        re.IGNORECASE,
                    )
                    if pattern.search(raw_text):
                        fields.append(
                            RawField(
                                field_name="skills",
                                value=canonical_name,
                                source=_SOURCE,
                                method=_METHOD_KEYWORD,
                                raw_text=raw_text,
                            )
                        )
                        break  # one match per canonical skill is enough

            return fields

        except Exception as exc:
            logger.warning(
                "NotesAdapter.extract() failed for %r: %s",
                source_path_or_url,
                exc,
            )
            return []
