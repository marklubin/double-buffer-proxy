#!/usr/bin/env python3
"""VM E2E harness: orchestrates a real Claude Code session through the proxy.

Sends messages to Claude Code via tmux, polls proxy logs for phase
transitions, and verifies the Synix compaction lifecycle.

Usage (from inside the VM):
    cd ~/double-buffer-proxy && uv run python tests/e2e_harness.py

Prerequisites:
    - Proxy running on port 443:
        sudo -E SYNIX_HOST=0.0.0.0 ~/.local/bin/uv run -m dbproxy --log-level DEBUG
    - Claude Code running in tmux session 'ct'
    - /etc/hosts: 127.0.0.1 api.anthropic.com
    - NODE_EXTRA_CA_CERTS set in the tmux session

Note on thresholds:
    The compaction API requires trigger.value >= 50000 tokens. This means
    the conversation must accumulate at least 50k input tokens before a
    checkpoint can succeed. Recommended proxy config for testing:

        SYNIX_CHECKPOINT_THRESHOLD=0.25 SYNIX_SWAP_THRESHOLD=0.26

    This triggers checkpoint at 50k tokens (25% of 200k) and swap at 52k.
    Claude Code's system prompt + tools ≈ 19k base tokens, so reaching
    50k requires ~10-15 rounds of verbose prompts (~3k tokens/round).
    Total runtime: ~15-20 minutes.
"""

from __future__ import annotations

import json
import os
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.request

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PROXY_HOST = os.environ.get("E2E_PROXY_HOST", "127.0.0.1")
PROXY_PORT = int(os.environ.get("E2E_PROXY_PORT", "443"))
TMUX_SESSION = os.environ.get("E2E_TMUX_SESSION", "ct")
MAX_ROUNDS = int(os.environ.get("E2E_MAX_ROUNDS", "60"))

BASE_URL = f"https://{PROXY_HOST}:{PROXY_PORT}"

SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def fail(msg: str) -> None:
    log(f"FAIL: {msg}")
    sys.exit(1)


def api_get(path: str) -> dict:
    url = f"{BASE_URL}{path}"
    try:
        with urllib.request.urlopen(url, context=SSL_CTX, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as exc:
        fail(f"GET {path} failed: {exc}")
        return {}


def tmux_send(text: str) -> None:
    """Send text to tmux and submit with C-m."""
    if len(text) < 800:
        subprocess.run(
            ["tmux", "send-keys", "-t", TMUX_SESSION, "-l", text],
            check=True,
        )
    else:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write(text)
            tmp = f.name
        try:
            subprocess.run(["tmux", "load-buffer", tmp], check=True)
            subprocess.run(["tmux", "paste-buffer", "-t", TMUX_SESSION], check=True)
        finally:
            os.unlink(tmp)
    subprocess.run(["tmux", "send-keys", "-t", TMUX_SESSION, "C-m"], check=True)


def tmux_alive() -> bool:
    return subprocess.run(
        ["tmux", "has-session", "-t", TMUX_SESSION],
        capture_output=True,
    ).returncode == 0


def log_events() -> list[dict]:
    """Parse JSON lines from the proxy log."""
    out = []
    try:
        with open("/tmp/proxy.log") as f:
            for line in f:
                try:
                    out.append(json.loads(line.strip()))
                except (json.JSONDecodeError, ValueError):
                    pass
    except FileNotFoundError:
        pass
    return out


def seen_events(names: set[str]) -> list[dict]:
    return [e for e in log_events() if e.get("event") in names]


def latest_phase() -> str | None:
    for e in reversed(log_events()):
        if e.get("event") == "request_received" and e.get("model") != "claude-haiku-4-5-20251001":
            return e.get("phase")
    return None


def latest_tokens() -> int:
    """Get the most recent input token count from proxy logs."""
    for e in reversed(log_events()):
        if e.get("event") == "tokens_updated" and e.get("total", 0) > 500:
            return e.get("total", 0)
    return 0


def wait_idle(timeout: float = 120) -> bool:
    """Wait until Claude Code is idle (no spinner, no queue)."""
    start = time.time()
    # Wait for processing to start
    started = False
    while time.time() - start < min(15, timeout):
        r = subprocess.run(
            ["tmux", "capture-pane", "-t", TMUX_SESSION, "-p"],
            capture_output=True, text=True,
        )
        if any(s in r.stdout for s in ("✽", "⏳", "Thinking", "Churned", "queued")):
            started = True
            break
        time.sleep(1)
    if not started:
        time.sleep(3)

    # Wait for processing to finish
    while time.time() - start < timeout:
        r = subprocess.run(
            ["tmux", "capture-pane", "-t", TMUX_SESSION, "-p"],
            capture_output=True, text=True,
        )
        busy = any(s in r.stdout for s in ("✽", "⏳", "Thinking", "Churned", "queued"))
        if not busy and "bypass permissions" in r.stdout:
            return True
        time.sleep(2)
    return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run() -> None:
    log("=" * 50)
    log("Synix E2E Harness")
    log("=" * 50)

    # Preflight
    health = api_get("/health")
    if health.get("status") != "ok":
        fail(f"Proxy unhealthy: {health}")
    log(f"Proxy OK (convs={health.get('conversations', 0)})")

    if not tmux_alive():
        fail(f"tmux session '{TMUX_SESSION}' not found")
    log("tmux session OK")

    # Seed conversation
    log("Sending seed message...")
    tmux_send("What is 2+2? Reply with just the number.")
    wait_idle(timeout=30)
    log(f"  phase={latest_phase()} tokens={latest_tokens()}")

    CHECKPOINT_EVENTS = {
        "checkpoint_started", "wal_started",
        "checkpoint_completed", "emergency_checkpoint_to_swap",
    }
    SWAP_EVENTS = {"swap_executed", "synthetic_response_sent"}

    checkpoint_ok = False
    swap_ok = False

    # Verbose prompts that produce long responses to inflate context faster.
    # Each response adds ~2000-5000 tokens to conversation history.
    prompts = [
        "Write a detailed 1500-word technical analysis of microservice architecture patterns including service mesh, event sourcing, CQRS, and saga patterns. Cover tradeoffs, failure modes, and when to use each pattern. Be extremely thorough.",
        "Write a comprehensive 1500-word comparison of database indexing strategies: B-tree, hash, GIN, GiST, and BRIN indexes. Include concrete examples of queries each optimizes for, storage overhead, and maintenance costs. Be very detailed.",
        "Write a detailed 1500-word explanation of distributed consensus algorithms: Raft, Paxos, and PBFT. Cover leader election, log replication, membership changes, and Byzantine fault tolerance. Include specific message flow examples.",
        "Write a thorough 1500-word analysis of memory management strategies in systems programming: stack vs heap allocation, garbage collection algorithms (mark-sweep, generational, concurrent), reference counting, and arena allocators. Include performance characteristics.",
        "Write a comprehensive 1500-word guide to TLS 1.3 handshake protocol. Cover cipher suites, key exchange mechanisms, certificate verification, 0-RTT resumption, and compare with TLS 1.2. Include the exact message sequence.",
        "Write a detailed 1500-word analysis of container orchestration internals: how Kubernetes scheduling works, pod lifecycle, CNI networking, CSI storage, and the control plane reconciliation loop. Be thorough.",
        "Write a 1500-word deep dive into Linux kernel networking: the packet receive path from NIC interrupt through NAPI, sk_buff, netfilter hooks, and socket delivery. Cover XDP and eBPF optimizations.",
        "Write a comprehensive 1500-word explanation of modern CPU cache architecture: L1/L2/L3 cache hierarchies, cache coherence protocols (MESI, MOESI), false sharing, prefetching strategies, and their impact on software performance.",
    ]

    for i in range(MAX_ROUNDS):
        tokens = latest_tokens()
        phase = latest_phase()
        log(f"Round {i+1}/{MAX_ROUNDS}  phase={phase}  tokens={tokens}")

        if not checkpoint_ok and seen_events(CHECKPOINT_EVENTS):
            checkpoint_ok = True
            log("  >> CHECKPOINT detected")
        if not swap_ok and seen_events(SWAP_EVENTS):
            swap_ok = True
            log("  >> SWAP detected")
        if swap_ok:
            break

        tmux_send(prompts[i % len(prompts)])
        wait_idle(timeout=120)

    # Final check
    if not checkpoint_ok and seen_events(CHECKPOINT_EVENTS):
        checkpoint_ok = True
    if not swap_ok and seen_events(SWAP_EVENTS):
        swap_ok = True

    # Post-swap verification
    if swap_ok:
        log("Verifying Claude works after swap...")
        tmux_send("What is 7+7? Reply with just the number.")
        if wait_idle(timeout=30):
            log("Claude responded after swap OK")
        else:
            log("WARNING: Claude did not respond after swap")

    # Report
    health = api_get("/health")
    log("=" * 50)
    log("RESULTS")
    log(f"  proxy_healthy:       {'PASS' if health.get('status') == 'ok' else 'FAIL'}")
    log(f"  checkpoint_detected: {'PASS' if checkpoint_ok else 'FAIL'}")
    log(f"  swap_detected:       {'PASS' if swap_ok else 'FAIL'}")
    log(f"  final_tokens:        {latest_tokens()}")
    log(f"  phase={latest_phase()}")
    log("=" * 50)

    if checkpoint_ok and swap_ok:
        log("OVERALL: PASS")
    elif checkpoint_ok:
        log("OVERALL: PARTIAL (checkpoint OK, swap not reached)")
        log("Swap requires more context inflation or lower thresholds.")
        sys.exit(1)
    else:
        log("OVERALL: FAIL")
        log("Check /tmp/proxy.log for details")
        sys.exit(1)


if __name__ == "__main__":
    run()
