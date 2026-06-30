from __future__ import annotations
"""Reads ATS JSON exports."""
"""
adapters/ats_adapter.py — adapter for ATS (Applicant Tracking System) JSON exports.

The ATS uses non-canonical field names.  This adapter translates them using
the mapping below before emitting :class:`~src.transformer.raw_field.RawField`
instances.

Field mapping (ATS key → logical field name)
---------------------------------------------
    cand_full_nm  → full_name
    primary_eml   → emails
    mobile_no     → phones
    employer_nm   → current_company
    job_ttl       → current_title
    gh_url        → links.github
    loc_city      → location.city
    loc_country   → location.country

Rules
-----
* source='ats_json', method='field_remap' on every RawField emitted.
* A missing key in a record skips only that field, not the whole record.
* record_index is set to the record's position in the JSON list, so every
  RawField from the same ATS record carries the same record_index — this
  mirrors CsvAdapter's approach: the adapter is the only place that knows
  the record boundary, so it assigns the index itself.
* If the top-level JSON is not a list, or the file is missing / malformed,
  log a WARNING and return [].
* No exception ever propagates out of extract().
"""

import json
import logging
from pathlib import Path

from src.transformer.adapters.base import SourceAdapter
from src.transformer.raw_field import RawField

logger = logging.getLogger(__name__)

# ATS field name → canonical logical field name used in RawField.field_name.
# Every known ATS/CRM field name variant → canonical logical field name.
# Keys are lowercase-stripped for case-insensitive matching.
_ATS_FIELD_ALIASES: dict[str, str] = {
    # ── full name ──────────────────────────────────────────────────────────
    "cand_full_nm":       "full_name",
    "candidate_name":     "full_name",
    "full_name":          "full_name",
    "fullname":           "full_name",
    "name":               "full_name",
    "candidate":          "full_name",
    "applicant_name":     "full_name",
    "applicant":          "full_name",
    "contact_name":       "full_name",
    "person_name":        "full_name",

    # ── email ──────────────────────────────────────────────────────────────
    "primary_eml":        "emails",
    "email":              "emails",
    "email_address":      "emails",
    "emails":             "emails",
    "e_mail":             "emails",
    "work_email":         "emails",
    "personal_email":     "emails",
    "candidate_email":    "emails",
    "applicant_email":    "emails",

    # ── phone ──────────────────────────────────────────────────────────────
    "mobile_no":          "phones",
    "mobile":             "phones",
    "phone":              "phones",
    "phone_number":       "phones",
    "phones":             "phones",
    "cell":               "phones",
    "telephone":          "phones",
    "contact_number":     "phones",
    "mobile_number":      "phones",
    "work_phone":         "phones",

    # ── company ────────────────────────────────────────────────────────────
    "employer_nm":        "current_company",
    "employer":           "current_company",
    "company":            "current_company",
    "current_company":    "current_company",
    "organization":       "current_company",
    "organisation":       "current_company",
    "current_employer":   "current_company",
    "firm":               "current_company",
    "workplace":          "current_company",
    "org":                "current_company",

    # ── title ──────────────────────────────────────────────────────────────
    "job_ttl":            "current_title",
    "job_title":          "current_title",
    "title":              "current_title",
    "current_title":      "current_title",
    "position":           "current_title",
    "role":               "current_title",
    "designation":        "current_title",
    "job_role":           "current_title",
    "current_position":   "current_title",
    "current_role":       "current_title",

    # ── github ─────────────────────────────────────────────────────────────
    "gh_url":             "links.github",
    "github_url":         "links.github",
    "github":             "links.github",
    "github_profile":     "links.github",
    "github_link":        "links.github",

    # ── linkedin ───────────────────────────────────────────────────────────
    "linkedin_url":       "links.linkedin",
    "linkedin":           "links.linkedin",
    "linkedin_profile":   "links.linkedin",
    "linkedin_link":      "links.linkedin",

    # ── location ───────────────────────────────────────────────────────────
    "loc_city":           "location.city",
    "city":               "location.city",
    "location_city":      "location.city",
    "location":           "location.city",
    "loc_country":        "location.country",
    "country":            "location.country",
    "location_country":   "location.country",
    "loc_region":         "location.region",
    "region":             "location.region",
    "state":              "location.region",
    "location_region":    "location.region",

    # ── portfolio / website ────────────────────────────────────────────────
    "portfolio_url":      "links.portfolio",
    "portfolio":          "links.portfolio",
    "website":            "links.other",
    "personal_website":   "links.other",
}

_SOURCE = "ats_json"
_METHOD = "field_remap"


class AtsJsonAdapter(SourceAdapter):
    """
    Reads a JSON file whose top-level value is a list of ATS candidate
    records and emits one :class:`~src.transformer.raw_field.RawField`
    per present (non-null, non-empty) mapped key per record.

    Dot-notation field names (e.g. ``"location.city"``) are preserved
    verbatim in ``RawField.field_name`` so that downstream stages can
    route them into the correct nested sub-model.
    """

    def extract(self, source_path_or_url: str) -> list[RawField]:
        try:
            path = Path(source_path_or_url)

            if not path.exists():
                logger.warning(
                    "AtsJsonAdapter: file not found: %r — returning []",
                    source_path_or_url,
                )
                return []

            raw = path.read_text(encoding="utf-8")

            try:
                records = json.loads(raw)
            except json.JSONDecodeError as exc:
                logger.warning(
                    "AtsJsonAdapter: malformed JSON in %r: %s — returning []",
                    source_path_or_url,
                    exc,
                )
                return []

            if not isinstance(records, list):
                logger.warning(
                    "AtsJsonAdapter: top-level JSON in %r is %s, expected list — returning []",
                    source_path_or_url,
                    type(records).__name__,
                )
                return []

            fields: list[RawField] = []

            for record_index, record in enumerate(records):
                if not isinstance(record, dict):
                    # Malformed entry — skip just this record, keep going.
                    logger.warning(
                        "AtsJsonAdapter: skipping non-dict record in %r: %r",
                        source_path_or_url,
                        record,
                    )
                    continue

                for raw_key, raw_value in record.items():
                    canonical_field = _ATS_FIELD_ALIASES.get(raw_key.strip().lower())
                    if canonical_field is None:
                        continue

                    # Skip null / empty strings; keep 0 and False (valid values).
                    if raw_value is None:
                        continue
                    if isinstance(raw_value, str) and raw_value.strip() == "":
                        continue

                    value = raw_value.strip() if isinstance(raw_value, str) else raw_value

                    fields.append(
                        RawField(
                            field_name=canonical_field,
                            value=value,
                            source=_SOURCE,
                            method=_METHOD,
                            raw_text=str(raw_value) if isinstance(raw_value, str) else None,
                            record_index=record_index,
                        )
                    )

            return fields

        except Exception as exc:
            logger.warning(
                "AtsJsonAdapter.extract() failed for %r: %s",
                source_path_or_url,
                exc,
            )
            return []