"""
project.py — runtime projection of a CanonicalProfile into a flat output dict.

Public API
----------
project(profile, config) -> dict
    Applies an ``OutputConfig`` to a ``CanonicalProfile`` and returns a
    validated output dictionary.

Internal helpers (not exported)
--------------------------------
resolve_path(profile_dict, path) -> Any
    Safe path-resolver — no eval(), no exec(), no dynamic code execution.
_apply_normalizer(value, normalizer_name) -> Any
    Dispatches to the correct Phase-4 normalizer by name.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

import jsonschema

from ..normalize import normalize_country, normalize_date, normalize_phone, normalize_skill
from ..schema import CanonicalProfile
from .config_model import FieldConfig, OutputConfig
from .schema_gen import generate_json_schema

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Custom exception
# ---------------------------------------------------------------------------


class MissingRequiredFieldError(Exception):
    """Raised when a required field resolves to None/empty and on_missing='error'."""

    def __init__(self, field_path: str) -> None:
        super().__init__(
            f"Required field '{field_path}' resolved to None/empty but "
            "on_missing is set to 'error'. Populate the field or change "
            "the on_missing policy."
        )
        self.field_path = field_path


# ---------------------------------------------------------------------------
# Normalizer dispatch
# ---------------------------------------------------------------------------

_NORMALIZER_MAP = {
    "phone": normalize_phone,
    "skill": normalize_skill,
    "date": normalize_date,
    "country": normalize_country,
}


def _apply_normalizer(value: Any, normalizer_name: str) -> Any:
    """
    Re-apply a named Phase-4 normalizer to *value*.

    If *value* is a list, the normalizer is mapped over each element.
    Unknown normalizer names are logged as warnings and the raw value
    is returned unchanged.
    """
    fn = _NORMALIZER_MAP.get(normalizer_name)
    if fn is None:
        logger.warning(
            "Unknown normalizer '%s' — value returned unchanged.", normalizer_name
        )
        return value

    if isinstance(value, list):
        return [fn(item) for item in value]

    return fn(value)


# ---------------------------------------------------------------------------
# Path resolver — explicit parsing, never eval()
# ---------------------------------------------------------------------------

class _Key:
    """Access a dict by key."""
    __slots__ = ("name",)
    def __init__(self, name: str) -> None: self.name = name

class _Index:
    """Access a list by integer index."""
    __slots__ = ("idx",)
    def __init__(self, idx: int) -> None: self.idx = idx

class _Wildcard:
    """Collect all elements from a list (returns a list)."""


_Step = _Key | _Index | _Wildcard

_SEGMENT_RE = re.compile(r"(\w+)(\[(\d+|\*|)\])?")


def _parse_path(path: str) -> list[_Step]:
    """
    Convert a dotted/bracketed path string into an ordered list of steps.

    Examples
    --------
    "full_name"          → [_Key("full_name")]
    "location.city"      → [_Key("location"), _Key("city")]
    "emails[0]"          → [_Key("emails"), _Index(0)]
    "skills[].name"      → [_Key("skills"), _Wildcard(), _Key("name")]
    """
    steps: list[_Step] = []
    for segment in path.split("."):
        if not segment:
            continue
        m = _SEGMENT_RE.fullmatch(segment)
        if not m:
            raise ValueError(f"Unrecognisable path segment: '{segment}' in '{path}'")

        steps.append(_Key(m.group(1)))

        bracket = m.group(2)
        if bracket is not None:
            inner = m.group(3)
            if inner == "" or inner == "*":
                steps.append(_Wildcard())
            else:
                steps.append(_Index(int(inner)))

    return steps


def resolve_path(profile_dict: dict, path: str) -> Any:
    """
    Walk *profile_dict* following *path* and return the resolved value.

    Supports
    --------
    * Dotted nested keys: ``"location.city"``
    * Integer indexing:   ``"emails[0]"``
    * Wildcard collect:   ``"skills[].name"``  →  list of name values

    Returns ``None`` if any key or index is missing (never raises for
    missing data — only raises for malformed path strings).

    Security
    --------
    Uses explicit regex parsing — no ``eval()``, no ``exec()``, no
    ``getattr`` on arbitrary objects.  Only plain ``dict`` key lookups
    and ``list`` index accesses are performed.
    """
    steps = _parse_path(path)

    current: Any = profile_dict
    i = 0
    while i < len(steps):
        step = steps[i]

        if current is None:
            return None

        if isinstance(step, _Key):
            if not isinstance(current, dict):
                return None
            current = current.get(step.name)

        elif isinstance(step, _Index):
            if not isinstance(current, list):
                return None
            try:
                current = current[step.idx]
            except IndexError:
                return None

        elif isinstance(step, _Wildcard):
            if not isinstance(current, list):
                return None
            remaining_steps = steps[i + 1:]
            if not remaining_steps:
                return current

            collected: list[Any] = []
            for element in current:
                sub: Any = element
                for rs in remaining_steps:
                    if sub is None:
                        break
                    if isinstance(rs, _Key):
                        sub = sub.get(rs.name) if isinstance(sub, dict) else None
                    elif isinstance(rs, _Index):
                        sub = sub[rs.idx] if isinstance(sub, list) and rs.idx < len(sub) else None
                    elif isinstance(rs, _Wildcard):
                        sub = sub if isinstance(sub, list) else None
                collected.append(sub)
            return collected

        i += 1

    return current


# ---------------------------------------------------------------------------
# Provenance/confidence helpers
# ---------------------------------------------------------------------------


def _field_base(field_path: str) -> str:
    """
    Reduce a (possibly indexed/dotted) projection source path down to the
    base canonical field name used in ProvenanceEntry.field.

    Examples: "emails[0]" -> "emails", "location.city" -> "location",
    "skills[].name" -> "skills".
    """
    return field_path.split(".")[0].split("[")[0]


def _confidence_for_field(profile: CanonicalProfile, field_path: str) -> Optional[float]:
    """
    Return the per-field confidence for *field_path*.

    For skill fields, average per-skill confidence (Skill.confidence already
    tracks this precisely). For everything else, average the confidence of
    every ProvenanceEntry whose field matches field_path's base — these were
    populated by merge.py with the field's OWN confidence, not a single
    profile-wide number. Falls back to overall_confidence only if no
    matching provenance entry exists at all (e.g. the field was never
    populated by any source).
    """
    if field_path.startswith("skills"):
        if profile.skills:
            return sum(s.confidence for s in profile.skills) / len(profile.skills)
        return None

    base = _field_base(field_path)
    matches = [
        p.confidence for p in profile.provenance
        if p.field == base or p.field.startswith(base)
    ]
    if matches:
        return sum(matches) / len(matches)

    return profile.overall_confidence if profile.overall_confidence > 0 else None


def _provenance_for_field(profile: CanonicalProfile, field_path: str) -> list[dict]:
    """Return provenance entries that relate to *field_path* (best-effort)."""
    base = _field_base(field_path)
    return [
        {
            "field": p.field,
            "source": p.source,
            "method": p.method,
            "confidence": p.confidence,
        }
        for p in profile.provenance
        if p.field == base or p.field.startswith(base)
    ]


# ---------------------------------------------------------------------------
# Main public function
# ---------------------------------------------------------------------------


def project(profile: CanonicalProfile, config: OutputConfig) -> dict:
    """
    Project *profile* into a flat output dictionary governed by *config*.

    Steps
    -----
    1. For each ``FieldConfig``:
       a. Resolve the ``from_`` (or ``path``) against the canonical profile.
       b. Re-apply the named normalizer if ``normalize`` is set.
       c. Handle missing values: a field marked ``required=True`` always
          raises when missing, regardless of ``on_missing`` — 'omit' must
          not be allowed to silently drop data the config says is required.
          Non-required missing fields follow ``on_missing`` as configured.
       d. Optionally wrap in a confidence/provenance envelope.
    2. Validate the assembled dict against the JSON Schema generated from
       the same ``OutputConfig`` via ``generate_json_schema()``.
    3. Return the validated dict.

    Raises
    ------
    MissingRequiredFieldError
        When a required field resolves to None/empty (regardless of
        on_missing), or when on_missing == 'error' for any missing field.
    jsonschema.ValidationError
        When the projected output fails JSON Schema validation.
    """
    profile_dict: dict = profile.model_dump()
    output: dict = {}

    for field_cfg in config.fields:
        source_path = field_cfg.from_ if field_cfg.from_ is not None else field_cfg.path

        # 1a. Resolve value from canonical profile.
        value = resolve_path(profile_dict, source_path)

        # 1b. Re-normalize if requested.
        if field_cfg.normalize and value is not None:
            value = _apply_normalizer(value, field_cfg.normalize)

        # Treat empty lists/strings as "missing" for policy purposes.
        is_missing = value is None or value == [] or value == ""

        # 1c. Handle missing-value policy.
        if is_missing:
            if field_cfg.required:
                # Required always wins — 'omit' must not silently drop a
                # field the config explicitly marked as required. Without
                # this check, a required+omit field would vanish from the
                # output dict here, then fail jsonschema validation later
                # with a confusing error instead of this clear one.
                raise MissingRequiredFieldError(field_cfg.path)
            if config.on_missing == "omit":
                continue  # skip key entirely
            elif config.on_missing == "error":
                raise MissingRequiredFieldError(field_cfg.path)
            else:  # 'null'
                value = None

        # 1d. Build output value — plain or wrapped.
        if config.include_confidence or config.include_provenance:
            envelope: dict = {"value": value}
            if config.include_confidence:
                envelope["confidence"] = _confidence_for_field(profile, source_path)
            if config.include_provenance:
                envelope["provenance"] = _provenance_for_field(profile, source_path)
            output[field_cfg.path] = envelope
        else:
            output[field_cfg.path] = value

    # 2. Validate against the schema generated from the *same* config.
    schema = generate_json_schema(config)
    try:
        jsonschema.validate(instance=output, schema=schema)
    except jsonschema.ValidationError as exc:
        raise jsonschema.ValidationError(
            f"Projected output failed schema validation: {exc.message}"
        ) from exc

    return output