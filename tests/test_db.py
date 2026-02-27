"""Tests for database operations."""

import os
import tempfile

import pytest

from dbproxy.store.db import Database


@pytest.fixture
async def db():
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Database(os.path.join(tmpdir, "test.sqlite"))
        await db.connect()
        yield db
        await db.close()


class TestDatabase:
    @pytest.mark.asyncio
    async def test_connect_creates_tables(self, db):
        cursor = await db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        tables = {row[0] for row in await cursor.fetchall()}
        assert "conversations" in tables
        assert "messages" in tables
        assert "events" in tables

    @pytest.mark.asyncio
    async def test_upsert_and_get_conversation(self, db):
        await db.upsert_conversation(
            fingerprint="abc123",
            model="claude-sonnet-4-6",
            context_window=200_000,
            phase="IDLE",
            total_input_tokens=50_000,
        )
        row = await db.get_conversation("abc123")
        assert row is not None
        assert row.fingerprint == "abc123"
        assert row.model == "claude-sonnet-4-6"
        assert row.phase == "IDLE"
        assert row.total_input_tokens == 50_000

    @pytest.mark.asyncio
    async def test_upsert_updates_existing(self, db):
        await db.upsert_conversation("abc", "claude-sonnet-4-6", 200_000, "IDLE")
        await db.upsert_conversation("abc", "claude-sonnet-4-6", 200_000, "WAL_ACTIVE", total_input_tokens=100_000)
        row = await db.get_conversation("abc")
        assert row is not None
        assert row.phase == "WAL_ACTIVE"
        assert row.total_input_tokens == 100_000

    @pytest.mark.asyncio
    async def test_list_conversations(self, db):
        await db.upsert_conversation("a", "claude-sonnet-4-6", 200_000, "IDLE")
        await db.upsert_conversation("b", "claude-sonnet-4-6", 200_000, "WAL_ACTIVE")
        rows = await db.list_conversations()
        assert len(rows) == 2

    @pytest.mark.asyncio
    async def test_delete_conversation(self, db):
        await db.upsert_conversation("abc", "claude-sonnet-4-6", 200_000, "IDLE")
        await db.delete_conversation("abc")
        row = await db.get_conversation("abc")
        assert row is None

    @pytest.mark.asyncio
    async def test_log_and_get_events(self, db):
        await db.log_event("phase_transition", "conv1", {"from": "IDLE", "to": "WAL_ACTIVE"})
        await db.log_event("checkpoint_started", "conv1")
        events = await db.get_recent_events("conv1")
        assert len(events) == 2
        assert events[0].event_type == "checkpoint_started"  # Most recent first

    @pytest.mark.asyncio
    async def test_get_nonexistent_conversation(self, db):
        row = await db.get_conversation("nonexistent")
        assert row is None
