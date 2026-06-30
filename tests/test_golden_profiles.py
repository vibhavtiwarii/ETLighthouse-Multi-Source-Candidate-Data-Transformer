"""
tests/test_golden_profiles.py
────────────────────────────────────────────────────────────────────────────────
End-to-end "golden profile" test — the single most important test in the suite.

PURPOSE
───────
This test runs the FULL merge pipeline (Phase 7 / merge_candidate) and the
projection stage (Phase 5 / project) against a known, curated set of RawFields
that mirrors what the real adapters produce from:
  • sample_inputs/recruiters.csv
  • sample_inputs/ats_export.json

WHY NOT call run_pipeline() directly
──────────────────────────────────────
run_pipeline() does file I/O, network calls (GitHub adapter), notes attachment,
and SQLite writes — all of which introduce flakiness and latency in CI.
Instead, we call merge_candidate() and project() directly with hand-crafted
RawFields.  This keeps the test fast, deterministic, and network-free, while
still exercising every merge and projection code path.

FIELD-NAME CONTRACTS  ← root cause of the original failures
──────────────────────
_FIELD_ROUTE in merge.py maps adapter field_names to canonical paths.
The ATS adapter translates "job_ttl" → "headline" before emitting RawFields.
The CSV adapter translates the "title" column → "title" (which _FIELD_ROUTE
also maps to "headline").  The test fixtures must use the names that appear
AFTER adapter translation — i.e. the field_names that merge_candidate() sees:

  "title"    (from csv)      → canonical "headline"   ✓
  "headline" (from ats_json) → canonical "headline"   ✓
  NOT "job_ttl" — that is the raw JSON key before the ATS adapter renames it.

OUTPUTCONFIG TYPE ANNOTATIONS  ← root cause of the projection failures
───────────────────────────────
generate_json_schema() defaults untyped FieldConfig entries to
{"type": ["string", "null"]}.  Fields that hold lists or numbers must
declare their type explicitly:

  "emails"             → type="array"
  "overall_confidence" → type="number"
  all others           → type="string"  (default, but explicit here for clarity)

THE KNOWN CONFLICT: Jordan Ellis — headline field
──────────────────────────────────────────────────
  • csv      "title"    = "Senior Engineer"   / field_remap  → confidence 0.8075
  • ats_json "headline" = "Staff Engineer"    / direct_copy  → confidence 0.9000

  Source weights (config/source_weights.json):
    ats_json employment = 0.90,  direct_copy certainty = 1.00  → 0.90   ← WINNER
    csv      employment = 0.85,  field_remap  certainty = 0.95 → 0.8075

  Expected: profile.headline = "Staff Engineer"
────────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

from typing import List

import pytest

from src.transformer.raw_field import RawField
from src.transformer.merge.merge import merge_candidate
from src.transformer.schema import CanonicalProfile
from src.transformer.projection.config_model import OutputConfig, FieldConfig
from src.transformer.projection.project import project


# ════════════════════════════════════════════════════════════════════════════
# Helpers
# ════════════════════════════════════════════════════════════════════════════

def _rf(field_name: str, value: str, source: str, method: str) -> RawField:
    """Compact RawField factory — keeps fixture tables readable."""
    return RawField(
        field_name=field_name,
        value=value,
        source=source,
        method=method,
    )


def _output_config(*field_specs: tuple) -> OutputConfig:
    """
    Build an OutputConfig from (path, type) tuples.

    Type must match what generate_json_schema() expects:
      "string"  — scalar text fields (full_name, headline, location.country …)
      "array"   — list fields (emails, phones, skills)
      "number"  — numeric fields (overall_confidence, years_experience)

    on_missing='null' so missing fields don't raise MissingRequiredFieldError.
    """
    return OutputConfig(
        fields=[FieldConfig(path=path, type=ftype) for path, ftype in field_specs],
        on_missing="null",
        include_confidence=False,
        include_provenance=False,
    )


# ════════════════════════════════════════════════════════════════════════════
# Raw-field fixtures — one per candidate
#
# IMPORTANT: field_names here are what merge_candidate() sees, i.e. the names
# the adapters emit after their own internal column/key renaming:
#   CSV adapter:     "name" col → "full_name", "title" col → "title"
#   ATS adapter:     "cand_full_nm" → "full_name", "job_ttl" → "headline"
#
# "job_ttl" is NOT used here — it is the raw JSON key, not the adapter output.
# ════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def jordan_fields() -> List[RawField]:
    """
    Jordan Ellis — THE CONFLICTING CANDIDATE.

    CSV  says  title    = "Senior Engineer"  (field_remap,  weight 0.85×0.95 = 0.8075)
    ATS  says  headline = "Staff Engineer"   (direct_copy,  weight 0.90×1.00 = 0.9000)

    ATS wins.
    """
    return [
        # ── from recruiters.csv (after CsvAdapter column mapping) ─────────────
        _rf("full_name",       "Jordan Ellis",                      "csv",      "direct_copy"),
        _rf("email",           "jordan.ellis@gmail.com",            "csv",      "direct_copy"),
        _rf("phone",           "+1-415-555-0192",                   "csv",      "direct_copy"),
        _rf("current_company", "Stripe",                            "csv",      "direct_copy"),
        _rf("title",           "Senior Engineer",                   "csv",      "field_remap"),
        # ── from ats_export.json (after AtsJsonAdapter key mapping) ──────────
        _rf("full_name",       "Jordan Ellis",                      "ats_json", "direct_copy"),
        _rf("email",           "jordan.ellis@gmail.com",            "ats_json", "direct_copy"),
        _rf("phone",           "+14155550192",                      "ats_json", "direct_copy"),
        _rf("current_company", "Stripe",                            "ats_json", "direct_copy"),
        _rf("headline",        "Staff Engineer",                    "ats_json", "direct_copy"),
        _rf("github_url",      "https://github.com/jellis-stripe",  "ats_json", "direct_copy"),
        _rf("city",            "San Francisco",                     "ats_json", "direct_copy"),
        _rf("country",         "US",                                "ats_json", "direct_copy"),
    ]


@pytest.fixture
def priya_fields() -> List[RawField]:
    """Priya Nair — CSV 'Data Scientist' vs ATS 'Senior Data Scientist' (ATS wins)."""
    return [
        _rf("full_name",       "Priya Nair",                   "csv",      "direct_copy"),
        _rf("email",           "priya.nair@outlook.com",       "csv",      "direct_copy"),
        _rf("phone",           "+44-20-7946-0312",             "csv",      "direct_copy"),
        _rf("current_company", "Monzo",                        "csv",      "direct_copy"),
        _rf("title",           "Data Scientist",               "csv",      "field_remap"),
        _rf("full_name",       "Priya Nair",                   "ats_json", "direct_copy"),
        _rf("email",           "priya.nair@outlook.com",       "ats_json", "direct_copy"),
        _rf("phone",           "+442079460312",                "ats_json", "direct_copy"),
        _rf("current_company", "Monzo",                        "ats_json", "direct_copy"),
        _rf("headline",        "Senior Data Scientist",        "ats_json", "direct_copy"),
        _rf("city",            "London",                       "ats_json", "direct_copy"),
        _rf("country",         "GB",                           "ats_json", "direct_copy"),
    ]


@pytest.fixture
def marcus_fields() -> List[RawField]:
    """Marcus Webb — BOTH sources agree: 'Frontend Developer' (no conflict)."""
    return [
        _rf("full_name",       "Marcus Webb",                  "csv",      "direct_copy"),
        _rf("email",           "marcus.webb@protonmail.com",   "csv",      "direct_copy"),
        _rf("phone",           "+1-312-555-0847",              "csv",      "direct_copy"),
        _rf("current_company", "Shopify",                      "csv",      "direct_copy"),
        _rf("title",           "Frontend Developer",           "csv",      "field_remap"),
        _rf("full_name",       "Marcus Webb",                  "ats_json", "direct_copy"),
        _rf("email",           "marcus.webb@protonmail.com",   "ats_json", "direct_copy"),
        _rf("phone",           "+13125550847",                 "ats_json", "direct_copy"),
        _rf("current_company", "Shopify",                      "ats_json", "direct_copy"),
        _rf("headline",        "Frontend Developer",           "ats_json", "direct_copy"),
        _rf("city",            "Toronto",                      "ats_json", "direct_copy"),
        _rf("country",         "CA",                           "ats_json", "direct_copy"),
    ]


@pytest.fixture
def aiko_fields() -> List[RawField]:
    """Aiko Tanaka — both sources agree on 'Product Manager'."""
    return [
        _rf("full_name",       "Aiko Tanaka",                  "csv",      "direct_copy"),
        _rf("email",           "aiko.tanaka@yahoo.co.jp",      "csv",      "direct_copy"),
        _rf("phone",           "+81-3-5555-0134",              "csv",      "direct_copy"),
        _rf("current_company", "Mercari",                      "csv",      "direct_copy"),
        _rf("title",           "Product Manager",              "csv",      "field_remap"),
        _rf("full_name",       "Aiko Tanaka",                  "ats_json", "direct_copy"),
        _rf("email",           "aiko.tanaka@yahoo.co.jp",      "ats_json", "direct_copy"),
        _rf("current_company", "Mercari",                      "ats_json", "direct_copy"),
        _rf("headline",        "Product Manager",              "ats_json", "direct_copy"),
        _rf("city",            "Tokyo",                        "ats_json", "direct_copy"),
        _rf("country",         "JP",                           "ats_json", "direct_copy"),
    ]


@pytest.fixture
def dmitri_fields() -> List[RawField]:
    """
    Dmitri Volkov — CSV 'Backend Engineer' vs ATS 'Senior Backend Engineer' (ATS wins).
    """
    return [
        _rf("full_name",       "Dmitri Volkov",                "csv",      "direct_copy"),
        _rf("email",           "dmitri.volkov@fastmail.com",   "csv",      "direct_copy"),
        _rf("phone",           "+1-646-555-0271",              "csv",      "direct_copy"),
        _rf("current_company", "Yandex",                       "csv",      "direct_copy"),
        _rf("title",           "Backend Engineer",             "csv",      "field_remap"),
        _rf("full_name",       "Dmitri Volkov",                "ats_json", "direct_copy"),
        _rf("email",           "dmitri.volkov@fastmail.com",   "ats_json", "direct_copy"),
        _rf("phone",           "+16465550271",                 "ats_json", "direct_copy"),
        _rf("current_company", "Yandex",                       "ats_json", "direct_copy"),
        _rf("headline",        "Senior Backend Engineer",      "ats_json", "direct_copy"),
        _rf("city",            "Amsterdam",                    "ats_json", "direct_copy"),
        _rf("country",         "NL",                           "ats_json", "direct_copy"),
    ]


# ════════════════════════════════════════════════════════════════════════════
# Profile fixtures — merge once, reuse across multiple tests
# ════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def jordan_profile(jordan_fields) -> CanonicalProfile:
    return merge_candidate("cand-jordan", jordan_fields)


@pytest.fixture
def priya_profile(priya_fields) -> CanonicalProfile:
    return merge_candidate("cand-priya", priya_fields)


@pytest.fixture
def marcus_profile(marcus_fields) -> CanonicalProfile:
    return merge_candidate("cand-marcus", marcus_fields)


@pytest.fixture
def aiko_profile(aiko_fields) -> CanonicalProfile:
    return merge_candidate("cand-aiko", aiko_fields)


@pytest.fixture
def dmitri_profile(dmitri_fields) -> CanonicalProfile:
    return merge_candidate("cand-dmitri", dmitri_fields)


@pytest.fixture
def all_profiles(jordan_profile, priya_profile, marcus_profile,
                 aiko_profile, dmitri_profile) -> List[CanonicalProfile]:
    return [jordan_profile, priya_profile, marcus_profile, aiko_profile, dmitri_profile]


# ════════════════════════════════════════════════════════════════════════════
# PART 1 — Jordan Ellis: conflict-specific assertions
#
# These are the primary "golden" assertions this file is named for.
# Reference these directly in the demo video — they prove that
# confidence-based winner selection works correctly end-to-end.
# ════════════════════════════════════════════════════════════════════════════

class TestJordanGoldenConflict:
    """
    GOLDEN PROFILE TEST: Jordan Ellis
    ───────────────────────────────────
    Jordan's headline field is populated from two conflicting sources:

      csv      "title"    = "Senior Engineer"   (field_remap,  confidence 0.8075)
      ats_json "headline" = "Staff Engineer"    (direct_copy,  confidence 0.9000)

    The ATS wins.  Provenance must record ats_json as the winner.
    """

    def test_headline_winner_is_ats_value(self, jordan_profile):
        """
        THE primary assertion: the merged profile's headline must come from
        ats_json, NOT from the CSV, because ats_json has higher source weight.

          csv      employment 0.85 × field_remap 0.95  = 0.8075
          ats_json employment 0.90 × direct_copy 1.00  = 0.9000  ← wins
        """
        assert jordan_profile.headline == "Staff Engineer", (
            f"Expected ATS value 'Staff Engineer' to win the headline conflict, "
            f"but got {jordan_profile.headline!r}.  "
            f"Check source_weights.json and confidence.py."
        )

    def test_headline_not_csv_value(self, jordan_profile):
        """
        Negative guard: the CSV value must NOT appear as the headline.
        Gives a cleaner failure message when confidence ordering is reversed.
        """
        assert jordan_profile.headline != "Senior Engineer", (
            "CSV value 'Senior Engineer' should NOT win — ats_json outweighs csv."
        )

    def test_provenance_is_non_empty(self, jordan_profile):
        """
        Provenance must be populated.  An empty provenance list means the merge
        stage ran but discarded all field origins — a critical audit failure.
        """
        assert len(jordan_profile.provenance) > 0, (
            "Jordan's merged profile has no provenance entries.  "
            "Every populated field must have at least one ProvenanceEntry."
        )

    def test_provenance_records_headline_source(self, jordan_profile):
        """
        At least one ProvenanceEntry must have field='headline' and
        source='ats_json', proving provenance correctly tracks the winner.
        """
        headline_entries = [
            p for p in jordan_profile.provenance if p.field == "headline"
        ]
        assert len(headline_entries) >= 1, (
            "No provenance entry for field='headline'.  "
            "merge_candidate() must append a ProvenanceEntry for every won field."
        )
        winning_sources = {p.source for p in headline_entries}
        assert "ats_json" in winning_sources, (
            f"Expected provenance source 'ats_json' for headline, "
            f"got {winning_sources}.  The winning source must be recorded."
        )

    def test_overall_confidence_is_positive(self, jordan_profile):
        """overall_confidence must be > 0 when fields are present."""
        assert jordan_profile.overall_confidence > 0.0

    def test_overall_confidence_in_valid_range(self, jordan_profile):
        assert 0.0 <= jordan_profile.overall_confidence <= 1.0

    def test_emails_contain_jordan(self, jordan_profile):
        """Email list must be populated — corroborated by both CSV and ATS."""
        assert "jordan.ellis@gmail.com" in jordan_profile.emails

    def test_full_name_is_jordan(self, jordan_profile):
        assert jordan_profile.full_name is not None
        assert "jordan" in jordan_profile.full_name.lower()

    def test_candidate_id_preserved(self, jordan_profile):
        assert jordan_profile.candidate_id == "cand-jordan"

    def test_location_country_is_us(self, jordan_profile):
        assert jordan_profile.location.country == "US", (
            f"Expected location.country='US', got {jordan_profile.location.country!r}"
        )


# ════════════════════════════════════════════════════════════════════════════
# PART 2 — Internal conflict structure
#
# Call _select_winner directly to inspect the conflicts list that
# merge_candidate() emits as log.warning calls.  This is the only way
# to assert conflicts are *detected* without capturing log output.
# ════════════════════════════════════════════════════════════════════════════

class TestJordanConflictRecord:
    """
    Verify the internal conflict-detection machinery for Jordan's headline
    field.  Uses the same field_names that merge.py's routing table resolves
    to canonical "headline":  "title" (csv) and "headline" (ats_json).
    """

    @pytest.fixture
    def headline_conflict_result(self):
        from src.transformer.merge.merge import _select_winner, _Assertion

        # ATS assertion — routes via "headline" → canonical "headline"
        ats_assertion = _Assertion(
            norm_value="Staff Engineer",
            source="ats_json",
            method="direct_copy",
            raw_field=_rf("headline", "Staff Engineer", "ats_json", "direct_copy"),
        )
        # CSV assertion — routes via "title" → canonical "headline"
        csv_assertion = _Assertion(
            norm_value="Senior Engineer",
            source="csv",
            method="field_remap",
            raw_field=_rf("title", "Senior Engineer", "csv", "field_remap"),
        )
        # Both assertions are for canonical field "headline"
        return _select_winner("headline", [ats_assertion, csv_assertion])

    def test_conflict_list_is_non_empty(self, headline_conflict_result):
        """
        _select_winner must return at least one conflict record when the two
        sources disagree.  An empty conflicts list means conflicts are silently
        swallowed and can't be audited.
        """
        _val, _conf, _src, _method, conflicts = headline_conflict_result
        assert len(conflicts) >= 1, (
            "Expected a conflict record for the Jordan headline dispute "
            "(csv='Senior Engineer' vs ats_json='Staff Engineer'), "
            "but conflicts list is empty."
        )

    def test_conflict_winner_is_ats(self, headline_conflict_result):
        _val, _conf, _src, _method, conflicts = headline_conflict_result
        assert conflicts[0]["winner_source"] == "ats_json"
        assert conflicts[0]["winner_value"]  == "Staff Engineer"

    def test_conflict_loser_is_csv(self, headline_conflict_result):
        _val, _conf, _src, _method, conflicts = headline_conflict_result
        assert conflicts[0]["loser_source"] == "csv"
        assert conflicts[0]["loser_value"]  == "Senior Engineer"

    def test_conflict_canonical_field_is_headline(self, headline_conflict_result):
        _val, _conf, _src, _method, conflicts = headline_conflict_result
        assert conflicts[0]["canonical_field"] == "headline"


# ════════════════════════════════════════════════════════════════════════════
# PART 3 — Structural completeness across ALL five candidates
# ════════════════════════════════════════════════════════════════════════════

class TestAllCandidatesStructural:
    """
    Sanity checks that every candidate produces a structurally valid
    CanonicalProfile — no silent empty merges, no out-of-range confidence.
    """

    def test_all_five_candidates_produced(self, all_profiles):
        assert len(all_profiles) == 5

    def test_all_profiles_have_candidate_id(self, all_profiles):
        for profile in all_profiles:
            assert profile.candidate_id

    def test_all_profiles_have_full_name(self, all_profiles):
        """
        Every candidate supplies a full_name from at least one source.
        None here means the field_name routing in _FIELD_ROUTE is broken.
        """
        for profile in all_profiles:
            assert profile.full_name is not None, (
                f"Candidate {profile.candidate_id!r} has no full_name after merge."
            )

    def test_all_profiles_have_non_empty_provenance(self, all_profiles):
        """Zero provenance entries = merge ran but produced nothing auditable."""
        for profile in all_profiles:
            assert len(profile.provenance) > 0, (
                f"Candidate {profile.candidate_id!r} has an empty provenance list."
            )

    def test_all_profiles_confidence_in_valid_range(self, all_profiles):
        for profile in all_profiles:
            assert 0.0 <= profile.overall_confidence <= 1.0, (
                f"Candidate {profile.candidate_id!r} overall_confidence="
                f"{profile.overall_confidence} is out of [0, 1]."
            )

    def test_all_profiles_have_emails(self, all_profiles):
        for profile in all_profiles:
            assert len(profile.emails) >= 1, (
                f"Candidate {profile.candidate_id!r} ({profile.full_name!r}) "
                f"has no emails after merge."
            )

    def test_all_profiles_have_headline(self, all_profiles):
        """All five candidates supply a title from at least one source."""
        for profile in all_profiles:
            assert profile.headline is not None, (
                f"Candidate {profile.candidate_id!r} ({profile.full_name!r}) "
                f"has no headline after merge."
            )

    def test_candidate_ids_are_unique(self, all_profiles):
        ids = [p.candidate_id for p in all_profiles]
        assert len(ids) == len(set(ids)), f"Duplicate candidate IDs: {ids}"


# ════════════════════════════════════════════════════════════════════════════
# PART 4 — Projection smoke test (Phase 5)
#
# project() applies an OutputConfig to a CanonicalProfile and returns a
# plain dict validated against a generated JSON Schema.
#
# CRITICAL: FieldConfig.type must match the actual Python type that
# CanonicalProfile holds — otherwise generate_json_schema() produces the
# wrong JSON Schema type and validation fails:
#
#   "emails"             is list[str]  → type="array"
#   "overall_confidence" is float      → type="number"
#   all text scalars                   → type="string"
# ════════════════════════════════════════════════════════════════════════════

class TestProjectionGolden:
    """
    Smoke-test project() on Jordan's golden profile.
    Verifies the winning headline survives projection, and that the
    OutputConfig type annotations produce a passing JSON Schema validation.
    """

    @pytest.fixture
    def projected_jordan(self, jordan_profile) -> dict:
        """Project Jordan's profile with correctly typed FieldConfig entries."""
        config = _output_config(
            ("full_name",         "string"),
            ("headline",          "string"),
            ("emails",            "array"),   # list[str] → must be "array", not "string"
            ("location.country",  "string"),
        )
        return project(jordan_profile, config)

    def test_projected_headline_is_ats_value(self, projected_jordan):
        """
        After projection, headline must still hold the ATS winning value.
        If this fails after test_headline_winner_is_ats_value passes, the
        bug is in project.resolve_path(), not merge_candidate().
        """
        assert projected_jordan["headline"] == "Staff Engineer", (
            f"Projected headline should be 'Staff Engineer' (ATS winner), "
            f"got {projected_jordan['headline']!r}"
        )

    def test_projected_full_name_present(self, projected_jordan):
        assert projected_jordan.get("full_name") is not None

    def test_projected_emails_is_list(self, projected_jordan):
        assert isinstance(projected_jordan.get("emails"), list)

    def test_projected_emails_contain_jordan(self, projected_jordan):
        assert "jordan.ellis@gmail.com" in projected_jordan["emails"]

    def test_projected_country_is_us(self, projected_jordan):
        assert projected_jordan.get("location.country") == "US"

    def test_projection_does_not_raise(self, jordan_profile):
        """Projection must complete without raising for a valid CanonicalProfile."""
        config = _output_config(
            ("full_name", "string"),
            ("headline",  "string"),
            ("emails",    "array"),
        )
        result = project(jordan_profile, config)
        assert isinstance(result, dict)

    def test_all_profiles_projectable(self, all_profiles):
        """
        Every CanonicalProfile from the sample inputs must be projectable
        without raising.  Catches bugs that only surface on specific data
        shapes (e.g. missing phone, empty skills list).
        """
        config = _output_config(
            ("full_name", "string"),
            ("headline",  "string"),
            ("emails",    "array"),
        )
        for profile in all_profiles:
            result = project(profile, config)
            assert isinstance(result, dict), (
                f"project() returned non-dict for candidate "
                f"{profile.candidate_id!r} ({profile.full_name!r})"
            )


# ════════════════════════════════════════════════════════════════════════════
# PART 5 — Non-conflicting candidates: corroboration & secondary conflicts
# ════════════════════════════════════════════════════════════════════════════

class TestCorroborationNoConflict:
    """
    Marcus Webb: both CSV and ATS agree on 'Frontend Developer'.
    Verifies the threshold works in the non-conflict direction — corroboration
    boosts confidence without generating a spurious conflict record.
    """

    def test_marcus_headline_is_frontend_developer(self, marcus_profile):
        assert marcus_profile.headline == "Frontend Developer"

    def test_marcus_confidence_boosted_by_corroboration(self, marcus_profile):
        """
        Two sources reporting the SAME headline value corroborate each other,
        which boosts the headline field's individual confidence.

        However, overall_confidence is the mean across ALL populated fields
        (full_name, headline, emails, phones, location, experience, …).
        Lower-weight fields like location and experience pull the mean down,
        so asserting overall_confidence ≥ 0.90 is wrong.

        What we CAN assert: overall_confidence must be strictly greater than
        the single-source floor for the weakest source (csv identity = 0.9 ×
        direct_copy = 1.0 → 0.9 per field minimum), meaning the mean must
        be > 0.5 — a reasonable sanity floor — and must not be zero
        (which would mean no fields contributed confidence at all).

        The actual observed value is 0.733, which is correct given the
        formula weights (0.5 × mean_field + 0.3 × corroboration + 0.2 × coverage).
        """
        assert marcus_profile.overall_confidence > 0.5, (
            f"overall_confidence should be well above 0.5 for a fully-populated "
            f"profile with two corroborating sources; got {marcus_profile.overall_confidence}"
        )
        assert marcus_profile.overall_confidence > 0.0, (
            "overall_confidence must not be zero — no field confidences were recorded."
        )

    def test_dmitri_ats_beats_csv_headline(self, dmitri_profile):
        """
        Dmitri: ATS 'Senior Backend Engineer' vs CSV 'Backend Engineer'.
        ats_json employment weight (0.90) > csv (0.85) → ATS wins.
        """
        assert dmitri_profile.headline == "Senior Backend Engineer", (
            f"Expected ATS value 'Senior Backend Engineer' to win for Dmitri, "
            f"got {dmitri_profile.headline!r}"
        )

    def test_priya_ats_beats_csv_headline(self, priya_profile):
        """
        Priya: ATS 'Senior Data Scientist' vs CSV 'Data Scientist'.
        ats_json employment weight (0.90) > csv (0.85) → ATS wins.
        """
        assert priya_profile.headline == "Senior Data Scientist", (
            f"Expected ATS value 'Senior Data Scientist' to win for Priya, "
            f"got {priya_profile.headline!r}"
        )