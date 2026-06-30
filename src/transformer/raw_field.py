from __future__ import annotations
"""
raw_field.py — envelope that wraps a single extracted value before normalization.

Every adapter yields RawField instances; downstream stages consume them.
"""
from typing import Any, Literal, Optional
from pydantic import BaseModel, Field

# Allowed source identifiers — extend as new adapters are added.
SourceName = Literal["csv", "ats_json", "github", "notes"]

# Allowed extraction methods.
MethodName = Literal[
    "direct_copy",
    "field_remap",
    "regex_extract",
    "api_fetch",
    "fuzzy_match",
]


class RawField(BaseModel):
    """
    A single raw field extracted from one source before any normalization.

    Attributes
    ----------
    field_name : str
        Logical name of the target field (e.g. ``"email"``, ``"phone"``).
    value : Any
        The extracted value in whatever form the adapter produced it.
    source : str
        Which adapter produced this field (e.g. ``"csv"``, ``"ats_json"``).
        Typed as ``str`` rather than the ``SourceName`` literal so that
        callers using ad-hoc adapter names are not rejected at parse time.
    method : str
        How the value was obtained (e.g. ``"direct_copy"``, ``"regex_extract"``).
        Also typed as ``str`` for the same flexibility reason.
    raw_text : Optional[str]
        The original, unprocessed text from the source document.
        Kept for debugging and audit trails; ``None`` when the adapter
        has nothing useful to store (e.g. a structured JSON value).
    record_index : int
        Index of the originating row/record within its source — set by
        the adapter itself, used by identity resolution to reconstruct
        per-record boundaries.
    candidate_id : Optional[str]
        Resolved candidate identity. ``None`` until identity resolution /
        enrichment attachment runs and assigns it.
    """

    field_name: str
    value: Any
    source: str = Field(
        ...,
        examples=["csv", "ats_json", "github", "notes"],
        description="Identifier of the adapter / source that produced this field.",
    )
    method: str = Field(
        ...,
        examples=["direct_copy", "field_remap", "regex_extract", "api_fetch", "fuzzy_match"],
        description="Technique used to extract the value from the source.",
    )
    raw_text: Optional[str] = Field(
        default=None,
        description="Original unprocessed text retained for debugging.",
    )
    record_index: int = Field(
        default=0,
        description="Index of the originating row/record within its source.",
    )
    candidate_id: Optional[str] = Field(
        default=None,
        description=(
            "Resolved candidate identity, assigned during identity resolution "
            "or enrichment attachment. None until that stage runs."
        ),
    )

    model_config = {
        # Allow arbitrary types so `value` can hold any Python object.
        "arbitrary_types_allowed": True,
    }