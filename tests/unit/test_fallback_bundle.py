import json
import os
import sys
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from alloc_platform.db.models import Base, Experiment, Run, RunMetric
from scripts.export_fallback import export_bundle


@pytest.fixture
def test_db_with_runs(tmp_path):
    db_path = str(tmp_path / "test_fallback.db")
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()

    # Create completed experiment
    exp = Experiment(
        id="exp_test_fallback",
        name="Fallback Test Experiment",
        scenario_id="scenario_over_declaration",
        status="done",
        policies_json=json.dumps(["adaptive", "drf"]),
        repeats=1,
    )
    session.add(exp)

    # Run 1: normal run
    r1 = Run(
        id="run_norm_1",
        experiment_id="exp_test_fallback",
        policy="adaptive",
        repeat_index=0,
        status="completed",
        is_valid=True,
    )
    session.add(r1)
    session.add(RunMetric(run_id="run_norm_1", metric_name="on_time_ratio", metric_value=0.35, provenance="MEASURED"))
    session.add(RunMetric(run_id="run_norm_1", metric_name="oom_kills", metric_value=0.0, provenance="MEASURED"))

    # Run 2: policy-induced OOM run
    r2 = Run(
        id="run_oom_2",
        experiment_id="exp_test_fallback",
        policy="drf",
        repeat_index=0,
        status="completed",
        is_valid=True,
        invalidation_reason="Container terminated by OOM kill",
    )
    session.add(r2)
    session.add(RunMetric(run_id="run_oom_2", metric_name="on_time_ratio", metric_value=0.0, provenance="MEASURED"))
    session.add(RunMetric(run_id="run_oom_2", metric_name="oom_kills", metric_value=1.0, provenance="MEASURED"))

    session.commit()
    session.close()

    yield db_path, engine


def test_export_fallback_bundle_counts_and_oom_flag(test_db_with_runs, tmp_path, monkeypatch):
    db_path, engine = test_db_with_runs
    monkeypatch.setenv("DB_PATH", db_path)

    # Monkeypatch SessionLocal in export_fallback
    from scripts import export_fallback
    test_session = sessionmaker(bind=engine)
    monkeypatch.setattr(export_fallback, "SessionLocal", test_session)

    bundle_path = str(tmp_path / "exported_bundle.json")
    export_bundle(output_path=bundle_path, experiment_id="all")

    assert os.path.exists(bundle_path)
    with open(bundle_path, "r", encoding="utf-8") as f:
        bundle = json.load(f)

    # Verify bundle structure
    assert "experiments" in bundle
    assert bundle["instance_type"] == "c7i-flex.large"
    assert bundle["run_count"] == 2
    assert bundle["provenance"] == "MEASURED"

    runs = [r for exp in bundle["experiments"] for r in exp["runs"]]
    assert len(runs) == 2

    # Verify OOM flagging
    r1 = next(r for r in runs if r["id"] == "run_norm_1")
    assert r1["is_oom"] is False

    r2 = next(r for r in runs if r["id"] == "run_oom_2")
    assert r2["is_oom"] is True


def test_committed_fallback_bundle_structure():
    bundle_path = "data/recorded_fallback.json"
    if not os.path.exists(bundle_path):
        pytest.skip("recorded_fallback.json not found")

    with open(bundle_path, "r", encoding="utf-8") as f:
        bundle = json.load(f)

    assert "experiments" in bundle
    runs = [r for exp in bundle["experiments"] for r in exp.get("runs", [])]
    assert len(runs) >= 100, f"Expected at least 100 runs in recorded bundle, got {len(runs)}"

    # Confirm policy OOM runs are flagged
    oom_runs = [r for r in runs if r.get("is_oom")]
    assert len(oom_runs) >= 2, "Expected policy OOM runs to be flagged with is_oom=True"
    oom_ids = [r["id"] for r in oom_runs]
    assert "run_732728d8afd2" in oom_ids
    assert "run_6ac917d18c24" in oom_ids
