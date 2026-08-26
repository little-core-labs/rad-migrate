"""SQLite resume journal.

This is a **cache, not a source of truth**. Whether a video already exists at the
destination is answered by asking the destination (see ``dest.reconcile``), never by
reading this file — that is what makes a fresh clone on a new machine behave
correctly, and it is the single biggest departure from the scripts this replaces,
which spread that answer across an archive file, three manifests, three logs, and two
hardcoded id lists.

What lives here is the stuff that is expensive to recompute and harmless to lose:
which videos we have already pulled bytes for, where they landed on disk, how many
times we have tried, and what went wrong last time. Delete the file and the next run
re-scans; it never publishes twice.

WAL mode is on so the MCP server can read progress while a background migration writes.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterator, Sequence
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import ItemState, VideoItem

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS items (
    channel_id TEXT NOT NULL,
    source     TEXT NOT NULL,
    source_id  TEXT NOT NULL,
    state      TEXT NOT NULL,
    data       TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (channel_id, source, source_id)
);

CREATE INDEX IF NOT EXISTS idx_items_state ON items (channel_id, state);

CREATE TABLE IF NOT EXISTS jobs (
    id         TEXT PRIMARY KEY,
    channel    TEXT,
    status     TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    request    TEXT,
    result     TEXT,
    error      TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs (status, created_at);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    """Thin persistence layer over SQLite."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    # -- connection --------------------------------------------------------

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=30000")
            yield conn
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._connect() as conn, closing(conn.cursor()) as cur:
            cur.executescript(_SCHEMA)
            cur.execute(
                "INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )

    # -- items -------------------------------------------------------------

    def upsert(self, item: VideoItem) -> None:
        item.updated_at = datetime.now(timezone.utc)
        payload = item.model_dump_json()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO items (channel_id, source, source_id, state, data, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (channel_id, source, source_id) DO UPDATE SET
                    state = excluded.state,
                    data = excluded.data,
                    updated_at = excluded.updated_at
                """,
                (item.channel_id, item.source, item.source_id, item.state.value, payload, _now()),
            )

    def upsert_many(self, items: Sequence[VideoItem]) -> None:
        if not items:
            return
        stamp = _now()
        rows = []
        for item in items:
            item.updated_at = datetime.now(timezone.utc)
            rows.append(
                (
                    item.channel_id,
                    item.source,
                    item.source_id,
                    item.state.value,
                    item.model_dump_json(),
                    stamp,
                )
            )
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO items (channel_id, source, source_id, state, data, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (channel_id, source, source_id) DO UPDATE SET
                    state = excluded.state,
                    data = excluded.data,
                    updated_at = excluded.updated_at
                """,
                rows,
            )

    def add_if_absent(self, items: Sequence[VideoItem]) -> int:
        """Insert newly discovered videos without clobbering existing progress.

        Discovery runs on every invocation, so it must never reset an item that is
        already downloaded or published back to ``discovered``.
        """
        if not items:
            return 0
        stamp = _now()
        rows = [
            (
                item.channel_id,
                item.source,
                item.source_id,
                item.state.value,
                item.model_dump_json(),
                stamp,
            )
            for item in items
        ]
        with self._connect() as conn, closing(conn.cursor()) as cur:
            cur.executemany(
                """
                INSERT OR IGNORE INTO items
                    (channel_id, source, source_id, state, data, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

    def get(self, channel_id: str, source_id: str, source: str = "youtube") -> VideoItem | None:
        with self._connect() as conn, closing(conn.cursor()) as cur:
            cur.execute(
                "SELECT data FROM items WHERE channel_id = ? AND source = ? AND source_id = ?",
                (channel_id, source, source_id),
            )
            row = cur.fetchone()
        return VideoItem.model_validate_json(row["data"]) if row else None

    def all_items(self, channel_id: str) -> list[VideoItem]:
        with self._connect() as conn, closing(conn.cursor()) as cur:
            cur.execute(
                "SELECT data FROM items WHERE channel_id = ? ORDER BY source_id",
                (channel_id,),
            )
            rows = cur.fetchall()
        return [VideoItem.model_validate_json(row["data"]) for row in rows]

    def items_in(self, channel_id: str, states: Sequence[ItemState]) -> list[VideoItem]:
        if not states:
            return []
        placeholders = ",".join("?" for _ in states)
        with self._connect() as conn, closing(conn.cursor()) as cur:
            cur.execute(
                f"SELECT data FROM items WHERE channel_id = ? AND state IN ({placeholders})",  # noqa: S608
                (channel_id, *[s.value for s in states]),
            )
            rows = cur.fetchall()
        items = [VideoItem.model_validate_json(row["data"]) for row in rows]
        items.sort(key=lambda i: i.sort_key)
        return items

    def channel_ids(self) -> list[str]:
        """Every channel this journal has tracked."""
        with self._connect() as conn, closing(conn.cursor()) as cur:
            cur.execute("SELECT DISTINCT channel_id FROM items ORDER BY channel_id")
            return [row["channel_id"] for row in cur.fetchall()]

    def counts(self, channel_id: str) -> dict[str, int]:
        with self._connect() as conn, closing(conn.cursor()) as cur:
            cur.execute(
                "SELECT state, COUNT(*) AS n FROM items WHERE channel_id = ? GROUP BY state",
                (channel_id,),
            )
            return {row["state"]: row["n"] for row in cur.fetchall()}

    def known_ids(self, channel_id: str, source: str = "youtube") -> set[str]:
        with self._connect() as conn, closing(conn.cursor()) as cur:
            cur.execute(
                "SELECT source_id FROM items WHERE channel_id = ? AND source = ?",
                (channel_id, source),
            )
            return {row["source_id"] for row in cur.fetchall()}

    def reset_states(self, channel_id: str, states: Sequence[ItemState]) -> int:
        """Move failed items back so the next pass retries them."""
        affected = self.items_in(channel_id, states)
        for item in affected:
            if item.state in {ItemState.FAILED_DOWNLOAD, ItemState.FAILED_QUALITY}:
                item.state = ItemState.DISCOVERED
            elif item.state == ItemState.FAILED_PUBLISH:
                item.state = ItemState.DOWNLOADED if _has_media(item) else ItemState.DISCOVERED
            elif item.state == ItemState.FAILED_PROCESSING:
                item.state = ItemState.PUBLISHED
            item.last_error = None
        self.upsert_many(affected)
        return len(affected)

    # -- jobs (background migrations driven by the MCP server) -------------

    def create_job(self, channel: str | None, request: dict[str, Any]) -> str:
        job_id = uuid.uuid4().hex[:12]
        stamp = _now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO jobs (id, channel, status, created_at, updated_at, request)
                VALUES (?, ?, 'running', ?, ?, ?)
                """,
                (job_id, channel, stamp, stamp, json.dumps(request, default=str)),
            )
        return job_id

    def finish_job(
        self,
        job_id: str,
        *,
        status: str,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE jobs SET status = ?, updated_at = ?, result = ?, error = ? WHERE id = ?",
                (
                    status,
                    _now(),
                    json.dumps(result, default=str) if result is not None else None,
                    error,
                    job_id,
                ),
            )

    def touch_job(self, job_id: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE jobs SET updated_at = ? WHERE id = ?", (_now(), job_id))

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._connect() as conn, closing(conn.cursor()) as cur:
            cur.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
            row = cur.fetchone()
        return _job_row(row) if row else None

    def list_jobs(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as conn, closing(conn.cursor()) as cur:
            cur.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,))
            return [_job_row(row) for row in cur.fetchall()]

    def cancel_job(self, job_id: str) -> bool:
        with self._connect() as conn, closing(conn.cursor()) as cur:
            cur.execute(
                "UPDATE jobs SET status = 'cancelling', updated_at = ? "
                "WHERE id = ? AND status = 'running'",
                (_now(), job_id),
            )
            return bool(cur.rowcount)

    def job_is_cancelling(self, job_id: str) -> bool:
        with self._connect() as conn, closing(conn.cursor()) as cur:
            cur.execute("SELECT status FROM jobs WHERE id = ?", (job_id,))
            row = cur.fetchone()
        return bool(row and row["status"] in {"cancelling", "cancelled"})


def _has_media(item: VideoItem) -> bool:
    return bool(item.video_path and Path(item.video_path).exists())


def _job_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "channel": row["channel"],
        "status": row["status"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "request": json.loads(row["request"]) if row["request"] else None,
        "result": json.loads(row["result"]) if row["result"] else None,
        "error": row["error"],
    }
