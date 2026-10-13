"""REST API v1 routes for system status, CRUD, experiments, and comparison."""

import asyncio
import json
import platform
import subprocess
import uuid
from datetime import datetime
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from alloc_platform.api.auth import require_admin
from alloc_platform.api.schemas import (
    ExperimentCreate,
    PoolCreate,
    PoolOut,
    StatusResponse,
    TenantCreate,
    TenantOut,
    TenantUpdate,
)
from alloc_platform.api.sse import broadcaster
from alloc_platform.db.models import (
    AllocationEvent,
    Experiment,
    ExperimentLock,
    HostSample,
    Run,
    RunMetric,
    TenantSample,
)
from alloc_platform.db.repo import (
    get_all_experiments,
    get_all_pools,
    get_all_tenants,
    get_experiment_by_id,
    get_run_metrics,
    get_tenant_by_id,
    log_audit,
    upsert_pool,
    upsert_tenant,
)
from alloc_platform.db.session import get_db
from alloc_platform.enforcement.docker_enforcer import DockerEnforcer
from alloc_platform.orchestrator.runner import cancel_active_experiment, execute_experiment_async

api_router = APIRouter(prefix="/api/v1")


def _get_git_sha() -> str:
    try:
        res = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=2
        )
        if res.returncode == 0:
            return res.stdout.strip()
    except Exception:
        pass
    return "unknown"


@api_router.get("/status", response_model=StatusResponse)
def get_system_status(db: Session = Depends(get_db)):
    active_count = db.query(ExperimentLock).count()
    docker_ok = False
    try:
        e = DockerEnforcer()
        e.client.ping()
        docker_ok = True
    except Exception:
        docker_ok = False

    return StatusResponse(
        status="healthy",
        system_time=datetime.utcnow().isoformat(),
        active_experiments=active_count,
        docker_available=docker_ok,
        cgroup_version="v2",
        git_sha=_get_git_sha(),
    )


@api_router.get("/system")
def get_system_details():
    docker_version = "unavailable"
    try:
        e = DockerEnforcer()
        v = e.client.version()
        docker_version = v.get("Version", "unknown")
    except Exception:
        pass

    return {
        "os": platform.system(),
        "release": platform.release(),
        "kernel": platform.version(),
        "architecture": platform.machine(),
        "python": platform.python_version(),
        "docker_version": docker_version,
        "cgroup_version": "v2",
        "git_sha": _get_git_sha(),
    }


# --- Pools CRUD ---


@api_router.get("/pools", response_model=List[PoolOut])
def list_pools(db: Session = Depends(get_db)):
    return get_all_pools(db)


@api_router.post("/pools", response_model=PoolOut)
def create_pool(req: PoolCreate, db: Session = Depends(get_db), _=Depends(require_admin)):
    pool = upsert_pool(db, req.id, req.name, req.cpu_cores, req.mem_mb)
    log_audit(db, "create_pool", details=f"Created pool {req.id}")
    return pool


# --- Tenants CRUD ---


@api_router.get("/tenants", response_model=List[TenantOut])
def list_tenants(db: Session = Depends(get_db)):
    return get_all_tenants(db)


@api_router.post("/tenants", response_model=TenantOut)
def create_tenant(req: TenantCreate, db: Session = Depends(get_db), _=Depends(require_admin)):
    existing = get_tenant_by_id(db, req.id)
    if existing:
        raise HTTPException(status_code=400, detail=f"Tenant {req.id} already exists")
    t = upsert_tenant(
        db,
        req.id,
        req.name,
        req.weight,
        req.priority,
        req.arrival_seq,
        req.active,
        req.demand_cpu,
        req.demand_mem_mb,
        req.min_cpu,
        req.min_mem_mb,
        req.workload_type,
    )
    log_audit(db, "create_tenant", details=f"Created tenant {req.id}")
    return t


@api_router.patch("/tenants/{tenant_id}", response_model=TenantOut)
def update_tenant(
    tenant_id: str, req: TenantUpdate, db: Session = Depends(get_db), _=Depends(require_admin)
):
    t = get_tenant_by_id(db, tenant_id)
    if not t:
        raise HTTPException(status_code=404, detail="Tenant not found")

    name = req.name if req.name is not None else t.name
    weight = req.weight if req.weight is not None else t.weight
    priority = req.priority if req.priority is not None else t.priority
    arrival_seq = req.arrival_seq if req.arrival_seq is not None else t.arrival_seq
    active = req.active if req.active is not None else t.active
    demand_cpu = req.demand_cpu if req.demand_cpu is not None else t.demand_cpu
    demand_mem_mb = req.demand_mem_mb if req.demand_mem_mb is not None else t.demand_mem_mb
    min_cpu = req.min_cpu if req.min_cpu is not None else t.min_cpu
    min_mem_mb = req.min_mem_mb if req.min_mem_mb is not None else t.min_mem_mb
    workload_type = req.workload_type if req.workload_type is not None else t.workload_type

    updated = upsert_tenant(
        db,
        tenant_id,
        name,
        weight,
        priority,
        arrival_seq,
        active,
        demand_cpu,
        demand_mem_mb,
        min_cpu,
        min_mem_mb,
        workload_type,
    )
    log_audit(db, "update_tenant", details=f"Updated tenant {tenant_id}")
    return updated


# --- Experiments ---


@api_router.get("/experiments")
def list_experiments(limit: int = 20, db: Session = Depends(get_db)):
    exps = get_all_experiments(db, limit=limit)
    out = []
    for e in exps:
        runs = db.query(Run).filter(Run.experiment_id == e.id).all()
        out.append(
            {
                "id": e.id,
                "name": e.name,
                "status": e.status,
                "scenario_id": e.scenario_id,
                "policies": json.loads(e.policies_json),
                "repeats": e.repeats,
                "created_at": e.created_at.isoformat(),
                "completed_at": e.completed_at.isoformat() if e.completed_at else None,
                "runs_count": len(runs),
            }
        )
    return out


@api_router.post("/experiments")
def create_experiment(
    req: ExperimentCreate, db: Session = Depends(get_db), _=Depends(require_admin)
):
    exp_id = f"exp_{uuid.uuid4().hex[:10]}"
    exp = Experiment(
        id=exp_id,
        name=req.name,
        scenario_id=req.scenario_id,
        policies_json=json.dumps(req.policies),
        repeats=req.repeats,
        status="pending",
    )
    db.add(exp)
    db.commit()
    log_audit(db, "create_experiment", details=f"Created experiment {exp_id}")
    return {"id": exp_id, "status": "pending"}


@api_router.post("/experiments/{experiment_id}/start")
async def start_experiment(experiment_id: str, db: Session = Depends(get_db), _=Depends(require_admin)):
    exp = get_experiment_by_id(db, experiment_id)
    if not exp:
        raise HTTPException(status_code=404, detail="Experiment not found")
    if exp.status in ("running", "preparing", "warmup"):
        raise HTTPException(status_code=400, detail="Experiment is already running")

    # Launch background runner task
    asyncio.create_task(execute_experiment_async(experiment_id))
    log_audit(db, "start_experiment", details=f"Started experiment {experiment_id}")
    return {"status": "started", "experiment_id": experiment_id}


@api_router.post("/experiments/{experiment_id}/stop")
def stop_experiment(experiment_id: str, db: Session = Depends(get_db), _=Depends(require_admin)):
    cancelled = cancel_active_experiment(experiment_id)
    log_audit(db, "stop_experiment", details=f"Stopped experiment {experiment_id}")
    return {"status": "stopping" if cancelled else "not_running"}


@api_router.post("/experiments/{experiment_id}/reset")
def reset_experiment(experiment_id: str, db: Session = Depends(get_db), _=Depends(require_admin)):
    exp = get_experiment_by_id(db, experiment_id)
    if not exp:
        raise HTTPException(status_code=404, detail="Experiment not found")
    if exp.status in ("running", "preparing"):
        cancel_active_experiment(experiment_id)

    # Delete runs and events
    runs = db.query(Run).filter(Run.experiment_id == experiment_id).all()
    for r in runs:
        db.query(AllocationEvent).filter(AllocationEvent.run_id == r.id).delete()
        db.query(TenantSample).filter(TenantSample.run_id == r.id).delete()
        db.query(HostSample).filter(HostSample.run_id == r.id).delete()
        db.query(RunMetric).filter(RunMetric.run_id == r.id).delete()
    db.query(Run).filter(Run.experiment_id == experiment_id).delete()

    exp.status = "pending"
    exp.completed_at = None
    exp.error_message = None
    db.commit()
    log_audit(db, "reset_experiment", details=f"Reset experiment {experiment_id}")
    return {"status": "reset", "experiment_id": experiment_id}


@api_router.get("/runs/{run_id}/metrics")
def get_single_run_metrics(run_id: str, db: Session = Depends(get_db)):
    metrics = db.query(RunMetric).filter(RunMetric.run_id == run_id).all()
    return [
        {"name": m.metric_name, "value": m.metric_value, "provenance": m.provenance}
        for m in metrics
    ]


@api_router.get("/compare")
def compare_experiments(
    experiment_ids: List[str] = Query(..., alias="experiments"), db: Session = Depends(get_db)
):
    results = {}
    for exp_id in experiment_ids:
        exp = get_experiment_by_id(db, exp_id)
        if not exp:
            continue
        runs = db.query(Run).filter(Run.experiment_id == exp_id).all()
        policy_metrics = {}
        for r in runs:
            m = get_run_metrics(db, r.id)
            if r.policy not in policy_metrics:
                policy_metrics[r.policy] = []
            policy_metrics[r.policy].append(m)
        results[exp_id] = {
            "name": exp.name,
            "status": exp.status,
            "policies": policy_metrics,
        }
    return results


@api_router.post("/demo/start")
async def start_demo_benchmark(db: Session = Depends(get_db), _=Depends(require_admin)):
    """Runs a rapid 3-tenant benchmark across FCFS, WP, and Adaptive."""
    demo_exp_id = f"demo_{uuid.uuid4().hex[:8]}"
    exp = Experiment(
        id=demo_exp_id,
        name="Faculty Demo: 3-Tenant Comparison with Burst",
        scenario_id="demo_scenario",
        policies_json=json.dumps(["fcfs", "proportional", "adaptive"]),
        repeats=1,
        status="pending",
    )
    db.add(exp)
    db.commit()

    asyncio.create_task(execute_experiment_async(demo_exp_id))
    log_audit(db, "demo_started", details=f"Launched demo experiment {demo_exp_id}")
    return {"status": "demo_started", "experiment_id": demo_exp_id}


@api_router.get("/events")
async def sse_event_stream():
    """Server-Sent Events endpoint streaming real-time live telemetry."""

    async def event_generator():
        q = await broadcaster.subscribe()
        try:
            while True:
                msg = await q.get()
                ev_name = msg.get("event", "message")
                data_str = json.dumps(msg.get("data", {}))
                yield f"event: {ev_name}\ndata: {data_str}\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            await broadcaster.unsubscribe(q)

    return StreamingResponse(event_generator(), media_type="text/event-stream")
