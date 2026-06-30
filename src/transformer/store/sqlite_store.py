"""
store/sqlite_store.py — SQLite-backed persistence for canonical profiles.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Optional

from ..schema import CanonicalProfile

logger = logging.getLogger(__name__)

_DDL = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS profiles (
    candidate_id      TEXT PRIMARY KEY,
    full_name         TEXT,
    primary_email     TEXT,
    primary_phone     TEXT,
    current_company   TEXT,
    current_title     TEXT,
    overall_confidence REAL,
    full_profile_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_profiles_email ON profiles(primary_email);
CREATE INDEX IF NOT EXISTS idx_profiles_phone ON profiles(primary_phone);

CREATE TABLE IF NOT EXISTS skills (
    candidate_id TEXT,
    skill_name   TEXT,
    confidence   REAL,
    FOREIGN KEY(candidate_id) REFERENCES profiles(candidate_id)
);
CREATE INDEX IF NOT EXISTS idx_skills_name ON skills(skill_name);

CREATE VIRTUAL TABLE IF NOT EXISTS profiles_fts USING fts5(
    candidate_id,
    full_name,
    headline,
    company,
    skills_text
);
"""


def _flatten(profile: CanonicalProfile) -> dict:
    primary_email: Optional[str] = profile.emails[0] if profile.emails else None
    primary_phone: Optional[str] = profile.phones[0] if profile.phones else None

    current_company: Optional[str] = None
    current_title: Optional[str] = None

    if profile.experience:
        most_recent = max(profile.experience, key=lambda e: e.start or "")
        current_company = most_recent.company
        current_title = most_recent.title

    return {
        "candidate_id": profile.candidate_id,
        "full_name": profile.full_name,
        "primary_email": primary_email,
        "primary_phone": primary_phone,
        "current_company": current_company,
        "current_title": current_title,
        "overall_confidence": profile.overall_confidence,
        "full_profile_json": profile.model_dump_json(),
    }


class SqliteStore:
    """SQLite-backed store for CanonicalProfile objects."""

    def __init__(self, db_path: str) -> None:
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._setup()

    def _setup(self) -> None:
        self._conn.executescript(_DDL)
        logger.debug("SqliteStore: schema initialised.")

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def write_profile(self, profile: CanonicalProfile) -> None:
        flat = _flatten(profile)
        skill_rows = [(profile.candidate_id, s.name, s.confidence) for s in profile.skills]
        skills_text = " ".join(s.name for s in profile.skills)
        headline = profile.headline or ""
        company = flat["current_company"] or ""

        try:
            cur = self._conn.cursor()
            cur.execute(
                """
                INSERT OR REPLACE INTO profiles
                    (candidate_id, full_name, primary_email, primary_phone,
                     current_company, current_title, overall_confidence,
                     full_profile_json)
                VALUES
                    (:candidate_id, :full_name, :primary_email, :primary_phone,
                     :current_company, :current_title, :overall_confidence,
                     :full_profile_json)
                """,
                flat,
            )
            cur.execute("DELETE FROM skills WHERE candidate_id = ?", (profile.candidate_id,))
            if skill_rows:
                cur.executemany(
                    "INSERT INTO skills (candidate_id, skill_name, confidence) VALUES (?, ?, ?)",
                    skill_rows,
                )
            cur.execute("DELETE FROM profiles_fts WHERE candidate_id = ?", (profile.candidate_id,))
            cur.execute(
                "INSERT INTO profiles_fts (candidate_id, full_name, headline, company, skills_text) "
                "VALUES (?, ?, ?, ?, ?)",
                (profile.candidate_id, profile.full_name or "", headline, company, skills_text),
            )
            self._conn.commit()
            logger.debug("SqliteStore: wrote profile %r.", profile.candidate_id)
        except sqlite3.Error as exc:
            self._conn.rollback()
            logger.error("SqliteStore: write_profile failed for %r, rolled back: %s",
                         profile.candidate_id, exc)
            raise

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def load_all_dicts(self) -> list[dict]:
        """
        Return all candidate profiles as plain dicts, parsed from full_profile_json.
        Used by the CLI print commands.
        """
        cur = self._conn.cursor()
        cur.execute("SELECT full_profile_json FROM profiles ORDER BY overall_confidence DESC")
        results = []
        for row in cur.fetchall():
            try:
                results.append(json.loads(row[0]))
            except (json.JSONDecodeError, TypeError) as exc:
                logger.warning("SqliteStore: could not parse profile JSON: %s", exc)
        return results

    def search_by_skill(self, skill_name: str) -> list[dict]:
        cur = self._conn.cursor()
        cur.execute(
            """
            SELECT p.candidate_id, p.full_name, p.primary_email, p.primary_phone,
                   p.current_company, p.current_title, p.overall_confidence,
                   s.confidence AS skill_confidence
            FROM skills s
            JOIN profiles p ON p.candidate_id = s.candidate_id
            WHERE LOWER(s.skill_name) = LOWER(?)
            ORDER BY s.confidence DESC
            """,
            (skill_name,),
        )
        return [dict(row) for row in cur.fetchall()]

    def full_text_search(self, query: str) -> list[dict]:
        try:
            cur = self._conn.cursor()
            cur.execute(
                """
                SELECT candidate_id, full_name, headline, company, skills_text
                FROM profiles_fts WHERE profiles_fts MATCH ? ORDER BY rank
                """,
                (query,),
            )
            return [dict(row) for row in cur.fetchall()]
        except sqlite3.OperationalError as exc:
            logger.warning("SqliteStore: full_text_search failed for query %r: %s", query, exc)
            return []

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        self._conn.close()
        logger.debug("SqliteStore: connection closed.")

    def __enter__(self) -> "SqliteStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()