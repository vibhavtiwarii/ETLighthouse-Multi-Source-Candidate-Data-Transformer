"""
schema.py — canonical data models for the Eightfold transformer pipeline.

All models use Pydantic v2.  Every field except ``candidate_id`` is optional
or defaults to an empty collection so that partially-populated profiles are
always valid throughout the pipeline.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Sub-models (nested, not flattened)
# ---------------------------------------------------------------------------


class Location(BaseModel):
    """Geographic location of the candidate."""

    city: Optional[str] = None
    region: Optional[str] = None
    country: Optional[str] = Field(
        default=None,
        description="ISO-3166 alpha-2 country code, e.g. 'US', 'DE', 'IN'.",
    )


class Links(BaseModel):
    """External profile and portfolio links."""

    linkedin: Optional[str] = None
    github: Optional[str] = None
    portfolio: Optional[str] = None
    other: list[str] = Field(default_factory=list)


class Skill(BaseModel):
    """A single skill with its aggregated confidence score and originating sources."""

    name: str
    confidence: float = Field(ge=0.0, le=1.0)
    sources: list[str] = Field(default_factory=list)


class Experience(BaseModel):
    """One entry in the candidate's work-history."""

    company: Optional[str] = None
    title: Optional[str] = None
    start: Optional[str] = Field(
        default=None,
        description="Start date in YYYY-MM format.",
    )
    end: Optional[str] = Field(
        default=None,
        description="End date in YYYY-MM format, or the string 'present'.",
    )
    summary: Optional[str] = None


class Education(BaseModel):
    """One entry in the candidate's education history."""

    institution: Optional[str] = None
    degree: Optional[str] = None
    field: Optional[str] = None
    end_year: Optional[int] = None



class ProvenanceEntry(BaseModel):
    """
    Audit record linking a single field value back to its origin.

    ``field``      — logical field name (e.g. ``"email"``)
    ``source``     — adapter that produced the value (e.g. ``"csv"``)
    ``method``     — extraction technique (e.g. ``"direct_copy"``)
    ``confidence`` — the per-field confidence merge.py computed for this
                      value at merge time. Distinct fields legitimately have
                      different confidence — this is what lets project.py
                      report real per-field confidence instead of falling
                      back to the single profile-wide overall_confidence.
    """

    field: str
    source: str
    method: str
    confidence: float = 0.0


# ---------------------------------------------------------------------------
# Root model
# ---------------------------------------------------------------------------


class CanonicalProfile(BaseModel):
    """
    The unified, normalized representation of a single candidate.

    Only ``candidate_id`` is required; every other field is optional or
    defaults to an empty collection so that profiles can be built
    incrementally and merged across sources.
    """

    candidate_id: str

    full_name: Optional[str] = None
    emails: list[str] = Field(default_factory=list)
    phones: list[str] = Field(default_factory=list)

    # Nested sub-models — always present as objects, never None.
    location: Location = Field(default_factory=Location)
    links: Links = Field(default_factory=Links)

    headline: Optional[str] = None
    years_experience: Optional[float] = None

    skills: list[Skill] = Field(default_factory=list)
    experience: list[Experience] = Field(default_factory=list)
    education: list[Education] = Field(default_factory=list)
    provenance: list[ProvenanceEntry] = Field(default_factory=list)

    overall_confidence: float = Field(default=0.0, ge=0.0, le=1.0)

    # ------------------------------------------------------------------
    # Factory helpers
    # ------------------------------------------------------------------

    @classmethod
    def empty(cls, candidate_id: str) -> "CanonicalProfile":
        """
        Return a fully valid ``CanonicalProfile`` with all defaults applied.

        No logic is performed — this is purely a structural convenience so
        callers always have a well-typed starting point before any fields
        are populated.

        Parameters
        ----------
        candidate_id:
            The unique identifier to assign to the new profile.
        """
        return cls(candidate_id=candidate_id)
