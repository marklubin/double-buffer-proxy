"""Background summarization via Anthropic's compaction API.

Sends a compaction request with pause_after_compaction=true to get a
summary of messages up to the checkpoint anchor.
"""

from __future__ import annotations

import copy
import json
from typing import Any

import httpx
import structlog

log = structlog.get_logger()

COMPACT_BETA_HEADER = "compact-2026-01-12"


def _strip_compaction_blocks(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert compaction content blocks to text blocks in messages.

    After a swap, the conversation may contain compaction blocks from our
    synthetic response. The API rejects these in checkpoint requests, so
    convert them to plain text.
    """
    has_any = False
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "compaction":
                    has_any = True
                    break
        if has_any:
            break

    if not has_any:
        return messages

    result = copy.deepcopy(messages)
    for msg in result:
        content = msg.get("content")
        if isinstance(content, list):
            for i, block in enumerate(content):
                if isinstance(block, dict) and block.get("type") == "compaction":
                    compaction_text = block.get("content", "")
                    content[i] = {
                        "type": "text",
                        "text": compaction_text or "[conversation summary]",
                    }
    return result


async def run_checkpoint(
    http_client: httpx.AsyncClient,
    upstream_url: str,
    auth_headers: dict[str, str],
    model: str,
    system: Any | None,
    tools: list[dict[str, Any]] | None,
    messages: list[dict[str, Any]],
    compact_trigger_tokens: int = 50_000,
) -> str:
    """Run a background checkpoint via the Anthropic compaction API.

    Returns the compaction content string.

    Raises httpx.HTTPStatusError on API errors.
    """
    # Strip any compaction blocks from previous swaps — API rejects them
    clean_messages = _strip_compaction_blocks(messages)

    # Build the compaction request
    request_body: dict[str, Any] = {
        "model": model,
        "max_tokens": 4096,
        "messages": clean_messages,
        "context_management": {
            "edits": [
                {
                    "type": "compact_20260112",
                    "trigger": {"type": "input_tokens", "value": compact_trigger_tokens},
                    "pause_after_compaction": True,
                }
            ]
        },
    }

    if system is not None:
        request_body["system"] = system
    if tools:
        request_body["tools"] = tools

    # Reuse auth headers from the original request (supports both
    # x-api-key and OAuth bearer token), plus add compact beta.
    headers: dict[str, str] = {"content-type": "application/json"}
    for k, v in auth_headers.items():
        if k.startswith("_"):
            continue  # skip internal metadata like _query_string
        headers[k] = v

    # Ensure compact beta is included (merge with existing anthropic-beta)
    existing_beta = headers.get("anthropic-beta", "")
    if COMPACT_BETA_HEADER not in existing_beta:
        if existing_beta:
            headers["anthropic-beta"] = f"{existing_beta},{COMPACT_BETA_HEADER}"
        else:
            headers["anthropic-beta"] = COMPACT_BETA_HEADER

    # Ensure anthropic-version is set
    if "anthropic-version" not in headers:
        headers["anthropic-version"] = "2023-06-01"

    log.info(
        "checkpoint_started",
        model=model,
        message_count=len(messages),
    )

    # Use same path format as original request, preserving query string
    url = "/v1/messages"
    query_string = auth_headers.get("_query_string", "")
    if query_string:
        url = f"{url}?{query_string}"

    response = await http_client.post(
        url,
        json=request_body,
        headers=headers,
        timeout=120.0,
    )
    if response.status_code != 200:
        log.error(
            "checkpoint_api_error",
            status=response.status_code,
            body=response.text[:500],
            url=url,
        )
    response.raise_for_status()

    result = response.json()

    # Extract compaction content from response
    content_blocks = result.get("content", [])
    for block in content_blocks:
        if block.get("type") == "compaction":
            compaction_text = block.get("content", "")
            log.info(
                "checkpoint_completed",
                compaction_length=len(compaction_text),
                stop_reason=result.get("stop_reason"),
            )
            return compaction_text

    # Shouldn't happen with pause_after_compaction, but handle gracefully
    log.error(
        "checkpoint_no_compaction_block",
        content_types=[b.get("type") for b in content_blocks],
        stop_reason=result.get("stop_reason"),
    )
    raise ValueError(
        f"Compaction response did not contain a compaction block. "
        f"stop_reason={result.get('stop_reason')}, "
        f"content_types={[b.get('type') for b in content_blocks]}"
    )


def find_checkpoint_anchor(messages: list[dict[str, Any]]) -> int:
    """Find a clean message boundary for the checkpoint anchor.

    Returns the index (exclusive) of the last message to include in
    the checkpoint. A "clean boundary" means no pending tool_use
    without a matching tool_result.

    Scans backward from the end to find the last point where all
    tool calls are resolved.
    """
    pending_tool_ids: set[str] = set()

    # Build set of all tool_use IDs and tool_result references
    tool_use_positions: dict[str, int] = {}  # tool_use id → message index
    tool_result_ids: set[str] = set()

    for i, msg in enumerate(messages):
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "tool_use":
                        tool_use_positions[block["id"]] = i
                    elif block.get("type") == "tool_result":
                        tool_result_ids.add(block.get("tool_use_id", ""))

    # Find unresolved tool_use IDs
    unresolved = set(tool_use_positions.keys()) - tool_result_ids

    if not unresolved:
        # All tool calls resolved, can use the full message list
        return len(messages)

    # Find the earliest unresolved tool_use and anchor before it
    earliest_unresolved_idx = min(tool_use_positions[tid] for tid in unresolved)
    return earliest_unresolved_idx
