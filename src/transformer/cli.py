"""
src/transformer/cli.py
────────────────────────────────────────────────────────────────────────────────
Eightfold Transformer — unified CLI.

Supports two invocation styles, both fully functional:

  A) Auto-scan mode (new CLI style — easiest for end users / .exe):
       transformer resolve all
       transformer resolve all --dir ./my_data --output ./out --skip-github

  B) Explicit-path mode (old CLI style — useful for CI / scripting):
       transformer run --csv-dir ./data --ats ./data/ats.json
           --notes-dir ./data/notes --config config/default_schema.json
           --out output/profiles.json [--sqlite output/pipeline.db]

Print commands (read from DB — no re-processing needed):
       transformer print candidates
       transformer print candidates --sorted
       transformer print candidates --recent
       transformer print candidates --best 10
       transformer print candidates --sorted --best 5
       transformer print candidates full_name headline overall_confidence
       transformer print candidates --best 3 --json
       transformer print schema

Command tree
────────────
transformer
├── resolve
│   └── all              Auto-scan directory, run full pipeline, write DB + JSON
├── run                  Explicit-path pipeline run (all paths given manually)
└── print
    ├── candidates        Print candidate table from DB
    │     [COL ...]       Optional column names to display
    │     --sorted        Non-increasing confidence order
    │     --recent        Read from recent-run buffer DB
    │     --best N        Top N candidates only
    │     --json          Raw JSON output instead of a table
    │     --db PATH       Explicit DB path override
    ├── candidates-sorted Alias: print candidates --sorted
    ├── candidates-recent Alias: print candidates --recent
    └── schema            List all available column names

EXE packaging
─────────────
    pip install pyinstaller
    pyinstaller --onefile --name transformer src/transformer/__main__.py
"""

from __future__ import annotations

import json
import logging
import pathlib
import sys
from typing import Optional

import click


# ─────────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────────

def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Path / DB helpers
# ─────────────────────────────────────────────────────────────────────────────

def _default_db(output_dir: str) -> str:
    return str(pathlib.Path(output_dir) / "candidates.db")


def _default_recent_db(output_dir: str) -> str:
    return str(pathlib.Path(output_dir) / "recent.db")


def _require_file(path: str, flag: str) -> None:
    if not pathlib.Path(path).is_file():
        click.echo(f"Error: {flag} path does not exist or is not a file: {path!r}", err=True)
        sys.exit(2)


def _require_dir(path: str, flag: str) -> None:
    if not pathlib.Path(path).is_dir():
        click.echo(f"Error: {flag} path does not exist or is not a directory: {path!r}", err=True)
        sys.exit(2)


def _find_default_config() -> str:
    """Walk up from CWD to find config/default_schema.json."""
    for candidate in [pathlib.Path.cwd(), *pathlib.Path.cwd().parents]:
        p = candidate / "config" / "default_schema.json"
        if p.exists():
            return str(p)
    return "config/default_schema.json"


def _load_profiles_from_db(db_path: str) -> list[dict]:
    """Load all candidate rows from an SqliteStore database as plain dicts."""
    try:
        from src.transformer.store.sqlite_store import SqliteStore  # type: ignore
    except ImportError as exc:
        click.echo(f"[error] Could not import SqliteStore: {exc}", err=True)
        sys.exit(1)

    if not pathlib.Path(db_path).exists():
        click.echo(f"[error] Database not found: {db_path}", err=True)
        click.echo("Run  transformer resolve all  or  transformer run ...  first.", err=True)
        sys.exit(1)

    store = SqliteStore(db_path)
    return store.load_all_dicts()


def _discover(source_dir: pathlib.Path) -> dict[str, list[pathlib.Path]]:
    """
    Scan source_dir recursively and return paths grouped by file type.
    Notes (.txt) inside an 'output' subfolder are excluded to avoid
    re-reading previously written JSON as notes.
    """
    result: dict[str, list[pathlib.Path]] = {"csv": [], "json": [], "pdf": [], "txt": []}
    for p in sorted(source_dir.rglob("*")):
        if not p.is_file():
            continue
        # Skip generated/cache directories
        skip = {".cache", "output", "__pycache__", ".git", "node_modules", ".venv", "venv"}
        if any(part in skip for part in p.parts):
            continue
        ext = p.suffix.lower().lstrip(".")
        if ext in result:
            result[ext].append(p)
    return result


def _print_run_summary(summary: dict) -> None:
    processed = summary.get("candidates_processed") or summary.get("candidates", 0)
    errors    = summary.get("errors", [])
    click.echo(f"\n✓  Done — {processed} candidate(s) processed.")
    if summary.get("output_path") or summary.get("output_json"):
        click.echo(f"   JSON export : {summary.get('output_path') or summary.get('output_json')}")
    if summary.get("db_path"):
        click.echo(f"   Database    : {summary['db_path']}")
    if errors:
        click.echo(f"\n⚠  {len(errors)} error(s) during run:", err=True)
        for e in errors:
            click.echo(f"   • {e}", err=True)


# ─────────────────────────────────────────────────────────────────────────────
# Table rendering
# ─────────────────────────────────────────────────────────────────────────────

_ALL_COLUMNS: list[tuple[str, int]] = [
    ("candidate_id",       14),
    ("full_name",          22),
    ("emails",             30),
    ("phones",             20),
    ("headline",           35),
    ("location",           22),
    ("skills",             42),
    ("years_experience",    6),
    ("overall_confidence",  7),
]
_COL_NAMES: list[str]      = [c for c, _ in _ALL_COLUMNS]
_COL_WIDTH: dict[str, int] = {c: w for c, w in _ALL_COLUMNS}


def _cell(profile: dict, col: str) -> str:
    val = profile.get(col)
    if val is None:
        return "—"
    if isinstance(val, list):
        if col == "skills":
            names = [s.get("name", str(s)) if isinstance(s, dict) else str(s) for s in val[:6]]
            return ", ".join(names) + ("…" if len(val) > 6 else "")
        parts = [str(v) for v in val[:3]]
        return ", ".join(parts) + ("…" if len(val) > 3 else "")
    if isinstance(val, dict):
        parts = [val.get("city"), val.get("region"), val.get("country")]
        return ", ".join(p for p in parts if p) or "—"
    if isinstance(val, float):
        return f"{val:.3f}"
    return str(val)


def _print_table(profiles: list[dict], columns: list[str]) -> None:
    widths = [max(len(c), _COL_WIDTH.get(c, 20)) for c in columns]
    header = "  ".join(c.ljust(w) for c, w in zip(columns, widths))
    sep    = "  ".join("─" * w for w in widths)
    click.echo(header)
    click.echo(sep)
    for p in profiles:
        row = "  ".join(_cell(p, c).ljust(w)[:w] for c, w in zip(columns, widths))
        click.echo(row)
    click.echo(f"\n{len(profiles)} candidate(s) shown.")


# ─────────────────────────────────────────────────────────────────────────────
# Root CLI group
# ─────────────────────────────────────────────────────────────────────────────

@click.group()
@click.option("--output", "-o", default="output", show_default=True,
              help="Output directory for DB files and JSON exports.")
@click.option("--verbose", "-v", is_flag=True, default=False,
              help="Enable DEBUG-level logging.")
@click.pass_context
def cli(ctx: click.Context, output: str, verbose: bool) -> None:
    """Eightfold Transformer — candidate data pipeline.

    Run  transformer COMMAND --help  for details on each command.
    """
    ctx.ensure_object(dict)
    ctx.obj["output"]  = output
    ctx.obj["verbose"] = verbose
    _configure_logging(verbose)


# ─────────────────────────────────────────────────────────────────────────────
# resolve all  — auto-scan mode
# ─────────────────────────────────────────────────────────────────────────────

@cli.group()
def resolve() -> None:
    """Source-file ingestion and identity-resolution commands."""


@resolve.command("all")
@click.option("--dir", "-d", "source_dir", default=".", show_default=True,
              help="Directory to scan for CSV, JSON, PDF, and TXT files.")
@click.option("--config", default=None, metavar="PATH",
              help="OutputConfig JSON file. Defaults to config/default_schema.json.")
@click.option("--out", default=None, metavar="PATH",
              help="Projected JSON output path. Defaults to OUTPUT/profiles.json.")
@click.option("--sqlite", default=None, metavar="PATH",
              help="Main SQLite DB path. Defaults to OUTPUT/candidates.db.")
@click.option("--skip-github", is_flag=True, default=False,
              help="Skip GitHub profile enrichment (useful offline).")
@click.pass_context
def resolve_all(
    ctx: click.Context,
    source_dir: str,
    config: Optional[str],
    out: Optional[str],
    sqlite: Optional[str],
    skip_github: bool,
) -> None:
    """Scan SOURCE_DIR for all supported file types and run the full pipeline.

    Reads all .csv, .json (ATS), .pdf (resumes), and .txt (notes) files found
    under the directory.  Performs identity resolution, confidence-scored merge,
    optional GitHub enrichment, and writes results to DB and profiles.json.

    \b
    Examples
    --------
        transformer resolve all
        transformer resolve all --dir ./candidates --skip-github
        transformer -o ./results resolve all --dir /data/q3_intake
        transformer resolve all --config config/custom_config_minimal.json
    """
    output_dir  = ctx.obj["output"]
    verbose     = ctx.obj["verbose"]

    src_path    = pathlib.Path(source_dir).resolve()
    out_dir     = pathlib.Path(output_dir).resolve()
    config_path = config or _find_default_config()
    out_json    = out    or str(out_dir / "profiles.json")
    db_path     = sqlite or str(out_dir / "candidates.db")
    recent_path = str(out_dir / "recent.db")

    # Discover all files under source_dir
    files = _discover(src_path)

    csv_paths = [str(p) for p in files["csv"]]
    pdf_paths = [str(p) for p in files["pdf"]]
    txt_paths = [str(p) for p in files["txt"]]

    # ATS JSON: prefer files named 'ats*' or inside a directory named
    # 'sample_inputs' — skip anything that looks like a config/schema file.
    _CONFIG_KEYWORDS = {"config", "schema", "minimal", "default", "custom"}
    ats_candidates = [
        p for p in files["json"]
        if not any(kw in p.stem.lower() for kw in _CONFIG_KEYWORDS)
        and not any(kw in p.parent.name.lower() for kw in _CONFIG_KEYWORDS)
    ]
    # Prefer files with 'ats' in the name, otherwise take first non-config json
    ats_preferred = [p for p in ats_candidates if "ats" in p.stem.lower()]
    ats_json_path = str((ats_preferred or ats_candidates)[0]) if (ats_preferred or ats_candidates) else ""

    # Notes dir: use the parent directory of the first .txt file found,
    # so the notes adapter can scan it. Fall back to src_path.
    if txt_paths:
        notes_dir_path = str(pathlib.Path(txt_paths[0]).parent)
    else:
        notes_dir_path = str(src_path)

    # PDF dir: use the parent directory of the first PDF found.
    pdf_dir_path = str(pathlib.Path(pdf_paths[0]).parent) if pdf_paths else None

    click.echo(f"[transformer] Scanning : {src_path}")
    click.echo(f"[transformer] Output   : {out_dir}")
    click.echo(f"[transformer] Config   : {config_path}")
    click.echo(f"[transformer] CSV      : {len(csv_paths)} file(s)")
    click.echo(f"[transformer] ATS JSON : {ats_json_path or 'none found'}")
    click.echo(f"[transformer] PDFs     : {len(pdf_paths)} file(s)")
    click.echo(f"[transformer] Notes    : {len(txt_paths)} file(s)")

    try:
        from src.transformer.pipeline import run_pipeline  # type: ignore
    except ImportError as exc:
        click.echo(f"[error] {exc}", err=True)
        sys.exit(1)

    summary = run_pipeline(
        csv_paths=csv_paths,
        ats_json_path=ats_json_path,
        notes_dir=notes_dir_path,
        output_config_path=config_path,
        output_json_path=out_json,
        sqlite_db_path=db_path,
        pdf_dir=pdf_dir_path,
    )

    _print_run_summary(summary)


# ─────────────────────────────────────────────────────────────────────────────
# run  — explicit-path mode (old CLI, fully preserved + extended)
# ─────────────────────────────────────────────────────────────────────────────

@cli.command("run")
@click.option("--csv-dir", default=".", show_default=True, metavar="DIR",
              help="Directory to scan for *.csv files.")
@click.option("--ats", default=None, metavar="PATH",
              help="Path to the ATS JSON export file.")
@click.option("--notes-dir", default=None, metavar="DIR",
              help="Directory containing recruiter note files (*.txt).")
@click.option("--pdf-dir", default=None, metavar="DIR",
              help="Directory containing PDF resume files (*.pdf).")
@click.option("--config", default=None, metavar="PATH",
              help="OutputConfig JSON file. Defaults to config/default_schema.json.")
@click.option("--out", default=None, metavar="PATH",
              help="Projected JSON output path. Defaults to OUTPUT/profiles.json.")
@click.option("--sqlite", default=None, metavar="PATH",
              help="Main SQLite DB path. Defaults to OUTPUT/candidates.db.")
@click.option("--skip-github", is_flag=True, default=False,
              help="Skip GitHub enrichment.")
@click.pass_context
def cmd_run(
    ctx: click.Context,
    csv_dir: str,
    ats: Optional[str],
    notes_dir: Optional[str],
    pdf_dir: Optional[str],
    config: Optional[str],
    out: Optional[str],
    sqlite: Optional[str],
    skip_github: bool,
) -> None:
    """Run the pipeline with explicitly specified file paths.

    All inputs default to sensible values — you can run  transformer run
    from inside your data directory with zero flags and it will auto-detect
    files in the current directory.

    \b
    Examples
    --------
        transformer run --ats sample_inputs/ats_export.json \\
            --notes-dir sample_inputs/notes \\
            --config config/default_schema.json \\
            --out output/profiles.json

        transformer run   # auto-detect everything in current directory

        transformer run --csv-dir ./intake --ats ./intake/ats.json \\
            --pdf-dir ./intake/resumes --sqlite output/pipeline.db
    """
    output_dir  = ctx.obj["output"]
    verbose     = ctx.obj["verbose"]

    # Pre-flight validation for explicitly supplied paths only
    if csv_dir and csv_dir != ".":
        _require_dir(csv_dir, "--csv-dir")
    if ats:
        _require_file(ats, "--ats")
    if notes_dir:
        _require_dir(notes_dir, "--notes-dir")
    if pdf_dir:
        _require_dir(pdf_dir, "--pdf-dir")
    if config:
        _require_file(config, "--config")

    out_dir     = pathlib.Path(output_dir).resolve()
    config_path = config or _find_default_config()
    out_json    = out    or str(out_dir / "profiles.json")
    db_path     = sqlite or str(out_dir / "candidates.db")
    recent_path = str(out_dir / "recent.db")

    click.echo(f"[transformer] Source   : {pathlib.Path(csv_dir).resolve()}")
    click.echo(f"[transformer] Output   : {out_dir}")
    click.echo(f"[transformer] Config   : {config_path}")

    try:
        from src.transformer.pipeline import run_pipeline  # type: ignore
    except ImportError as exc:
        click.echo(f"[error] {exc}", err=True)
        sys.exit(1)

    # Discover CSV files from csv_dir; ATS/notes/pdf from explicit flags or same dir
    csv_paths_list = [str(p) for p in pathlib.Path(csv_dir).glob("*.csv")]

    # ATS: explicit flag wins; otherwise find first .json in csv_dir
    if ats:
        ats_json_path = ats
    else:
        found_json = list(pathlib.Path(csv_dir).glob("*.json"))
        ats_json_path = str(found_json[0]) if found_json else ""

    effective_notes_dir = notes_dir or csv_dir
    effective_pdf_dir   = pdf_dir   # None = skip PDF extraction

    summary = run_pipeline(
        csv_paths=csv_paths_list,
        ats_json_path=ats_json_path,
        notes_dir=effective_notes_dir,
        output_config_path=config_path,
        output_json_path=out_json,
        sqlite_db_path=db_path,
        pdf_dir=effective_pdf_dir,
    )

    _print_run_summary(summary)


# ─────────────────────────────────────────────────────────────────────────────
# print group
# ─────────────────────────────────────────────────────────────────────────────

@cli.group("print")
def print_group() -> None:
    """Display stored candidate data."""


@print_group.command("candidates")
@click.argument("columns", nargs=-1, metavar="[COL ...]")
@click.option("--sorted", "do_sort", is_flag=True, default=False,
              help="Sort by overall_confidence (non-increasing).")
@click.option("--recent", is_flag=True, default=False,
              help="Read from the recent-run buffer DB instead of the main DB.")
@click.option("--best", "top_n", default=None, type=int, metavar="N",
              help="Show only the top N candidates (by confidence).")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Output raw JSON instead of a table.")
@click.option("--db", default=None, metavar="PATH",
              help="Explicit DB path (overrides --recent and default location).")
@click.pass_context
def print_candidates(
    ctx: click.Context,
    columns: tuple[str, ...],
    do_sort: bool,
    recent: bool,
    top_n: Optional[int],
    as_json: bool,
    db: Optional[str],
) -> None:
    """Print candidate profiles from the database.

    Optionally pass column names as positional arguments to restrict output.

    \b
    Available columns
    -----------------
        candidate_id  full_name  emails  phones  headline
        location  skills  years_experience  overall_confidence

    \b
    Examples
    --------
        transformer print candidates
        transformer print candidates --sorted
        transformer print candidates --recent
        transformer print candidates --best 10
        transformer print candidates --sorted --best 5
        transformer print candidates full_name headline overall_confidence
        transformer print candidates --best 3 --json
        transformer print candidates --db /path/to/custom.db
    """
    output_dir = ctx.obj["output"]

    if db:
        db_path = db
    elif recent:
        db_path = _default_recent_db(output_dir)
    else:
        db_path = _default_db(output_dir)

    profiles = _load_profiles_from_db(db_path)

    # Always sort when --best is used, even without explicit --sorted
    if do_sort or top_n is not None:
        profiles.sort(
            key=lambda p: float(p.get("overall_confidence") or 0.0),
            reverse=True,
        )

    if top_n is not None:
        profiles = profiles[:top_n]

    if not profiles:
        click.echo("No candidates found in the database.")
        return

    # Column selection
    if columns:
        selected: list[str] = []
        for c in columns:
            if c in _COL_NAMES:
                selected.append(c)
            else:
                click.echo(f"[warn] Unknown column '{c}' — skipped.", err=True)
        if not selected:
            click.echo(f"[error] No valid columns.  Valid: {', '.join(_COL_NAMES)}", err=True)
            sys.exit(1)
    else:
        selected = ["candidate_id", "full_name", "emails", "headline", "overall_confidence"]

    if as_json:
        out = [{c: p.get(c) for c in selected} for p in profiles]
        click.echo(json.dumps(out, indent=2, default=str))
    else:
        _print_table(profiles, selected)


@print_group.command("candidates-sorted")
@click.argument("columns", nargs=-1, metavar="[COL ...]")
@click.option("--best", "top_n", default=None, type=int, metavar="N",
              help="Show only top N candidates.")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Output raw JSON.")
@click.option("--db", default=None, metavar="PATH",
              help="Explicit DB path.")
@click.pass_context
def print_candidates_sorted(
    ctx: click.Context,
    columns: tuple[str, ...],
    top_n: Optional[int],
    as_json: bool,
    db: Optional[str],
) -> None:
    """Print candidates sorted by confidence score (non-increasing).

    \b
    Examples
    --------
        transformer print candidates-sorted
        transformer print candidates-sorted --best 5
        transformer print candidates-sorted full_name headline overall_confidence
    """
    ctx.invoke(print_candidates, columns=columns, do_sort=True,
               recent=False, top_n=top_n, as_json=as_json, db=db)


@print_group.command("candidates-recent")
@click.argument("columns", nargs=-1, metavar="[COL ...]")
@click.option("--sorted", "do_sort", is_flag=True, default=False,
              help="Sort by confidence.")
@click.option("--best", "top_n", default=None, type=int, metavar="N",
              help="Show only top N.")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Output raw JSON.")
@click.pass_context
def print_candidates_recent(
    ctx: click.Context,
    columns: tuple[str, ...],
    do_sort: bool,
    top_n: Optional[int],
    as_json: bool,
) -> None:
    """Print candidates from the recent-run buffer database.

    \b
    Examples
    --------
        transformer print candidates-recent
        transformer print candidates-recent --sorted --best 3
        transformer print candidates-recent full_name overall_confidence
    """
    ctx.invoke(print_candidates, columns=columns, do_sort=do_sort,
               recent=True, top_n=top_n, as_json=as_json, db=None)


@print_group.command("schema")
def print_schema() -> None:
    """List all column names available for  transformer print candidates."""
    click.echo("Available columns for  transformer print candidates [COL ...]:\n")
    for col, width in _ALL_COLUMNS:
        click.echo(f"  {col:<25}  (default display width: {width})")
    click.echo("\nUsage:  transformer print candidates full_name emails overall_confidence")


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    """Console-script entry point (also used by __main__.py and the .exe)."""
    cli(obj={})


if __name__ == "__main__":
    main()