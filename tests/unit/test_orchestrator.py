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


@pytest.mark.asyncio
async def test_alloc_mode_live_failure_when_docker_absent(monkeypatch):
    """Verify that ALLOC_MODE=live fails cleanly when Docker daemon is not available."""
    import json

    from alloc_platform.config import config
    from alloc_platform.db.models import Experiment
    from alloc_platform.orchestrator.runner import execute_experiment_async

    db = SessionLocal()
    exp_id = "test_live_fail"
    existing = db.query(Experiment).filter(Experiment.id == exp_id).first()
    if existing:
        db.delete(existing)
        db.commit()

    exp = Experiment(
        id=exp_id,
        name="Test Live Fail",
        scenario_id="scenario_1_baseline",
        policies_json=json.dumps(["fcfs"]),
        repeats=1,
        status="pending",
    )
    db.add(exp)
    db.commit()
    db.close()

    monkeypatch.setattr(config, "alloc_mode", "live")
    monkeypatch.setattr(config, "docker_host", "tcp://127.0.0.1:9999")

    await execute_experiment_async(exp_id)

    db = SessionLocal()
    updated_exp = db.query(Experiment).filter(Experiment.id == exp_id).first()
    assert updated_exp.status == "failed"
    assert "ALLOC_MODE=live" in updated_exp.error_message
    db.close()

