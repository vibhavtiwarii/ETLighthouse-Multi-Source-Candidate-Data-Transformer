from __future__ import annotations
"""Defines the base adapter interface.""" 
"""
adapters/base.py — abstract base class for all source adapters.

CONTRACT
--------
Every concrete subclass MUST wrap its entire ``extract()`` body in a
``try / except`` block.  On **any** failure the method must:

1. Log a ``WARNING`` via stdlib ``logging`` (never ``print``).
2. Return an empty list ``[]``.
3. NEVER re-raise or let any exception propagate to the caller.

This guarantee lets the pipeline call adapters in sequence without
defensive wrapping at the call site — the safety net lives here.
"""



import logging
from abc import ABC, abstractmethod

from src.transformer.raw_field import RawField

logger = logging.getLogger(__name__)


class SourceAdapter(ABC):
    """
    Abstract base for every data-source adapter in the pipeline.

    Sub-classes must implement :meth:`extract` and must uphold the
    no-raise contract described in the module docstring.  The base
    class intentionally provides no implementation so that the
    ``@abstractmethod`` decorator forces compliance.

    Example skeleton every concrete adapter must follow::

        def extract(self, source_path_or_url: str) -> list[RawField]:
            try:
                # ... all real work happens here ...
                return fields
            except Exception as exc:
                logger.warning(
                    "%s.extract() failed for %r: %s",
                    self.__class__.__name__,
                    source_path_or_url,
                    exc,
                )
                return []
    """

    @abstractmethod
    def extract(self, source_path_or_url: str) -> list[RawField]:
        """
        Extract :class:`~src.transformer.raw_field.RawField` instances
        from *source_path_or_url*.

        Parameters
        ----------
        source_path_or_url:
            A filesystem path or remote URL that identifies the data
            source to read.  The concrete adapter decides how to
            interpret this string.

        Returns
        -------
        list[RawField]
            Zero or more raw fields.  An empty list is returned on any
            error — the method must never raise.
        """
