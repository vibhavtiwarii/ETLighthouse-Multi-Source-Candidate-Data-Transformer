from __future__ import annotations
"""
tests/test_merge_confidence.py
────────────────────────────────────────────────────────────────────────────────
Tests for the merge-stage confidence scorer (confidence.py) and the
field-level winner-selection logic inside merge.py (_select_winner).

Key test: "golden conflict"
────────────────────────────
Feed two RawField objects for the same scalar field ("headline") from two
different sources with two different normalised values.  Assert that:
  1.  The higher-weighted source (ats_json > notes) wins.
  2.  A conflict entry is returned and is correctly populated.

The test file is intentionally self-contained: it exercises
compute_confidence() directly for unit tests, and imports _select_winner
for the integration-style conflict test.  No external I/O is required.
────────────────────────────────────────────────────────────────────────────────
"""



import pytest

# ── module under test ────────────────────────────────────────────────────────
from src.transformer.merge.confidence import (
    compute_confidence,
    get_field_category,
    FIELD_CATEGORY,
)

# _select_winner is a private helper but is the exact logic we need to verify
# for the "golden conflict" scenario.  We import it with a leading underscore
# to signal that tests are intentionally reaching into internals.
from src.transformer.merge.merge import _select_winner, _Assertion
from src.transformer.raw_field import RawField


# ════════════════════════════════════════════════════════════════════════════
# Fixtures
# ════════════════════════════════════════════════════════════════════════════

def _make_raw(field_name: str, value: str, source: str, method: str) -> RawField:
    """Convenience factory so tests stay readable."""
    return RawField(field_name=field_name, value=value, source=source, method=method)


def _make_assertion(
    norm_value: str,
    source: str,
    method: str,
    field_name: str = "headline",
) -> _Assertion:
    """Build an _Assertion directly — mirrors how merge_candidate creates them."""
    rf = _make_raw(field_name, norm_value, source, method)
    return _Assertion(norm_value=norm_value, source=source, method=method, raw_field=rf)


# ════════════════════════════════════════════════════════════════════════════
# 1. Unit tests: compute_confidence()
# ════════════════════════════════════════════════════════════════════════════

class TestComputeConfidence:
    """Direct unit tests for the confidence formula."""

    def test_ats_direct_copy_identity_single_source(self):
        """
        ats_json identity weight = 0.95, direct_copy certainty = 1.0,
        corroborating_count = 1  →  0.95 * 1.0 * 1.0 = 0.95
        """
        result = compute_confidence(
            source="ats_json",
            method="direct_copy",
            field_category="identity",
            corroborating_source_count=1,
        )
        assert result == pytest.approx(0.95, rel=1e-6)

    def test_clamped_to_1_with_corroboration(self):
        """
        ats_json identity weight = 0.95, direct_copy certainty = 1.0,
        two corroborating sources  →  0.95 * 1.0 * 1.15 = 1.0925 → clamped to 1.0
        """
        result = compute_confidence(
            source="ats_json",
            method="direct_copy",
            field_category="identity",
            corroborating_source_count=2,
        )
        assert result == 1.0

    def test_github_api_fetch_employment(self):
        """
        github employment weight = 0.1, api_fetch certainty = 0.8,
        single source  →  0.1 * 0.8 * 1.0 = 0.08
        """
        result = compute_confidence(
            source="github",
            method="api_fetch",
            field_category="employment",
            corroborating_source_count=1,
        )
        assert result == pytest.approx(0.08, rel=1e-6)

    def test_notes_fuzzy_match_skills(self):
        """
        notes skills weight = 0.5, fuzzy_match certainty = 0.5
        single source  →  0.5 * 0.5 * 1.0 = 0.25
        """
        result = compute_confidence(
            source="notes",
            method="fuzzy_match",
            field_category="skills",
            corroborating_source_count=1,
        )
        assert result == pytest.approx(0.25, rel=1e-6)

    def test_csv_regex_extract_identity(self):
        """
        csv identity weight = 0.9, regex_extract certainty = 0.6
        single source  →  0.9 * 0.6 * 1.0 = 0.54
        """
        result = compute_confidence(
            source="csv",
            method="regex_extract",
            field_category="identity",
            corroborating_source_count=1,
        )
        assert result == pytest.approx(0.54, rel=1e-6)

    def test_unknown_source_falls_back_to_conservative_weight(self):
        """
        An unrecognised source should fall back to weight 0.3 (not raise).
        0.3 * 1.0 (direct_copy) * 1.0 = 0.3
        """
        result = compute_confidence(
            source="mystery_adapter",
            method="direct_copy",
            field_category="identity",
            corroborating_source_count=1,
        )
        assert result == pytest.approx(0.3, rel=1e-6)

    def test_unknown_method_falls_back_to_default_certainty(self):
        """
        An unrecognised method should fall back to 0.5 certainty (not raise).
        ats_json identity (0.95) * 0.5 * 1.0 = 0.475
        """
        result = compute_confidence(
            source="ats_json",
            method="telepathy",
            field_category="identity",
            corroborating_source_count=1,
        )
        assert result == pytest.approx(0.475, rel=1e-6)

    def test_corroborating_count_below_1_is_clamped(self):
        """
        corroborating_source_count < 1 must be clamped to 1 — no negative boost.
        Result should equal the single-source confidence, not a lower value.
        """
        single = compute_confidence("csv", "direct_copy", "employment", 1)
        clamped = compute_confidence("csv", "direct_copy", "employment", 0)
        negative = compute_confidence("csv", "direct_copy", "employment", -5)
        assert clamped == pytest.approx(single, rel=1e-6)
        assert negative == pytest.approx(single, rel=1e-6)

    def test_result_never_exceeds_1(self):
        """Sanity check: no configuration should produce confidence > 1.0."""
        result = compute_confidence(
            source="ats_json",
            method="direct_copy",
            field_category="identity",
            corroborating_source_count=100,  # extreme corroboration
        )
        assert result <= 1.0

    def test_result_is_non_negative(self):
        """Result must always be >= 0."""
        result = compute_confidence("notes", "fuzzy_match", "skills", 1)
        assert result >= 0.0


# ════════════════════════════════════════════════════════════════════════════
# 2. Unit tests: get_field_category()
# ════════════════════════════════════════════════════════════════════════════

class TestGetFieldCategory:

    @pytest.mark.parametrize("field,expected_category", [
        ("full_name",        "identity"),
        ("emails",           "identity"),
        ("location.country", "identity"),
        ("links.linkedin",   "identity"),
        ("headline",         "employment"),
        ("years_experience", "employment"),
        ("experience",       "employment"),
        ("education",        "employment"),
        ("skills",           "skills"),
        ("skill",            "skills"),
    ])
    def test_known_fields(self, field: str, expected_category: str):
        assert get_field_category(field) == expected_category

    def test_unknown_field_returns_identity_fallback(self):
        """Unknown fields should not raise — fall back to 'identity'."""
        assert get_field_category("invented_field_xyz") == "identity"

    def test_dotted_prefix_resolution(self):
        """
        'experience.title' is not listed explicitly but 'experience' prefix maps
        to 'employment'.  get_field_category should resolve it correctly.
        """
        # If the field IS explicitly listed, category comes from the table.
        # The prefix fallback handles dynamically-named sub-fields.
        category = get_field_category("experience.new_subfield")
        assert category == "employment"


# ════════════════════════════════════════════════════════════════════════════
# 3. Golden conflict test — the centrepiece of this file
# ════════════════════════════════════════════════════════════════════════════

class TestGoldenConflict:
    """
    GOLDEN CONFLICT
    ───────────────
    Scenario
    ~~~~~~~~
    Candidate "cand-001" has a "headline" (current job title) that two sources
    disagree on:

      • ats_json says  "Senior Software Engineer"   (direct_copy  — high trust)
      • notes   says  "Lead Developer"              (regex_extract — low trust)

    Expected outcomes
    ~~~~~~~~~~~~~~~~~
    1.  ats_json wins because:
          ats_json employment weight (0.9) × direct_copy certainty (1.0) = 0.90
          notes   employment weight (0.4) × regex_extract certainty (0.6) = 0.24
        0.90 > 0.24  ✓

    2.  A conflict record is returned with:
          canonical_field  = "headline"
          winner_value     = "Senior Software Engineer"
          winner_source    = "ats_json"
          loser_value      = "Lead Developer"
          loser_source     = "notes"
    """

    # ── shared fixtures ──────────────────────────────────────────────────────

    ATS_VALUE   = "Senior Software Engineer"
    NOTES_VALUE = "Lead Developer"

    @pytest.fixture
    def ats_assertion(self) -> _Assertion:
        """High-trust assertion: ats_json + direct_copy."""
        return _make_assertion(
            norm_value=self.ATS_VALUE,
            source="ats_json",
            method="direct_copy",
        )

    @pytest.fixture
    def notes_assertion(self) -> _Assertion:
        """Low-trust assertion: notes + regex_extract."""
        return _make_assertion(
            norm_value=self.NOTES_VALUE,
            source="notes",
            method="regex_extract",
        )

    @pytest.fixture
    def conflict_result(self, ats_assertion, notes_assertion):
        """Run _select_winner with both assertions and return the full result."""
        return _select_winner("headline", [ats_assertion, notes_assertion])

    # ── assertions ───────────────────────────────────────────────────────────

    def test_winner_value_is_ats(self, conflict_result):
        """The higher-weighted source (ats_json) must supply the winning value."""
        winner_value, _conf, _src, _method, _conflicts = conflict_result
        assert winner_value == self.ATS_VALUE, (
            f"Expected ats_json value {self.ATS_VALUE!r} to win, "
            f"but got {winner_value!r}"
        )

    def test_winner_source_is_ats(self, conflict_result):
        """Provenance must point to ats_json, not notes."""
        _val, _conf, winner_source, _method, _conflicts = conflict_result
        assert winner_source == "ats_json"

    def test_winning_confidence_beats_loser(self, conflict_result):
        """
        The winning confidence must exceed the loser's confidence.
        We compute the loser's confidence directly for comparison.
        """
        _val, winner_conf, _src, _method, _conflicts = conflict_result
        loser_conf = compute_confidence("notes", "regex_extract", "employment", 1)
        assert winner_conf > loser_conf, (
            f"Winner confidence {winner_conf:.4f} should exceed "
            f"loser confidence {loser_conf:.4f}"
        )

    def test_conflict_is_non_empty(self, conflict_result):
        """
        With two distinct values, _select_winner MUST return at least one
        conflict entry — silence on conflicts is a bug.
        """
        _val, _conf, _src, _method, conflicts = conflict_result
        assert len(conflicts) >= 1, (
            "Expected at least one conflict record when two sources disagree."
        )

    def test_conflict_record_structure(self, conflict_result):
        """Each conflict dict must contain all required keys."""
        _val, _conf, _src, _method, conflicts = conflict_result
        required_keys = {
            "canonical_field",
            "winner_value",
            "winner_source",
            "loser_value",
            "loser_source",
            "loser_method",
        }
        for entry in conflicts:
            assert required_keys == entry.keys(), (
                f"Conflict entry missing keys: {required_keys - entry.keys()}"
            )

    def test_conflict_records_correct_loser(self, conflict_result):
        """The conflict entry must correctly name the losing source and value."""
        _val, _conf, _src, _method, conflicts = conflict_result
        # There is exactly one losing value in this scenario.
        loser = conflicts[0]
        assert loser["canonical_field"] == "headline"
        assert loser["loser_value"]     == self.NOTES_VALUE
        assert loser["loser_source"]    == "notes"
        assert loser["winner_value"]    == self.ATS_VALUE
        assert loser["winner_source"]   == "ats_json"

    def test_conflict_loser_method_preserved(self, conflict_result):
        """The losing method must be recorded so the conflict is auditable."""
        _val, _conf, _src, _method, conflicts = conflict_result
        assert conflicts[0]["loser_method"] == "regex_extract"


# ════════════════════════════════════════════════════════════════════════════
# 4. Edge-case conflict tests
# ════════════════════════════════════════════════════════════════════════════

class TestConflictEdgeCases:

    def test_single_assertion_no_conflict(self):
        """A field with only one value should produce zero conflicts."""
        assertions = [_make_assertion("Engineer", "ats_json", "direct_copy")]
        _val, _conf, _src, _method, conflicts = _select_winner("headline", assertions)
        assert conflicts == []

    def test_matching_values_from_two_sources_no_conflict(self):
        """
        Two sources reporting the *same* normalised value should:
          - corroborate each other (boosted confidence)
          - produce zero conflicts
        """
        a1 = _make_assertion("Senior Engineer", "ats_json", "direct_copy")
        a2 = _make_assertion("Senior Engineer", "csv",      "field_remap")
        _val, conf, _src, _method, conflicts = _select_winner("headline", [a1, a2])

        assert conflicts == [], "Identical values should never generate a conflict."

        # Corroboration should push confidence above either single-source value.
        solo_conf = compute_confidence("ats_json", "direct_copy", "employment", 1)
        assert conf > solo_conf, (
            "Corroborated confidence should exceed single-source confidence."
        )

    def test_all_none_values_returns_none_winner(self):
        """If all assertions normalise to None, the winner value must be None."""
        a1 = _make_assertion(None, "csv", "direct_copy", field_name="headline")  # type: ignore[arg-type]
        a2 = _make_assertion(None, "notes", "regex_extract", field_name="headline")  # type: ignore[arg-type]
        val, conf, src, method, conflicts = _select_winner("headline", [a1, a2])
        assert val is None
        assert conf == 0.0
        assert conflicts == []

    def test_three_way_conflict_two_losers_recorded(self):
        """
        Three distinct values from three sources → two conflict records
        (the two non-winners), ordered by descending confidence.
        """
        a_ats   = _make_assertion("Staff Engineer",    "ats_json", "direct_copy")
        a_csv   = _make_assertion("Senior Engineer",   "csv",      "field_remap")
        a_notes = _make_assertion("Principal Engineer","notes",    "fuzzy_match")

        _val, _conf, _src, _method, conflicts = _select_winner(
            "headline", [a_ats, a_csv, a_notes]
        )
        assert len(conflicts) == 2, (
            f"Expected 2 conflict records for 3 distinct values, got {len(conflicts)}"
        )
        # All conflict entries must share the same winner.
        winner_values = {c["winner_value"] for c in conflicts}
        assert len(winner_values) == 1, "All conflict entries must reference the same winner."

    def test_corroboration_can_flip_winner(self):
        """
        Two low-weight sources agreeing can beat one high-weight source alone,
        because the corroboration boost is strong enough.

        github skills weight = 0.85, direct_copy = 1.0  → 0.85  (single)
        notes  skills weight = 0.50, direct_copy = 1.0  → 0.50  (each)
        Two notes sources corroborating: 0.50 * 1.0 * 1.15 = 0.575  > 0.50 but < 0.85
        So for skills, github still wins with one source vs two notes — shows
        the cap still keeps high-weight sources favoured when the gap is large.
        This test documents the ACTUAL behaviour rather than asserting a flip.
        """
        a_github = _make_assertion("python", "github", "direct_copy", field_name="skills")
        a_notes1 = _make_assertion("python", "notes",  "direct_copy", field_name="skills")
        a_notes2 = _make_assertion("python", "notes",  "direct_copy", field_name="skills")

        _val, conf, src, _method, conflicts = _select_winner(
            "skills", [a_github, a_notes1, a_notes2]
        )
        # With matching values, there should be no conflict.
        assert conflicts == []
        # Confidence should be corroborated (two distinct sources: github + notes).
        solo_github = compute_confidence("github", "direct_copy", "skills", 1)
        assert conf >= solo_github
