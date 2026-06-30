"""
src/transformer/merge/merge.py
────────────────────────────────────────────────────────────────────────────────
Candidate field merge — Phase 7.

Entry point
-----------
    merge_candidate(candidate_id, raw_fields) -> CanonicalProfile

For each canonical field the function:
  1. Groups all RawFields whose field_name maps to that canonical field.
  2. Normalises every raw value using the appropriate Phase-4 normaliser.
  3. Groups normalised values by content to find corroborating sources.
  4. Computes per-value confidence via compute_confidence().
  5. Winner-selects the highest-confidence value (scalar fields) OR unions all
     distinct values (list-type fields: emails, phones, skills).
  6. For scalar fields with 2+ differing normalised values, logs a conflict
     warning (source + losing value) via stdlib logging — the conflict is NOT
     silently dropped.
  7. Appends a ProvenanceEntry for every populated canonical field, carrying
     that field's own confidence — NOT the profile-wide overall_confidence.
  8. Sets overall_confidence = mean of all populated-field confidences.

Nested sub-models (Location, Links)
------------------------------------
Each sub-field (e.g. location.city, links.github) is treated as its own
independent scalar field going through steps 1-6 above.

Structured list fields (experience, education)
----------------------------------------------
RawFields for "experience.*" sub-fields are grouped by a structural key
(company name for experience, institution for education) derived from whichever
sub-field anchors the entry.  Within each group the same scalar winner-selection
logic applies.  This means:
  - Two sources that both report "Acme / Senior Engineer" get corroboration.
  - Two sources reporting different companies produce two separate Experience
    entries (not a conflict), because they likely represent different jobs.

skills
------
Each skill name is normalised (lowercased, stripped); the union of all distinct
skill names is taken.  Per-skill confidence is computed using the best
(highest-confidence) individual RawField for that skill name, with the full
corroborating-source count applied.

Multi-target field routing
---------------------------
Most adapter field_names map to exactly one canonical path. "current_title"
is the one exception today: CsvAdapter's "title" column and AtsJsonAdapter's
"job_ttl" key both get remapped to field_name "current_title" by their
respective adapters, and that single raw value legitimately belongs in TWO
places — the candidate's top-level `headline` AND the `title` of whichever
Experience entry it's grouped under. A plain str->str dict can't express
"one input, two outputs", so multi-target field names are routed via
_MULTI_FIELD_ROUTE, checked before the single-target _FIELD_ROUTE map.
────────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

from src.transformer.raw_field import RawField  # type: ignore
from src.transformer.schema import (  # type: ignore
    CanonicalProfile,
    Education,
    Experience,
    Links,
    Location,
    ProvenanceEntry,
    Skill,
)
from src.transformer.merge.confidence import (  # type: ignore
    compute_confidence,
    get_field_category,
)

# Phase-4 normalisers — imported defensively so that missing modules produce
# a clear ImportError rather than a silent AttributeError at call time.
from src.transformer.normalize.phone import normalize_phone  # type: ignore
from src.transformer.normalize.date import normalize_date  # type: ignore
from src.transformer.normalize.skills import normalize_skill  # type: ignore
from src.transformer.normalize.country import normalize_country  # type: ignore

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Field-name routing
# ---------------------------------------------------------------------------
# Maps every adapter field_name variant to a canonical dotted path.
# Canonical paths mirror the CanonicalProfile attribute tree:
#   scalar root fields:   "full_name", "headline", "years_experience"
#   list root fields:     "emails", "phones", "skills"
#   nested scalar fields: "location.city", "links.github", …
#   structured list:      "experience.company", "experience.title", …
#                         "education.institution", "education.degree", …

_FIELD_ROUTE: Dict[str, str] = {
    # ── identity / scalar ──────────────────────────────────────────────────
    "full_name":        "full_name",
    "name":             "full_name",
    "candidate_name":   "full_name",

    # ── identity / list ────────────────────────────────────────────────────
    "email":            "emails",
    "email_address":    "emails",
    "emails":           "emails",
    "phone":            "phones",
    "phone_number":     "phones",
    "mobile":           "phones",
    "phones":           "phones",

    # ── location sub-fields ────────────────────────────────────────────────
    "city":             "location.city",
    "location.city":    "location.city",
    "region":           "location.region",
    "state":            "location.region",
    "location.region":  "location.region",
    "country":          "location.country",
    "location.country": "location.country",
    "location":         "location.city",   # bare "location" → city as best proxy

    # ── links sub-fields ──────────────────────────────────────────────────
    "linkedin":         "links.linkedin",
    "linkedin_url":     "links.linkedin",
    "links.linkedin":   "links.linkedin",
    "github":           "links.github",
    "github_url":       "links.github",
    "links.github":     "links.github",
    "portfolio":        "links.portfolio",
    "portfolio_url":    "links.portfolio",
    "links.portfolio":  "links.portfolio",
    "website":          "links.other",
    "links.other":      "links.other",

    # ── employment / scalar ────────────────────────────────────────────────
    # NOTE: "current_title" is NOT listed here — it is multi-routed below via
    # _MULTI_FIELD_ROUTE, because it needs to populate BOTH headline AND
    # experience.title from a single raw value.
    "headline":           "headline",
    "title":              "headline",       # fallback for adapters using this generic field_name
    "current_title":      "headline",

    "years_experience":   "years_experience",
    "years_exp":          "years_experience",

    # ── skills ────────────────────────────────────────────────────────────
    "skill":  "skills",
    "skills": "skills",

    # ── experience sub-fields ─────────────────────────────────────────────
    "experience.company":  "experience.company",
    "experience.title":    "experience.title",
    "experience.start":    "experience.start",
    "experience.end":      "experience.end",
    "experience.summary":  "experience.summary",
    "company":             "experience.company",
    "current_company":     "experience.company",
    "employer":            "experience.company",
    "job_title":           "experience.title",
    "start_date":          "experience.start",
    "end_date":            "experience.end",
    "job_summary":         "experience.summary",

    # ── education sub-fields ──────────────────────────────────────────────
    "education.institution": "education.institution",
    "education.degree":      "education.degree",
    "education.field":       "education.field",
    "education.end_year":    "education.end_year",
    "institution":           "education.institution",
    "school":                "education.institution",
    "university":            "education.institution",
    "degree":                "education.degree",
    "major":                 "education.field",
    "field_of_study":        "education.field",
    "graduation_year":       "education.end_year",
    "end_year":              "education.end_year",
}

# Field names that must populate MORE THAN ONE canonical path simultaneously.
# Checked BEFORE _FIELD_ROUTE in the merge loop below.
_MULTI_FIELD_ROUTE: Dict[str, Tuple[str, ...]] = {
    "current_title": ("headline", "experience.title"),
    "job_ttl":       ("headline", "experience.title"),  # ← ADD THIS LINE
}

# Fields whose canonical path is a *list* of scalars (union semantics).
_LIST_SCALAR_FIELDS = frozenset({"emails", "phones"})

# Fields treated as structured-list entries (grouped by anchor key).
_EXPERIENCE_SUBFIELDS = frozenset({
    "experience.company", "experience.title",
    "experience.start",   "experience.end",
    "experience.summary",
})
_EDUCATION_SUBFIELDS = frozenset({
    "education.institution", "education.degree",
    "education.field",       "education.end_year",
})


# ---------------------------------------------------------------------------
# Normaliser dispatch
# ---------------------------------------------------------------------------

def _normalise_value(canonical_path: str, raw_value: Any) -> Any:
    """
    Apply the correct Phase-4 normaliser for *canonical_path* to *raw_value*.

    Returns the normalised value, or the original value stripped/lowercased if
    no specific normaliser applies (safe default for text fields).
    Falls back to the raw value unchanged if normalisation raises — the merge
    stage must be robust to bad data; a failed normalisation is not fatal.
    """
    if raw_value is None:
        return None

    try:
        if canonical_path in ("emails",):
            return str(raw_value).lower().strip() if raw_value else None

        if canonical_path in ("phones",):
            return normalize_phone(str(raw_value)) if raw_value else None

        if canonical_path in ("location.country",):
            return normalize_country(str(raw_value)) if raw_value else None

        if canonical_path in ("experience.start", "experience.end"):
            return normalize_date(str(raw_value)) if raw_value else None

        if canonical_path in ("education.end_year",):
            # Attempt integer coercion; normalise_date can also handle years.
            try:
                return int(raw_value)
            except (ValueError, TypeError):
                return normalize_date(str(raw_value)) if raw_value else None

        if canonical_path in ("years_experience",):
            try:
                return float(raw_value)
            except (ValueError, TypeError):
                return None

        if canonical_path in ("skills",):
            return normalize_skill(str(raw_value)) if raw_value else None

        # Default: stringify, lowercase, strip
        return str(raw_value).strip() if raw_value else None

    except Exception as exc:  # pragma: no cover — defensive only
        log.debug(
            "Normalisation failed for field %r value %r: %s",
            canonical_path, raw_value, exc,
        )
        return str(raw_value).strip() if raw_value else None


# ---------------------------------------------------------------------------
# Internal data structure for a single canonical field's collected assertions
# ---------------------------------------------------------------------------

class _Assertion:
    """One normalised value assertion from one RawField."""

    __slots__ = ("norm_value", "source", "method", "raw_field")

    def __init__(
        self,
        norm_value: Any,
        source: str,
        method: str,
        raw_field: RawField,
    ) -> None:
        self.norm_value = norm_value
        self.source = source
        self.method = method
        self.raw_field = raw_field


# ---------------------------------------------------------------------------
# Helper: winner-selection for a group of _Assertion objects
# ---------------------------------------------------------------------------

def _select_winner(
    canonical_path: str,
    assertions: List[_Assertion],
) -> Tuple[Optional[Any], float, str, str, List[Dict]]:
    """
    Given all assertions for one scalar canonical field, return:
        (winning_value, winning_confidence, winning_source, winning_method,
         conflict_records)

    *conflict_records* is a list of dicts (one per losing distinct value)
    to be emitted as logging.warning calls by the caller — the function itself
    does not log so that tests can inspect conflicts without capturing logs.

    Algorithm
    ---------
    1. Group assertions by their normalised value (using str() as the hash key
       so that e.g. two "acme corp" strings from different sources corroborate).
    2. For each distinct value, count the number of *distinct* sources.
    3. Compute confidence using the best (highest source_weight × method_certainty)
       single assertion for that value, but with the full corroborating count.
    4. The value with the highest confidence wins.
    5. If there are 2+ distinct values (true conflict), record the losers.
    """
    valid = [a for a in assertions if a.norm_value is not None]
    if not valid:
        return None, 0.0, "", "", []

    category = get_field_category(canonical_path)

    value_groups: Dict[str, List[_Assertion]] = defaultdict(list)
    for a in valid:
        value_groups[str(a.norm_value)].append(a)

    scored: List[Tuple[float, Any, str, str, str]] = []
    for val_key, group in value_groups.items():
        distinct_sources = len({a.source for a in group})
        best = max(
            group,
            key=lambda a: compute_confidence(a.source, a.method, category, 1),
        )
        conf = compute_confidence(
            best.source, best.method, category, distinct_sources
        )
        scored.append((conf, best.norm_value, best.source, best.method, val_key))

    scored.sort(key=lambda t: t[0], reverse=True)

    winner_conf, winner_val, winner_src, winner_method, winner_key = scored[0]

    conflicts: List[Dict] = []
    if len(scored) > 1:
        for (_, lose_val, lose_src, lose_method, _) in scored[1:]:
            conflicts.append({
                "canonical_field": canonical_path,
                "winner_value":    winner_val,
                "winner_source":   winner_src,
                "loser_value":     lose_val,
                "loser_source":    lose_src,
                "loser_method":    lose_method,
            })

    return winner_val, winner_conf, winner_src, winner_method, conflicts


# ---------------------------------------------------------------------------
# Helper: union semantics for list-scalar fields (emails, phones)
# ---------------------------------------------------------------------------

def _union_list_field(
    canonical_path: str,
    assertions: List[_Assertion],
) -> Tuple[List[Any], float, List[Tuple[str, str]]]:
    """
    Union all distinct normalised values; return (values, mean_confidence,
    [(source, method), …] for provenance).

    Confidence for a union field = mean of per-value best confidences.
    """
    category = get_field_category(canonical_path)
    seen: Dict[str, Tuple[float, str, str]] = {}  # val_key → (conf, src, method)

    for a in assertions:
        if a.norm_value is None:
            continue
        key = str(a.norm_value)
        conf = compute_confidence(a.source, a.method, category, 1)
        if key not in seen or conf > seen[key][0]:
            seen[key] = (conf, a.source, a.method)

    if not seen:
        return [], 0.0, []

    values = list(seen.keys())
    mean_conf = sum(c for c, _, _ in seen.values()) / len(seen)
    prov = [(src, meth) for _, src, meth in seen.values()]
    return values, mean_conf, prov


# ---------------------------------------------------------------------------
# Helper: skill union with per-skill confidence + source tracking
# ---------------------------------------------------------------------------

def _union_skills(
    assertions: List[_Assertion],
) -> Tuple[List[Skill], float]:
    """
    Build a list of Skill objects from all skill assertions.

    For each distinct normalised skill name:
      - sources = set of distinct sources that mentioned it
      - confidence = compute_confidence using the highest-weight source for
        that skill, with the full corroborating source count
    Returns (skills_list, mean_confidence_over_all_skills).
    """
    category = "skills"

    skill_map: Dict[str, Dict[str, _Assertion]] = defaultdict(dict)
    for a in assertions:
        if not a.norm_value:
            continue
        name = str(a.norm_value)
        existing = skill_map[name].get(a.source)
        if existing is None:
            skill_map[name][a.source] = a
        else:
            if (compute_confidence(a.source, a.method, category, 1) >
                    compute_confidence(existing.source, existing.method, category, 1)):
                skill_map[name][a.source] = a

    skills: List[Skill] = []
    for name, src_assertions in skill_map.items():
        distinct_sources = len(src_assertions)
        best_a = max(
            src_assertions.values(),
            key=lambda a: compute_confidence(a.source, a.method, category, 1),
        )
        conf = compute_confidence(
            best_a.source, best_a.method, category, distinct_sources
        )
        skills.append(Skill(
            name=name,
            confidence=conf,
            sources=sorted(src_assertions.keys()),
        ))

    if not skills:
        return [], 0.0
    mean_conf = sum(s.confidence for s in skills) / len(skills)
    return skills, mean_conf


# ---------------------------------------------------------------------------
# Helper: Experience grouping
# ---------------------------------------------------------------------------

def _build_experience(
    by_path: Dict[str, List[_Assertion]],
) -> Tuple[List[Experience], float, List[Dict]]:
    """
    Build Experience objects from all experience.* assertions.

    Grouping strategy: assertions are grouped by their normalised company name.
    This handles the common case where a candidate has multiple jobs — each
    company becomes a separate Experience entry.  If company is absent we use a
    single unnamed group ("__unknown__") so data is not lost.

    Within each group, scalar winner-selection is applied to each sub-field.
    """
    company_assertions = by_path.get("experience.company", [])

    company_keys: List[str] = []
    seen_companies: Dict[str, str] = {}

    if company_assertions:
        all_norm_companies: List[str] = []
        for a in company_assertions:
            if a.norm_value and str(a.norm_value) not in seen_companies:
                seen_companies[str(a.norm_value)] = str(a.norm_value)
                all_norm_companies.append(str(a.norm_value))
        company_keys = all_norm_companies if all_norm_companies else ["__unknown__"]
    else:
        company_keys = ["__unknown__"]

    # For sub-fields other than company, we can't split by company without
    # cross-source record pairing (not available at this stage).  We apply
    # winner-selection globally per sub-field, then fan the winner out to all
    # Experience entries.  This is the correct conservative approach — we don't
    # invent separate titles/dates for jobs we can't distinguish.
    def _win(path: str) -> Tuple[Optional[Any], float, str, str, List[Dict]]:
        return _select_winner(path, by_path.get(path, []))

    title_val,   title_conf,   title_src,   title_meth,   title_cfls   = _win("experience.title")
    start_val,   start_conf,   start_src,   start_meth,   start_cfls   = _win("experience.start")
    end_val,     end_conf,     end_src,     end_meth,     end_cfls     = _win("experience.end")
    summary_val, summary_conf, summary_src, summary_meth, summary_cfls = _win("experience.summary")

    all_conflicts = title_cfls + start_cfls + end_cfls + summary_cfls

    experiences: List[Experience] = []
    for company_norm in company_keys:
        display_company = None if company_norm == "__unknown__" else seen_companies.get(company_norm, company_norm)
        experiences.append(Experience(
            company=display_company,
            title=title_val,
            start=start_val,
            end=end_val,
            summary=summary_val,
        ))

    company_val, company_conf, _, _, comp_cfls = _win("experience.company")
    all_conflicts += comp_cfls

    sub_confs = [c for c in (company_conf, title_conf, start_conf, end_conf, summary_conf) if c > 0.0]
    mean_conf = sum(sub_confs) / len(sub_confs) if sub_confs else 0.0

    return experiences, mean_conf, all_conflicts


# ---------------------------------------------------------------------------
# Helper: Education grouping
# ---------------------------------------------------------------------------

def _build_education(
    by_path: Dict[str, List[_Assertion]],
) -> Tuple[List[Education], float, List[Dict]]:
    """
    Build Education objects from all education.* assertions.

    Grouping: by normalised institution name (same logic as Experience/company).
    """
    inst_assertions = by_path.get("education.institution", [])
    inst_keys: List[str] = []
    seen_insts: Dict[str, str] = {}

    if inst_assertions:
        for a in inst_assertions:
            if a.norm_value and str(a.norm_value) not in seen_insts:
                seen_insts[str(a.norm_value)] = str(a.norm_value)
                inst_keys.append(str(a.norm_value))
    if not inst_keys:
        inst_keys = ["__unknown__"]

    def _win(path: str) -> Tuple[Optional[Any], float, str, str, List[Dict]]:
        return _select_winner(path, by_path.get(path, []))

    deg_val, deg_conf, deg_src, deg_meth, deg_cfls           = _win("education.degree")
    field_val, field_conf, field_src, field_meth, field_cfls = _win("education.field")
    year_val, year_conf, year_src, year_meth, year_cfls      = _win("education.end_year")
    inst_val, inst_conf, inst_src, inst_meth, inst_cfls      = _win("education.institution")

    all_conflicts = deg_cfls + field_cfls + year_cfls + inst_cfls

    educations: List[Education] = []
    for inst_norm in inst_keys:
        display_inst = None if inst_norm == "__unknown__" else seen_insts.get(inst_norm, inst_norm)
        end_year_int: Optional[int] = None
        if year_val is not None:
            try:
                end_year_int = int(year_val)
            except (ValueError, TypeError):
                end_year_int = None
        educations.append(Education(
            institution=display_inst,
            degree=deg_val,
            field=field_val,
            end_year=end_year_int,
        ))

    sub_confs = [c for c in (inst_conf, deg_conf, field_conf, year_conf) if c > 0.0]
    mean_conf = sum(sub_confs) / len(sub_confs) if sub_confs else 0.0
    return educations, mean_conf, all_conflicts


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def merge_candidate(
    candidate_id: str,
    raw_fields: List[RawField],
) -> CanonicalProfile:
    """
    Merge all RawFields for one candidate into a single CanonicalProfile.

    Parameters
    ----------
    candidate_id : str
        The identity-resolution ID for this candidate (from Phase 6).
    raw_fields : list[RawField]
        Every RawField attributed to this candidate, across all sources.

    Returns
    -------
    CanonicalProfile
        Fully populated (where data exists) canonical profile.  Conflicts are
        logged as WARNING-level messages — they are not silently dropped and do
        not raise exceptions.
    """
    profile = CanonicalProfile.empty(candidate_id)

    # ── 1. Route every RawField to its canonical path(s) ─────────────────────
    # Most field_names route to exactly one canonical path via _FIELD_ROUTE.
    # A small number (currently just "current_title") route to MULTIPLE paths
    # via _MULTI_FIELD_ROUTE, checked first.
    by_path: Dict[str, List[_Assertion]] = defaultdict(list)

    for rf in raw_fields:
        fn_lower = rf.field_name.lower()

        multi = _MULTI_FIELD_ROUTE.get(fn_lower)
        if multi is not None:
            canonical_paths: Tuple[str, ...] = multi
        else:
            single = _FIELD_ROUTE.get(fn_lower)
            canonical_paths = (single,) if single is not None else ()

        if not canonical_paths:
            log.debug(
                "candidate=%s: unroutable field_name=%r — skipped",
                candidate_id, rf.field_name,
            )
            continue

        for canon in canonical_paths:
            norm_val = _normalise_value(canon, rf.value)
            by_path[canon].append(
                _Assertion(
                    norm_value=norm_val,
                    source=rf.source,
                    method=rf.method,
                    raw_field=rf,
                )
            )

    # ── 2. Accumulate per-field confidences for overall_confidence ───────────
    field_confidences: List[float] = []
    all_conflicts: List[Dict] = []

    def _record_conf(conf: float) -> None:
        if conf > 0.0:
            field_confidences.append(conf)

    def _record_conflicts(cfls: List[Dict]) -> None:
        all_conflicts.extend(cfls)

    # ── 3. Scalar root fields ────────────────────────────────────────────────

    # full_name
    if by_path.get("full_name"):
        val, conf, src, meth, cfls = _select_winner("full_name", by_path["full_name"])
        if val is not None:
            profile.full_name = val
            profile.provenance.append(
                ProvenanceEntry(field="full_name", source=src, method=meth, confidence=conf)
            )
            _record_conf(conf)
            _record_conflicts(cfls)

    # headline
    if by_path.get("headline"):
        val, conf, src, meth, cfls = _select_winner("headline", by_path["headline"])
        if val is not None:
            profile.headline = val
            profile.provenance.append(
                ProvenanceEntry(field="headline", source=src, method=meth, confidence=conf)
            )
            _record_conf(conf)
            _record_conflicts(cfls)

    # years_experience
    if by_path.get("years_experience"):
        val, conf, src, meth, cfls = _select_winner("years_experience", by_path["years_experience"])
        if val is not None:
            profile.years_experience = val
            profile.provenance.append(
                ProvenanceEntry(field="years_experience", source=src, method=meth, confidence=conf)
            )
            _record_conf(conf)
            _record_conflicts(cfls)

    # ── 4. List-scalar root fields (union semantics) ─────────────────────────

    # emails
    if by_path.get("emails"):
        values, mean_conf, prov_pairs = _union_list_field("emails", by_path["emails"])
        profile.emails = values
        for src, meth in prov_pairs:
            profile.provenance.append(
                ProvenanceEntry(field="emails", source=src, method=meth, confidence=mean_conf)
            )
        _record_conf(mean_conf)

    # phones
    if by_path.get("phones"):
        values, mean_conf, prov_pairs = _union_list_field("phones", by_path["phones"])
        profile.phones = values
        for src, meth in prov_pairs:
            profile.provenance.append(
                ProvenanceEntry(field="phones", source=src, method=meth, confidence=mean_conf)
            )
        _record_conf(mean_conf)

    # ── 5. Location sub-fields ───────────────────────────────────────────────
    loc_conf_values: List[float] = []

    for sub in ("location.city", "location.region", "location.country"):
        if not by_path.get(sub):
            continue
        val, conf, src, meth, cfls = _select_winner(sub, by_path[sub])
        if val is None:
            continue
        attr = sub.split(".")[1]
        setattr(profile.location, attr, val)
        profile.provenance.append(
            ProvenanceEntry(field=sub, source=src, method=meth, confidence=conf)
        )
        loc_conf_values.append(conf)
        _record_conflicts(cfls)

    if loc_conf_values:
        _record_conf(sum(loc_conf_values) / len(loc_conf_values))

    # ── 6. Links sub-fields ───────────────────────────────────────────────────
    links_conf_values: List[float] = []

    for sub in ("links.linkedin", "links.github", "links.portfolio"):
        if not by_path.get(sub):
            continue
        val, conf, src, meth, cfls = _select_winner(sub, by_path[sub])
        if val is None:
            continue
        attr = sub.split(".")[1]
        setattr(profile.links, attr, val)
        profile.provenance.append(
            ProvenanceEntry(field=sub, source=src, method=meth, confidence=conf)
        )
        links_conf_values.append(conf)
        _record_conflicts(cfls)

    # links.other — union of all "other" URL values
    if by_path.get("links.other"):
        values, mean_conf, prov_pairs = _union_list_field("links.other", by_path["links.other"])
        profile.links.other = values
        for src, meth in prov_pairs:
            profile.provenance.append(
                ProvenanceEntry(field="links.other", source=src, method=meth, confidence=mean_conf)
            )
        links_conf_values.append(mean_conf)

    if links_conf_values:
        _record_conf(sum(links_conf_values) / len(links_conf_values))

    # ── 7. Skills (union by normalised name) ──────────────────────────────────
    skill_assertions = by_path.get("skills", [])
    if skill_assertions:
        skills, mean_conf = _union_skills(skill_assertions)
        if skills:
            profile.skills = skills
            skill_sources_seen: set = set()
            for a in skill_assertions:
                if a.norm_value and a.source not in skill_sources_seen:
                    profile.provenance.append(
                        ProvenanceEntry(field="skills", source=a.source, method=a.method, confidence=mean_conf)
                    )
                    skill_sources_seen.add(a.source)
            _record_conf(mean_conf)

    # ── 8. Experience (grouped by company) ───────────────────────────────────
    exp_paths = {p: by_path[p] for p in _EXPERIENCE_SUBFIELDS if by_path.get(p)}
    if exp_paths:
        experiences, exp_conf, exp_cfls = _build_experience(exp_paths)
        profile.experience = experiences
        if experiences:
            exp_sources: set = set()
            for assertions in exp_paths.values():
                for a in assertions:
                    if a.source not in exp_sources:
                        profile.provenance.append(
                            ProvenanceEntry(field="experience", source=a.source, method=a.method, confidence=exp_conf)
                        )
                        exp_sources.add(a.source)
        _record_conf(exp_conf)
        _record_conflicts(exp_cfls)

    # ── 9. Education (grouped by institution) ─────────────────────────────────
    edu_paths = {p: by_path[p] for p in _EDUCATION_SUBFIELDS if by_path.get(p)}
    if edu_paths:
        educations, edu_conf, edu_cfls = _build_education(edu_paths)
        profile.education = educations
        if educations:
            edu_sources: set = set()
            for assertions in edu_paths.values():
                for a in assertions:
                    if a.source not in edu_sources:
                        profile.provenance.append(
                            ProvenanceEntry(field="education", source=a.source, method=a.method, confidence=edu_conf)
                        )
                        edu_sources.add(a.source)
        _record_conf(edu_conf)
        _record_conflicts(edu_cfls)

    # ── 10. Emit conflict warnings ────────────────────────────────────────────
    for conflict in all_conflicts:
        log.warning(
            "candidate=%s | FIELD CONFLICT on %r: "
            "winner=%r (source=%r) beat loser=%r (source=%r method=%r)",
            candidate_id,
            conflict["canonical_field"],
            conflict["winner_value"],
            conflict["winner_source"],
            conflict["loser_value"],
            conflict["loser_source"],
            conflict["loser_method"],
        )

    # ── 11. overall_confidence ─────────────────────────────────────
    if field_confidences:
        mean_field_confidence = sum(field_confidences) / len(field_confidences)

        # corroboration_score: fraction of populated scalar fields that
        # were confirmed by 2+ distinct sources
        scalar_fields = [
            "full_name", "headline", "years_experience",
            "location.city", "location.region", "location.country",
            "links.linkedin", "links.github", "links.portfolio",
        ]
        populated_scalars = [
            f for f in scalar_fields if by_path.get(f)
        ]
        corroborated = [
            f for f in populated_scalars
            if len({a.source for a in by_path[f]}) >= 2
        ]
        corroboration_score = (
            len(corroborated) / len(populated_scalars)
            if populated_scalars else 0.0
        )

        # source_coverage_score: fraction of known sources that
        # contributed at least one field
        possible_sources = {"csv", "ats_json", "github", "notes"}
        seen_sources = {a.source for assertions in by_path.values() for a in assertions}
        source_coverage_score = len(seen_sources & possible_sources) / len(possible_sources)

        profile.overall_confidence = round(
            0.5 * mean_field_confidence +
            0.3 * corroboration_score +
            0.2 * source_coverage_score,
            6,
        )
        return profile