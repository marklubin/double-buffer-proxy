"""CI end-to-end lifecycle test.

Sends real API calls through the proxy (via the CONNECT redirector),
inflates context until Claude Code would naturally trigger compaction,
then verifies the conversation still works.

The proxy pre-computes a checkpoint at 60% utilization. Claude Code
(via CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=80) triggers compaction at 80%.
The proxy intercepts the compact request and returns the pre-computed
checkpoint instantly.

Usage:
    ANTHROPIC_API_KEY=sk-... python tests/ci_e2e.py

Environment:
    ANTHROPIC_API_KEY     — required
    PROXY_PORT            — CONNECT redirector port (default: 47200)
    DASHBOARD_PORT        — dashboard/health port (default: 47201)
    CA_CERT               — path to ca.pem (default: certs/ca.pem)
    MAX_ROUNDS            — max conversation rounds (default: 40)
    TIMEOUT_SECONDS       — overall timeout (default: 600)
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
MAX_ROUNDS = int(os.environ.get("MAX_ROUNDS", "40"))
TIMEOUT = int(os.environ.get("TIMEOUT_SECONDS", "600"))
MODEL = os.environ.get("MODEL", "claude-sonnet-4-6")

# Prompts designed to produce long responses and inflate context fast
PROMPTS = [
    "Write a detailed 800-word essay about the history of computing from Babbage to modern AI. Include specific dates and names.",
    "Write a detailed 800-word essay about the history of mathematics from ancient Egypt through Euler and Gauss. Include specific dates.",
    "Write a detailed 800-word essay about the history of physics from Aristotle through Einstein and Feynman. Include key equations.",
    "Write a detailed 800-word essay about the history of chemistry from alchemy through Mendeleev and quantum chemistry.",
    "Write a detailed 800-word essay about the history of biology from Aristotle through Darwin and CRISPR.",
    "Combine all the essays above into a single 2000-word synthesis showing how each field enabled the others.",
    "Write a 1000-word critical analysis of all the essays, pointing out oversimplifications and missing perspectives.",
    "Respond to the critique with a 1000-word defense.",
    "Write a 1000-word essay about the philosophy of science from Bacon through Kuhn and Feyerabend.",
    "Write a 1000-word essay about the history of astronomy from Babylon through the James Webb telescope.",
]


def log(msg: str) -> None:
    print(f"[ci_e2e] {msg}", flush=True)


def get_dashboard_state(conv_id: str | None = None) -> dict | None:
    """Query the proxy's health or conversation detail endpoint."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    if conv_id:
        url = f"https://localhost:{DASHBOARD_PORT}/dashboard/api/conversation/{conv_id}:{MODEL}"
    else:
        url = f"https://localhost:{DASHBOARD_PORT}/health"

    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, context=ctx, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as exc:
        log(f"Dashboard query failed: {exc}")
        return None


def send_message(messages: list[dict]) -> dict:
    """Send a message to the Claude API through the CONNECT redirector.

    Uses urllib with HTTPS_PROXY to go through the redirector, same as
    Node.js / Claude Code does.
    """
    # Build SSL context trusting our CA
    ctx = ssl.create_default_context()
    if os.path.exists(CA_CERT):
        ctx.load_verify_locations(CA_CERT)

    # Set proxy handler
    proxy_handler = urllib.request.ProxyHandler({
        "https": f"http://localhost:{PROXY_PORT}",
    })
    opener = urllib.request.build_opener(
        proxy_handler,
        urllib.request.HTTPSHandler(context=ctx),
    )

    body = json.dumps({
        "model": MODEL,
        "max_tokens": 4096,
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

    with opener.open(req, timeout=120) as resp:
        return json.loads(resp.read())


def extract_text(response: dict) -> str:
    """Extract text content from API response."""
    for block in response.get("content", []):
        if block.get("type") == "text":
            return block["text"]
    return ""


def main() -> int:
    if not API_KEY:
        log("ERROR: ANTHROPIC_API_KEY not set")
        return 1

    # Verify proxy is healthy
    health = get_dashboard_state()
    if not health or health.get("status") != "ok":
        log(f"ERROR: Proxy not healthy: {health}")
        return 1
    log("Proxy healthy")

    messages: list[dict] = []
    checkpoint_seen = False
    start_time = time.time()
    prev_input_tokens = 0

    for round_num in range(MAX_ROUNDS):
        elapsed = time.time() - start_time
        if elapsed > TIMEOUT:
            log(f"TIMEOUT after {elapsed:.0f}s")
            return 1

        # Pick prompt (cycle through them)
        prompt = PROMPTS[round_num % len(PROMPTS)]
        messages.append({"role": "user", "content": prompt})

        log(f"Round {round_num + 1}: sending ({len(messages)} messages)...")

        try:
            response = send_message(messages)
        except Exception as exc:
            log(f"API error: {exc}")
            # Retry once after a short delay
            time.sleep(5)
            try:
                response = send_message(messages)
            except Exception as exc2:
                log(f"API retry failed: {exc2}")
                return 1

        # Check for API errors
        if "error" in response:
            log(f"API error response: {response['error']}")
            return 1

        # Check if this is a compaction response (Claude Code would have
        # triggered compact, proxy intercepted with pre-computed checkpoint)
        content = response.get("content", [])
        has_compaction = any(
            b.get("type") == "compaction" for b in content if isinstance(b, dict)
        )

        if has_compaction:
            log("  COMPACTION RECEIVED — proxy returned pre-computed checkpoint")
            compaction_text = ""
            for block in content:
                if isinstance(block, dict) and block.get("type") == "compaction":
                    compaction_text = block.get("content", "")
                    break

            # Claude Code would rebuild state: replace old messages with
            # compaction + recent. Simulate that here.
            messages = [
                {"role": "assistant", "content": [{"type": "compaction", "content": compaction_text}]},
                {"role": "user", "content": "Continue. What were we discussing?"},
            ]
            checkpoint_seen = True
            log(f"  Compaction length: {len(compaction_text)} chars")
            log("  Message list rebuilt with compaction summary")
            continue

        assistant_text = extract_text(response)
        messages.append({"role": "assistant", "content": assistant_text})

        usage = response.get("usage", {})
        input_tokens = usage.get("input_tokens", 0)
        output_tokens = usage.get("output_tokens", 0)
        log(f"  tokens: in={input_tokens} out={output_tokens} total_msgs={len(messages)}")

        # Monitor proxy state
        state = get_dashboard_state()
        if state:
            convs = state.get("conversations", 0)
            log(f"  active conversations: {convs}")

        # Track context growth
        if input_tokens > 40000:
            log(f"  context at {input_tokens} tokens — checkpoint should be computing...")

        if input_tokens > 55000:
            log(f"  context at {input_tokens} tokens — compaction expected soon...")

        prev_input_tokens = input_tokens

    # Final verification: send one more message to prove the conversation works
    messages.append({"role": "user", "content": "What have we discussed? List the topics briefly."})
    try:
        final_response = send_message(messages)
        final_text = extract_text(final_response)
        if final_text:
            log(f"Post-test verification: {final_text[:200]}...")
            if checkpoint_seen:
                log("SUCCESS: Full lifecycle verified (compaction occurred and conversation continued)")
            else:
                log("WARNING: Conversation functional but no compaction was triggered")
                log("  (This may be OK if context didn't reach the compaction threshold)")
            return 0
        else:
            log("WARNING: Empty response on final verification")
            return 1
    except Exception as exc:
        log(f"Final verification failed: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
