"""
cache/extraction_cache.py — lightweight file-backed cache for raw API responses.

Each cache entry is stored as a JSON file named after the SHA-256 hash of the
lookup key (typically a GitHub API URL).  This keeps filenames safe for every
filesystem regardless of what characters appear in the original URL.

Design goals
------------
* Zero external dependencies (stdlib only: ``hashlib``, ``json``, ``pathlib``).
* ``get()`` never raises — returns ``None`` on any failure (missing file,
  corrupted JSON, permission error) so callers can treat every miss uniformly.
* ``set()`` logs a warning on write failure instead of propagating exceptions,
  keeping the adapter's "never raise" contract intact.
* The cache directory is created on first use, not at import time.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class ExtractionCache:
    """
    SHA-256-keyed, JSON-backed file cache for raw API response objects.

    Parameters
    ----------
    cache_dir:
        Path to the directory where ``.json`` cache files are stored.
        Created automatically (including parents) if it does not exist.
        Defaults to ``'.cache'`` relative to the current working directory.

    Usage
    -----
    ::

        cache = ExtractionCache(".cache")

        data = cache.get("https://api.github.com/users/octocat")
        if data is None:                      # cache miss
            data = requests.get(...).json()
            cache.set("https://api.github.com/users/octocat", data)
    """

    def __init__(self, cache_dir: str = ".cache") -> None:
        self._cache_dir = Path(cache_dir)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_dir(self) -> None:
        """Create the cache directory (and any parents) if absent."""
        self._cache_dir.mkdir(parents=True, exist_ok=True)

    def _key_to_path(self, key: str) -> Path:
        """
        Derive the cache file path for *key*.

        The key is hashed with SHA-256 so the filename is always a safe,
        fixed-length hex string regardless of the original key's content.
        """
        digest = hashlib.sha256(key.encode()).hexdigest()
        return self._cache_dir / f"{digest}.json"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(self, key: str) -> Optional[dict]:
        """
        Return the cached value for *key*, or ``None`` on any miss/error.

        Never raises — callers can treat ``None`` as a simple cache miss
        without wrapping the call in a try/except.

        Parameters
        ----------
        key:
            The original lookup key (e.g. a GitHub API URL).

        Returns
        -------
        dict | None
            The previously cached JSON object, or ``None`` if:

            * the entry has never been stored,
            * the cache file is missing or unreadable,
            * the file contains corrupted / non-JSON content.
        """
        path = self._key_to_path(key)
        try:
            text = path.read_text(encoding="utf-8")
            return json.loads(text)
        except FileNotFoundError:
            # Normal cache miss — not an error worth logging.
            return None
        except json.JSONDecodeError as exc:
            logger.warning(
                "ExtractionCache: corrupted JSON in %s (key=%r): %s — treating as miss.",
                path,
                key,
                exc,
            )
            return None
        except OSError as exc:
            logger.warning(
                "ExtractionCache: could not read %s (key=%r): %s — treating as miss.",
                path,
                key,
                exc,
            )
            return None

    def set(self, key: str, value: dict) -> None:
        """
        Persist *value* under *key* in the cache.

        Write failures are logged as warnings and swallowed so the
        adapter's "never raise" contract is preserved end-to-end.

        Parameters
        ----------
        key:
            The original lookup key (e.g. a GitHub API URL).
        value:
            A JSON-serialisable dict (typically a raw API response).
        """
        try:
            self._ensure_dir()
            path = self._key_to_path(key)
            path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
            logger.debug("ExtractionCache: wrote %s (key=%r)", path, key)
        except (OSError, TypeError) as exc:
            logger.warning(
                "ExtractionCache: failed to write cache for key=%r: %s",
                key,
                exc,
            )
