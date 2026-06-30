"""Tests identity resolution.""" 
"""
tests/test_identity_resolution.py
────────────────────────────────────────────────────────────────────────────────
Tests for the two-tier deterministic blocking in resolve_identities().

Positive case:  two records sharing the same email → same candidate_id.
Negative case:  two records with similar names but different companies →
                DIFFERENT candidate_ids (proves the threshold works correctly
                and does not over-merge).
────────────────────────────────────────────────────────────────────────────────
"""

import pytest

from src.transformer.raw_field import RawField
from src.transformer.resolve.identity import resolve_identities


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_field(field_name: str, value: str, source: str, record_index: int) -> RawField:
    return RawField(
        field_name=field_name,
        value=value,
        source=source,
        method="direct_copy",
        record_index=record_index,
    )


def _csv_record(idx: int, name: str, email: str, company: str) -> list[RawField]:
    return [
        _make_field("full_name",        name,    "csv", idx),
        _make_field("email",            email,   "csv", idx),
        _make_field("current_company",  company, "csv", idx),
    ]


def _ats_record(idx: int, name: str, email: str, company: str) -> list[RawField]:
    return [
        _make_field("full_name",        name,    "ats_json", idx),
        _make_field("email",            email,   "ats_json", idx),
        _make_field("current_company",  company, "ats_json", idx),
    ]


# ─────────────────────────────────────────────────────────────────────────────
# Positive case — Tier 1: exact email match
# ─────────────────────────────────────────────────────────────────────────────

def test_same_email_resolves_to_same_candidate():
    """
    Two records from different sources sharing the same email address must
    resolve to exactly ONE candidate_id (Tier 1 exact match).
    """
    shared_email = "jordan.ellis@gmail.com"

    csv_fields = _csv_record(0, "Jordan Ellis", shared_email, "Stripe")
    ats_fields = _ats_record(0, "Jordan Ellis", shared_email, "Stripe")

    all_fields = csv_fields + ats_fields
    groups = resolve_identities(all_fields)

    # There must be exactly one candidate.
    assert len(groups) == 1, (
        f"Expected 1 candidate group, got {len(groups)}. "
        "Two records with the same email must resolve to the same candidate."
    )

    # All input fields must appear in the output exactly once.
    all_output_fields = list(groups.values())[0]
    assert len(all_output_fields) == len(all_fields)


# ─────────────────────────────────────────────────────────────────────────────
# Positive case — Tier 1: exact phone match (different email)
# ─────────────────────────────────────────────────────────────────────────────

def test_same_phone_resolves_to_same_candidate():
    """
    Two records with the same E.164 phone but different email addresses still
    resolve to one candidate via Tier-1 phone blocking.
    """
    fields_a = [
        _make_field("full_name", "Priya Nair",             "csv",      0),
        _make_field("email",     "priya@work.com",          "csv",      0),
        _make_field("phone",     "+442079460312",           "csv",      0),
    ]
    fields_b = [
        _make_field("full_name", "Priya Nair",             "ats_json", 0),
        _make_field("email",     "priya.personal@gmail.com", "ats_json", 0),
        _make_field("phone",     "+44 20 7946 0312",        "ats_json", 0),
    ]

    groups = resolve_identities(fields_a + fields_b)
    assert len(groups) == 1, (
        "Two records sharing an E.164-equivalent phone must resolve to one candidate."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Negative case — Tier 2 threshold: similar names, DIFFERENT companies
# ─────────────────────────────────────────────────────────────────────────────

def test_similar_name_different_company_resolves_to_different_candidates():
    """
    Two records with similar names (would pass the name threshold in isolation)
    but at clearly different companies must resolve to TWO candidates.

    This proves the Tier-2 compound condition (name AND company) is enforced:
    name similarity alone is not sufficient for a match.
    """
    # "Marcus Webb" and "Marcus Weber" — name similarity is high (~90+)
    # but their companies are completely different, so they must NOT merge.
    fields_a = _csv_record(0, "Marcus Webb",  "marcus.webb@shopify.com",  "Shopify")
    fields_b = _ats_record(0, "Marcus Weber", "m.weber@deepmind.com",     "DeepMind")

    groups = resolve_identities(fields_a + fields_b)

    assert len(groups) == 2, (
        f"Expected 2 candidate groups, got {len(groups)}. "
        "Similar names at different companies must NOT be merged."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Negative case — completely different people
# ─────────────────────────────────────────────────────────────────────────────

def test_completely_different_records_resolve_to_different_candidates():
    """
    Two records with no shared email, phone, or name similarity must always
    produce two distinct candidates.
    """
    fields_a = _csv_record(0, "Aiko Tanaka",   "aiko@mercari.com",  "Mercari")
    fields_b = _csv_record(1, "Dmitri Volkov",  "dv@yandex.com",    "Yandex")

    groups = resolve_identities(fields_a + fields_b)

    assert len(groups) == 2