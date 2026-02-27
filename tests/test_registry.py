"""Tests for conversation registry."""

import time

from dbproxy.identity.registry import ConversationRegistry


class TestConversationRegistry:
    def test_get_or_create_new(self):
        reg = ConversationRegistry()
        mgr = reg.get_or_create("fp1", "claude-sonnet-4-6", 200_000)
        assert mgr.conv_id == "fp1"
        assert len(reg) == 1

    def test_get_or_create_existing(self):
        reg = ConversationRegistry()
        mgr1 = reg.get_or_create("fp1", "claude-sonnet-4-6", 200_000)
        mgr2 = reg.get_or_create("fp1", "claude-sonnet-4-6", 200_000)
        assert mgr1 is mgr2
        assert len(reg) == 1

    def test_get_existing(self):
        reg = ConversationRegistry()
        reg.get_or_create("fp1", "claude-sonnet-4-6", 200_000)
        mgr = reg.get("fp1")
        assert mgr is not None

    def test_get_nonexistent(self):
        reg = ConversationRegistry()
        assert reg.get("nonexistent") is None

    def test_remove(self):
        reg = ConversationRegistry()
        reg.get_or_create("fp1", "claude-sonnet-4-6", 200_000)
        reg.remove("fp1")
        assert len(reg) == 0
        assert reg.get("fp1") is None

    def test_expire_stale(self):
        reg = ConversationRegistry(ttl_seconds=0)  # Expire immediately
        reg.get_or_create("fp1", "claude-sonnet-4-6", 200_000)
        key = "fp1:claude-sonnet-4-6"
        reg._last_seen[key] = time.time() - 1  # Force stale
        expired = reg.expire_stale()
        assert key in expired
        assert len(reg) == 0

    def test_all_conversations(self):
        reg = ConversationRegistry()
        reg.get_or_create("fp1", "claude-sonnet-4-6", 200_000)
        reg.get_or_create("fp2", "claude-opus-4-6", 200_000)
        all_convs = reg.all_conversations()
        assert len(all_convs) == 2

    def test_different_models_separate_managers(self):
        """Same fingerprint but different models get separate managers."""
        reg = ConversationRegistry()
        mgr_opus = reg.get_or_create("fp1", "claude-opus-4-6", 200_000)
        mgr_haiku = reg.get_or_create("fp1", "claude-haiku-4-5-20251001", 200_000)
        assert mgr_opus is not mgr_haiku
        assert len(reg) == 2
        # Token updates on one don't affect the other
        mgr_opus.total_input_tokens = 160_000
        mgr_haiku.total_input_tokens = 5_000
        assert mgr_opus.total_input_tokens == 160_000
