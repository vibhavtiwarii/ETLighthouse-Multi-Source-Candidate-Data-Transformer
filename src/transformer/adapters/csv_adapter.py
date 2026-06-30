from __future__ import annotations
"""Reads recruiter CSV files."""
"""
adapters/csv_adapter.py — adapter for recruiter / candidate CSV exports.

Expected columns (all optional — missing columns are skipped per-row,
never per-file):

    name            → full_name       (direct_copy)
    email           → emails          (direct_copy)
    phone           → phones          (direct_copy)
    current_company → current_company (direct_copy)   ← merged into Experience downstream
    title           → current_title   (direct_copy)   ← merged into Experience downstream

Rules
-----
* source='csv', method='direct_copy' on every RawField emitted.
* A blank / missing cell in one column never causes the whole row to be
  skipped — the adapter emits RawFields for whatever IS present.
* record_index is set to the zero-based row number, so every RawField
  from the same CSV row carries the same record_index. This is what lets
  identity resolution reconstruct "one candidate per row" reliably —
  the adapter is the only place that actually knows the row boundary,
  so it sets the index itself rather than leaving it to be inferred later.
* A missing file or empty file logs a WARNING and returns [].
* No exception ever propagates out of extract().
"""

import csv
import logging
from pathlib import Path

from src.transformer.adapters.base import SourceAdapter
from src.transformer.raw_field import RawField

logger = logging.getLogger(__name__)

# Maps CSV column name → logical RawField field_name.
_COLUMN_ALIASES: dict[str, str] = {
    # ── full name ──────────────────────────────────────────────
    "name":               "full_name",
    "full name":          "full_name",
    "full_name":          "full_name",
    "candidate name":     "full_name",
    "candidate_name":     "full_name",
    "applicant name":     "full_name",
    "applicant_name":     "full_name",
    "person":             "full_name",
    "contact name":       "full_name",
    "contact_name":       "full_name",

    # ── email ──────────────────────────────────────────────────
    "email":              "emails",
    "email address":      "emails",
    "email_address":      "emails",
    "e-mail":             "emails",
    "e_mail":             "emails",
    "mail":               "emails",
    "work email":         "emails",
    "personal email":     "emails",

    # ── phone ──────────────────────────────────────────────────
    "phone":              "phones",
    "phone number":       "phones",
    "phone_number":       "phones",
    "mobile":             "phones",
    "mobile number":      "phones",
    "cell":               "phones",
    "cell phone":         "phones",
    "contact number":     "phones",
    "tel":                "phones",
    "telephone":          "phones",

    # ── company ────────────────────────────────────────────────
    "current_company":    "current_company",
    "current company":    "current_company",
    "company":            "current_company",
    "employer":           "current_company",
    "organization":       "current_company",
    "organisation":       "current_company",
    "org":                "current_company",
    "workplace":          "current_company",
    "firm":               "current_company",

    # ── title ──────────────────────────────────────────────────
    "title":              "current_title",
    "current_title":      "current_title",
    "current title":      "current_title",
    "job title":          "current_title",
    "job_title":          "current_title",
    "position":           "current_title",
    "role":               "current_title",
    "designation":        "current_title",
    "job role":           "current_title",
    "current role":       "current_title",
    "current position":   "current_title",

    # ── linkedin ───────────────────────────────────────────────
    "linkedin":           "links.linkedin",
    "linkedin url":       "links.linkedin",
    "linkedin_url":       "links.linkedin",
    "linkedin profile":   "links.linkedin",
    "linkedin link":      "links.linkedin",

    # ── github ─────────────────────────────────────────────────
    "github":             "links.github",
    "github url":         "links.github",
    "github_url":         "links.github",
    "github profile":     "links.github",
    "gh_url":             "links.github",

    # ── location ───────────────────────────────────────────────
    "city":               "location.city",
    "location":           "location.city",
    "country":            "location.country",
    "region":             "location.region",
    "state":              "location.region",}

_SOURCE = "csv"
_METHOD = "direct_copy"


class CsvAdapter(SourceAdapter):
    """
    Reads a CSV file with :class:`csv.DictReader` and emits one
    :class:`~src.transformer.raw_field.RawField` per non-empty cell
    in the recognised columns.

    ``emails`` and ``phones`` values are emitted as plain strings here;
    the merge stage is responsible for collecting them into lists.
    """

    def extract(self, source_path_or_url: str) -> list[RawField]:  # noqa: C901
        try:
            path = Path(source_path_or_url)

            if not path.exists():
                logger.warning(
                    "CsvAdapter: file not found: %r — returning []",
                    source_path_or_url,
                )
                return []

            fields: list[RawField] = []

            with path.open(newline="", encoding="utf-8-sig") as fh:
                reader = csv.DictReader(fh)

                row_count = 0
                for row_index, row in enumerate(reader):
                    row_count += 1
                    for col, raw_value in row.items():
                        col_norm = col.strip().lower()
                        field_name = _COLUMN_ALIASES.get(col_norm)
                        if field_name is None:
                            continue
                        # Skip blank / missing cells; never skip the whole row.
                        if raw_value is None or str(raw_value).strip() == "":
                            continue

                        value = str(raw_value).strip()

                        fields.append(
                            RawField(
                                field_name=field_name,
                                value=value,
                                source=_SOURCE,
                                method=_METHOD,
                                raw_text=value,  # same as value for direct copies
                                record_index=row_index,
                            )
                        )

                if row_count == 0:
                    logger.warning(
                        "CsvAdapter: file %r is empty or has only a header row — returning []",
                        source_path_or_url,
                    )
                    return []

            return fields

        except Exception as exc:
            logger.warning(
                "CsvAdapter.extract() failed for %r: %s",
                source_path_or_url,
                exc,
            )
            return []