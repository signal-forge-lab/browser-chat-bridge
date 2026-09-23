from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class BridgeStore:
    """Small durable run/conversation and request-idempotency store."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    def _init_schema(self) -> None:
        with closing(self._connect()) as db:
            with db:
                db.execute("PRAGMA journal_mode=WAL")
                db.executescript(
                    """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    conversation_id TEXT,
                    conversation_url TEXT,
                    state TEXT NOT NULL DEFAULT 'ACTIVE',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS turns (
                    request_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    prompt_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    content TEXT,
                    conversation_id TEXT,
                    conversation_url TEXT,
                    error TEXT,
                    raw_result TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES runs(run_id)
                );

                CREATE INDEX IF NOT EXISTS turns_run_id_idx ON turns(run_id);
                    """
                )

    @staticmethod
    def _dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return None if row is None else dict(row)

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as db:
            return self._dict(db.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone())

    def get_turn(self, request_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as db:
            return self._dict(
                db.execute("SELECT * FROM turns WHERE request_id = ?", (request_id,)).fetchone()
            )

    def get_latest_turn_for_run(self, run_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as db:
            return self._dict(
                db.execute(
                    "SELECT * FROM turns WHERE run_id = ? ORDER BY created_at DESC LIMIT 1",
                    (run_id,),
                ).fetchone()
            )

    def get_recovery_protected_conversation_urls(self, *, max_age_seconds: float = 1800.0) -> list[str]:
        """Return recent durable URLs whose latest turn may need reconciliation."""
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=max(0.0, float(max_age_seconds)))
        with closing(self._connect()) as db:
            rows = db.execute(
                """
                SELECT COALESCE(t.conversation_url, r.conversation_url) AS conversation_url,
                       t.updated_at AS updated_at
                FROM runs AS r
                JOIN turns AS t
                  ON t.request_id = (
                    SELECT newest.request_id
                    FROM turns AS newest
                    WHERE newest.run_id = r.run_id
                    ORDER BY newest.created_at DESC
                    LIMIT 1
                  )
                WHERE t.status IN ('TIMEOUT', 'AMBIGUOUS')
                  AND COALESCE(t.conversation_url, r.conversation_url) IS NOT NULL
                  AND COALESCE(t.conversation_url, r.conversation_url) != ''
                """
            ).fetchall()
        protected: set[str] = set()
        for row in rows:
            try:
                updated_at = datetime.fromisoformat(str(row["updated_at"]))
            except ValueError:
                continue
            if updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=timezone.utc)
            if updated_at >= cutoff:
                protected.add(str(row["conversation_url"]))
        return sorted(protected)

    def begin_turn(self, run_id: str, request_id: str, prompt_hash: str) -> tuple[dict[str, Any], bool]:
        now = _now()
        with closing(self._connect()) as db:
            with db:
                db.execute("BEGIN IMMEDIATE")
                db.execute(
                    "INSERT OR IGNORE INTO runs(run_id, created_at, updated_at) VALUES(?, ?, ?)",
                    (run_id, now, now),
                )
                existing = db.execute("SELECT * FROM turns WHERE request_id = ?", (request_id,)).fetchone()
                if existing is not None:
                    row = dict(existing)
                    if row["run_id"] != run_id or row["prompt_hash"] != prompt_hash:
                        raise ValueError("request_id is already bound to a different run or prompt")
                    return row, False
                db.execute(
                    """
                INSERT INTO turns(
                    request_id, run_id, prompt_hash, status, created_at, updated_at
                ) VALUES(?, ?, ?, 'DISPATCHING', ?, ?)
                    """,
                    (request_id, run_id, prompt_hash, now, now),
                )
                row = db.execute("SELECT * FROM turns WHERE request_id = ?", (request_id,)).fetchone()
                return dict(row), True

    def finish_turn(self, request_id: str, result: dict[str, Any]) -> None:
        now = _now()
        with closing(self._connect()) as db:
            with db:
                db.execute(
                    """
                UPDATE turns
                SET status = ?, content = ?, conversation_id = ?, conversation_url = ?,
                    error = ?, raw_result = ?, updated_at = ?
                WHERE request_id = ?
                    """,
                    (
                        str(result.get("status") or "ERROR"),
                        result.get("content"),
                        result.get("conversation_id"),
                        result.get("conversation_url"),
                        result.get("error"),
                        json.dumps(result, ensure_ascii=False, separators=(",", ":")),
                        now,
                        request_id,
                    ),
                )

    def bind_run(self, run_id: str, conversation_id: str, conversation_url: str) -> None:
        now = _now()
        with closing(self._connect()) as db:
            with db:
                row = db.execute(
                    "SELECT conversation_id, conversation_url FROM runs WHERE run_id = ?", (run_id,)
                ).fetchone()
                if row is None:
                    raise KeyError(f"unknown run: {run_id}")
                current_id = row["conversation_id"]
                current_url = row["conversation_url"]
                if current_id is not None and (
                    current_id != conversation_id or current_url != conversation_url
                ):
                    raise ValueError("run is already bound to a different conversation")
                db.execute(
                    """
                UPDATE runs
                SET conversation_id = ?, conversation_url = ?, updated_at = ?
                WHERE run_id = ?
                    """,
                    (conversation_id, conversation_url, now, run_id),
                )

    def delete_run(self, run_id: str) -> None:
        """Purge one run and its cached turns after remote cleanup is confirmed."""
        with closing(self._connect()) as db:
            with db:
                db.execute("DELETE FROM turns WHERE run_id = ?", (run_id,))
                db.execute("DELETE FROM runs WHERE run_id = ?", (run_id,))

