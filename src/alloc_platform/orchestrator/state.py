"""Orchestrator state machine definitions."""

from enum import Enum


class ExperimentState(str, Enum):
    IDLE = "idle"
    PREPARING = "preparing"
    WARMUP = "warmup"
    RUNNING = "running"
    COOLDOWN = "cooldown"
    CLEANUP = "cleanup"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"
