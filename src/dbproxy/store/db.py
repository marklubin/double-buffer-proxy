"""SQLite database connection and operations via aiosqlite."""

from __future__ import annotations

import json
import os
import time
from typing import Any

import aiosqlite
import structlog

from .models import SCHEMA_SQL, ConversationRow, EventRow

log = structlog.get_logger()


class Database:
    """Async SQLite database for persisting conversation state."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        """Open database connection and initialize schema."""
        os.makedirs(os.path.dirname(self._db_path) or ".", exist_ok=True)
        self._conn = await aiosqlite.connect(self._db_path)
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.executescript(SCHEMA_SQL)
        await self._conn.commit()
        log.info("db_connected", path=self._db_path)

    async def close(self) -> None:
        """Close database connection."""
        if self._conn:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database not connected")
        return self._conn

    async def upsert_conversation(
        self,
        fingerprint: str,
        model: str,
        context_window: int,
        phase: str,
        total_input_tokens: int = 0,
        checkpoint_content: str | None = None,
        checkpoint_anchor_index: int | None = None,
        wal_start_index: int | None = None,
    ) -> None:
        """Insert or update a conversation record."""
        now = time.time()
        await self.conn.execute(
            """
            INSERT INTO conversations
                (fingerprint, model, context_window, phase, total_input_tokens,
                 checkpoint_content, checkpoint_anchor_index, wal_start_index,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(fingerprint) DO UPDATE SET
                phase = excluded.phase,
                total_input_tokens = excluded.total_input_tokens,
                checkpoint_content = excluded.checkpoint_content,
                checkpoint_anchor_index = excluded.checkpoint_anchor_index,
                wal_start_index = excluded.wal_start_index,
                updated_at = excluded.updated_at
            """,
            (
                fingerprint, model, context_window, phase, total_input_tokens,
                checkpoint_content, checkpoint_anchor_index, wal_start_index,
                now, now,
            ),
        )
        await self.conn.commit()

    async def get_conversation(self, fingerprint: str) -> ConversationRow | None:
        """Fetch a conversation by fingerprint."""
        cursor = await self.conn.execute(
            "SELECT * FROM conversations WHERE fingerprint = ?",
            (fingerprint,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return ConversationRow(*row)

    async def list_conversations(self) -> list[ConversationRow]:
        """List all conversations."""
        cursor = await self.conn.execute(
            "SELECT * FROM conversations ORDER BY updated_at DESC"
        )
        rows = await cursor.fetchall()
        return [ConversationRow(*r) for r in rows]

    async def delete_conversation(self, fingerprint: str) -> None:
        """Delete a conversation and its messages."""
        await self.conn.execute(
            "DELETE FROM messages WHERE fingerprint = ?", (fingerprint,)
        )
        await self.conn.execute(
            "DELETE FROM conversations WHERE fingerprint = ?", (fingerprint,)
        )
        await self.conn.commit()

    async def log_event(
        self,
        event_type: str,
        fingerprint: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Log a timestamped event."""
        await self.conn.execute(
            "INSERT INTO events (fingerprint, event_type, payload_json, created_at) VALUES (?, ?, ?, ?)",
            (fingerprint, event_type, json.dumps(payload) if payload else None, time.time()),
        )
        await self.conn.commit()

    async def get_recent_events(
        self, fingerprint: str | None = None, limit: int = 100
    ) -> list[EventRow]:
        """Fetch recent events, optionally filtered by conversation."""
        if fingerprint:
            cursor = await self.conn.execute(
                "SELECT * FROM events WHERE fingerprint = ? ORDER BY created_at DESC LIMIT ?",
                (fingerprint, limit),
            )
        else:
            cursor = await self.conn.execute(
                "SELECT * FROM events ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )
        rows = await cursor.fetchall()
        return [EventRow(*r) for r in rows]
