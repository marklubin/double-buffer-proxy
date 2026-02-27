"""Buffer phase state machine.

IDLE ──[tokens ≥ 70%]──→ CHECKPOINT_PENDING
                              │
              ┌───────────────┼───────────────┐
              │                               │
    [summary task starts]           [tokens ≥ 95%, emergency]
              │                               │
              v                               │
        CHECKPOINTING                         │
              │                               │
    [summary completes]                       │
              │                               │
              v                               │
        WAL_ACTIVE ←──────────────────────────┘
              │              (blocking summary forced first)
    [tokens ≥ 95%]
              │
              v
        SWAP_READY ──[next request]──→ SWAP_EXECUTING ──→ IDLE
"""

from __future__ import annotations

import enum

import structlog

log = structlog.get_logger()


class BufferPhase(enum.Enum):
    IDLE = "IDLE"
    CHECKPOINT_PENDING = "CHECKPOINT_PENDING"
    CHECKPOINTING = "CHECKPOINTING"
    WAL_ACTIVE = "WAL_ACTIVE"
    SWAP_READY = "SWAP_READY"
    SWAP_EXECUTING = "SWAP_EXECUTING"


# Valid transitions: (from_phase, to_phase)
VALID_TRANSITIONS: set[tuple[BufferPhase, BufferPhase]] = {
    (BufferPhase.IDLE, BufferPhase.CHECKPOINT_PENDING),
    (BufferPhase.CHECKPOINT_PENDING, BufferPhase.CHECKPOINTING),
    (BufferPhase.CHECKPOINT_PENDING, BufferPhase.WAL_ACTIVE),  # emergency: 95% hit before checkpoint started
    (BufferPhase.CHECKPOINTING, BufferPhase.WAL_ACTIVE),
    (BufferPhase.WAL_ACTIVE, BufferPhase.SWAP_READY),
    (BufferPhase.SWAP_READY, BufferPhase.SWAP_EXECUTING),
    (BufferPhase.SWAP_EXECUTING, BufferPhase.IDLE),
    # Reset from any state
    (BufferPhase.CHECKPOINT_PENDING, BufferPhase.IDLE),
    (BufferPhase.CHECKPOINTING, BufferPhase.IDLE),
    (BufferPhase.WAL_ACTIVE, BufferPhase.IDLE),
    (BufferPhase.SWAP_READY, BufferPhase.IDLE),
    (BufferPhase.SWAP_EXECUTING, BufferPhase.IDLE),
}


class InvalidTransition(Exception):
    """Raised when an invalid phase transition is attempted."""

    def __init__(self, from_phase: BufferPhase, to_phase: BufferPhase) -> None:
        self.from_phase = from_phase
        self.to_phase = to_phase
        super().__init__(f"Invalid transition: {from_phase.value} → {to_phase.value}")


def validate_transition(from_phase: BufferPhase, to_phase: BufferPhase) -> None:
    """Validate a phase transition, raising InvalidTransition if not allowed."""
    if (from_phase, to_phase) not in VALID_TRANSITIONS:
        raise InvalidTransition(from_phase, to_phase)


def transition(
    current: BufferPhase,
    target: BufferPhase,
    conv_id: str,
    trigger: str = "",
) -> BufferPhase:
    """Execute a validated phase transition, logging the change."""
    validate_transition(current, target)
    log.info(
        "phase_transition",
        conv_id=conv_id[:16],
        from_phase=current.value,
        to_phase=target.value,
        trigger=trigger,
    )
    return target
