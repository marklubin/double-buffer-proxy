"""Proxy configuration via environment variables (DBPROXY_ prefix) or defaults."""

from __future__ import annotations

from pydantic_settings import BaseSettings


class ProxyConfig(BaseSettings):
    host: str = "127.0.0.1"
    port: int = 443
    upstream_url: str = "https://api.anthropic.com"
    checkpoint_threshold: float = 0.60
    swap_threshold: float = 0.80
    max_sse_buffer_bytes: int = 50_000_000  # 50 MB
    db_path: str = "data/dbproxy.sqlite"
    log_dir: str = "logs"
    log_level: str = "DEBUG"
    conversation_ttl_seconds: int = 7200
    passthrough: bool = False
    compact_trigger_tokens: int = 50_000
    tls_ca_dir: str = "certs"
    model_context_windows: dict[str, int] = {
        "claude-opus-4-6": 200_000,
        "claude-sonnet-4-6": 200_000,
        "claude-sonnet-4-5-20250514": 200_000,
        "claude-haiku-4-5-20251001": 200_000,
    }

    model_config = {"env_prefix": "DBPROXY_"}

    def context_window_for(self, model: str) -> int:
        """Return the context window size for the given model, defaulting to 200k."""
        return self.model_context_windows.get(model, 200_000)
