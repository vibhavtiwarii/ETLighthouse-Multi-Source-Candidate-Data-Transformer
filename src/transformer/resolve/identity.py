"""
src/transformer/resolve/identity.py
────────────────────────────────────────────────────────────────────────────────
WHY THRESHOLD-BASED BLOCKING AND NOT K-MEANS / ANY CLUSTERING ALGORITHM
────────────────────────────────────────────────────────────────────────────────

The core problem here is *record linkage*: given records from heterogeneous
sources (CSV recruiter export, ATS JSON, GitHub, free-text notes), decide which
records describe the same real-world person and group them together.

Clustering algorithms such as K-means, DBSCAN, agglomerative clustering, or
any embedding-based approach are inappropriate here for several concrete reasons:

1.  **K is unknown.**
    K-means requires specifying K — the number of clusters — in advance.  We do
    not know how many distinct candidates exist in the input.  Heuristics like
    the elbow method or silhouette scoring would make the grouping vary with
    dataset size and random seed, producing non-deterministic results across
    runs on the same logical input set.

2.  **Distance metrics are heterogeneous.**
    Candidates are identified by email (exact), phone (E.164 normalised exact),
    and name+company (fuzzy string).  These are incommensurable signals that
    cannot be collapsed into a single numeric vector distance without arbitrary
    weighting choices.  Blocking keys handle each signal type natively.

3.  **Determinism is a hard requirement.**
    Record linkage for a hiring platform must produce the same grouping every
    time the same inputs are processed — for auditability, reproducibility, and
    downstream merge confidence scoring.  Any algorithm with a random component
    (random initialisation, stochastic graph cuts) violates this.

4.  **Blocking is the industry-standard approach at this scale.**
    The standard entity-resolution literature (Fellegi-Sunter, 1969; Christen,
    2012 "Data Matching") decomposes the problem into:
      (a) *Blocking* — cheaply reduce the candidate-pair space using exact keys
          so that O(n²) comparisons are avoided.
      (b) *Comparison* — apply a similarity metric only within blocks.
      (c) *Classification* — threshold the similarity score.
    This is exactly what the two-tier strategy below implements.  It is
    explainable to recruiters and auditors ("two records matched because they
    share the same e-mail address"), requires no training data, and is fully
    deterministic for the same input.

────────────────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

import os
import uuid
from collections import defaultdict
from typing import Any

from rapidfuzz import fuzz

# ---------------------------------------------------------------------------
# Import project internals.
# raw_field.RawField is a pydantic model with at least:
#   - field_name: str
#   - value: Any
#   - source: str          ("csv" | "ats_json" | "github" | "notes" | …)
#   - record_index: int    (row/record index within the originating source)
#   - candidate_id: str | None  (None until identity resolution assigns one)
# ---------------------------------------------------------------------------
from src.transformer.raw_field import RawField  # type: ignore
from src.transformer.normalize.phone import normalize_phone  # type: ignore

# ── constants ────────────────────────────────────────────────────────────────

NAME_SIMILARITY_THRESHOLD: int = 88   # token_sort_ratio must be >= this
COMPANY_SIMILARITY_THRESHOLD: int = 75  # rapidfuzz ratio must be >= this

_GITHUB_FIELD_NAMES: frozenset[str] = frozenset({"links.github", "github_url"})

# IMPORTANT: these must match the field_name strings the adapters actually
# emit. CsvAdapter, AtsJsonAdapter, and NotesAdapter all emit the PLURAL
# forms ("emails", "phones") — never the singular "email"/"phone". The
# singular variants are kept here only as a defensive fallback in case a
# future adapter emits them; they are not what drives matching today.
_EMAIL_FIELD_NAMES: frozenset[str] = frozenset({"emails", "email", "email_address"})
_PHONE_FIELD_NAMES: frozenset[str] = frozenset({"phones", "phone", "phone_number", "mobile"})


# ── helpers ──────────────────────────────────────────────────────────────────

def _new_candidate_id() -> str:
    """Return a short, unique candidate identifier."""
    return uuid.uuid4().hex[:12]


def _norm_email(value: Any) -> str:
    """Lowercase and strip an e-mail string; return '' if not a string."""
    if not isinstance(value, str):
        return ""
    return value.lower().strip()


def _norm_phone(value: Any) -> str:
    """Return E.164 normalised phone or '' on failure."""
    if not isinstance(value, str) or not value.strip():
        return ""
    try:
        return normalize_phone(value.strip()) or ""
    except Exception:
        return ""


def _norm_name(value: Any) -> str:
    """Lowercase and strip a full-name string."""
    if not isinstance(value, str):
        return ""
    return value.lower().strip()


# ── record-level key extraction ──────────────────────────────────────────────

class _RecordKeys:
    """Aggregated blocking keys extracted from one raw record's RawFields."""

    __slots__ = ("emails", "phones", "names", "companies")

    def __init__(self) -> None:
        self.emails: set[str] = set()
        self.phones: set[str] = set()
        self.names: list[str] = []
        self.companies: list[str] = []

    @property
    def best_name(self) -> str:
        return self.names[0] if self.names else ""

    @property
    def best_company(self) -> str:
        return self.companies[0] if self.companies else ""


def _extract_keys(fields: list[RawField]) -> _RecordKeys:
    """Build a _RecordKeys from the RawFields belonging to a single record."""
    keys = _RecordKeys()
    for f in fields:
        fn = f.field_name.lower()
        if fn in _EMAIL_FIELD_NAMES:
            norm = _norm_email(f.value)
            if norm:
                keys.emails.add(norm)
        elif fn in _PHONE_FIELD_NAMES:
            norm = _norm_phone(f.value)
            if norm:
                keys.phones.add(norm)
        elif fn in ("full_name", "name", "candidate_name"):
            norm = _norm_name(f.value)
            if norm:
                keys.names.append(norm)
        elif fn in ("current_company", "company", "employer"):
            if isinstance(f.value, str) and f.value.strip():
                keys.companies.append(f.value.strip().lower())
    return keys


# ── grouping raw records by (source, record_index) ───────────────────────────

def _group_by_record(raw_fields: list[RawField]) -> list[tuple[str, int, list[RawField]]]:
    """
    Partition a flat list of RawFields into per-record groups.

    Returns a list of (source, record_index, fields) tuples, one per distinct
    (source, record_index) pair — i.e. one per CSV row or ATS record. Both
    adapters now set record_index themselves at extraction time, so this is
    a pure regroup with no inference involved.
    """
    buckets: dict[tuple[str, int], list[RawField]] = defaultdict(list)
    for f in raw_fields:
        key = (f.source, f.record_index)
        buckets[key].append(f)

    return [
        (source, idx, fields)
        for (source, idx), fields in sorted(buckets.items())
    ]


# ── two-tier blocking ────────────────────────────────────────────────────────

def resolve_identities(
    raw_fields: list[RawField],
) -> dict[str, list[RawField]]:
    """
    Group RawFields into candidate profiles using two-tier deterministic blocking.

    Parameters
    ----------
    raw_fields:
        Flat list of RawField objects from ALL sources (CSV + ATS, and
        optionally others).  Each RawField must carry a non-None ``source``
        and a non-negative ``record_index`` so records can be reconstructed.

    Returns
    -------
    dict[candidate_id, list[RawField]]
        Every RawField appears exactly once.  The original ``source`` tag on
        each RawField is preserved; only ``candidate_id`` is (re-)assigned.

    Algorithm
    ---------
    1.  Reconstruct per-record groups from the flat field list.
    2.  For each incoming record, attempt to match it against already-resolved
        candidates:
        Tier 1 — exact normalised e-mail OR exact E.164 phone match → merge.
        Tier 2 (only if no Tier-1 match) — name token_sort_ratio >= 88 AND
                company similarity >= 75 → merge.
        No match → new candidate.
    3.  Maintain index structures (email→cid, phone→cid) for O(1) Tier-1
        lookups; iterate existing candidates only for Tier-2 (acceptable at
        typical recruitment dataset scale of tens of thousands of records).
    """

    # ── index structures ────────────────────────────────────────────────────
    candidates: dict[str, list[RawField]] = {}
    cid_to_keys: dict[str, _RecordKeys] = {}

    # fast exact-match indexes
    email_index: dict[str, str] = {}   # normalised_email → candidate_id
    phone_index: dict[str, str] = {}   # E.164_phone      → candidate_id

    def _assign(cid: str, fields: list[RawField], keys: _RecordKeys) -> None:
        """Attach *fields* to existing or newly created candidate *cid*."""
        if cid not in candidates:
            candidates[cid] = []
            cid_to_keys[cid] = _RecordKeys()

        # Tag each field with its resolved candidate_id so downstream stages
        # never have to re-resolve identity.
        for f in fields:
            f.candidate_id = cid  # type: ignore[attr-defined]

        candidates[cid].extend(fields)

        merged = cid_to_keys[cid]
        for em in keys.emails:
            merged.emails.add(em)
            email_index.setdefault(em, cid)
        for ph in keys.phones:
            merged.phones.add(ph)
            phone_index.setdefault(ph, cid)
        if keys.names:
            merged.names.extend(keys.names)
        if keys.companies:
            merged.companies.extend(keys.companies)

    # ── main loop ───────────────────────────────────────────────────────────
    record_groups = _group_by_record(raw_fields)

    for source, record_index, fields in record_groups:
        keys = _extract_keys(fields)

        # ── Tier 1: exact e-mail match ──────────────────────────────────────
        matched_cid: str | None = None
        for em in keys.emails:
            if em in email_index:
                matched_cid = email_index[em]
                break

        # ── Tier 1: exact phone match ───────────────────────────────────────
        if matched_cid is None:
            for ph in keys.phones:
                if ph in phone_index:
                    matched_cid = phone_index[ph]
                    break

        # ── Tier 2: fuzzy name + company (only when no exact key matched) ───
        if matched_cid is None and keys.best_name:
            inc_name = keys.best_name
            inc_company = keys.best_company

            for cid, existing_keys in cid_to_keys.items():
                if not existing_keys.best_name:
                    continue  # can't compare without a name

                name_score: float = fuzz.token_sort_ratio(
                    inc_name, existing_keys.best_name
                )
                if name_score < NAME_SIMILARITY_THRESHOLD:
                    continue  # short-circuit: name alone disqualifies

                company_score: float = 0.0
                if inc_company and existing_keys.best_company:
                    company_score = fuzz.token_sort_ratio(
                        inc_company, existing_keys.best_company
                    )
                # Missing company on either side is NOT treated as a match —
                # avoids false positives from name similarity alone.
                if (
                    inc_company
                    and existing_keys.best_company
                    and company_score >= COMPANY_SIMILARITY_THRESHOLD
                ):
                    matched_cid = cid
                    break

        # ── assign to existing candidate or create new one ──────────────────
        if matched_cid is None:
            matched_cid = _new_candidate_id()

        _assign(matched_cid, fields, keys)

    return candidates


# ── enrichment attachment ────────────────────────────────────────────────────

def attach_enrichment(
    candidate_groups: dict[str, list[RawField]],
    github_adapter: Any,
    notes_adapter: Any,
    notes_dir: str,
) -> dict[str, list[RawField]]:
    """
    Attach GitHub profile data and free-text note data to candidates.

    This function uses EXACT normalised e-mail / phone matching ONLY.
    Name-similarity logic is deliberately excluded: notes attachment must be
    conservative.  Attaching a note to the wrong candidate would silently
    contaminate that candidate's profile with another person's information,
    which is worse than leaving a note unattached.

    Parameters
    ----------
    candidate_groups:
        Output of ``resolve_identities()``.
    github_adapter:
        An adapter instance exposing ``extract(url: str) -> list[RawField]``.
    notes_adapter:
        An adapter instance exposing ``extract(path: str) -> list[RawField]``.
    notes_dir:
        Directory path to scan for ``*.txt`` note files.

    Returns
    -------
    The same dict (mutated in-place and also returned) with enrichment
    RawFields appended to matching candidate lists.
    """

    # ── pre-build per-candidate exact-key sets for O(1) note matching ───────
    cid_emails: dict[str, set[str]] = {}
    cid_phones: dict[str, set[str]] = {}

    for cid, fields in candidate_groups.items():
        emails: set[str] = set()
        phones: set[str] = set()
        for f in fields:
            fn = f.field_name.lower()
            if fn in _EMAIL_FIELD_NAMES:
                norm = _norm_email(f.value)
                if norm:
                    emails.add(norm)
            elif fn in _PHONE_FIELD_NAMES:
                norm = _norm_phone(f.value)
                if norm:
                    phones.add(norm)
        cid_emails[cid] = emails
        cid_phones[cid] = phones

    # reverse indexes for fast lookup: normalised_email → candidate_id
    email_to_cid: dict[str, str] = {}
    phone_to_cid: dict[str, str] = {}
    for cid, emails in cid_emails.items():
        for em in emails:
            email_to_cid[em] = cid
    for cid, phones in cid_phones.items():
        for ph in phones:
            phone_to_cid[ph] = cid

    # ── (1) GitHub enrichment ────────────────────────────────────────────────
    for cid, fields in list(candidate_groups.items()):
        github_url: str | None = None
        for f in fields:
            if f.field_name.lower() in _GITHUB_FIELD_NAMES:
                if isinstance(f.value, str) and f.value.strip():
                    github_url = f.value.strip()
                    break

        if github_url is None:
            continue

        try:
            enriched_fields: list[RawField] = github_adapter.extract(github_url)
        except Exception:
            # GitHub adapter already follows the never-raise contract; this
            # try/except is a defensive second layer only.
            continue

        for ef in enriched_fields:
            ef.candidate_id = cid  # type: ignore[attr-defined]

        candidate_groups[cid].extend(enriched_fields)

    # ── (2) Notes enrichment ─────────────────────────────────────────────────
    if not os.path.isdir(notes_dir):
        return candidate_groups

    note_files = sorted(
        os.path.join(notes_dir, fn)
        for fn in os.listdir(notes_dir)
        if fn.lower().endswith(".txt")
    )

    for note_path in note_files:
        try:
            note_fields: list[RawField] = notes_adapter.extract(note_path)
        except Exception:
            continue  # skip unreadable notes

        note_emails: set[str] = set()
        note_phones: set[str] = set()
        for f in note_fields:
            fn = f.field_name.lower()
            if fn in _EMAIL_FIELD_NAMES:
                norm = _norm_email(f.value)
                if norm:
                    note_emails.add(norm)
            elif fn in _PHONE_FIELD_NAMES:
                norm = _norm_phone(f.value)
                if norm:
                    note_phones.add(norm)

        target_cid: str | None = None

        for em in note_emails:
            if em in email_to_cid:
                target_cid = email_to_cid[em]
                break

        if target_cid is None:
            for ph in note_phones:
                if ph in phone_to_cid:
                    target_cid = phone_to_cid[ph]
                    break

        # No exact match → do NOT attach (conservative by design)
        if target_cid is None:
            continue

        for nf in note_fields:
            nf.candidate_id = target_cid  # type: ignore[attr-defined]

        candidate_groups[target_cid].extend(note_fields)

        for em in note_emails:
            email_to_cid.setdefault(em, target_cid)
            cid_emails[target_cid].add(em)
        for ph in note_phones:
            phone_to_cid.setdefault(ph, target_cid)
            cid_phones[target_cid].add(ph)

    return candidate_groups