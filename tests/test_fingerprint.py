"""Tests for conversation fingerprinting."""

from dbproxy.identity.fingerprint import (
    SYSTEM_PREFIX_LENGTH,
    _extract_session_id,
    _fallback_fingerprint,
    compute_fingerprint,
)


class TestExtractSessionId:
    def test_extracts_session_uuid(self):
        body = {
            "metadata": {
                "user_id": "user_abc123_account_def-456_session_ec41ccf5-0cad-44c1-9b20-c0dc9829b848"
            }
        }
        assert _extract_session_id(body) == "ec41ccf5-0cad-44c1-9b20-c0dc9829b848"

    def test_no_metadata(self):
        assert _extract_session_id({"messages": []}) is None

    def test_no_user_id(self):
        assert _extract_session_id({"metadata": {}}) is None

    def test_user_id_without_session(self):
        assert _extract_session_id({"metadata": {"user_id": "just-a-string"}}) is None

    def test_metadata_not_dict(self):
        assert _extract_session_id({"metadata": "string"}) is None


class TestComputeFingerprint:
    def test_uses_session_id_when_available(self):
        body = {
            "metadata": {
                "user_id": "user_x_account_y_session_aaaa-bbbb-cccc"
            },
            "system": "You are helpful",
            "messages": [{"role": "user", "content": "hello"}],
        }
        fp = compute_fingerprint(body)
        assert fp == "aaaa-bbbb-cccc"

    def test_same_session_different_messages_same_fingerprint(self):
        body1 = {
            "metadata": {"user_id": "user_x_account_y_session_aaaa"},
            "messages": [{"role": "user", "content": "hello"}],
        }
        body2 = {
            "metadata": {"user_id": "user_x_account_y_session_aaaa"},
            "messages": [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi"},
                {"role": "user", "content": "more"},
            ],
        }
        assert compute_fingerprint(body1) == compute_fingerprint(body2)

    def test_different_session_different_fingerprint(self):
        body1 = {
            "metadata": {"user_id": "user_x_account_y_session_aaaa"},
            "messages": [{"role": "user", "content": "hello"}],
        }
        body2 = {
            "metadata": {"user_id": "user_x_account_y_session_bbbb"},
            "messages": [{"role": "user", "content": "hello"}],
        }
        assert compute_fingerprint(body1) != compute_fingerprint(body2)

    def test_session_id_stable_with_system_reminder_changes(self):
        """The exact bug that triggered this change — system-reminder in
        first user message changed between requests, but session ID stays."""
        body1 = {
            "metadata": {"user_id": "user_x_account_y_session_ec41ccf5-0cad-44c1-9b20-aaa"},
            "messages": [{"role": "user", "content": "<system-reminder>date: Jan 1</system-reminder>\nhello"}],
        }
        body2 = {
            "metadata": {"user_id": "user_x_account_y_session_ec41ccf5-0cad-44c1-9b20-aaa"},
            "messages": [{"role": "user", "content": "<system-reminder>date: Jan 2</system-reminder>\nhello"}],
        }
        assert compute_fingerprint(body1) == compute_fingerprint(body2)


class TestFallbackFingerprint:
    """Tests for the hash-based fallback when metadata is unavailable."""

    def test_same_input_same_hash(self):
        body = {
            "system": "You are helpful",
            "messages": [{"role": "user", "content": "hello"}],
        }
        fp1 = _fallback_fingerprint(body)
        fp2 = _fallback_fingerprint(body)
        assert fp1 == fp2

    def test_different_system_different_hash(self):
        body1 = {
            "system": "You are helpful",
            "messages": [{"role": "user", "content": "hello"}],
        }
        body2 = {
            "system": "You are a pirate",
            "messages": [{"role": "user", "content": "hello"}],
        }
        assert _fallback_fingerprint(body1) != _fallback_fingerprint(body2)

    def test_different_first_message_different_hash(self):
        body1 = {
            "system": "You are helpful",
            "messages": [{"role": "user", "content": "hello"}],
        }
        body2 = {
            "system": "You are helpful",
            "messages": [{"role": "user", "content": "goodbye"}],
        }
        assert _fallback_fingerprint(body1) != _fallback_fingerprint(body2)

    def test_later_messages_ignored(self):
        body1 = {
            "system": "You are helpful",
            "messages": [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi"},
            ],
        }
        body2 = {
            "system": "You are helpful",
            "messages": [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "different"},
                {"role": "user", "content": "more"},
            ],
        }
        assert _fallback_fingerprint(body1) == _fallback_fingerprint(body2)

    def test_no_system_prompt(self):
        body = {"messages": [{"role": "user", "content": "hello"}]}
        fp = _fallback_fingerprint(body)
        assert isinstance(fp, str)
        assert len(fp) == 64  # SHA-256 hex

    def test_system_as_list(self):
        body = {
            "system": [{"type": "text", "text": "You are helpful"}],
            "messages": [{"role": "user", "content": "hello"}],
        }
        fp = _fallback_fingerprint(body)
        assert isinstance(fp, str)

    def test_fallback_used_when_no_metadata(self):
        body = {
            "system": "You are helpful",
            "messages": [{"role": "user", "content": "hello"}],
        }
        assert compute_fingerprint(body) == _fallback_fingerprint(body)

    def test_system_suffix_changes_ignored(self):
        stable_prefix = "You are Claude Code, a helpful assistant." * 50
        body1 = {
            "system": stable_prefix + "\n\nCurrent file: foo.py",
            "messages": [{"role": "user", "content": "hello"}],
        }
        body2 = {
            "system": stable_prefix + "\n\nCurrent file: bar.py\nExtra context here",
            "messages": [{"role": "user", "content": "hello"}],
        }
        assert _fallback_fingerprint(body1) == _fallback_fingerprint(body2)

    def test_system_prefix_difference_detected(self):
        body1 = {
            "system": "You are Claude Code" + "x" * 2000,
            "messages": [{"role": "user", "content": "hello"}],
        }
        body2 = {
            "system": "You are Cursor AI" + "x" * 2000,
            "messages": [{"role": "user", "content": "hello"}],
        }
        assert _fallback_fingerprint(body1) != _fallback_fingerprint(body2)
