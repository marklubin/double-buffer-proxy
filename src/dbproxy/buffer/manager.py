"""Per-conversation buffer manager — the core of the double-buffer algorithm.

Tracks token usage, manages state machine transitions, and orchestrates
checkpoint/swap operations for a single conversation.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import structlog

from .checkpoint import find_checkpoint_anchor, run_checkpoint
from .state_machine import BufferPhase, transition
from .swap import build_swap_response

log = structlog.get_logger()


class BufferManager:
    """Manages the double-buffer lifecycle for a single conversation."""

    def __init__(
        self,
        conv_id: str,
        model: str,
        context_window: int,
        checkpoint_threshold: float = 0.70,
        swap_threshold: float = 0.95,
        compact_trigger_tokens: int = 50_000,
    ) -> None:
        self.conv_id = conv_id
        self.model = model
        self.context_window = context_window
        self.checkpoint_threshold = checkpoint_threshold
        self.swap_threshold = swap_threshold
        self.compact_trigger_tokens = compact_trigger_tokens

        self.phase = BufferPhase.IDLE
        self.total_input_tokens: int = 0

        # Checkpoint state
        self.checkpoint_content: str | None = None
        self.checkpoint_anchor_index: int | None = None
        self._checkpoint_task: asyncio.Task[str] | None = None
        # Persists after swap for dashboard visibility
        self.last_checkpoint_content: str | None = None
        self._last_swap_messages: list[dict[str, Any]] = []
        self._last_swap_anchor: int | None = None

        # Latest request metadata for checkpoint calls
        self._auth_headers: dict[str, str] = {}
        self._system: Any | None = None
        self._tools: list[dict[str, Any]] | None = None
        self._all_messages: list[dict[str, Any]] = []

        # Lock for phase transitions
        self._lock = asyncio.Lock()

        # Callback for broadcasting state changes
        self._on_state_change: Any | None = None

    @property
    def utilization(self) -> float:
        """Current context window utilization as a fraction."""
        if self.context_window <= 0:
            return 0.0
        return self.total_input_tokens / self.context_window

    def set_state_change_callback(self, callback: Any) -> None:
        """Set a callback to be invoked on phase transitions."""
        self._on_state_change = callback

    async def _notify_state_change(self) -> None:
        """Notify listeners of a state change."""
        if self._on_state_change:
            try:
                await self._on_state_change(self)
            except Exception:
                log.exception("state_change_callback_error", conv_id=self.conv_id[:16])

    def update_from_request(self, body: dict[str, Any], auth_headers: dict[str, str]) -> None:
        """Update conversation state from an intercepted request."""
        self._auth_headers = auth_headers
        self._system = body.get("system")
        self._tools = body.get("tools")
        self._all_messages = body.get("messages", [])

    def update_tokens(self, usage: dict[str, Any]) -> None:
        """Update token count from a response's usage block."""
        input_tokens = usage.get("input_tokens", 0)
        cache_creation = usage.get("cache_creation_input_tokens", 0)
        cache_read = usage.get("cache_read_input_tokens", 0)
        self.total_input_tokens = input_tokens + cache_creation + cache_read

        log.info(
            "tokens_updated",
            conv_id=self.conv_id[:16],
            total=self.total_input_tokens,
            utilization=f"{self.utilization:.1%}",
            phase=self.phase.value,
        )

    async def evaluate_thresholds(self, http_client: httpx.AsyncClient, upstream_url: str) -> None:
        """Check token thresholds and trigger transitions as needed."""
        async with self._lock:
            util = self.utilization

            if self.phase == BufferPhase.IDLE and util >= self.checkpoint_threshold:
                if util >= self.swap_threshold:
                    # Emergency: jumped past both thresholds in one request.
                    # Run checkpoint synchronously (blocking) and go straight
                    # to SWAP_READY.
                    log.warning(
                        "emergency_skip_to_swap",
                        conv_id=self.conv_id[:16],
                        utilization=f"{util:.1%}",
                    )
                    await self._run_blocking_checkpoint(http_client, upstream_url)
                else:
                    # Normal: crossed checkpoint threshold only.  Start
                    # background checkpoint and continue.
                    self.phase = transition(
                        self.phase, BufferPhase.CHECKPOINT_PENDING,
                        self.conv_id, f"utilization={util:.1%}",
                    )
                    await self._notify_state_change()
                    await self._start_checkpoint(http_client, upstream_url)

            elif self.phase == BufferPhase.CHECKPOINT_PENDING and util >= self.swap_threshold:
                # Emergency: hit 95% before checkpoint started
                log.warning(
                    "emergency_checkpoint",
                    conv_id=self.conv_id[:16],
                    utilization=f"{util:.1%}",
                )
                await self._start_checkpoint(http_client, upstream_url)
                # Wait for it to complete (blocking — degrades to status quo)
                await self._await_checkpoint()

            elif self.phase == BufferPhase.WAL_ACTIVE and util >= self.swap_threshold:
                self.phase = transition(
                    self.phase, BufferPhase.SWAP_READY,
                    self.conv_id, f"utilization={util:.1%}",
                )
                await self._notify_state_change()

            elif self.phase == BufferPhase.CHECKPOINTING:
                # Check if checkpoint completed
                if self._checkpoint_task and self._checkpoint_task.done():
                    await self._finalize_checkpoint()
                    if util >= self.swap_threshold:
                        self.phase = transition(
                            self.phase, BufferPhase.SWAP_READY,
                            self.conv_id, f"utilization={util:.1%}",
                        )
                        await self._notify_state_change()
                elif util >= self.swap_threshold:
                    # Emergency: hit swap threshold while checkpoint running
                    log.warning(
                        "emergency_blocking_checkpoint",
                        conv_id=self.conv_id[:16],
                        utilization=f"{util:.1%}",
                    )
                    await self._await_checkpoint()
                    if self.checkpoint_content and self.phase == BufferPhase.WAL_ACTIVE:
                        self.phase = transition(
                            self.phase, BufferPhase.SWAP_READY,
                            self.conv_id, f"emergency_swap_ready",
                        )
                        await self._notify_state_change()

    async def _run_blocking_checkpoint(
        self, http_client: httpx.AsyncClient, upstream_url: str,
    ) -> None:
        """Run a checkpoint synchronously and transition straight to SWAP_READY.

        Used when utilization jumps past both checkpoint and swap thresholds
        in a single request.  No background task — just block.
        """
        if not self._auth_headers or not self._all_messages:
            log.error("checkpoint_missing_context", conv_id=self.conv_id[:16])
            return

        anchor = find_checkpoint_anchor(self._all_messages)
        if anchor <= 0:
            log.warning("checkpoint_no_valid_anchor", conv_id=self.conv_id[:16])
            return

        self.checkpoint_anchor_index = anchor
        messages_to_checkpoint = self._all_messages[:anchor]

        self.phase = transition(
            self.phase, BufferPhase.CHECKPOINT_PENDING,
            self.conv_id, "emergency_blocking",
        )
        await self._notify_state_change()

        try:
            self.checkpoint_content = await run_checkpoint(
                http_client=http_client,
                upstream_url=upstream_url,
                auth_headers=self._auth_headers,
                model=self.model,
                system=self._system,
                tools=self._tools,
                messages=messages_to_checkpoint,
                compact_trigger_tokens=self.compact_trigger_tokens,
            )
            self.last_checkpoint_content = self.checkpoint_content
        except Exception as exc:
            log.error("blocking_checkpoint_failed", conv_id=self.conv_id[:16], error=str(exc))
            self.phase = transition(
                self.phase, BufferPhase.IDLE,
                self.conv_id, "checkpoint_failed",
            )
            await self._notify_state_change()
            return

        self.phase = transition(
            self.phase, BufferPhase.WAL_ACTIVE,
            self.conv_id, "blocking_checkpoint_complete",
        )
        self.phase = transition(
            self.phase, BufferPhase.SWAP_READY,
            self.conv_id, "emergency_swap_ready",
        )
        await self._notify_state_change()
        log.info(
            "emergency_checkpoint_to_swap",
            conv_id=self.conv_id[:16],
            checkpoint_length=len(self.checkpoint_content or ""),
            anchor_index=self.checkpoint_anchor_index,
        )

    async def _start_checkpoint(self, http_client: httpx.AsyncClient, upstream_url: str) -> None:
        """Launch a background checkpoint task."""
        if self._checkpoint_task and not self._checkpoint_task.done():
            return  # Already running

        if not self._auth_headers or not self._all_messages:
            log.error("checkpoint_missing_context", conv_id=self.conv_id[:16])
            return

        anchor = find_checkpoint_anchor(self._all_messages)
        if anchor <= 0:
            log.warning("checkpoint_no_valid_anchor", conv_id=self.conv_id[:16])
            return

        self.checkpoint_anchor_index = anchor
        messages_to_checkpoint = self._all_messages[:anchor]

        self.phase = transition(
            self.phase, BufferPhase.CHECKPOINTING,
            self.conv_id, f"anchor_index={anchor}",
        )
        await self._notify_state_change()

        self._checkpoint_task = asyncio.create_task(
            run_checkpoint(
                http_client=http_client,
                upstream_url=upstream_url,
                auth_headers=self._auth_headers,
                model=self.model,
                system=self._system,
                tools=self._tools,
                messages=messages_to_checkpoint,
                compact_trigger_tokens=self.compact_trigger_tokens,
            ),
            name=f"checkpoint-{self.conv_id[:16]}",
        )
        # Auto-finalize when checkpoint completes (don't wait for next request)
        self._checkpoint_task.add_done_callback(
            lambda _: asyncio.ensure_future(self._finalize_checkpoint())
        )

    async def _await_checkpoint(self) -> None:
        """Wait for checkpoint task to complete (blocking)."""
        if self._checkpoint_task:
            try:
                await self._checkpoint_task
            except Exception:
                log.exception("checkpoint_failed", conv_id=self.conv_id[:16])
            await self._finalize_checkpoint()

    async def _finalize_checkpoint(self) -> None:
        """Process completed checkpoint task."""
        if not self._checkpoint_task or not self._checkpoint_task.done():
            return

        try:
            self.checkpoint_content = self._checkpoint_task.result()
            self.last_checkpoint_content = self.checkpoint_content
        except Exception as exc:
            log.error("checkpoint_result_error", conv_id=self.conv_id[:16], error=str(exc))
            # Reset to IDLE on failure
            self.phase = transition(
                self.phase, BufferPhase.IDLE,
                self.conv_id, "checkpoint_failed",
            )
            self._checkpoint_task = None
            await self._notify_state_change()
            return

        self._checkpoint_task = None
        self.phase = transition(
            self.phase, BufferPhase.WAL_ACTIVE,
            self.conv_id, "checkpoint_complete",
        )
        await self._notify_state_change()
        log.info(
            "wal_started",
            conv_id=self.conv_id[:16],
            checkpoint_length=len(self.checkpoint_content or ""),
            anchor_index=self.checkpoint_anchor_index,
        )

    async def execute_swap(self, stream: bool) -> dict[str, Any] | list:
        """Execute the buffer swap, returning the synthetic response."""
        async with self._lock:
            if self.phase != BufferPhase.SWAP_READY:
                raise RuntimeError(f"Cannot swap in phase {self.phase.value}")

            self.phase = transition(
                self.phase, BufferPhase.SWAP_EXECUTING,
                self.conv_id, "swap_triggered",
            )
            await self._notify_state_change()

            # Compute WAL: messages after the checkpoint anchor
            wal_messages: list[dict[str, Any]] = []
            if self.checkpoint_anchor_index is not None and self._all_messages:
                wal_messages = self._all_messages[self.checkpoint_anchor_index:]

            response = build_swap_response(
                checkpoint_content=self.checkpoint_content or "",
                model=self.model,
                stream=stream,
                wal_messages=wal_messages,
            )

            log.info(
                "swap_executed",
                conv_id=self.conv_id[:16],
                wal_length=len(wal_messages),
                stream=stream,
            )

            # Snapshot pre-swap state for dashboard visibility
            self._last_swap_messages = list(self._all_messages)
            self._last_swap_anchor = self.checkpoint_anchor_index

            # Reset state
            self.phase = transition(
                self.phase, BufferPhase.IDLE,
                self.conv_id, "swap_complete",
            )
            self.checkpoint_content = None
            self.checkpoint_anchor_index = None
            self.total_input_tokens = 0  # Will be updated from next response
            await self._notify_state_change()

            return response

    async def handle_client_compact(
        self,
        stream: bool,
        http_client: httpx.AsyncClient,
        upstream_url: str,
    ) -> dict[str, Any] | list | None:
        """Handle a client-initiated compact request (/compact).

        Returns a synthetic response if we can handle it, or None to
        forward the native compact.

        Four cases:
        1. SWAP_READY → replay cached compaction
        2. WAL_ACTIVE → early swap (checkpoint is ready)
        3. CHECKPOINTING → await checkpoint, then swap
        4. IDLE/CHECKPOINT_PENDING → forward native compact
        """
        async with self._lock:
            if self.phase == BufferPhase.SWAP_READY:
                # Case 1: swap ready, use our compaction
                pass  # Fall through to swap below

            elif self.phase == BufferPhase.WAL_ACTIVE:
                # Case 2: checkpoint done, promote to swap ready
                self.phase = transition(
                    self.phase, BufferPhase.SWAP_READY,
                    self.conv_id, "client_compact_early_swap",
                )
                await self._notify_state_change()

            elif self.phase == BufferPhase.CHECKPOINTING:
                # Case 3: wait for checkpoint
                log.info("client_compact_awaiting_checkpoint", conv_id=self.conv_id[:16])

        # Release lock for blocking await
        if self.phase == BufferPhase.CHECKPOINTING:
            await self._await_checkpoint()
            if self.checkpoint_content:
                async with self._lock:
                    self.phase = transition(
                        self.phase, BufferPhase.SWAP_READY,
                        self.conv_id, "client_compact_after_checkpoint",
                    )
                    await self._notify_state_change()

        if self.phase == BufferPhase.SWAP_READY:
            log.info("client_compact_intercepted", conv_id=self.conv_id[:16], action="synthetic_swap")
            return await self.execute_swap(stream)

        # Cases 4: no checkpoint available, forward native
        log.info("client_compact_intercepted", conv_id=self.conv_id[:16], action="forward_native")
        return None

    async def reset(self, reason: str = "manual") -> None:
        """Reset conversation state to IDLE."""
        async with self._lock:
            old_phase = self.phase
            if self._checkpoint_task and not self._checkpoint_task.done():
                self._checkpoint_task.cancel()
            self._checkpoint_task = None

            if old_phase != BufferPhase.IDLE:
                self.phase = transition(
                    self.phase, BufferPhase.IDLE,
                    self.conv_id, f"reset:{reason}",
                )

            self.checkpoint_content = None
            self.checkpoint_anchor_index = None
            await self._notify_state_change()

    def to_dict(self) -> dict[str, Any]:
        """Serialize state for dashboard/persistence."""
        return {
            "key": f"{self.conv_id}:{self.model}",
            "conv_id": self.conv_id[:16],
            "model": self.model,
            "phase": self.phase.value,
            "utilization": round(self.utilization, 4),
            "total_input_tokens": self.total_input_tokens,
            "context_window": self.context_window,
            "checkpoint_ready": self.checkpoint_content is not None,
            "checkpoint_anchor_index": self.checkpoint_anchor_index,
            "message_count": len(self._all_messages),
        }

    def to_detail_dict(self) -> dict[str, Any]:
        """Serialize full state including messages for dashboard detail view."""
        anchor = self.checkpoint_anchor_index

        def summarize_msg(msg: dict[str, Any]) -> dict[str, Any]:
            """Extract full message text for dashboard display."""
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            if isinstance(content, str):
                preview = content
            elif isinstance(content, list):
                parts: list[str] = []
                for block in content:
                    if isinstance(block, str):
                        parts.append(block)
                    elif isinstance(block, dict):
                        btype = block.get("type", "unknown")
                        if btype == "text":
                            parts.append(block.get("text", ""))
                        elif btype == "tool_use":
                            name = block.get("name", "?")
                            inp = block.get("input", {})
                            inp_str = json.dumps(inp, indent=2) if isinstance(inp, dict) else str(inp)
                            parts.append(f"[tool_use: {name}]\n{inp_str}")
                        elif btype == "tool_result":
                            rc = block.get("content", "")
                            if isinstance(rc, list):
                                rc = "\n".join(
                                    b.get("text", "")
                                    for b in rc if isinstance(b, dict)
                                )
                            parts.append(f"[tool_result]\n{str(rc)}")
                        elif btype == "compaction":
                            parts.append(f"[compaction]\n{block.get('content', '')}")
                        else:
                            parts.append(f"[{btype}]")
                preview = "\n".join(parts)
            else:
                preview = str(content)
            return {"role": role, "preview": preview}

        messages = [summarize_msg(m) for m in self._all_messages]

        result = self.to_dict()
        # Show current checkpoint content, or the last one if swap already cleared it
        visible_checkpoint = self.checkpoint_content or self.last_checkpoint_content
        result.update({
            "messages": messages,
            "checkpoint_content": visible_checkpoint or "",
            "wal_start_index": anchor,
        })

        # Include pre-swap snapshot if available (shows what was checkpointed vs WAL)
        if self._last_swap_messages:
            result["last_swap"] = {
                "messages": [summarize_msg(m) for m in self._last_swap_messages],
                "wal_start_index": self._last_swap_anchor,
            }

        return result
