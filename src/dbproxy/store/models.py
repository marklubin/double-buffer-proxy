"""Database table definitions and dataclass row types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS conversations (
    fingerprint TEXT PRIMARY KEY,
    model TEXT NOT NULL,
    context_window INTEGER NOT NULL,
    phase TEXT NOT NULL DEFAULT 'IDLE',
    total_input_tokens INTEGER NOT NULL DEFAULT 0,
    checkpoint_content TEXT,
    checkpoint_anchor_index INTEGER,
    wal_start_index INTEGER,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint TEXT NOT NULL,
    index_in_conversation INTEGER NOT NULL,
    role TEXT NOT NULL,
    content_json TEXT NOT NULL,
    token_estimate INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    FOREIGN KEY (fingerprint) REFERENCES conversations(fingerprint)
);

CREATE INDEX IF NOT EXISTS idx_messages_fingerprint
    ON messages(fingerprint, index_in_conversation);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint TEXT,
    event_type TEXT NOT NULL,
    payload_json TEXT,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_fingerprint
    ON events(fingerprint, created_at);
"""


@dataclass
class ConversationRow:
    fingerprint: str
    model: str
    context_window: int
    phase: str
    total_input_tokens: int
    checkpoint_content: str | None
    checkpoint_anchor_index: int | None
    wal_start_index: int | None
    created_at: float
    updated_at: float


@dataclass
class MessageRow:
    id: int
    fingerprint: str
    index_in_conversation: int
    role: str
    content_json: str
    token_estimate: int
    created_at: float


@dataclass
class EventRow:
    id: int
    fingerprint: str | None
    event_type: str
    payload_json: str | None
    created_at: float
