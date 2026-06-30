"""
config_model.py — Pydantic models for the projection-layer runtime config.

Two models are defined here:

  ``FieldConfig``  — describes how a single output field is sourced,
                     typed, and optionally re-normalized.
  ``OutputConfig`` — the top-level config object consumed by
                     ``project.py`` and ``schema_gen.py``.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


class FieldConfig(BaseModel):
    """
    Configuration for a single output field.

    Attributes
    ----------
    path : str
        Dot-notation key that will appear in the projected output dict.
        E.g. ``"full_name"``, ``"primary_email"``, ``"location.city"``.
    type : str
        JSON Schema primitive type for this field.
        One of: ``"string"``, ``"number"``, ``"integer"``, ``"boolean"``,
        ``"array"``, ``"object"``.
    from_ : Optional[str]
        Source path inside the ``CanonicalProfile`` to read from.
        Supports dotted paths (``"location.city"``), indexed arrays
        (``"emails[0]"``), and wildcard projections (``"skills[].name"``).
        When ``None``, defaults to ``path`` itself.
    required : bool
        If ``True`` the field is listed under ``required`` in the generated
        JSON Schema and ``on_missing == 'error'`` will raise for it.
    normalize : Optional[str]
        Name of a normalizer to re-apply to the resolved value.
        Must be one of: ``"phone"``, ``"skill"``, ``"date"``, ``"country"``.
        ``None`` means no re-normalization.
    """

    path: str
    type: Optional[str] = None
    from_: Optional[str] = Field(default=None, alias="from")
    required: bool = False
    normalize: Optional[str] = None

    model_config = {
        # Allow both ``from_`` (Python) and ``"from"`` (JSON alias).
        "populate_by_name": True,
    }


class OutputConfig(BaseModel):
    """
    Top-level projection configuration.

    Attributes
    ----------
    fields : list[FieldConfig]
        Ordered list of fields to include in the projected output.
    include_confidence : bool
        When ``True``, each field in the output is wrapped in an object
        with ``value`` and ``confidence`` sub-keys.
    include_provenance : bool
        When ``True``, each field object also carries a ``provenance``
        sub-key with source/method audit data.
    on_missing : Literal['null', 'omit', 'error']
        Strategy when a field resolves to ``None`` / empty:

        * ``'null'``  — keep the key, set value to ``null``.
        * ``'omit'``  — drop the key from the output dict entirely.
        * ``'error'`` — raise ``MissingRequiredFieldError``.
    """

    fields: list[FieldConfig]
    include_confidence: bool = False
    include_provenance: bool = False
    on_missing: Literal["null", "omit", "error"] = "null"
