"""CI end-to-end lifecycle test.

Sends real API calls through the proxy (via the CONNECT redirector),
stuffs large context to quickly hit checkpoint thresholds, then verifies
the conversation still works after many rounds.

Usage:
    ANTHROPIC_API_KEY=sk-... python tests/ci_e2e.py

Environment:
    ANTHROPIC_API_KEY     — required
    PROXY_PORT            — CONNECT redirector port (default: 47200)
    DASHBOARD_PORT        — dashboard/health port (default: 47201)
    CA_CERT               — path to ca.pem (default: certs/ca.pem)
    MAX_ROUNDS            — max conversation rounds (default: 10)
    TIMEOUT_SECONDS       — overall timeout (default: 300)
"""

from __future__ import annotations

import json
import os
import ssl
import sys
import time
import urllib.request
import urllib.error

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
PROXY_PORT = int(os.environ.get("PROXY_PORT", "47200"))
DASHBOARD_PORT = int(os.environ.get("DASHBOARD_PORT", "47201"))
CA_CERT = os.environ.get("CA_CERT", "certs/ca.pem")
MAX_ROUNDS = int(os.environ.get("MAX_ROUNDS", "10"))
TIMEOUT = int(os.environ.get("TIMEOUT_SECONDS", "300"))
MODEL = os.environ.get("MODEL", "claude-haiku-4-5-20251001")

# ~20k tokens of padding per round (~4 chars/token).
# With checkpoint threshold at 25% of 200k = 50k tokens,
# 3 rounds should hit it.
PADDING_CHARS = 80_000
PADDING = ("The quick brown fox jumps over the lazy dog. " * 2000)[:PADDING_CHARS]


def log(msg: str) -> None:
    print(f"[ci_e2e] {msg}", flush=True)


def get_dashboard_state() -> dict | None:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    url = f"https://localhost:{DASHBOARD_PORT}/health"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, context=ctx, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as exc:
        log(f"Dashboard query failed: {exc}")
        return None


def send_message(messages: list[dict], max_tokens: int = 128) -> dict:
    """Send a message through the CONNECT redirector to the API."""
    ctx = ssl.create_default_context()
    if os.path.exists(CA_CERT):
        ctx.load_verify_locations(CA_CERT)

    proxy_handler = urllib.request.ProxyHandler({
        "https": f"http://localhost:{PROXY_PORT}",
    })
    opener = urllib.request.build_opener(
        proxy_handler,
        urllib.request.HTTPSHandler(context=ctx),
    )

    body = json.dumps({
        "model": MODEL,
        "max_tokens": max_tokens,
        "messages": messages,
    }).encode()

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "x-api-key": API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        method="POST",
    )

    with opener.open(req, timeout=60) as resp:
        return json.loads(resp.read())


def extract_text(response: dict) -> str:
    for block in response.get("content", []):
        if block.get("type") == "text":
            return block["text"]
    return ""


def main() -> int:
    if not API_KEY:
        log("ERROR: ANTHROPIC_API_KEY not set")
        return 1

    health = get_dashboard_state()
    if not health or health.get("status") != "ok":
        log(f"ERROR: Proxy not healthy: {health}")
        return 1
    log("Proxy healthy")

    messages: list[dict] = []
    start_time = time.time()

    for round_num in range(MAX_ROUNDS):
        elapsed = time.time() - start_time
        if elapsed > TIMEOUT:
            log(f"TIMEOUT after {elapsed:.0f}s at round {round_num}")
            return 1 if round_num < 3 else 0

        # Each user message includes padding to inflate context fast
        prompt = (
            f"Round {round_num + 1}. Respond with exactly one sentence. "
            f"Ignore the padding below.\n\n{PADDING}"
        )
        messages.append({"role": "user", "content": prompt})

        log(f"Round {round_num + 1}: sending ({len(messages)} messages)...")
        t0 = time.time()

        try:
            response = send_message(messages)
        except Exception as exc:
            log(f"API error: {exc}")
            time.sleep(3)
            try:
                response = send_message(messages)
            except Exception as exc2:
                log(f"API retry failed: {exc2}")
                return 1

        rtt = time.time() - t0

        if "error" in response:
            log(f"API error response: {response['error']}")
            return 1

        assistant_text = extract_text(response)
        messages.append({"role": "assistant", "content": assistant_text})

        usage = response.get("usage", {})
        in_tok = usage.get("input_tokens", 0)
        out_tok = usage.get("output_tokens", 0)
        log(f"  tokens: in={in_tok} out={out_tok} rtt={rtt:.1f}s msgs={len(messages)}")

        state = get_dashboard_state()
        if state:
            log(f"  conversations: {state.get('conversations', 0)}")

    # Final check
    messages.append({"role": "user", "content": "Say 'test complete'."})
    try:
        final = send_message(messages)
        text = extract_text(final)
        if text:
            log(f"Final: {text[:200]}")
            log("SUCCESS: All rounds completed through proxy")
            return 0
        log("WARNING: Empty final response")
        return 1
    except Exception as exc:
        log(f"Final verification failed: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
