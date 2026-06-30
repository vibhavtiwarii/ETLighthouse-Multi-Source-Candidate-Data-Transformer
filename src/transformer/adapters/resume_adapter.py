from __future__ import annotations
"""
src/transformer/adapters/resume_adapter.py
──────────────────────────────────────────
PDF resume parser — converts a candidate's resume PDF into RawField objects
that flow through the same identity-resolution and merge pipeline as CSV and
ATS JSON records.

Design contracts (must match the rest of the pipeline)
──────────────────────────────────────────────────────
1.  extract(path, record_index) → list[RawField]
      Returns a FLAT list (not list-of-lists). This matches what NotesAdapter
      and GithubAdapter return, and what pipeline.py expects for enrichment
      adapters.  CsvAdapter and AtsJsonAdapter return flat lists too — the
      pipeline just calls .extend() on the result.

2.  source = "resume_pdf"
      Must have a matching entry in config/source_weights.json (see files
      to edit below).

3.  field_name values must be keys in merge.py's _FIELD_ROUTE.
      Every field_name used here has been verified against _FIELD_ROUTE:
        full_name, email, phone, links.linkedin, links.github, links.other,
        location.city, location.region, years_experience,
        experience.company, experience.start, experience.end,
        education.degree, skill, headline
      "current_title" is intentionally NOT used — _FIELD_ROUTE maps both
      "title" (csv) and "current_title" to "headline".  We emit "headline"
      directly so the routing is unambiguous.

4.  record_index is passed in by the caller (pipeline.py) and stamped on
      every RawField here, exactly as CsvAdapter does per row_index and
      AtsJsonAdapter does per record_index.

5.  Skill synonyms are loaded from data/skill_synonyms.json directly —
      NOT imported from notes_adapter, because notes_adapter has no
      module-level SYNONYM_MAP export (it loads synonyms inside extract()).
      This avoids the silent empty-dict fallback in the original parser.

Error policy (matches all other adapters)
──────────────────────────────────────────
Missing file, unreadable PDF, pdfplumber not installed → log WARNING,
return [].  No exception ever propagates out of extract().
"""



import json
import logging
import pathlib
import re
from typing import Optional

from src.transformer.adapters.base import SourceAdapter
from src.transformer.raw_field import RawField

logger = logging.getLogger(__name__)

_SOURCE = "resume_pdf"

# Path to the skill synonyms file, resolved from the project root.
# Same path used by NotesAdapter — single source of truth.
_SYNONYMS_PATH = pathlib.Path("data/skill_synonyms.json")


# ---------------------------------------------------------------------------
# Synonym map loader (mirrors NotesAdapter._load_synonyms exactly)
# ---------------------------------------------------------------------------

def _load_synonyms(synonyms_path: pathlib.Path) -> dict[str, list[str]]:
    """
    Load skill_synonyms.json → {canonical_name: [variant, ...]}

    Returns {} on any failure so skill extraction degrades gracefully
    rather than crashing.
    """
    try:
        if not synonyms_path.exists():
            logger.warning(
                "ResumeAdapter: skill_synonyms.json not found at %r — "
                "skill synonym matching disabled",
                str(synonyms_path),
            )
            return {}
        with synonyms_path.open(encoding="utf-8") as fh:
            return json.load(fh)
    except Exception as exc:
        logger.warning(
            "ResumeAdapter: failed to load skill_synonyms.json: %s — "
            "skill synonym matching disabled",
            exc,
        )
        return {}


# ---------------------------------------------------------------------------
# Compiled patterns
# ---------------------------------------------------------------------------

_EMAIL_RE    = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
_PHONE_RE    = re.compile(r"(?:\+?[\d][\d\s\-().]{5,}[\d])")
_LINKEDIN_RE = re.compile(r"linkedin\.com/in/[\w\-]+", re.IGNORECASE)
_GITHUB_RE   = re.compile(r"github\.com/[\w\-]+", re.IGNORECASE)
_URL_RE      = re.compile(r"https?://[^\s]+")

# Section header detection — covers the most common resume layouts
_SECTION_RE = re.compile(
    r"^\s*(EXPERIENCE|WORK EXPERIENCE|EMPLOYMENT|PROFESSIONAL EXPERIENCE"
    r"|EDUCATION|ACADEMIC|SKILLS|TECHNICAL SKILLS|TECHNOLOGIES"
    r"|SUMMARY|OBJECTIVE|PROFILE|PROJECTS|CERTIFICATIONS|AWARDS)\s*$",
    re.IGNORECASE | re.MULTILINE,
)

# Name: first short line (≤5 words) that looks like "Firstname Lastname"
# Only uppercase-initial words, no digits.
_NAME_RE = re.compile(r"^([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,4})\s*$", re.MULTILINE)

# Headline / current title — a title-like phrase near the top of the doc
_HEADLINE_RE = re.compile(
    r"(?:Senior|Junior|Lead|Principal|Staff|Head of|Director of|VP of|"
    r"Software|Data|Product|UX|Full[- ]Stack|Frontend|Backend|DevOps|ML|"
    r"AI|Platform|Cloud|Mobile|iOS|Android|QA|Site Reliability)[^\n]{0,80}",
    re.IGNORECASE,
)

# Location: "City, ST" or "City, Country"
_LOCATION_RE = re.compile(
    r"([A-Z][a-zA-Z\s]{1,25}),\s*([A-Z]{2}|[A-Z][a-zA-Z\s]{2,20})"
)

# Years of experience: "X+ years of experience"
_YEARS_RE = re.compile(r"(\d+)\+?\s*years?\s+(?:of\s+)?experience", re.IGNORECASE)

# Degree lines: starts with a degree keyword
_DEGREE_RE = re.compile(
    r"^(B\.?S\.?|B\.?A\.?|M\.?S\.?|M\.?B\.?A\.?|Ph\.?D\.?|"
    r"Bachelor|Master|Doctor)[^\n]{0,80}",
    re.IGNORECASE | re.MULTILINE,  # MULTILINE makes ^ match start of each line
)

# Experience entry: "Company Name  |  Jan 2020 – Present"
_JOB_RE = re.compile(
    r"^(.{3,60}?)\s*[\|·—–\-]\s*"
    r"(\w+\.?\s+\d{4})\s*(?:–|-|to)\s*(\w+\.?\s+\d{4}|Present|Current)",
    re.IGNORECASE | re.MULTILINE,
)

# Skill section tokeniser
_SKILL_TOKEN_RE = re.compile(r"[,;/|•\n]+")

# Fallback tech keyword list for full-text scanning
_TECH_KEYWORDS = frozenset({
    "python", "java", "javascript", "typescript", "go", "golang", "rust",
    "c++", "c#", "ruby", "php", "swift", "kotlin", "scala", "sql",
    "postgresql", "mysql", "mongodb", "redis", "elasticsearch",
    "aws", "gcp", "azure", "docker", "kubernetes", "terraform", "ansible",
    "react", "angular", "vue", "node", "django", "flask", "fastapi",
    "spark", "kafka", "airflow", "dbt", "pytorch", "tensorflow",
    "git", "linux", "bash", "rest", "graphql", "grpc",
})


class ResumeAdapter(SourceAdapter):
    """
    Parse a single PDF resume into a flat list of RawField objects.

    Parameters
    ----------
    synonyms_path : pathlib.Path, optional
        Override the default path to data/skill_synonyms.json.
        Useful in tests (pass a tmp_path).
    """

    def __init__(self, synonyms_path: Optional[pathlib.Path] = None) -> None:
        self._synonyms_path = synonyms_path or _SYNONYMS_PATH

    def extract(self, source_path_or_url: str, record_index: int = 0) -> list[RawField]:
        """
        Extract fields from a PDF resume.

        Parameters
        ----------
        source_path_or_url : str
            Path to the .pdf file.
        record_index : int
            Monotonic index assigned by the pipeline — stamped on every
            RawField so identity resolution can reconstruct record boundaries.

        Returns
        -------
        list[RawField]
            Flat list of extracted fields.  Returns [] on any failure.
        """
        try:
            import pdfplumber  # type: ignore  # noqa: F401 — import check only
        except ImportError:
            logger.warning(
                "ResumeAdapter: pdfplumber is not installed — "
                "install it with: pip install pdfplumber>=0.10.0"
            )
            return []

        try:
            text = self._extract_text(source_path_or_url)
        except Exception as exc:
            logger.warning(
                "ResumeAdapter: failed to read PDF %r: %s — returning []",
                source_path_or_url, exc,
            )
            return []

        if not text or not text.strip():
            logger.warning(
                "ResumeAdapter: no text extracted from %r — "
                "file may be scanned/image-only. Returning []",
                source_path_or_url,
            )
            return []

        try:
            return self._parse(text, record_index)
        except Exception as exc:
            logger.warning(
                "ResumeAdapter: parsing failed for %r: %s — returning []",
                source_path_or_url, exc,
            )
            return []

    # ── private helpers ──────────────────────────────────────────────────────

    def _extract_text(self, path: str) -> str:
        """Concatenate text from all PDF pages, separated by a sentinel."""
        import pdfplumber  # type: ignore

        p = pathlib.Path(path)
        if not p.exists():
            logger.warning(
                "ResumeAdapter: file not found: %r — returning []", path
            )
            return ""

        with pdfplumber.open(str(p)) as pdf:
            pages = []
            for page in pdf.pages:
                t = page.extract_text(x_tolerance=2, y_tolerance=2)
                if t:
                    pages.append(t)
        return "\n\n--- PAGE BREAK ---\n\n".join(pages)

    def _parse(self, text: str, record_index: int) -> list[RawField]:
        """Run all extraction heuristics and return a flat RawField list."""
        fields: list[RawField] = []

        def _add(
            field_name: str,
            value: str,
            method: str = "regex_extract",
            raw_text: Optional[str] = None,
        ) -> None:
            v = str(value).strip()
            if v:
                fields.append(RawField(
                    field_name=field_name,
                    value=v,
                    source=_SOURCE,
                    method=method,
                    raw_text=raw_text or v,
                    record_index=record_index,
                ))

        lines = [ln.strip() for ln in text.splitlines()]
        non_empty = [ln for ln in lines if ln]

        # ── name ─────────────────────────────────────────────────────────────
        # Check the first five non-empty lines for a "Firstname Lastname" match.
        for ln in non_empty[:5]:
            m = _NAME_RE.match(ln)
            if m:
                _add("full_name", m.group(1))
                break

        # ── headline ─────────────────────────────────────────────────────────
        # Emit as "headline" directly — _FIELD_ROUTE maps "headline" → "headline".
        # This avoids the ambiguity of "title" which could be mistaken for
        # experience.title in some routing contexts.
        m = _HEADLINE_RE.search(text)
        if m:
            _add("headline", m.group().strip())

        # ── contact: email ────────────────────────────────────────────────────
        # Emit as "email" — _FIELD_ROUTE maps "email" → "emails" (list field).
        for m in _EMAIL_RE.finditer(text):
            _add("email", m.group())

        # ── contact: phone ────────────────────────────────────────────────────
        # Emit as "phone" — _FIELD_ROUTE maps "phone" → "phones" (list field).
        for m in _PHONE_RE.finditer(text):
            raw_ph = m.group().strip()
            if sum(c.isdigit() for c in raw_ph) >= 7:
                _add("phone", raw_ph)
                break  # one phone per resume is enough

        # ── links ─────────────────────────────────────────────────────────────
        for m in _LINKEDIN_RE.finditer(text):
            _add("links.linkedin", "https://" + m.group().rstrip("/"))

        for m in _GITHUB_RE.finditer(text):
            _add("links.github", "https://" + m.group().rstrip("/"))

        for m in _URL_RE.finditer(text):
            url = m.group().rstrip(".,)")
            if "linkedin" not in url and "github" not in url:
                _add("links.other", url)

        # ── location ─────────────────────────────────────────────────────────
        # Scan the first 10 non-empty lines where contact info typically lives.
        loc_m = _LOCATION_RE.search("\n".join(non_empty[:10]))
        if loc_m:
            _add("location.city",   loc_m.group(1).strip())
            _add("location.region", loc_m.group(2).strip())

        # ── years of experience ───────────────────────────────────────────────
        years_m = _YEARS_RE.search(text)
        if years_m:
            _add("years_experience", years_m.group(1))

        # ── experience entries ────────────────────────────────────────────────
        for m in _JOB_RE.finditer(text):
            _add("experience.company", m.group(1).strip())
            _add("experience.start",   m.group(2).strip())
            _add("experience.end",     m.group(3).strip())

        # ── education ─────────────────────────────────────────────────────────
        for m in _DEGREE_RE.finditer(text):
            _add("education.degree", m.group().strip())

        # ── skills ────────────────────────────────────────────────────────────
        # Strategy 1: parse the Skills / Technical Skills section body.
        skill_tokens: set[str] = set()
        sections = list(_SECTION_RE.finditer(text))
        for i, sec in enumerate(sections):
            header = sec.group(1).upper()
            if "SKILL" not in header and "TECH" not in header:
                continue
            start = sec.end()
            end   = sections[i + 1].start() if i + 1 < len(sections) else len(text)
            body  = text[start:end]
            for token in _SKILL_TOKEN_RE.split(body):
                t = token.strip().lower()
                if t and len(t) > 1:
                    skill_tokens.add(t)

        # Strategy 2: scan full text for known tech keywords.
        for word in re.split(r"\W+", text.lower()):
            if word in _TECH_KEYWORDS:
                skill_tokens.add(word)

        # Strategy 3: match tokens against synonym map (same logic as NotesAdapter).
        synonyms = _load_synonyms(self._synonyms_path)
        text_lower = text.lower()
        for canonical_name, variants in synonyms.items():
            all_terms = {canonical_name.lower()} | {v.lower() for v in variants}
            for term in all_terms:
                pattern = re.compile(
                    r"(?<![a-zA-Z0-9_])" + re.escape(term) + r"(?![a-zA-Z0-9_])",
                    re.IGNORECASE,
                )
                if pattern.search(text_lower):
                    skill_tokens.add(canonical_name.lower())
                    break  # one match per canonical skill is enough

        for tok in sorted(skill_tokens):  # sorted for deterministic output
            _add("skill", tok, method="keyword_match")

        return fields