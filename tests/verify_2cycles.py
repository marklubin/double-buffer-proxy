#!/usr/bin/env python3
"""Verify 2 complete double-buffer swap cycles with real Claude Code.

Drives a conversation through:
  Cycle 1: IDLE → checkpoint → WAL_ACTIVE → SWAP_READY → swap → IDLE
  Cycle 2: IDLE → checkpoint → WAL_ACTIVE → SWAP_READY → swap → IDLE

Proxy must be running with:
    DBPROXY_CHECKPOINT_THRESHOLD=0.25 DBPROXY_SWAP_THRESHOLD=0.26
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

PROXY_HOST = os.environ.get("E2E_PROXY_HOST", "127.0.0.1")
PROXY_PORT = int(os.environ.get("E2E_PROXY_PORT", "443"))
TMUX_SESSION = os.environ.get("E2E_TMUX_SESSION", "ct")
BASE_URL = f"https://{PROXY_HOST}:{PROXY_PORT}"

SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE


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


def wait_idle(timeout: float = 120) -> bool:
    start = time.time()
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


def log_events() -> list[dict]:
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


def count_swaps() -> int:
    return sum(1 for e in log_events() if e.get("event") == "swap_executed")


def latest_tokens() -> int:
    for e in reversed(log_events()):
        if e.get("event") == "tokens_updated" and e.get("total", 0) > 500:
            return e.get("total", 0)
    return 0


def latest_phase() -> str | None:
    for e in reversed(log_events()):
        if e.get("event") == "request_received" and "haiku" not in e.get("model", ""):
            return e.get("phase")
    return None


PROMPTS = [
    "Write a detailed 1500-word technical analysis of microservice architecture patterns including service mesh, event sourcing, CQRS, and saga patterns. Cover tradeoffs, failure modes, and when to use each pattern. Be extremely thorough.",
    "Write a comprehensive 1500-word comparison of database indexing strategies: B-tree, hash, GIN, GiST, and BRIN indexes. Include concrete examples of queries each optimizes for, storage overhead, and maintenance costs.",
    "Write a detailed 1500-word explanation of distributed consensus algorithms: Raft, Paxos, and PBFT. Cover leader election, log replication, membership changes, and Byzantine fault tolerance.",
    "Write a thorough 1500-word analysis of memory management strategies in systems programming: stack vs heap allocation, garbage collection algorithms, reference counting, and arena allocators.",
    "Write a comprehensive 1500-word guide to TLS 1.3 handshake protocol. Cover cipher suites, key exchange mechanisms, certificate verification, 0-RTT resumption, and compare with TLS 1.2.",
    "Write a detailed 1500-word analysis of container orchestration internals: how Kubernetes scheduling works, pod lifecycle, CNI networking, CSI storage, and the control plane reconciliation loop.",
    "Write a 1500-word deep dive into Linux kernel networking: the packet receive path from NIC interrupt through NAPI, sk_buff, netfilter hooks, and socket delivery.",
    "Write a comprehensive 1500-word explanation of modern CPU cache architecture: L1/L2/L3 cache hierarchies, cache coherence protocols (MESI, MOESI), false sharing, and prefetching strategies.",
]


def run() -> None:
    log("=" * 60)
    log("Double-Buffer Proxy: 2-Cycle Verification")
    log("=" * 60)

    health = api_get("/health")
    if health.get("status") != "ok":
        fail(f"Proxy unhealthy: {health}")
    log(f"Proxy OK")

    # Seed
    log("Seeding conversation...")
    tmux_send("What is 2+2? Reply with just the number.")
    wait_idle(timeout=30)
    log(f"  tokens={latest_tokens()}")

    target_swaps = 2
    prompt_idx = 0
    max_rounds = 120  # safety limit

    for rnd in range(max_rounds):
        swaps = count_swaps()
        tokens = latest_tokens()
        phase = latest_phase()
        log(f"Round {rnd+1}  swaps={swaps}/{target_swaps}  tokens={tokens}  phase={phase}")

        if swaps >= target_swaps:
            log(f"Reached {target_swaps} swaps!")
            break

        tmux_send(PROMPTS[prompt_idx % len(PROMPTS)])
        prompt_idx += 1
        wait_idle(timeout=120)

    # Final check
    swaps = count_swaps()

    # Post-swap verification
    log("Verifying Claude still works...")
    tmux_send("What is 7+7? Reply with just the number.")
    post_ok = wait_idle(timeout=30)

    # Collect phase transitions for report
    transitions = [
        e for e in log_events()
        if e.get("event") == "phase_transition"
    ]

    log("=" * 60)
    log("PHASE TRANSITIONS:")
    for t in transitions:
        log(f"  {t.get('from_phase')} → {t.get('to_phase')}  ({t.get('trigger', '')})")
    log("=" * 60)
    log("RESULTS")
    log(f"  swaps_completed:  {swaps}/{target_swaps}  {'PASS' if swaps >= target_swaps else 'FAIL'}")
    log(f"  post_swap_alive:  {'PASS' if post_ok else 'FAIL'}")
    log(f"  final_tokens:     {latest_tokens()}")
    log(f"  final_phase:      {latest_phase()}")
    log("=" * 60)

    if swaps >= target_swaps and post_ok:
        log("OVERALL: PASS")
    else:
        log("OVERALL: FAIL")
        sys.exit(1)


if __name__ == "__main__":
    run()
