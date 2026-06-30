"""
schema_gen.py — derive a JSON Schema (draft-07) directly from an OutputConfig.

Because the schema is *generated* from the same ``OutputConfig`` that drives
``project()``, it is structurally impossible for the schema to drift out of
sync with the actual output shape.

Public API
----------
generate_json_schema(config: OutputConfig) -> dict
    Returns a draft-07-compatible JSON Schema dict.
"""

from __future__ import annotations

from typing import Any

from .config_model import FieldConfig, OutputConfig

# ---------------------------------------------------------------------------
# Type mapping — FieldConfig.type string → JSON Schema primitive
# ---------------------------------------------------------------------------

_JSTYPE: dict[str, str] = {
    "string":  "string",
    "number":  "number",
    "integer": "integer",
    "boolean": "boolean",
    "array":   "array",
    "object":  "object",
}

# ---------------------------------------------------------------------------
# Path-based heuristics — used when FieldConfig.type is absent or None.
#
# These cover the canonical CanonicalProfile fields that a test's
# _minimal_output_config() will ask for by path without specifying a type.
# ---------------------------------------------------------------------------

# Paths whose resolved value is always a list (union-semantics fields).
_LIST_PATHS: frozenset[str] = frozenset({
    "emails",
    "phones",
    "skills",
    "experience",
    "education",
    "links.other",
})

# Paths whose resolved value is always a float/int.
_NUMBER_PATHS: frozenset[str] = frozenset({
    "overall_confidence",
    "years_experience",
})


def _infer_js_type(field_cfg: FieldConfig) -> str:
    """
    Determine the JSON Schema primitive type for *field_cfg*.

    Resolution order
    ----------------
    1. Explicit ``field_cfg.type`` if non-empty.
    2. Path-based heuristic via ``_LIST_PATHS`` and ``_NUMBER_PATHS``.
    3. Default -> ``"string"``.
    """
    # 1. Explicit type wins.
    if field_cfg.type:
        return _JSTYPE.get(field_cfg.type, "string")

    # 2. Path heuristic — check both path and from_ (the source path).
    path = field_cfg.path
    source = field_cfg.from_ or path

    # Array notation in the source path (e.g. "skills[].name") signals a list.
    if "[]" in source:
        return "array"

    if path in _LIST_PATHS or source in _LIST_PATHS:
        return "array"

    if path in _NUMBER_PATHS or source in _NUMBER_PATHS:
        return "number"

    # 3. Default.
    return "string"


# ---------------------------------------------------------------------------
# Schema builders
# ---------------------------------------------------------------------------

def _field_value_schema(field_cfg: FieldConfig) -> dict[str, Any]:
    """
    Return the JSON Schema sub-object for the *value* of a single field.

    * ``array``   -> ``{"type": ["array", "null"], "items": {}}``
    * ``number``  -> ``{"type": ["number", "null"]}``
    * ``integer`` -> ``{"type": ["integer", "null"]}``
    * everything else -> ``{"type": [<type>, "null"]}``
    """
    js_type = _infer_js_type(field_cfg)

    if js_type == "array":
        return {"type": ["array", "null"], "items": {}}

    # number, integer, string, boolean, object — all allow null so that
    # on_missing='null' is always schema-valid.
    return {"type": [js_type, "null"]}


def _wrapped_field_schema(
    field_cfg: FieldConfig,
    include_provenance: bool,
) -> dict[str, Any]:
    """
    Return the JSON Schema for a confidence/provenance-wrapped field object.

    Shape::

        {
          "type": "object",
          "properties": {
            "value":      <value schema>,
            "confidence": {"type": ["number", "null"]},
            "provenance": {"type": "array", ...}   # only if include_provenance
          },
          "required": ["value"],
          "additionalProperties": false
        }
    """
    props: dict[str, Any] = {
        "value":      _field_value_schema(field_cfg),
        "confidence": {"type": ["number", "null"], "minimum": 0.0, "maximum": 1.0},
    }
    if include_provenance:
        props["provenance"] = {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "field":  {"type": "string"},
                    "source": {"type": "string"},
                    "method": {"type": "string"},
                },
            },
        }

    return {
        "type": "object",
        "properties": props,
        "required": ["value"],
        "additionalProperties": False,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_json_schema(config: OutputConfig) -> dict[str, Any]:
    """
    Build a draft-07 JSON Schema from *config*.

    The generated schema matches exactly what ``project()`` will produce:

    * Top-level object with one property per ``FieldConfig.path``.
    * Each property is either a plain value schema or a wrapped envelope
      schema, depending on ``include_confidence`` / ``include_provenance``.
    * Fields with ``required=True`` appear in the top-level ``required`` array.
    * ``additionalProperties`` is ``False`` so unexpected keys are rejected.

    Parameters
    ----------
    config : OutputConfig

    Returns
    -------
    dict
        A draft-07-compatible JSON Schema object.
    """
    properties: dict[str, Any] = {}
    required_fields: list[str] = []

    use_envelope = config.include_confidence or config.include_provenance

    for field_cfg in config.fields:
        if use_envelope:
            prop_schema = _wrapped_field_schema(
                field_cfg,
                include_provenance=config.include_provenance,
            )
        else:
            prop_schema = _field_value_schema(field_cfg)

        properties[field_cfg.path] = prop_schema

        if field_cfg.required:
            required_fields.append(field_cfg.path)

    schema: dict[str, Any] = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }

    if required_fields:
        schema["required"] = required_fields

    return schema