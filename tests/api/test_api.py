"""API endpoint, validation, and status tests."""

import pytest
from httpx import ASGITransport, AsyncClient

from alloc_platform.api.main import app
from alloc_platform.db.session import init_db


@pytest.fixture(autouse=True)
def setup_test_db():
    import json

    from alloc_platform.db.models import PoolModel, ScenarioModel
    from alloc_platform.db.repo import upsert_pool
    from alloc_platform.db.session import SessionLocal

    init_db()
    db = SessionLocal()
    try:
        if db.query(PoolModel).filter(PoolModel.id == "default_pool").first() is None:
            upsert_pool(db, "default_pool", "Host Primary Pool", cpu=2.0, mem_mb=4096.0)

        from alloc_platform.orchestrator.scenario import get_predefined_scenarios

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
        db.commit()
    finally:
        db.close()



@pytest.mark.asyncio
async def test_get_status_endpoint():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "healthy"
        assert "git_sha" in data


@pytest.mark.asyncio
async def test_get_system_endpoint():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/system")
        assert resp.status_code == 200
        data = resp.json()
        assert "python" in data
        assert "cgroup_version" in data


@pytest.mark.asyncio
async def test_list_pools_and_tenants():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp_p = await client.get("/api/v1/pools")
        assert resp_p.status_code == 200

        resp_t = await client.get("/api/v1/tenants")
        assert resp_t.status_code == 200
        assert isinstance(resp_t.json(), list)


@pytest.mark.asyncio
async def test_validation_rejections():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Invalid tenant with negative CPU
        bad_payload = {
            "id": "bad_tenant",
            "name": "Bad",
            "weight": 1.0,
            "demand_cpu": -1.0,
            "demand_mem_mb": 512.0,
        }
        resp = await client.post("/api/v1/tenants", json=bad_payload)
        # Auth or validation error
        assert resp.status_code in (401, 403, 422)


@pytest.mark.asyncio
async def test_demo_start_and_execution():
    import asyncio

    from alloc_platform.api.auth import create_session_token
    from alloc_platform.config import config
    from alloc_platform.db.models import Experiment, Run, RunMetric, User
    from alloc_platform.db.session import SessionLocal

    # Seed test admin user
    db = SessionLocal()
    admin = db.query(User).filter(User.username == "test_admin").first()
    if not admin:
        admin = User(username="test_admin", password_hash="dummy_hash", role="admin")
        db.add(admin)
        db.commit()
    db.close()

    cookie = create_session_token("test_admin", "admin")
    transport = ASGITransport(app=app)

    # Accelerate execution for unit test
    orig_interval = config.epoch_interval_s
    config.epoch_interval_s = 0.001
    try:
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
            cookies={config.session_cookie_name: cookie},
        ) as client:
            resp = await client.post("/api/v1/demo/start")
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "demo_started"
            exp_id = data["experiment_id"]

            # Poll for experiment completion (allowing up to 30s if live Docker is running)
            status = "pending"
            for _ in range(150):
                await asyncio.sleep(0.2)
                db = SessionLocal()
                exp = db.query(Experiment).filter(Experiment.id == exp_id).first()
                if exp:
                    status = exp.status
                db.close()
                if status in ("done", "failed"):
                    break

            assert status == "done"


            # Check that runs and metrics were created
            db = SessionLocal()
            runs = db.query(Run).filter(Run.experiment_id == exp_id).all()
            assert len(runs) == 3
            policies = {r.policy for r in runs}
            assert policies == {"fcfs", "proportional", "adaptive"}

            for r in runs:
                metrics = db.query(RunMetric).filter(RunMetric.run_id == r.id).all()
                assert len(metrics) > 0
                m_dict = {m.metric_name: m.metric_value for m in metrics}
                assert "jain_fairness_primary" in m_dict
                assert "allocated_util_cpu" in m_dict
                assert "consumed_util_cpu" in m_dict
                assert "churn" in m_dict
                assert "completed" in m_dict
                assert "throughput" in m_dict
                assert "completion_ratio" in m_dict
                assert "deadline_miss_rate" in m_dict
                assert "mean_latency_ms" in m_dict
                assert "p95_latency_ms" in m_dict
                assert "oom_kills" in m_dict
                assert "unmet_demand_cpu" in m_dict

                for m in metrics:
                    if m.metric_name in ("jain_fairness_primary", "mean_satisfaction", "unmet_demand_cpu", "allocated_util_cpu", "churn"):
                        assert m.provenance in ("CALCULATED", "SIMULATED")
                    elif m.metric_name in ("consumed_util_cpu", "throughput", "mean_latency_ms", "completed", "oom_kills"):
                        assert m.provenance in ("MEASURED", "SIMULATED")
            db.close()
    finally:
        config.epoch_interval_s = orig_interval


@pytest.mark.asyncio
async def test_api_auth_and_roles():
    """Verify API authentication and role separation (unauthenticated vs viewer vs admin)."""
    from alloc_platform.api.auth import create_session_token
    from alloc_platform.config import config
    from alloc_platform.db.models import User
    from alloc_platform.db.session import SessionLocal

    # Seed users
    db = SessionLocal()
    if not db.query(User).filter(User.username == "test_viewer_role").first():
        db.add(User(username="test_viewer_role", password_hash="dummy_hash", role="viewer"))
    if not db.query(User).filter(User.username == "test_admin_role").first():
        db.add(User(username="test_admin_role", password_hash="dummy_hash", role="admin"))
    db.commit()
    db.close()

    transport = ASGITransport(app=app)
    viewer_cookie = create_session_token("test_viewer_role", "viewer")
    admin_cookie = create_session_token("test_admin_role", "admin")

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Unauthenticated request to protected admin endpoint -> 401
        resp_unauth = await client.post(
            "/api/v1/experiments",
            json={"name": "exp", "scenario_id": "scenario_1_baseline", "policies": ["fcfs"], "repeats": 1},
        )
        assert resp_unauth.status_code == 401

        # 2. Viewer role request to read-only endpoint -> 200
        client.cookies.set(config.session_cookie_name, viewer_cookie)
        resp_view_get = await client.get("/api/v1/pools")
        assert resp_view_get.status_code == 200

        # 3. Viewer role request to admin-only mutation -> 403 Forbidden
        resp_view_post = await client.post(
            "/api/v1/experiments",
            json={"name": "exp", "scenario_id": "scenario_1_baseline", "policies": ["fcfs"], "repeats": 1},
        )
        assert resp_view_post.status_code == 403

        # 4. Admin role request to admin-only mutation -> 200 OK
        client.cookies.set(config.session_cookie_name, admin_cookie)
        resp_admin_post = await client.post(
            "/api/v1/experiments",
            json={"name": "admin_exp", "scenario_id": "scenario_1_baseline", "policies": ["fcfs"], "repeats": 1},
        )
        assert resp_admin_post.status_code == 200
        assert "id" in resp_admin_post.json()

