"""SQLite library of imported context capsules and share links."""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from .capsule import Capsule
from .errors import CtxshareError
from .util import db_path, share_token

SCHEMA = """
CREATE TABLE IF NOT EXISTS library (
    id            TEXT PRIMARY KEY,
    title         TEXT NOT NULL,
    provider      TEXT NOT NULL,
    model         TEXT NOT NULL,
    origin        TEXT NOT NULL DEFAULT '',
    source_url    TEXT NOT NULL DEFAULT '',
    system        TEXT NOT NULL DEFAULT '',
    message_count INTEGER NOT NULL,
    tokens        INTEGER NOT NULL,
    created_at    REAL NOT NULL,
    imported_at   REAL NOT NULL,
    updated_at    REAL NOT NULL,
    json          TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_library_source_url
    ON library(source_url) WHERE source_url <> '';
CREATE INDEX IF NOT EXISTS idx_library_imported
    ON library(imported_at DESC);
CREATE TABLE IF NOT EXISTS shares (
    token       TEXT PRIMARY KEY,
    capsule_id  TEXT NOT NULL REFERENCES library(id) ON DELETE CASCADE,
    created_at  REAL NOT NULL,
    expires_at  REAL
);
CREATE INDEX IF NOT EXISTS idx_shares_capsule ON shares(capsule_id);
CREATE INDEX IF NOT EXISTS idx_shares_expiry ON shares(expires_at);
"""


class Store:
    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(self.path), timeout=15, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        with self._lock:
            self.conn.execute("PRAGMA foreign_keys = ON")
            self.conn.execute("PRAGMA journal_mode = WAL")
            self.conn.executescript(SCHEMA)
            self.conn.commit()

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                yield self.conn
                self.conn.commit()
            except BaseException:
                self.conn.rollback()
                raise

    # -- library -----------------------------------------------------------

    def save_capsule(self, capsule: Capsule, replace: bool = True) -> str:
        payload = capsule.to_json()
        timestamp = time.time()
        with self._write() as conn:
            existing = None
            if capsule.source_url:
                existing = conn.execute(
                    "SELECT id FROM library WHERE source_url = ?", (capsule.source_url,)
                ).fetchone()
            if existing is None:
                existing = conn.execute(
                    "SELECT id FROM library WHERE id = ?", (capsule.id,)
                ).fetchone()
            if existing is not None:
                capsule_id = str(existing["id"])
                if not replace:
                    raise CtxshareError("Capsule ini sudah ada di library")
                conn.execute(
                    """
                    UPDATE library SET title=?, provider=?, model=?, origin=?, source_url=?,
                        system=?, message_count=?, tokens=?, created_at=?, updated_at=?, json=?
                    WHERE id=?
                    """,
                    (
                        capsule.title,
                        capsule.provider,
                        capsule.model,
                        capsule.origin,
                        capsule.source_url,
                        capsule.system,
                        len(capsule.messages),
                        capsule.tokens(),
                        capsule.created_at,
                        timestamp,
                        payload,
                        capsule_id,
                    ),
                )
            else:
                capsule_id = capsule.id
                conn.execute(
                    """
                    INSERT INTO library
                        (id, title, provider, model, origin, source_url, system,
                         message_count, tokens, created_at, imported_at, updated_at, json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        capsule_id,
                        capsule.title,
                        capsule.provider,
                        capsule.model,
                        capsule.origin,
                        capsule.source_url,
                        capsule.system,
                        len(capsule.messages),
                        capsule.tokens(),
                        capsule.created_at,
                        timestamp,
                        timestamp,
                        payload,
                    ),
                )
        capsule.id = capsule_id
        return capsule_id

    def list_capsules(self, query: str = "") -> List[Dict[str, Any]]:
        sql = (
            "SELECT id, title, provider, model, origin, source_url, message_count, "
            "tokens, created_at, imported_at, updated_at FROM library"
        )
        params: List[Any] = []
        if query.strip():
            needle = f"%{query.strip()}%"
            sql += " WHERE title LIKE ? OR provider LIKE ? OR model LIKE ? OR origin LIKE ?"
            params = [needle, needle, needle, needle]
        sql += " ORDER BY imported_at DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def resolve_id(self, prefix: str) -> str:
        value = (prefix or "").strip()
        if not value or value in ("last", "latest", "@last"):
            rows = self.list_capsules()
            if not rows:
                raise CtxshareError("Library masih kosong. Impor context dulu.")
            return str(rows[0]["id"])
        with self._lock:
            exact = self.conn.execute("SELECT id FROM library WHERE id = ?", (value,)).fetchone()
            if exact:
                return str(exact["id"])
            rows = self.conn.execute(
                "SELECT id, title FROM library WHERE id LIKE ? ORDER BY imported_at DESC",
                (value + "%",),
            ).fetchall()
        if not rows:
            raise CtxshareError(f"Capsule '{prefix}' tidak ditemukan")
        if len(rows) > 1:
            choices = ", ".join(str(row["id"]) for row in rows[:6])
            raise CtxshareError(f"ID '{prefix}' ambigu ({choices}); pakai lebih banyak karakter")
        return str(rows[0]["id"])

    def get_capsule(self, capsule_id: str) -> Capsule:
        resolved = self.resolve_id(capsule_id)
        with self._lock:
            row = self.conn.execute(
                "SELECT json FROM library WHERE id = ?", (resolved,)
            ).fetchone()
        if row is None:
            raise CtxshareError(f"Capsule '{capsule_id}' tidak ditemukan")
        try:
            capsule = Capsule.from_json(str(row["json"]))
        except Exception as exc:
            raise CtxshareError(f"Capsule rusak: {exc}") from exc
        capsule.id = resolved
        return capsule

    def delete_capsule(self, capsule_id: str) -> None:
        resolved = self.resolve_id(capsule_id)
        with self._write() as conn:
            cursor = conn.execute("DELETE FROM library WHERE id = ?", (resolved,))
            if cursor.rowcount == 0:
                raise CtxshareError(f"Capsule '{capsule_id}' tidak ditemukan")

    def count_capsules(self) -> int:
        with self._lock:
            row = self.conn.execute("SELECT COUNT(*) AS n FROM library").fetchone()
        return int(row["n"])

    # -- shares ------------------------------------------------------------

    def create_share(self, capsule_id: str, ttl_hours: float = 0.0) -> Dict[str, Any]:
        resolved = self.resolve_id(capsule_id)
        token = share_token()
        created = time.time()
        expires = created + ttl_hours * 3600 if ttl_hours and ttl_hours > 0 else None
        with self._write() as conn:
            exists = conn.execute("SELECT 1 FROM library WHERE id = ?", (resolved,)).fetchone()
            if not exists:
                raise CtxshareError(f"Capsule '{resolved}' tidak ditemukan")
            conn.execute(
                "INSERT INTO shares (token, capsule_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
                (token, resolved, created, expires),
            )
        return {
            "token": token,
            "capsule_id": resolved,
            "created_at": created,
            "expires_at": expires,
        }

    def get_share(self, token: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                """
                SELECT s.token, s.capsule_id, s.created_at, s.expires_at, l.json
                FROM shares s JOIN library l ON l.id = s.capsule_id
                WHERE s.token = ?
                """,
                (token,),
            ).fetchone()
        if row is None:
            return None
        expires = row["expires_at"]
        if expires is not None and float(expires) <= time.time():
            with self._write() as conn:
                conn.execute("DELETE FROM shares WHERE token = ?", (token,))
            return None
        try:
            capsule = Capsule.from_json(str(row["json"]))
        except Exception as exc:
            raise CtxshareError(f"Capsule untuk token ini rusak: {exc}") from exc
        capsule.id = str(row["capsule_id"])
        return {
            "token": str(row["token"]),
            "capsule_id": str(row["capsule_id"]),
            "created_at": float(row["created_at"]),
            "expires_at": float(expires) if expires else None,
            "capsule": capsule,
        }

    def list_shares(self, capsule_id: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = (
            "SELECT s.token, s.capsule_id, s.created_at, s.expires_at, l.title "
            "FROM shares s JOIN library l ON l.id = s.capsule_id"
        )
        params: List[Any] = []
        if capsule_id:
            sql += " WHERE s.capsule_id = ?"
            params.append(self.resolve_id(capsule_id))
        sql += " ORDER BY s.created_at DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def purge_expired(self) -> int:
        with self._write() as conn:
            cursor = conn.execute(
                "DELETE FROM shares WHERE expires_at IS NOT NULL AND expires_at <= ?",
                (time.time(),),
            )
            return cursor.rowcount

    def stats(self) -> Dict[str, int]:
        with self._lock:
            capsules = self.conn.execute("SELECT COUNT(*) AS n FROM library").fetchone()["n"]
            shares = self.conn.execute("SELECT COUNT(*) AS n FROM shares").fetchone()["n"]
            tokens = self.conn.execute("SELECT COALESCE(SUM(tokens), 0) AS n FROM library").fetchone()["n"]
        return {
            "capsules": int(capsules),
            "shares": int(shares),
            "tokens": int(tokens),
        }
