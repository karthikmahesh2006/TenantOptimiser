"""Tests verifying that database metrics, /compare UI, and report exports agree on p95 and excluded counts."""

import csv
import io
import json
import os
import sys
import numpy as np
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from alloc_platform.api.main import app
from alloc_platform.db.models import Base, Experiment, RequestSampleModel, Run, RunMetric
from alloc_platform.db.session import get_db


@pytest.fixture
def consistency_db(tmp_path):
    db_path = str(tmp_path / "consistency.db")
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()

    # Create experiment
    exp = Experiment(
        id="exp_consistency_test",
        name="Consistency Validation Experiment",
        scenario_id="scenario_over_declaration",
        status="done",
        policies_json=json.dumps(["adaptive"]),
        repeats=1,
    )
    session.add(exp)

    run = Run(
        id="run_cons_1",
        experiment_id="exp_consistency_test",
        policy="adaptive",
        repeat_index=0,
        status="completed",
        is_valid=True,
    )
    session.add(run)

    # 10 completed requests (latencies: 100, 200, ..., 1000 ms) -> p95 = 955.0 ms
    # 2 rejected requests (excluded)
    # 1 censored request (excluded)
    latencies = [100.0, 200.0, 300.0, 400.0, 500.0, 600.0, 700.0, 800.0, 900.0, 1000.0]
    expected_p95 = float(np.percentile(latencies, 95))
    expected_mean = float(np.mean(latencies))
    expected_excluded = 3.0

    for i, lat in enumerate(latencies):
        status = "ok" if i < 8 else "deadline_missed"
        session.add(
            RequestSampleModel(
                run_id="run_cons_1",
                arrival_id=f"arr_{i}",
                tenant_id="tenant_c_honest",
                scheduled_time_s=float(i),
                dispatch_time_s=float(i) + 0.002,
                completion_time_s=float(i) + lat / 1000.0,
                latency_ms=lat,
                service_ms=lat - 5.0,
                status=status,
                provenance="MEASURED",
            )
        )

    # Add excluded requests
    session.add(
        RequestSampleModel(
            run_id="run_cons_1",
            arrival_id="arr_rej_1",
            tenant_id="tenant_c_honest",
            scheduled_time_s=11.0,
            dispatch_time_s=11.0,
            completion_time_s=11.0,
            latency_ms=0.0,
            service_ms=0.0,
            status="rejected",
            provenance="MEASURED",
        )
    )
    session.add(
        RequestSampleModel(
            run_id="run_cons_1",
            arrival_id="arr_rej_2",
            tenant_id="tenant_c_honest",
            scheduled_time_s=12.0,
            dispatch_time_s=12.0,
            completion_time_s=12.0,
            latency_ms=0.0,
            service_ms=0.0,
            status="rejected",
            provenance="MEASURED",
        )
    )
    session.add(
        RequestSampleModel(
            run_id="run_cons_1",
            arrival_id="arr_cens_1",
            tenant_id="tenant_c_honest",
            scheduled_time_s=13.0,
            dispatch_time_s=13.0,
            completion_time_s=13.0,
            latency_ms=0.0,
            service_ms=0.0,
            status="timeout",
            provenance="MEASURED",
        )
    )

    # Persist calculated metrics matching standard platform definition
    session.add(RunMetric(run_id="run_cons_1", metric_name="on_time_ratio", metric_value=8.0 / 13.0, provenance="CALCULATED"))
    session.add(RunMetric(run_id="run_cons_1", metric_name="mean_latency_ms", metric_value=expected_mean, provenance="MEASURED"))
    session.add(RunMetric(run_id="run_cons_1", metric_name="p95_latency_ms", metric_value=expected_p95, provenance="MEASURED"))
    session.add(RunMetric(run_id="run_cons_1", metric_name="excluded_count", metric_value=expected_excluded, provenance="MEASURED"))
    session.add(RunMetric(run_id="run_cons_1", metric_name="throughput", metric_value=10.0 / 30.0, provenance="MEASURED"))

    session.commit()
    session.close()

    yield engine, expected_p95, expected_mean, expected_excluded


def test_reports_and_compare_agree_on_p95_and_excluded(consistency_db):
    engine, expected_p95, expected_mean, expected_excluded = consistency_db
    TestingSession = sessionmaker(bind=engine)

    def override_get_db():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)

    try:
        # 1. Test JSON export
        res_json = client.get("/reports/exp_consistency_test.json")
        assert res_json.status_code == 200
        data = res_json.json()
        assert len(data["runs"]) == 1
        run_m = data["runs"][0]["metrics"]
        assert run_m["p95_latency_ms"] == pytest.approx(expected_p95)
        assert run_m["excluded_count"] == pytest.approx(expected_excluded)

        # 2. Test CSV export
        res_csv = client.get("/reports/exp_consistency_test.csv")
        assert res_csv.status_code == 200
        reader = csv.DictReader(io.StringIO(res_csv.text))
        rows = list(reader)
        csv_metrics = {r["metric_name"]: float(r["metric_value"]) for r in rows if r["run_id"] == "run_cons_1"}
        assert csv_metrics["p95_latency_ms"] == pytest.approx(expected_p95)
        assert csv_metrics["excluded_count"] == pytest.approx(expected_excluded)

        # 3. Test Compare Page renders identical p95 and excluded count
        res_compare = client.get("/compare?experiment_id=exp_consistency_test")
        assert res_compare.status_code == 200
        html = res_compare.text
        # Compare page formats: "%.1f"|format(m.p95_latency_ms) and "%.0f"|format(m.excluded_count)
        expected_p95_str = f"{expected_p95:.1f}"
        expected_excl_str = f"[{expected_excluded:.0f} excl]"
        assert expected_p95_str in html, f"Expected {expected_p95_str} in compare HTML"
        assert expected_excl_str in html, f"Expected {expected_excl_str} in compare HTML"

        # 4. Test Markdown export renders identical p95 and excluded count
        res_md = client.get("/reports/exp_consistency_test.md")
        assert res_md.status_code == 200
        md_text = res_md.text
        assert expected_p95_str in md_text, f"Expected {expected_p95_str} in markdown report"
        assert expected_excl_str in md_text, f"Expected {expected_excl_str} in markdown report"

    finally:
        app.dependency_overrides.clear()
