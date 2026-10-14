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
    import json

    from alloc_platform.db.models import PoolModel, ScenarioModel
    from alloc_platform.db.repo import upsert_pool

    init_db()
    db = SessionLocal()
    try:
        if db.query(PoolModel).filter(PoolModel.id == "default_pool").first() is None:
            upsert_pool(db, "default_pool", "Host Primary Pool", cpu=2.0, mem_mb=4096.0)

        scenarios = get_predefined_scenarios()
        for sc_id, sc in scenarios.items():
            if db.query(ScenarioModel).filter(ScenarioModel.id == sc_id).first() is None:
                db.add(
                    ScenarioModel(
                        id=sc_id,
                        name=sc.name,
                        duration_s=sc.duration_s,
                        seed=sc.seed,
                        pool_id="default_pool",
                        spec_json=json.dumps({"name": sc.name}),
                    )
                )
        from alloc_platform.db.models import ExperimentLock

        db.query(ExperimentLock).delete()
        db.commit()
    finally:
        db.close()


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
    import uuid

    from alloc_platform.config import config
    from alloc_platform.db.models import Experiment
    from alloc_platform.orchestrator.runner import execute_experiment_async

    exp_id = f"test_live_fail_{uuid.uuid4().hex[:8]}"

    db = SessionLocal()



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


@pytest.mark.asyncio
async def test_cancellation_cleanup():
    """Verify that cancelling an experiment releases locks and updates status cleanly."""
    import asyncio
    import json
    import uuid

    from alloc_platform.db.models import Experiment, ExperimentLock
    from alloc_platform.orchestrator.runner import cancel_active_experiment, execute_experiment_async

    exp_id = f"test_cancel_{uuid.uuid4().hex[:8]}"
    db = SessionLocal()
    exp = Experiment(
        id=exp_id,
        name="Test Cancel Experiment",
        scenario_id="scenario_1_baseline",
        policies_json=json.dumps(["fcfs", "adaptive"]),
        repeats=2,
        status="pending",
    )
    db.add(exp)
    db.commit()
    db.close()

    task = asyncio.create_task(execute_experiment_async(exp_id))
    await asyncio.sleep(0.05)

    cancelled = cancel_active_experiment(exp_id)
    assert cancelled is True
    await task

    db = SessionLocal()
    updated_exp = db.query(Experiment).filter(Experiment.id == exp_id).first()
    assert updated_exp.status in ("cancelled", "failed")
    lock = db.query(ExperimentLock).filter(ExperimentLock.experiment_id == exp_id).first()
    assert lock is None
    db.close()


@pytest.mark.asyncio
async def test_failed_workload_handling(monkeypatch):
    """Verify that a failed workload execution cleanly records failure and releases locks."""
    import json
    import uuid

    from alloc_platform.db.models import Experiment, ExperimentLock
    from alloc_platform.orchestrator.runner import execute_experiment_async

    exp_id = f"test_fail_{uuid.uuid4().hex[:8]}"
    db = SessionLocal()
    exp = Experiment(
        id=exp_id,
        name="Test Failed Workload",
        scenario_id="scenario_1_baseline",
        policies_json=json.dumps(["fcfs"]),
        repeats=1,
        status="pending",
    )
    db.add(exp)
    db.commit()
    db.close()

    async def fail_run(*args, **kwargs):
        raise RuntimeError("Simulated container crash or OOM during run")

    monkeypatch.setattr("alloc_platform.orchestrator.runner._execute_single_run", fail_run)

    await execute_experiment_async(exp_id)

    db = SessionLocal()
    updated_exp = db.query(Experiment).filter(Experiment.id == exp_id).first()
    assert updated_exp.status == "failed"
    assert "Simulated container crash" in updated_exp.error_message
    lock = db.query(ExperimentLock).filter(ExperimentLock.experiment_id == exp_id).first()
    assert lock is None
    db.close()


def test_orphan_sweep():
    """Verify that sweep_orphans identifies and removes untracked containers."""
    from unittest.mock import MagicMock

    from alloc_platform.enforcement.docker_enforcer import DockerEnforcer

    mock_client = MagicMock()
    c_active = MagicMock()
    c_active.labels = {"project": "alloc", "run_id": "run_active_1"}
    c_orphan = MagicMock()
    c_orphan.labels = {"project": "alloc", "run_id": "run_orphan_99"}
    mock_client.containers.list.return_value = [c_active, c_orphan]

    enforcer = DockerEnforcer(client=mock_client)

    swept = enforcer.sweep_orphans(active_run_ids=["run_active_1"])
    assert swept == 1
    c_orphan.remove.assert_called_once_with(force=True)
    c_active.remove.assert_not_called()

