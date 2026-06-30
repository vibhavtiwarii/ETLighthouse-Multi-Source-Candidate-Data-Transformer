from __future__ import annotations
"""
src/transformer/pipeline.py
────────────────────────────────────────────────────────────────────────────────
Pipeline orchestration — Phase 11.

Wires every upstream phase in order:

  1a. CsvAdapter      → flat list[RawField]   (record_index set by adapter)
  1b. AtsJsonAdapter  → flat list[RawField]   (record_index set by adapter)
  1c. ResumeAdapter   → flat list[RawField]   (record_index set here, one per PDF)
  2.  resolve_identities()  → candidate_groups: dict[cid, list[RawField]]
  3.  attach_enrichment()   → enriched candidate_groups (GitHub + Notes)
  4.  merge_candidate()     → list[CanonicalProfile]
  5.  Load OutputConfig, project()  → list[dict]  (validated against JSON Schema)
  6.  Write projected dicts         → output_json_path
  7.  SqliteStore.write_profile()   → sqlite_db_path  (optional)

Error policy
────────────
Each stage is wrapped in its own try/except.  A failure in one stage is
logged at ERROR level and appended to the ``errors`` list in the summary
dict; the pipeline continues with whatever data it has.  Individual
candidates that fail projection or merge are also logged and skipped,
not dropped silently.

PDF resume scanning
────────────────────
Pass ``pdf_dir`` to scan a directory for *.pdf files automatically.
Every PDF is extracted with ResumeAdapter and given a unique record_index
(continuing from where CSV/ATS left off so there are no collisions).
pdf_dir=None (the default) disables PDF extraction entirely — the rest
of the pipeline is unaffected.

Returns
───────
{
  "candidates_processed": int,   # profiles that made it to the output file
  "errors": list[str],           # human-readable error descriptions
  "output_path": str             # path of the written JSON file
}
"""



import json
import logging
import logging.config
import pathlib
from typing import Optional

# ── configure logging once, at INFO, before any other imports touch it ────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── project imports ───────────────────────────────────────────────────────────
from src.transformer.adapters.csv_adapter    import CsvAdapter        # noqa: E402
from src.transformer.adapters.ats_adapter    import AtsJsonAdapter    # noqa: E402
from src.transformer.adapters.resume_adapter import ResumeAdapter     # noqa: E402
from src.transformer.adapters.github_adapter import GithubAdapter     # noqa: E402
from src.transformer.adapters.notes_adapter  import NotesAdapter      # noqa: E402
from src.transformer.resolve.identity        import resolve_identities, attach_enrichment  # noqa: E402
from src.transformer.merge.merge             import merge_candidate   # noqa: E402
from src.transformer.projection.config_model import OutputConfig      # noqa: E402
from src.transformer.projection.project      import project           # noqa: E402
from src.transformer.projection.schema_gen   import generate_json_schema  # noqa: E402
from src.transformer.cache.extraction_cache  import ExtractionCache   # noqa: E402
from src.transformer.store.sqlite_store      import SqliteStore       # noqa: E402
from src.transformer.schema                  import CanonicalProfile  # noqa: E402
from src.transformer.raw_field               import RawField          # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _load_output_config(path: str) -> OutputConfig:
    """Load and parse an OutputConfig from a JSON file."""
    text = pathlib.Path(path).read_text(encoding="utf-8")
    data = json.loads(text)
    return OutputConfig.model_validate(data)


def _next_record_index(fields: list[RawField]) -> int:
    """
    Return the next available record_index — one higher than the current
    maximum across all fields already collected, or 0 if the list is empty.

    This ensures PDF record indices never collide with CSV or ATS indices
    even if the counts differ between runs.
    """
    if not fields:
        return 0
    return max(f.record_index for f in fields) + 1


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────

def run_pipeline(
    csv_paths: list[str],
    ats_json_path: str,
    notes_dir: str,
    output_config_path: str,
    output_json_path: str,
    sqlite_db_path: Optional[str] = None,
    pdf_dir: Optional[str] = None,
) -> dict:
    """
    Run the full Eightfold transformer pipeline end-to-end.

    Parameters
    ----------
    csv_paths:
        List of paths to recruiter CSV exports (processed by CsvAdapter).
    ats_json_path:
        Path to the ATS JSON export (processed by AtsJsonAdapter).
    notes_dir:
        Directory containing free-text ``.txt`` note files.
    output_config_path:
        Path to a JSON file that deserialises into an ``OutputConfig``.
    output_json_path:
        Destination path for the projected JSON array output.
    sqlite_db_path:
        Optional path to a SQLite database.  When supplied, every
        ``CanonicalProfile`` (not the projected dict) is written here.
    pdf_dir:
        Optional path to a directory containing PDF resume files.
        Every ``*.pdf`` file found directly inside this directory is parsed
        by ResumeAdapter and its RawFields are merged with the CSV/ATS data.
        Sub-directories are NOT recursed — put all resumes flat in this dir.
        Pass None (the default) to skip PDF extraction entirely.

    Returns
    -------
    dict
        ``{"candidates_processed": int, "errors": list[str], "output_path": str}``
    """
    errors: list[str] = []
    raw_fields: list[RawField] = []

    # ── Phase 1a: CSV extraction ──────────────────────────────────────────────
    # CsvAdapter sets record_index = row number within its own file.
    for csv_path in csv_paths:
        log.info("Phase 1a — extracting from CSV: %s", csv_path)
        try:
            csv_fields = CsvAdapter().extract(csv_path)
            log.info("  CSV → %d RawFields", len(csv_fields))
            raw_fields.extend(csv_fields)
        except Exception as exc:
            msg = f"Phase 1a CSV extraction failed for {csv_path}: {exc}"
            log.error(msg)
            errors.append(msg)

    # ── Phase 1b: ATS JSON extraction ────────────────────────────────────────
    # AtsJsonAdapter sets record_index = position in the JSON array.
    log.info("Phase 1b — extracting from ATS JSON: %s", ats_json_path)
    try:
        ats_fields = AtsJsonAdapter().extract(ats_json_path)
        log.info("  ATS JSON → %d RawFields", len(ats_fields))
        raw_fields.extend(ats_fields)
    except Exception as exc:
        msg = f"Phase 1b ATS extraction failed: {exc}"
        log.error(msg)
        errors.append(msg)

    # ── Phase 1c: PDF resume extraction (optional) ────────────────────────────
    # ResumeAdapter does not know its own index (it processes one file at a
    # time), so the pipeline assigns record_index here — continuing from the
    # highest index already used by CSV/ATS so there are no collisions.
    # Each PDF = one candidate = one unique record_index.
    if pdf_dir is not None:
        pdf_path_obj = pathlib.Path(pdf_dir)
        if not pdf_path_obj.is_dir():
            msg = f"Phase 1c pdf_dir {pdf_dir!r} is not a directory — skipping PDF extraction."
            log.warning(msg)
            errors.append(msg)
        else:
            pdf_files = sorted(pdf_path_obj.glob("*.pdf"))
            log.info(
                "Phase 1c — extracting from %d PDF resume(s) in: %s",
                len(pdf_files), pdf_dir,
            )
            resume_adapter = ResumeAdapter()
            for pdf_file in pdf_files:
                try:
                    # Assign index BEFORE extending raw_fields so
                    # _next_record_index sees all previously added fields.
                    record_index = _next_record_index(raw_fields)
                    resume_fields = resume_adapter.extract(
                        str(pdf_file), record_index=record_index
                    )
                    log.info(
                        "  PDF %s (record_index=%d) → %d RawFields",
                        pdf_file.name, record_index, len(resume_fields),
                    )
                    raw_fields.extend(resume_fields)
                except Exception as exc:
                    msg = f"Phase 1c PDF extraction failed for {pdf_file.name!r}: {exc}"
                    log.error(msg)
                    errors.append(msg)
                    # Non-fatal: skip this PDF, keep processing others.

    # ── Early exit if nothing was extracted ───────────────────────────────────
    if not raw_fields:
        msg = "Phase 1 produced zero RawFields — nothing to process."
        log.error(msg)
        errors.append(msg)
        return {"candidates_processed": 0, "errors": errors, "output_path": output_json_path}

    # ── Phase 2: identity resolution ──────────────────────────────────────────
    log.info("Phase 2 — resolving identities across %d RawFields …", len(raw_fields))
    candidate_groups: dict[str, list[RawField]] = {}
    try:
        candidate_groups = resolve_identities(raw_fields)
        log.info("  Resolved → %d distinct candidates", len(candidate_groups))
    except Exception as exc:
        msg = f"Phase 2 identity resolution failed: {exc}"
        log.error(msg)
        errors.append(msg)
        return {"candidates_processed": 0, "errors": errors, "output_path": output_json_path}

    # ── Phase 3: enrichment (GitHub + Notes) ──────────────────────────────────
    log.info("Phase 3 — attaching GitHub and Notes enrichment …")
    try:
        cache = ExtractionCache()
        github_adapter = GithubAdapter(cache=cache)
        notes_adapter = NotesAdapter()
        candidate_groups = attach_enrichment(
            candidate_groups,
            github_adapter=github_adapter,
            notes_adapter=notes_adapter,
            notes_dir=notes_dir,
        )
        log.info("  Enrichment complete.")
    except Exception as exc:
        msg = f"Phase 3 enrichment failed: {exc}"
        log.error(msg)
        errors.append(msg)
        # Non-fatal: continue with whatever fields are already present.

    # ── Phase 4: merge ────────────────────────────────────────────────────────
    log.info("Phase 4 — merging %d candidates …", len(candidate_groups))
    canonical_profiles: list[CanonicalProfile] = []

    for cid, fields in candidate_groups.items():
        try:
            profile = merge_candidate(cid, fields)
            canonical_profiles.append(profile)
        except Exception as exc:
            msg = f"Phase 4 merge failed for candidate {cid!r}: {exc}"
            log.error(msg)
            errors.append(msg)

    log.info("  Merged → %d CanonicalProfiles", len(canonical_profiles))

    if not canonical_profiles:
        msg = "Phase 4 produced zero CanonicalProfiles — aborting."
        log.error(msg)
        errors.append(msg)
        return {"candidates_processed": 0, "errors": errors, "output_path": output_json_path}

    # ── Phase 5: load config + project ───────────────────────────────────────
    log.info("Phase 5 — loading OutputConfig from %s …", output_config_path)
    output_config: Optional[OutputConfig] = None
    try:
        output_config = _load_output_config(output_config_path)
        log.info("  OutputConfig loaded: %d fields defined", len(output_config.fields))
    except Exception as exc:
        msg = f"Phase 5 OutputConfig load failed: {exc}"
        log.error(msg)
        errors.append(msg)
        return {"candidates_processed": 0, "errors": errors, "output_path": output_json_path}

    log.info("Phase 5 — projecting %d profiles …", len(canonical_profiles))
    projected_dicts: list[dict] = []

    for profile in canonical_profiles:
        try:
            projected = project(profile, output_config)
            projected_dicts.append(projected)
        except Exception as exc:
            msg = f"Phase 5 projection failed for candidate {profile.candidate_id!r}: {exc}"
            log.error(msg)
            errors.append(msg)

    log.info("  Projected → %d output dicts", len(projected_dicts))

    # ── Phase 6: write JSON output ────────────────────────────────────────────
    log.info("Phase 6 — writing output JSON to %s …", output_json_path)
    try:
        out_path = pathlib.Path(output_json_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(projected_dicts, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        log.info("  Written: %d candidates → %s", len(projected_dicts), output_json_path)
    except Exception as exc:
        msg = f"Phase 6 JSON write failed: {exc}"
        log.error(msg)
        errors.append(msg)

    # ── Phase 7: SQLite persistence (optional) ────────────────────────────────
    if sqlite_db_path:
        log.info(
            "Phase 7 — writing %d profiles to SQLite: %s …",
            len(canonical_profiles), sqlite_db_path,
        )
        try:
            with SqliteStore(sqlite_db_path) as store:
                for profile in canonical_profiles:
                    try:
                        store.write_profile(profile)
                    except Exception as exc:
                        msg = (
                            f"Phase 7 SQLite write failed for candidate "
                            f"{profile.candidate_id!r}: {exc}"
                        )
                        log.error(msg)
                        errors.append(msg)
            log.info("  SQLite writes complete.")
        except Exception as exc:
            msg = f"Phase 7 SQLite store initialisation failed: {exc}"
            log.error(msg)
            errors.append(msg)

    # ── Summary ───────────────────────────────────────────────────────────────
    summary = {
        "candidates_processed": len(projected_dicts),
        "errors": errors,
        "output_path": output_json_path,
    }
    log.info(
        "Pipeline complete — candidates_processed=%d, errors=%d, output=%s",
        summary["candidates_processed"],
        len(summary["errors"]),
        summary["output_path"],
    )
    return summary
