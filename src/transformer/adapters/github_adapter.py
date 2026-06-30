from __future__ import annotations
"""Fetches GitHub profile information.""" 
"""
adapters/github_adapter.py — adapter that pulls candidate data from the GitHub API.

What this adapter emits
-----------------------
* ``full_name``  — from ``GET /users/{username}`` → ``name`` field.
* ``headline``   — from ``GET /users/{username}`` → ``bio`` field.
* ``skills``     — one RawField per distinct programming language that
                   appears in ``GET /users/{username}/repos?per_page=100``.

What this adapter deliberately does NOT emit
--------------------------------------------
GitHub bio/name are unreliable proxies for employer or job title.
``current_company`` and ``current_title`` are therefore not emitted
here — only ``full_name``, ``headline``, and ``skills``.

Cache contract (Phase 9)
------------------------
Before any network call the adapter checks :class:`ExtractionCache`.
On a miss it fetches, then stores the response.  The stub cache always
returns ``None`` (miss) so every request is live until Phase 9 lands.

Error policy
------------
On ANY failure (timeout, 403, 404, bad JSON, malformed URL …) the
adapter logs a WARNING and returns [].  It never raises.
"""



import logging
import re
from typing import Any
from urllib.parse import urlparse

from src.transformer.adapters.base import SourceAdapter
from src.transformer.cache.extraction_cache import ExtractionCache
from src.transformer.raw_field import RawField

logger = logging.getLogger(__name__)

_GITHUB_API = "https://api.github.com"
_TIMEOUT = 5  # seconds
_SOURCE = "github"
_METHOD_API = "api_fetch"


def _username_from_url(url: str) -> str:
    """
    Parse a GitHub profile URL and return the username component.

    Accepts forms such as:
        https://github.com/octocat
        https://github.com/octocat/
        github.com/octocat

    Raises ``ValueError`` if the URL cannot be parsed to a non-empty username.
    """
    # Normalise: add scheme if missing so urlparse works correctly.
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    parsed = urlparse(url)
    # Path looks like '/octocat' or '/octocat/'.
    parts = [p for p in parsed.path.split("/") if p]
    if not parts:
        raise ValueError(f"Cannot extract GitHub username from URL: {url!r}")
    return parts[0]


class GithubAdapter(SourceAdapter):
    """
    Fetches candidate data from the public GitHub REST API (v3).

    Parameters
    ----------
    cache:
        An :class:`~src.transformer.cache.extraction_cache.ExtractionCache`
        instance.  Defaults to a fresh stub; inject a real cache in
        Phase 9 or in tests.
    """

    def __init__(self, cache: ExtractionCache | None = None) -> None:
        self._cache = cache or ExtractionCache()

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def extract(self, source_path_or_url: str) -> list[RawField]:
        try:
            return self._do_extract(source_path_or_url)
        except Exception as exc:
            logger.warning(
                "GithubAdapter.extract() failed for %r: %s",
                source_path_or_url,
                exc,
            )
            return []

    # ------------------------------------------------------------------
    # Internal implementation (called only from inside the try/except)
    # ------------------------------------------------------------------

    def _do_extract(self, url: str) -> list[RawField]:
        import requests  # local import keeps the module importable when requests is absent

        username = _username_from_url(url)
        fields: list[RawField] = []

        # ---- 1. User profile endpoint --------------------------------
        profile_url = f"{_GITHUB_API}/users/{username}"
        profile = self._fetch_json(requests, profile_url)

        if profile is None:
            # Warning already logged inside _fetch_json.
            return []

        name: str | None = profile.get("name") or None
        bio: str | None = profile.get("bio") or None

        if name:
            fields.append(
                RawField(
                    field_name="full_name",
                    value=name,
                    source=_SOURCE,
                    method=_METHOD_API,
                )
            )

        if bio:
            fields.append(
                RawField(
                    field_name="headline",
                    value=bio,
                    source=_SOURCE,
                    method=_METHOD_API,
                )
            )

        # ---- 2. Repos endpoint → skills ------------------------------
        repos_url = f"{_GITHUB_API}/users/{username}/repos?per_page=100"
        repos = self._fetch_json(requests, repos_url)

        if repos is not None and isinstance(repos, list):
            seen_languages: set[str] = set()
            for repo in repos:
                lang = repo.get("language") if isinstance(repo, dict) else None
                if lang and lang not in seen_languages:
                    seen_languages.add(lang)
                    fields.append(
                        RawField(
                            field_name="skills",
                            value=lang,
                            source=_SOURCE,
                            method=_METHOD_API,
                        )
                    )

        return fields

    def _fetch_json(self, requests_mod: Any, url: str) -> Any | None:
        """
        Fetch *url* as JSON, using the cache when available.

        Returns the parsed JSON object, or ``None`` on any error.
        Logs a warning (without raising) on every failure path.
        """
        # Cache check.
        cached = self._cache.get(url)
        if cached is not None:
            return cached

        try:
            response = requests_mod.get(
                url,
                timeout=_TIMEOUT,
                headers={"Accept": "application/vnd.github+json"},
            )
        except requests_mod.exceptions.Timeout:
            logger.warning("GithubAdapter: request timed out for %r", url)
            return None
        except requests_mod.exceptions.RequestException as exc:
            logger.warning("GithubAdapter: network error for %r: %s", url, exc)
            return None

        if response.status_code == 403:
            logger.warning(
                "GithubAdapter: rate-limited (403) fetching %r — skipping", url
            )
            return None
        if response.status_code == 404:
            logger.warning(
                "GithubAdapter: resource not found (404) for %r — skipping", url
            )
            return None
        if not response.ok:
            logger.warning(
                "GithubAdapter: unexpected HTTP %d for %r — skipping",
                response.status_code,
                url,
            )
            return None

        try:
            data = response.json()
        except ValueError as exc:
            logger.warning(
                "GithubAdapter: malformed JSON from %r: %s", url, exc
            )
            return None

        # Store in cache for future calls.
        self._cache.set(url, data)
        return data
