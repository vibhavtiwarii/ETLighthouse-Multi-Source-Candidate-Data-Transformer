## Project structure

```
eightfold-transformer/
├── config/
│   ├── default_schema.json          # Full-field output config (all canonical fields)
│   ├── custom_config_minimal.json   # Minimal output config example
│   └── source_weights.json          # Per-source confidence weights by field category
├── data/
│   ├── skill_synonyms.json          # Canonical skill names and their variants
│   └── iso3166_countries.json       # Country name → ISO 3166-1 alpha-2 lookup
├── sample_inputs/
│   ├── recruiters.csv
│   ├── ats_export.json
│   └── notes/
│       ├── note_001.txt
│       ├── note_002.txt
│       └── note_003.txt
├── src/transformer/
│   ├── cli.py                       # Argument parsing and entry point
│   ├── pipeline.py                  # Phase orchestration
│   ├── raw_field.py                 # RawField Pydantic model
│   ├── schema.py                    # CanonicalProfile and sub-models
│   ├── adapters/
│   │   ├── base.py                  # Abstract SourceAdapter base class
│   │   ├── csv_adapter.py
│   │   ├── ats_adapter.py
│   │   ├── github_adapter.py
│   │   └── notes_adapter.py
│   ├── normalize/
│   │   ├── phone.py                 # E.164 normalisation (phonenumbers)
│   │   ├── date.py                  # YYYY-MM normalisation (dateutil)
│   │   ├── skills.py                # Synonym + fuzzy normalisation (rapidfuzz)
│   │   └── country.py              # ISO 3166 exact-match normalisation
│   ├── resolve/
│   │   └── identity.py             # resolve_identities() + attach_enrichment()
│   ├── merge/
│   │   ├── confidence.py            # Confidence formula and weight loading
│   │   └── merge.py                 # merge_candidate() — field-level merge logic
│   ├── projection/
│   │   ├── config_model.py          # FieldConfig + OutputConfig Pydantic models
│   │   ├── project.py               # profile → output dict projection
│   │   └── schema_gen.py            # JSON Schema generation from OutputConfig
│   ├── store/
│   │   └── sqlite_store.py          # SQLite persistence (WAL, FTS5)
│   └── cache/
│       └── extraction_cache.py      # SHA-256-keyed file cache for GitHub responses
├── tests/
│   ├── conftest.py
│   ├── test_normalize.py
│   ├── test_adapters.py
│   ├── test_identity_resolution.py
│   ├── test_merge_confidence.py
│   ├── test_projection.py
│   └── test_golden_profiles.py
├── output/                          # Default output directory (git-ignored)
├── .cache/                          # GitHub API response cache (auto-created)
├── requirements.txt
└── README.md
```



## Pipeline phases

### Phase 1 — Extraction

`CsvAdapter` and `AtsJsonAdapter` read their respective input files and emit a flat `list[RawField]`. Each `RawField` carries:

- `field_name` — canonical logical name (e.g. `"emails"`, `"location.city"`)
- `value` — the raw extracted value
- `source` — adapter identifier (`"csv"`, `"ats_json"`, etc.)
- `method` — extraction technique (`"direct_copy"`, `"field_remap"`, etc.)
- `raw_text` — original unprocessed text for audit
- `record_index` — row/record position within the source (set by the adapter itself)

### Phase 2 — Identity resolution

`resolve_identities()` groups the flat field list into per-candidate buckets using two-tier deterministic blocking (see [Identity resolution](#identity-resolution) below). Each `RawField`'s `candidate_id` attribute is set in-place.

### Phase 3 — Enrichment

`attach_enrichment()` scans each candidate's existing fields for a `links.github` URL, calls `GithubAdapter.extract()` for each, and appends the returned `RawField`s to that candidate. It then reads every `*.txt` file in `--notes-dir`, runs `NotesAdapter.extract()`, and matches the note to a candidate via exact email or phone. Notes are attached conservatively — fuzzy name matching is deliberately excluded to avoid contaminating the wrong candidate's profile.

### Phase 4 — Merge

`merge_candidate()` runs for each candidate independently. It routes every `RawField` to its canonical path, normalises the value, computes a confidence score, and resolves conflicts. The result is a fully populated `CanonicalProfile`.

### Phase 5 — Projection

`project()` walks the `CanonicalProfile` dictionary, resolves each `FieldConfig.from_` path, optionally re-normalises, applies the `on_missing` policy, and wraps in confidence/provenance envelopes. The output dict is validated against a JSON Schema generated from the same `OutputConfig`.

### Phase 6 — JSON output

The list of projected dicts is written to `--out` as a UTF-8 JSON array.

### Phase 7 — SQLite persistence (optional)

When `--sqlite` is supplied, each `CanonicalProfile` is upserted into a SQLite database with WAL journal mode. Three tables are maintained: `profiles` (scalar columns + full JSON blob), `skills` (indexed for O(log n) skill search), and `profiles_fts` (FTS5 virtual table for full-text search).

---

## Data sources and adapters

All adapters inherit from `SourceAdapter` (defined in `adapters/base.py`) and must uphold one contract: `extract()` **never raises**. On any failure the method logs a `WARNING` and returns `[]`.

### CSV adapter

Reads recruiter CSV exports with `csv.DictReader`. Column names are mapped to canonical field names via a broad alias table (e.g. `"candidate name"`, `"applicant_name"`, and `"full_name"` all map to `full_name`). Blank cells are skipped; the whole row is never skipped due to a single missing field.

### ATS JSON adapter

Reads a JSON file whose top-level value is a list of ATS candidate records. Translates non-canonical ATS field names (e.g. `cand_full_nm`, `job_ttl`, `gh_url`) to canonical names. Supports an extensive alias table covering many common ATS and CRM field name conventions.

### GitHub adapter

Parses the GitHub username from a profile URL, then makes two calls to the GitHub REST API v3:

- `GET /users/{username}` — extracts `full_name` (from `name`) and `headline` (from `bio`)
- `GET /users/{username}/repos?per_page=100` — extracts one `skills` `RawField` per distinct programming language

Responses are cached in `.cache/` before each HTTP call and stored after (see [Caching](#caching)).

### Notes adapter

Reads a plain-text `.txt` file and extracts:

1. **Email** — first match of a standard email regex, `method="regex_extract"`
2. **Phone** — first phone-like string with ≥7 digits, `method="regex_extract"`
3. **Skills** — whole-word case-insensitive keyword match against `data/skill_synonyms.json`, `method="keyword_match"`. One `RawField` per distinct canonical skill found.

All `RawField`s carry `raw_text` set to the full note content for a complete audit trail.

### Resume Adaptor
Reads resumes available in resume directory and extracts data from them.
---

## Identity resolution

Identity resolution uses **deterministic two-tier blocking** — the standard entity resolution approach (Fellegi-Sunter, 1969) at typical recruitment dataset scale. Clustering algorithms (K-means, DBSCAN) are intentionally not used because:

- K (number of candidates) is unknown in advance
- Matching signals are incommensurable (exact email vs. fuzzy name) and cannot be collapsed into a single distance vector
- Determinism is a hard requirement for auditability

**Tier 1 — exact match (O(1) via hash index)**

A record is merged into an existing candidate if it shares a normalised email address or an E.164-normalised phone number with any field already attributed to that candidate.

**Tier 2 — fuzzy name + company (fallback only)**

If no Tier-1 key matched, the incoming record is compared against all existing candidates using:
- `rapidfuzz.fuzz.token_sort_ratio` on `full_name` ≥ 88
- `rapidfuzz.fuzz.token_sort_ratio` on `current_company` ≥ 75

Both conditions must be met. A missing company on either side does **not** count as a match — name similarity alone is insufficient.

Records that match neither tier become new candidates with a UUID-derived ID.

---

## Merge and confidence scoring

`merge_candidate()` processes each canonical field through a common pipeline:

1. **Route** — map each `RawField.field_name` to its canonical dotted path. `current_title` is a special case: it routes to both `headline` and `experience.title` simultaneously (multi-target routing).

2. **Normalise** — apply the field-appropriate normaliser (phone, date, skill, country, or default string strip).

3. **Score** — compute confidence for each value assertion:

   ```
   confidence = min(1.0,
     source_weight(source, field_category)
     × method_certainty(method)
     × (1.0 + 0.15 × (corroborating_source_count − 1))
   )
   ```

   Method certainty table:

   | Method | Certainty |
   |---|---|
   | `direct_copy` | 1.00 |
   | `field_remap` | 0.95 |
   | `api_fetch` | 0.80 |
   | `regex_extract` | 0.60 |
   | `keyword_match` | 0.50 |
   | `fuzzy_match` | 0.50 |

4. **Resolve** — for scalar fields, the highest-confidence value wins. Conflicts (two or more distinct values) are logged as `WARNING` with full source attribution — never silently dropped. For list fields (`emails`, `phones`, `skills`), the union of all distinct normalised values is taken.

5. **Provenance** — a `ProvenanceEntry` is appended for every populated field, carrying that field's own confidence score (not a single profile-wide number).

6. **Overall confidence** — a weighted blend:
   - 50% mean of per-field confidences
   - 30% corroboration score (fraction of scalar fields confirmed by ≥2 distinct sources)
   - 20% source coverage score (fraction of the 4 known sources that contributed at least one field)

---

## Normalisation

All normalisers are pure functions with no I/O per call. Data files are loaded once at module import time.

### `normalize_phone(raw, country_hint=None)`

Uses the `phonenumbers` library. Tries parsing with the provided country hint, then falls back to `"US"` as a last resort. Returns an E.164 string (e.g. `"+14155550192"`) or `None` if the input cannot be parsed.

### `normalize_skill(raw)`

Lookup order:
1. Exact lowercase match against `data/skill_synonyms.json`
2. `rapidfuzz.fuzz.ratio` fuzzy match against all keys (threshold 85)
3. Return the original input capitalised as a best-effort fallback (unknown skills are never dropped)

### `normalize_country(raw)`

Exact lowercase match against `data/iso3166_countries.json`. Returns ISO 3166-1 alpha-2 code or `None`. Deliberately strict — no fuzzy matching — because a wrong country code would corrupt downstream phone normalisation.

### `normalize_date(raw)`

Uses `dateutil.parser.parse` with `fuzzy=True`. Returns a `"YYYY-MM"` string, the literal string `"present"` for open-ended ranges, or `None` if the input cannot be parsed.

---

## Projection

**Missing-value policy** (`on_missing`):

| Policy | Behaviour |
|---|---|
| `"null"` | Key is kept in output; value is `null` |
| `"omit"` | Key is dropped from output entirely |
| `"error"` | `MissingRequiredFieldError` is raised |

Fields marked `required: true` always raise on missing, regardless of `on_missing`.

After projection, the output dict is validated against a JSON Schema generated from the same `OutputConfig` by `generate_json_schema()`. This makes schema drift structurally impossible.

---

## SQLite persistence

When `--sqlite` is provided, `SqliteStore` maintains three tables:

```sql
profiles          -- one row per candidate; scalar columns + full_profile_json blob
skills            -- one row per (candidate, skill); indexed for fast skill search
profiles_fts      -- FTS5 virtual table on full_name, headline, company, skills_text
```

Key design choices:

- **WAL journal mode** — allows concurrent readers while a write is in progress.
- **Single transaction per profile** — profiles, skills, and FTS rows are committed together; the database is never left in a partially-written state.
- **Idempotent** — re-running the pipeline for the same candidates uses `INSERT OR REPLACE`; the database converges to the latest state.


---

## Caching

`ExtractionCache` is a SHA-256-keyed, JSON-backed file cache stored in `.cache/`. It is used exclusively by `GithubAdapter` to avoid redundant API calls across pipeline runs.

```python
from src.transformer.cache.extraction_cache import ExtractionCache

cache = ExtractionCache(".cache")
data = cache.get("https://api.github.com/users/octocat")  # None on miss
if data is None:
    data = requests.get(...).json()
    cache.set("https://api.github.com/users/octocat", data)
```

`get()` never raises — it returns `None` on any miss, corrupted file, or permission error. `set()` logs a warning on failure and swallows the exception, preserving the adapter's never-raise contract.

To clear the cache:

```bash
rm -rf .cache/
```

---


