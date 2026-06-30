# Eightfold Transformer

A candidate-data transformation pipeline that ingests recruiter CSV exports,
ATS JSON exports, PDF resumes, GitHub profiles, and free-text recruiter notes;
deduplicates and resolves candidate identities across sources; merges
conflicting field values with confidence scoring; and projects the result
into a configurable JSON output and a queryable SQLite database, directly accessible with cli commands.


## Overview

Eightfold Transformer ingests candidate data from four heterogeneous sources — recruiter CSV exports, ATS JSON exports, GitHub profiles, and free-text recruiter notes — and produces a unified, confidence-scored `CanonicalProfile` per candidate.

Key properties:

- **Deterministic.** The same inputs always produce the same output. No random seeds, no non-deterministic clustering.
- **Source-agnostic.** Each adapter translates its source's field names into a common `RawField` envelope. Adding a new source means writing one new adapter — nothing else changes.
- **Confidence-aware.** Every field carries a computed confidence score based on source reliability, extraction method, and cross-source corroboration.
- **Configurable output.** A JSON config file controls which fields appear in the output, how they are named, whether they are re-normalised, and whether confidence/provenance metadata is included.

---

## Architecture
![alt text](image-1.png)
---

## Requirements

* Python 3.10 or later
* `pip`
* Internet access only if GitHub enrichment is used (everything else runs offline)

---

## Installation
clone the repo, install dependencies into a virtual environment, and run the tool with `python -m`

### macOS

```bash
# 1. Clone the repository
git clone <your-repo-url> eightfold-transformer
cd eightfold-transformer

# 2. Create a virtual environment
#    (use python3 here — plain "python" is unreliable on macOS)
python3 -m venv .venv

# 3. Activate the virtual environment
source .venv/bin/activate

# 4. Install dependencies
pip install -r requirements.txt

# 5. Run the pipeline
python -m src.transformer resolve all
```

> Once the virtual environment is activated, `python` correctly points to
> the venv's Python 3 interpreter — every command below works exactly as
> written, with no `python3` prefix needed after this point.

### Windows

```powershell
# 1. Clone the repository
git clone <your-repo-url> eightfold-transformer
cd eightfold-transformer

# 2. Create a virtual environment
python -m venv .venv

# 3. Activate the virtual environment
.venv\Scripts\activate

# 4. Install dependencies
pip install -r requirements.txt

# 5. Run the pipeline
python -m src.transformer resolve all
```

> If PowerShell blocks the activation script with an execution-policy error,
> run this once in an elevated PowerShell window, then retry step 3:
> `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`

### Dependencies

pydantic>=2.0
phonenumbers
python-dateutil
rapidfuzz
requests
jsonschema
pdfplumber>=0.10.0
click>=8.0

### Important — run from the repository root

On **both** platforms, every command below must be run from inside the
`eightfold-transformer/` folder (the one containing `src/`), with the
virtual environment activated. `python -m src.transformer` resolves its
imports relative to your current working directory, so running it from
anywhere else will fail with a `ModuleNotFoundError`.

---

## Full Command Reference

Every command below is identical on macOS and Windows once the virtual
environment is activated — the only platform differences are the
installation steps above.

### Pipeline commands

```bash
python -m src.transformer resolve all
python -m src.transformer resolve all --dir sample_inputs
python -m src.transformer resolve all --skip-github
python -m src.transformer resolve all --dir sample_inputs --config config/custom_config_minimal.json
python -m src.transformer resolve all --out output/profiles_custom.json
python -m src.transformer resolve all --sqlite output/custom.db
```

| Command | Description |
|---|---|
| `resolve all` | Scans a directory (default: current directory) for all supported file types — CSV, JSON (ATS), PDF (resumes), TXT (notes) — runs the full pipeline, and writes results to `output/candidates.db` and `output/profiles.json`. This is the main command you'll use day-to-day. |
| `resolve all --dir ./sample_inputs` | Same as above but scans a specific directory instead of the current one. Useful when your data files are in a subfolder. |
| `resolve all --skip-github` | Skips the GitHub API enrichment step. Use this when you're offline, rate-limited, or just want a faster run without live data fetching. |
| `resolve all --config config/custom_config_minimal.json` | Uses a custom output schema instead of the default one. Controls which fields appear in the output JSON and how they're named/normalized. |
| `resolve all --out output/profiles_custom.json` | Writes the projected JSON to a custom path instead of the default `output/profiles.json`. |
| `resolve all --sqlite output/custom.db` | Writes the SQLite database to a custom path instead of the default `output/candidates.db`. |

### Explicit-path mode

```bash
python -m src.transformer run --csv-dir sample_inputs --ats sample_inputs/ats_export.json --notes-dir sample_inputs/notes
python -m src.transformer run --csv-dir sample_inputs --ats sample_inputs/ats_export.json --notes-dir sample_inputs/notes --pdf-dir sample_inputs/resumes
python -m src.transformer run --csv-dir sample_inputs --ats sample_inputs/ats_export.json --notes-dir sample_inputs/notes --config config/custom_config_minimal.json --out output/minimal.json
```

| Command | Description |
|---|---|
| `run` | Explicit-path version of `resolve all`. Instead of auto-scanning, you tell it exactly where each type of file lives. More precise, better for scripting and CI pipelines. |
| `run --csv-dir sample_inputs` | Scans a specific directory for `*.csv` files only. |
| `run --ats sample_inputs/ats_export.json` | Points directly at the ATS JSON file instead of auto-detecting it. |
| `run --notes-dir sample_inputs/notes` | Points directly at the notes directory instead of auto-detecting it. |
| `run --pdf-dir sample_inputs/resumes` | Points directly at the PDF resumes directory instead of auto-detecting it. |
| `run --config config/custom_config_minimal.json` | Same as `resolve all --config` — uses a custom output schema. |
| `run --out output/minimal.json` | Custom output JSON path. |
| `run --sqlite output/pipeline.db` | Custom SQLite DB path. |

### Print candidates

```bash
python -m src.transformer print candidates
python -m src.transformer print candidates --sorted
python -m src.transformer print candidates --best 3
python -m src.transformer print candidates --sorted --best 3
python -m src.transformer print candidates --json
python -m src.transformer print candidates --sorted --json
```

| Command | Description |
|---|---|
| `print candidates` | Reads from `output/candidates.db` and prints a table with the five most useful columns: `candidate_id`, `full_name`, `emails`, `headline`, `overall_confidence`. Order is whatever the DB returns (insertion order by default). |
| `print candidates --sorted` | Same table but sorted by `overall_confidence` from highest to lowest. This is the most useful view for ranking candidates. |
| `print candidates --best 3` | Shows only the top N candidates by confidence score. Automatically sorts — no need to add `--sorted`. |
| `print candidates --sorted --best 3` | Sorted and capped. Same as `--best 3` alone but explicit. |
| `print candidates --recent` | Reads from `output/recent.db` (the buffer DB written by the last pipeline run) instead of the main candidates DB. Useful when you want to inspect just the profiles from the most recent batch without seeing older runs mixed in. |
| `print candidates --json` | Outputs raw JSON instead of a table. Useful for piping into other tools or saving to a file. |
| `print candidates --db output/custom.db` | Reads from a specific DB file instead of the default location. Useful when you've been writing to custom paths. |

### Print specific columns

```bash
python -m src.transformer print candidates full_name
python -m src.transformer print candidates full_name headline
python -m src.transformer print candidates full_name emails overall_confidence
python -m src.transformer print candidates full_name headline skills overall_confidence
python -m src.transformer print candidates candidate_id full_name emails phones headline location skills years_experience overall_confidence
python -m src.transformer print candidates full_name overall_confidence --sorted --best 3
python -m src.transformer print candidates full_name headline --json
```

| Command | Description |
|---|---|
| `print candidates full_name` | Shows only the `full_name` column. You can pass any number of column names as positional arguments. |
| `print candidates full_name headline` | Shows only `full_name` and `headline`. |
| `print candidates full_name emails overall_confidence` | Shows name, email, and confidence score — a clean summary view. |
| `print candidates full_name headline skills overall_confidence` | Adds the skills column — shows what each candidate is known for. |
| `print candidates candidate_id full_name emails phones headline location skills years_experience overall_confidence` | Shows every available column — the full view. |
| `print candidates full_name overall_confidence --sorted --best 3` | Combines column selection, sorting, and top-N cap. Shows the top 3 candidates with just name and confidence. |
| `print candidates full_name headline --json` | Custom columns output as JSON instead of a table. |

### Aliases

```bash
python -m src.transformer print candidates-sorted
python -m src.transformer print candidates-sorted --best 3
python -m src.transformer print candidates-sorted full_name headline overall_confidence
python -m src.transformer print candidates-recent
python -m src.transformer print candidates-recent --sorted
```

| Command | Description |
|---|---|
| `print candidates-sorted` | Alias for `print candidates --sorted`. Shorter to type. |
| `print candidates-sorted --best 5` | Top 5 by confidence, sorted. Same as `print candidates --sorted --best 5`. |
| `print candidates-sorted full_name headline overall_confidence` | Sorted table with custom columns. |
| `print candidates-recent` | Alias for `print candidates --recent`. Shows the most recent run's profiles. |
| `print candidates-recent --sorted` | Recent profiles, sorted by confidence. |

### Schema

```bash
python -m src.transformer print schema
```

| Command | Description |
|---|---|
| `print schema` | Lists all valid column names you can pass to `print candidates`. Run this whenever you forget a column name. Also shows the default display width for each column. |

### Global flags (work with any command)

```bash
python -m src.transformer --verbose resolve all
python -m src.transformer -v resolve all
python -m src.transformer --output my_output resolve all
python -m src.transformer -o my_output print candidates --sorted
```

| Flag | Description |
|---|---|
| `--verbose` / `-v` | Enables DEBUG-level logging — shows every internal step including cache hits, field routing decisions, and normalizer calls. Very useful for debugging why a candidate came out wrong. |
| `--output` / `-o` | Changes the output directory for all DB files and JSON exports. Applies to the entire session — put it before the subcommand: `python -m src.transformer -o my_output resolve all`. |

### Help

```bash
python -m src.transformer --help
python -m src.transformer resolve --help
python -m src.transformer resolve all --help
python -m src.transformer print --help
python -m src.transformer print candidates --help
python -m src.transformer run --help
```

`--help` works on every command and subcommand and shows available flags
and examples.

---

## Configuration

### Output config schema

The output config controls exactly which fields appear in the result, how they are named, and whether confidence/provenance metadata is included.

```json
{
  "include_confidence": true,
  "include_provenance": true,
  "on_missing": "null",
  "fields": [
    {
      "path": "candidate_id",
      "type": "string",
      "required": true
    },
    {
      "path": "full_name",
      "type": "string"
    },
    {
      "path": "emails",
      "type": "array"
    },
    {
      "path": "phones",
      "type": "array"
    },
    {
      "path": "location.city",
      "type": "string"
    },
    {
      "path": "location.region",
      "type": "string"
    },
    {
      "path": "location.country",
      "type": "string"
    },
    {
      "path": "links.linkedin",
      "type": "string"
    },
    {
      "path": "links.github",
      "type": "string"
    },
    {
      "path": "links.portfolio",
      "type": "string"
    },
    {
      "path": "links.other",
      "type": "array"
    },
    {
      "path": "headline",
      "type": "string"
    },
    {
      "path": "years_experience",
      "type": "number"
    },
    {
      "path": "skills",
      "type": "array"
    },
    {
      "path": "experience",
      "type": "array"
    },
    {
      "path": "education",
      "type": "array"
    },
    {
      "path": "overall_confidence",
      "type": "number"
    }
  ]
}

```


### Source weights

`config/source_weights.json` controls how much each source is trusted per field category:

```json
{
  "csv": {"identity": 0.9, "employment": 0.85, "skills": 0.3},
  "ats_json": {"identity": 0.95, "employment": 0.9, "skills": 0.4},
  "github": {"identity": 0.4, "employment": 0.1, "skills": 0.85},
  "notes": {"identity": 0.3, "employment": 0.4, "skills": 0.5},
  "resume_pdf" : {"identity": 0.75, "employment": 0.8, "skills": 0.7}
}

```

Field categories: `identity` (name, email, phone, location, links), `employment` (headline, experience, education), `skills`.

---

## Output formats

### Default schema output (`config/default_schema.json`)

```json
[
  {
    "candidate_id": "a3f8e1b2c901",
    "full_name": { "value": "Jordan Ellis", "confidence": 0.9500 },
    "emails":    { "value": ["jordan.ellis@gmail.com"], "confidence": 0.9025 },
    "phones":    { "value": ["+14155550192"], "confidence": 0.9025 },
    "location": {
      "city":    { "value": "San Francisco", "confidence": 0.9025 },
      "country": { "value": "US", "confidence": 0.9025 }
    },
    "skills":    { "value": ["Python", "React", "Kubernetes", "AWS"], "confidence": 0.62 },
    "overall_confidence": { "value": 0.7812, "confidence": 0.7812 }
  }
]
```

### Minimal schema output (`config/custom_config_minimal.json`)

```json
[
  {
    "full_name":     { "value": "Jordan Ellis",          "confidence": 1.0 },
    "primary_email": { "value": "jordan.ellis@gmail.com", "confidence": 0.9025 },
    "phone":         { "value": "+14155550192",           "confidence": 0.9025 },
    "skills":        { "value": ["Python", "React", "AWS"], "confidence": 0.25 }
  }
]
```

---

## Running tests

```bash
pytest tests/ -v

```

`tests/conftest.py` adds the project root to `sys.path` so tests can import `src.transformer` without installing the package.

---

## Design decisions

**Why not use a clustering algorithm for identity resolution?**

K-means and DBSCAN both require either a known K or a distance metric that works uniformly across all feature types. Email addresses are exact-match signals; phone numbers require E.164 normalisation before comparison; names require fuzzy string similarity. These signals are incommensurable — no single numeric distance captures all of them faithfully. Deterministic blocking by exact key is the standard entity-resolution approach at this scale (Fellegi-Sunter, 1969; Christen, *Data Matching*, 2012) and is fully explainable to recruiters and auditors.

**Why is country normalisation exact-match only?**

A wrong country code propagates silently into `normalize_phone()`, where it would cause the phone parser to interpret numbers under the wrong country's dialling rules. A `None` country is recoverable; `"AU"` when the candidate is in `"AT"` (Austria) is not. Strictness here is a deliberate trade-off: we prefer no country over a wrong one.

**Why does `current_title` route to two canonical paths?**

Both `headline` (the candidate's professional tagline) and `experience.title` (the title within a work-history entry) legitimately receive the same value from ATS and CSV sources. A flat `str → str` route map cannot express "one input, two outputs", so multi-target routing is handled via a separate `_MULTI_FIELD_ROUTE` dict checked before the primary `_FIELD_ROUTE`.

**Why is the JSON Schema generated from the OutputConfig rather than written by hand?**

Keeping a separate hand-maintained schema in sync with a config file that changes frequently is a maintenance liability. Generating the schema from the same config that drives projection makes schema drift structurally impossible: if the config says a field exists, the schema will validate for it; if the config removes it, the schema won't accept it.

**Why WAL mode for SQLite?**

Write-Ahead Logging allows concurrent reads while a write is in progress. This is important when a query tool (e.g. a reporting script or `sqlite3` CLI) is open against the database while the pipeline is running. Without WAL, a write transaction would lock the entire database and block all readers.


[def]: image-1.png
