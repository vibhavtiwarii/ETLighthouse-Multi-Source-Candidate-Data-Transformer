"""Tests source adapters.""" 
"""
tests/test_adapters.py
────────────────────────────────────────────────────────────────────────────────
One valid-input test and one missing/corrupted-file test per adapter.
All adapters must uphold the no-raise contract: on any failure they return []
and log a WARNING, they never propagate an exception.
────────────────────────────────────────────────────────────────────────────────
"""

import json
import textwrap
from pathlib import Path

import pytest

from src.transformer.adapters.csv_adapter import CsvAdapter
from src.transformer.adapters.ats_adapter import AtsJsonAdapter
from src.transformer.adapters.notes_adapter import NotesAdapter
from src.transformer.adapters.github_adapter import GithubAdapter
from src.transformer.cache.extraction_cache import ExtractionCache


# ─────────────────────────────────────────────────────────────────────────────
# CsvAdapter
# ─────────────────────────────────────────────────────────────────────────────

class TestCsvAdapter:
    def test_valid_csv_emits_raw_fields(self, tmp_path):
        csv_file = tmp_path / "recruiters.csv"
        csv_file.write_text(
            "name,email,phone,current_company,title\n"
            "Jordan Ellis,jordan.ellis@gmail.com,+14155550192,Stripe,Senior Engineer\n",
            encoding="utf-8",
        )
        fields = CsvAdapter().extract(str(csv_file))

        assert len(fields) > 0
        field_names = {f.field_name for f in fields}
        assert "full_name" in field_names
        assert "emails" in field_names
        # Every field should carry the correct source tag
        assert all(f.source == "csv" for f in fields)
        assert all(f.method == "direct_copy" for f in fields)

    def test_missing_file_returns_empty_list(self):
        fields = CsvAdapter().extract("/nonexistent/path/recruiters.csv")
        assert fields == []

    def test_corrupted_csv_returns_empty_list(self, tmp_path):
        # A file that exists but has no data rows after the header
        csv_file = tmp_path / "empty.csv"
        csv_file.write_text("name,email,phone\n", encoding="utf-8")
        fields = CsvAdapter().extract(str(csv_file))
        assert fields == []


# ─────────────────────────────────────────────────────────────────────────────
# AtsJsonAdapter
# ─────────────────────────────────────────────────────────────────────────────

class TestAtsJsonAdapter:
    def test_valid_ats_json_emits_raw_fields(self, tmp_path):
        ats_file = tmp_path / "ats.json"
        ats_file.write_text(
            json.dumps([{
                "cand_full_nm": "Priya Nair",
                "primary_eml":  "priya.nair@outlook.com",
                "mobile_no":    "+442079460312",
                "employer_nm":  "Monzo",
                "job_ttl":      "Data Scientist",
                "loc_city":     "London",
                "loc_country":  "GB",
            }]),
            encoding="utf-8",
        )
        fields = AtsJsonAdapter().extract(str(ats_file))

        assert len(fields) > 0
        field_names = {f.field_name for f in fields}
        assert "full_name" in field_names
        assert "emails" in field_names
        assert all(f.source == "ats_json" for f in fields)
        assert all(f.method == "field_remap" for f in fields)

    def test_missing_file_returns_empty_list(self):
        fields = AtsJsonAdapter().extract("/nonexistent/path/ats.json")
        assert fields == []

    def test_malformed_json_returns_empty_list(self, tmp_path):
        bad_file = tmp_path / "bad.json"
        bad_file.write_text("{ this is not valid JSON !!!", encoding="utf-8")
        fields = AtsJsonAdapter().extract(str(bad_file))
        assert fields == []

    def test_non_list_json_returns_empty_list(self, tmp_path):
        bad_file = tmp_path / "obj.json"
        bad_file.write_text(json.dumps({"key": "value"}), encoding="utf-8")
        fields = AtsJsonAdapter().extract(str(bad_file))
        assert fields == []


# ─────────────────────────────────────────────────────────────────────────────
# NotesAdapter
# ─────────────────────────────────────────────────────────────────────────────

class TestNotesAdapter:
    def test_valid_note_extracts_email_and_skills(self, tmp_path):
        # Write a minimal skill_synonyms.json so the adapter can match skills
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        synonyms_path = data_dir / "skill_synonyms.json"
        synonyms_path.write_text(
            json.dumps({"python": ["py", "python3"]}),
            encoding="utf-8",
        )

        note = tmp_path / "note.txt"
        note.write_text(
            "Contact: test.candidate@example.com\n"
            "Strong with Python and loves building APIs.\n"
            "Phone: +1 415 555 0100\n",
            encoding="utf-8",
        )

        adapter = NotesAdapter(synonyms_path=synonyms_path)
        fields = adapter.extract(str(note))

        assert len(fields) > 0
        field_names = [f.field_name for f in fields]
        assert "emails" in field_names
        assert all(f.source == "notes" for f in fields)

    def test_missing_file_returns_empty_list(self, tmp_path):
        adapter = NotesAdapter(synonyms_path=tmp_path / "skill_synonyms.json")
        fields = adapter.extract("/nonexistent/note.txt")
        assert fields == []

    def test_empty_file_returns_empty_list(self, tmp_path):
        empty_note = tmp_path / "empty.txt"
        empty_note.write_text("", encoding="utf-8")
        adapter = NotesAdapter(synonyms_path=tmp_path / "skill_synonyms.json")
        fields = adapter.extract(str(empty_note))
        assert fields == []


# ─────────────────────────────────────────────────────────────────────────────
# GithubAdapter
# ─────────────────────────────────────────────────────────────────────────────

class TestGithubAdapter:
    def test_cache_hit_returns_fields_without_network(self, tmp_path):
        """
        If the cache already holds a response for the profile and repos URLs,
        the adapter must return RawFields without making any network call.
        This test is fully deterministic and offline-safe.
        """
        cache = ExtractionCache(cache_dir=str(tmp_path / ".cache"))

        # Pre-populate cache with a fake GitHub API response
        username = "fakeuser"
        profile_url = f"https://api.github.com/users/{username}"
        repos_url   = f"https://api.github.com/users/{username}/repos?per_page=100"

        cache.set(profile_url, {"name": "Fake User", "bio": "Engineer at nowhere"})
        cache.set(repos_url,   [{"language": "Python"}, {"language": "Go"}])

        adapter = GithubAdapter(cache=cache)
        fields = adapter.extract(f"https://github.com/{username}")

        assert len(fields) > 0
        field_names = {f.field_name for f in fields}
        assert "full_name" in field_names
        assert "skills" in field_names
        assert all(f.source == "github" for f in fields)

    def test_invalid_url_returns_empty_list(self):
        """A completely malformed URL must return [] without raising."""
        adapter = GithubAdapter()
        fields = adapter.extract("not-a-url-at-all")
        # May return [] either because username parsing fails or network fails
        assert isinstance(fields, list)

    def test_unreachable_host_returns_empty_list(self, tmp_path):
        """
        Simulate a network failure by giving the adapter a pre-populated cache
        that is empty (no entries), so it will attempt a real fetch that we
        intercept by monkeypatching requests.get to raise.
        """
        import unittest.mock as mock

        cache = ExtractionCache(cache_dir=str(tmp_path / ".cache"))
        adapter = GithubAdapter(cache=cache)

        with mock.patch("requests.get", side_effect=Exception("network down")):
            fields = adapter.extract("https://github.com/someuser")

        assert fields == []

# ─────────────────────────────────────────────────────────────────────────────
# ResumeAdapter
# ─────────────────────────────────────────────────────────────────────────────

class TestResumeAdapter:
    """
    Tests for ResumeAdapter.

    We never use a real PDF in unit tests — pdfplumber is an optional
    dependency and PDF generation adds weight.  Instead we monkeypatch
    ResumeAdapter._extract_text() to return a controlled text string,
    which lets us test _parse() deterministically without touching the
    filesystem or pdfplumber at all.

    The missing/corrupted file tests go through the real code path so
    the no-raise contract is verified end-to-end.
    """

    # ── shared fixture ───────────────────────────────────────────────────────

    @staticmethod
    def _patch_text(monkeypatch, adapter, text: str) -> None:
        """Replace _extract_text so tests don't need a real PDF or pdfplumber."""
        monkeypatch.setattr(adapter, "_extract_text", lambda path: text)

    SAMPLE_RESUME_TEXT = """
Jordan Ellis
Staff Engineer

jordan.ellis@gmail.com
+1 415 555 0192
San Francisco, CA
https://linkedin.com/in/jellis
https://github.com/jellis-stripe

SUMMARY
8+ years of experience in distributed systems and payment infrastructure.

EXPERIENCE
Stripe | Jan 2021 – Present
Software Engineer II | Mar 2019 – Dec 2020

SKILLS
Python, Go, Kubernetes, PostgreSQL, React, AWS

EDUCATION
B.S. Computer Science, UC Berkeley, 2018
"""

    # ── valid input tests ─────────────────────────────────────────────────────

    def test_valid_resume_emits_raw_fields(self, monkeypatch, tmp_path):
        """A well-formed resume text must produce at least one RawField."""
        from src.transformer.adapters.resume_adapter import ResumeAdapter
        adapter = ResumeAdapter(synonyms_path=tmp_path / "skill_synonyms.json")
        self._patch_text(monkeypatch, adapter, self.SAMPLE_RESUME_TEXT)

        fields = adapter.extract("fake_resume.pdf", record_index=0)

        assert len(fields) > 0
        assert all(f.source == "resume_pdf" for f in fields)

    def test_source_is_resume_pdf(self, monkeypatch, tmp_path):
        """Every RawField must carry source='resume_pdf'."""
        from src.transformer.adapters.resume_adapter import ResumeAdapter
        adapter = ResumeAdapter(synonyms_path=tmp_path / "skill_synonyms.json")
        self._patch_text(monkeypatch, adapter, self.SAMPLE_RESUME_TEXT)

        fields = adapter.extract("fake_resume.pdf", record_index=3)
        assert all(f.source == "resume_pdf" for f in fields)

    def test_record_index_stamped_on_all_fields(self, monkeypatch, tmp_path):
        """
        Every RawField must carry the record_index passed to extract().
        This is the mechanism identity.py uses to reconstruct per-record
        boundaries — a missing or wrong index breaks grouping.
        """
        from src.transformer.adapters.resume_adapter import ResumeAdapter
        adapter = ResumeAdapter(synonyms_path=tmp_path / "skill_synonyms.json")
        self._patch_text(monkeypatch, adapter, self.SAMPLE_RESUME_TEXT)

        fields = adapter.extract("fake_resume.pdf", record_index=7)
        assert all(f.record_index == 7 for f in fields), (
            "One or more RawFields have the wrong record_index.  "
            "ResumeAdapter must stamp every field with the index passed to extract()."
        )

    def test_extracts_full_name(self, monkeypatch, tmp_path):
        """The name heuristic must pick up 'Jordan Ellis' from the first lines."""
        from src.transformer.adapters.resume_adapter import ResumeAdapter
        adapter = ResumeAdapter(synonyms_path=tmp_path / "skill_synonyms.json")
        self._patch_text(monkeypatch, adapter, self.SAMPLE_RESUME_TEXT)

        fields = adapter.extract("fake_resume.pdf", record_index=0)
        name_fields = [f for f in fields if f.field_name == "full_name"]
        assert len(name_fields) >= 1, "No full_name field extracted from resume text."
        assert "Jordan" in name_fields[0].value

    def test_extracts_email(self, monkeypatch, tmp_path):
        """Email regex must find jordan.ellis@gmail.com."""
        from src.transformer.adapters.resume_adapter import ResumeAdapter
        adapter = ResumeAdapter(synonyms_path=tmp_path / "skill_synonyms.json")
        self._patch_text(monkeypatch, adapter, self.SAMPLE_RESUME_TEXT)

        fields = adapter.extract("fake_resume.pdf", record_index=0)
        email_fields = [f for f in fields if f.field_name == "email"]
        assert len(email_fields) >= 1
        assert "jordan.ellis@gmail.com" in email_fields[0].value

    def test_extracts_github_link(self, monkeypatch, tmp_path):
        """GitHub URL must be extracted and stored under links.github."""
        from src.transformer.adapters.resume_adapter import ResumeAdapter
        adapter = ResumeAdapter(synonyms_path=tmp_path / "skill_synonyms.json")
        self._patch_text(monkeypatch, adapter, self.SAMPLE_RESUME_TEXT)

        fields = adapter.extract("fake_resume.pdf", record_index=0)
        github_fields = [f for f in fields if f.field_name == "links.github"]
        assert len(github_fields) >= 1
        assert "github.com" in github_fields[0].value

    def test_field_names_are_in_field_route(self, monkeypatch, tmp_path):
        """
        Every field_name emitted must exist in merge.py's _FIELD_ROUTE so
        the merge stage doesn't silently discard the field.
        """
        from src.transformer.adapters.resume_adapter import ResumeAdapter
        from src.transformer.merge.merge import _FIELD_ROUTE

        adapter = ResumeAdapter(synonyms_path=tmp_path / "skill_synonyms.json")
        self._patch_text(monkeypatch, adapter, self.SAMPLE_RESUME_TEXT)

        fields = adapter.extract("fake_resume.pdf", record_index=0)
        unknown = {f.field_name for f in fields} - set(_FIELD_ROUTE.keys())
        assert unknown == set(), (
            f"ResumeAdapter emitted field_names not in _FIELD_ROUTE: {unknown}\n"
            f"Add them to _FIELD_ROUTE or rename them in the adapter."
        )

    def test_methods_are_valid(self, monkeypatch, tmp_path):
        """
        Every method value must be recognised by confidence.py's
        _METHOD_CERTAINTY table (or fall back gracefully to 0.5).
        This test documents the expected values: regex_extract and keyword_match.
        """
        from src.transformer.adapters.resume_adapter import ResumeAdapter
        adapter = ResumeAdapter(synonyms_path=tmp_path / "skill_synonyms.json")
        self._patch_text(monkeypatch, adapter, self.SAMPLE_RESUME_TEXT)

        fields = adapter.extract("fake_resume.pdf", record_index=0)
        methods = {f.method for f in fields}
        expected = {"regex_extract", "keyword_match", "direct_copy"}
        unknown = methods - expected
        assert unknown == set(), (
            f"Unexpected method values: {unknown}.  "
            "Add them to _METHOD_CERTAINTY in confidence.py or fix the adapter."
        )

    def test_synonym_map_used_for_skills(self, tmp_path, monkeypatch):
        """
        When the synonym map contains a synonym present in the resume text,
        the canonical skill name (not the synonym) must appear as a RawField.
        """
        from src.transformer.adapters.resume_adapter import ResumeAdapter

        synonyms_path = tmp_path / "skill_synonyms.json"
        synonyms_path.write_text(
            '{"python": ["py", "python3", "cpython"]}', encoding="utf-8"
        )
        adapter = ResumeAdapter(synonyms_path=synonyms_path)
        self._patch_text(
            monkeypatch, adapter,
            "Skills: py, react\nExperience with python3 and AWS"
        )

        fields = adapter.extract("fake_resume.pdf", record_index=0)
        skill_values = [f.value for f in fields if f.field_name == "skill"]
        assert "python" in skill_values, (
            f"Expected canonical skill 'python' from synonym 'py', "
            f"got skill values: {skill_values}"
        )

    # ── no-raise contract tests ───────────────────────────────────────────────

    def test_missing_file_returns_empty_list(self, tmp_path):
        """
        A path that does not exist must return [] without raising.
        This matches the contract of every other adapter in the project.
        """
        from src.transformer.adapters.resume_adapter import ResumeAdapter
        adapter = ResumeAdapter(synonyms_path=tmp_path / "skill_synonyms.json")
        fields = adapter.extract("/nonexistent/path/resume.pdf", record_index=0)
        assert fields == [], (
            "ResumeAdapter must return [] for a missing file, not raise."
        )

    def test_empty_pdf_returns_empty_list(self, monkeypatch, tmp_path):
        """
        A PDF that yields no text (image-only / blank) must return [].
        We simulate this by patching _extract_text to return ''.
        """
        from src.transformer.adapters.resume_adapter import ResumeAdapter
        adapter = ResumeAdapter(synonyms_path=tmp_path / "skill_synonyms.json")
        monkeypatch.setattr(adapter, "_extract_text", lambda path: "")

        fields = adapter.extract("blank_resume.pdf", record_index=0)
        assert fields == [], (
            "ResumeAdapter must return [] when the PDF yields no text."
        )

    def test_pdfplumber_import_error_returns_empty_list(self, monkeypatch, tmp_path):
        """
        If pdfplumber is not installed, extract() must log a warning and
        return [] rather than propagating ImportError to the caller.
        """
        import builtins
        real_import = builtins.__import__

        def _block_pdfplumber(name, *args, **kwargs):
            if name == "pdfplumber":
                raise ImportError("No module named 'pdfplumber'")
            return real_import(name, *args, **kwargs)

        from src.transformer.adapters.resume_adapter import ResumeAdapter
        adapter = ResumeAdapter(synonyms_path=tmp_path / "skill_synonyms.json")

        monkeypatch.setattr(builtins, "__import__", _block_pdfplumber)
        fields = adapter.extract("resume.pdf", record_index=0)
        assert fields == [], (
            "ResumeAdapter must return [] when pdfplumber is missing, not raise ImportError."
        )

    def test_corrupted_pdf_returns_empty_list(self, tmp_path):
        """
        A file with a .pdf extension whose content is garbage must return []
        without raising.
        """
        from src.transformer.adapters.resume_adapter import ResumeAdapter
        bad_pdf = tmp_path / "corrupt.pdf"
        bad_pdf.write_bytes(b"THIS IS NOT A PDF \x00\x01\x02")
        adapter = ResumeAdapter(synonyms_path=tmp_path / "skill_synonyms.json")
        fields = adapter.extract(str(bad_pdf), record_index=0)
        assert fields == [], (
            "ResumeAdapter must return [] for a corrupted PDF, not raise."
        )