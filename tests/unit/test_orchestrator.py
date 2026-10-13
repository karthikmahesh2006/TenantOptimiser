"""Unit tests for orchestrator locking and scenario constraints."""

import pytest

from alloc_platform.db.session import SessionLocal, init_db
from alloc_platform.orchestrator.lock import (
    acquire_experiment_lock,
    recover_stale_locks_on_startup,
    release_experiment_lock,
)
from alloc_platform.orchestrator.scenario import get_predefined_scenarios


@pytest.fixture(autouse=True)
def setup_db():
    init_db()


def test_concurrent_experiment_locking():
    """Verify that only 1 experiment can acquire the host execution lock simultaneously."""
    db = SessionLocal()
    try:
        # First acquisition succeeds
        assert acquire_experiment_lock(db, "exp_1") is True

        # Second concurrent acquisition fails
        assert acquire_experiment_lock(db, "exp_2") is False

        # Release first lock
        release_experiment_lock(db, "exp_1")

        # Now second acquisition succeeds
        assert acquire_experiment_lock(db, "exp_2") is True
        release_experiment_lock(db, "exp_2")
    finally:
        db.close()


def test_stale_lock_recovery():
    """Verify stale lock cleanup on startup."""
    db = SessionLocal()
    try:
        acquire_experiment_lock(db, "stale_exp")
        recovered_id = recover_stale_locks_on_startup(db)
        assert recovered_id == "stale_exp"
        # Now lock is free again
        assert acquire_experiment_lock(db, "new_exp") is True
        release_experiment_lock(db, "new_exp")
    finally:
        db.close()


def test_scenario_validation_caps():
    """Verify scenario specs pass validation constraints."""
    scenarios = get_predefined_scenarios()
    for name, scen in scenarios.items():
        # Should not raise ValueError
        scen.validate()
        assert scen.duration_s >= 5
        assert len(scen.tenants) <= 10
