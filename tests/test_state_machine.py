"""Tests for buffer state machine."""

import pytest

from dbproxy.buffer.state_machine import (
    BufferPhase,
    InvalidTransition,
    transition,
    validate_transition,
)


class TestValidTransitions:
    def test_idle_to_checkpoint_pending(self):
        validate_transition(BufferPhase.IDLE, BufferPhase.CHECKPOINT_PENDING)

    def test_checkpoint_pending_to_checkpointing(self):
        validate_transition(BufferPhase.CHECKPOINT_PENDING, BufferPhase.CHECKPOINTING)

    def test_checkpointing_to_wal_active(self):
        validate_transition(BufferPhase.CHECKPOINTING, BufferPhase.WAL_ACTIVE)

    def test_wal_active_to_swap_ready(self):
        validate_transition(BufferPhase.WAL_ACTIVE, BufferPhase.SWAP_READY)

    def test_swap_ready_to_swap_executing(self):
        validate_transition(BufferPhase.SWAP_READY, BufferPhase.SWAP_EXECUTING)

    def test_swap_executing_to_idle(self):
        validate_transition(BufferPhase.SWAP_EXECUTING, BufferPhase.IDLE)

    def test_emergency_checkpoint_pending_to_wal_active(self):
        validate_transition(BufferPhase.CHECKPOINT_PENDING, BufferPhase.WAL_ACTIVE)

    def test_reset_from_any_state(self):
        for phase in BufferPhase:
            if phase != BufferPhase.IDLE:
                validate_transition(phase, BufferPhase.IDLE)


class TestInvalidTransitions:
    def test_idle_to_swap_ready(self):
        with pytest.raises(InvalidTransition):
            validate_transition(BufferPhase.IDLE, BufferPhase.SWAP_READY)

    def test_idle_to_wal_active(self):
        with pytest.raises(InvalidTransition):
            validate_transition(BufferPhase.IDLE, BufferPhase.WAL_ACTIVE)

    def test_wal_active_to_checkpointing(self):
        with pytest.raises(InvalidTransition):
            validate_transition(BufferPhase.WAL_ACTIVE, BufferPhase.CHECKPOINTING)

    def test_swap_executing_to_swap_ready(self):
        with pytest.raises(InvalidTransition):
            validate_transition(BufferPhase.SWAP_EXECUTING, BufferPhase.SWAP_READY)


class TestTransition:
    def test_returns_new_phase(self):
        result = transition(
            BufferPhase.IDLE,
            BufferPhase.CHECKPOINT_PENDING,
            conv_id="test123",
            trigger="threshold",
        )
        assert result == BufferPhase.CHECKPOINT_PENDING

    def test_raises_on_invalid(self):
        with pytest.raises(InvalidTransition):
            transition(
                BufferPhase.IDLE,
                BufferPhase.SWAP_READY,
                conv_id="test123",
            )
