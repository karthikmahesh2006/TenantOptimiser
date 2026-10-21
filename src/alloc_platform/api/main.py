"""FastAPI application entrypoint, middleware, and lifecycle manager."""

import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from alloc_platform.api.auth import bootstrap_admin_user
from alloc_platform.api.routes_api import api_router
from alloc_platform.api.routes_pages import pages_router
from alloc_platform.api.routes_reports import export_router, reports_router
from alloc_platform.config import config
from alloc_platform.db.models import PoolModel, ScenarioModel, TenantModel
from alloc_platform.db.repo import upsert_pool, upsert_tenant
from alloc_platform.db.session import SessionLocal, init_db
from alloc_platform.enforcement.docker_enforcer import DockerEnforcer
from alloc_platform.orchestrator.lock import recover_stale_locks_on_startup
from alloc_platform.orchestrator.scenario import get_predefined_scenarios


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 1. Initialize SQLite schema
    init_db()

    # 1b. If recorded mode, ensure fallback bundle is loaded
    if config.alloc_mode.lower() == "recorded":
        import os
        from scripts.import_fallback import import_bundle
        fb_candidates = [
            os.path.join(config.data_dir, "recorded_fallback.json"),
            "/opt/alloc/current/data/recorded_fallback.json",
            "data/recorded_fallback.json",
        ]
        fb_path = next((p for p in fb_candidates if os.path.exists(p)), None)
        if fb_path:
            try:
                import_bundle(fb_path)
            except Exception:
                pass


    # 2. Recover any stale locks left from prior daemon shutdown
    db = SessionLocal()
    try:
        recover_stale_locks_on_startup(db)

        # 3. Seed default pool and tenants if database is fresh
        if db.query(PoolModel).count() == 0:
            upsert_pool(
                db,
                "default_pool",
                "Host Primary Pool (75% Budget)",
                cpu=config.default_pool_cpu,
                mem_mb=config.default_pool_mem_mb,
            )

        scenarios = get_predefined_scenarios()

        if db.query(TenantModel).count() == 0:
            base_scen = scenarios["scenario_1_baseline"]
            for t in base_scen.tenants:
                upsert_tenant(
                    db,
                    t.id,
                    t.name,
                    t.weight,
                    t.priority,
                    t.arrival_seq,
                    t.active,
                    t.demand.get("cpu", 1.0),
                    t.demand.get("mem_mb", 512.0),
                    t.minimum.get("cpu", 0.1),
                    t.minimum.get("mem_mb", 64.0),
                    t.workload_type,
                )
        # 4. Seed predefined scenarios
        for sc_id, sc in scenarios.items():
            existing_sc = db.query(ScenarioModel).filter(ScenarioModel.id == sc_id).first()
            if not existing_sc:
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

    # 4. Bootstrap admin user credentials
    bootstrap_admin_user()

    # 5. Sweep orphaned containers with project=alloc label
    try:
        enforcer = DockerEnforcer()
        enforcer.sweep_orphans()
    except Exception:
        pass

    yield


app = FastAPI(
    title="Adaptive Multi-Tenant Cloud Resource Allocation Platform",
    version="0.1.0",
    lifespan=lifespan,
)

# Mount static assets
static_path = Path(__file__).parent / "static"
if not static_path.exists():
    candidate = Path.cwd() / "src" / "alloc_platform" / "api" / "static"
    if candidate.exists():
        static_path = candidate
    else:
        static_path.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_path)), name="static")


# Include routers
app.include_router(pages_router)
app.include_router(api_router)
app.include_router(reports_router)
app.include_router(export_router)
