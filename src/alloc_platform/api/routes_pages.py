"""HTML view routes serving Jinja2 templates."""

import os
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from alloc_platform.api.auth import create_session_token, get_current_user, verify_password
from alloc_platform.config import config
from alloc_platform.db.models import PoolModel, Run, User
from alloc_platform.db.repo import get_all_experiments, get_all_tenants, get_run_metrics
from alloc_platform.db.session import get_db

pages_router = APIRouter()

templates_dir = Path(__file__).parent / "templates"
if not templates_dir.exists():
    candidate = Path.cwd() / "src" / "alloc_platform" / "api" / "templates"
    if candidate.exists():
        templates_dir = candidate
templates = Jinja2Templates(directory=str(templates_dir))



def _get_modes() -> tuple[bool, bool]:
    fallback_mode = os.path.exists("data/fallback_active.flag")
    simulated_mode = (config.alloc_mode == "sim") or (not fallback_mode)
    return fallback_mode, simulated_mode


@pages_router.get("/", response_class=HTMLResponse)
def page_overview(
    request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    tenants = get_all_tenants(db)
    active_count = len([t for t in tenants if t.active])
    pool = db.query(PoolModel).first() or PoolModel(
        id="default",
        name="Tenant Pool (75% Budget)",
        cpu_cores=config.default_pool_cpu,
        mem_mb=config.default_pool_mem_mb,
    )
    experiments = get_all_experiments(db, limit=5)

    latest_jain = None
    if experiments:
        latest_run = db.query(Run).filter(Run.experiment_id == experiments[0].id).first()
        if latest_run:
            m = get_run_metrics(db, latest_run.id)
            latest_jain = m.get("jain_fairness_primary")

    fallback_mode, simulated_mode = _get_modes()

    return templates.TemplateResponse(
        request=request,
        name="overview.html",
        context={
            "active_page": "overview",
            "user": user,
            "pool": pool,
            "host_vcpus": config.host_vcpus,
            "host_mem_mb": config.host_mem_mb,
            "pool_cpu_ratio_pct": int(config.default_host_cpu_ratio * 100),
            "pool_mem_ratio_pct": int(config.default_host_mem_ratio * 100),
            "tenants": tenants,
            "active_tenants_count": active_count,
            "total_tenants_count": len(tenants),
            "experiments": experiments,
            "latest_jain": latest_jain,
            "fallback_mode": fallback_mode,
            "simulated_mode": simulated_mode,
            "config": config,
        },
    )


@pages_router.get("/tenants", response_class=HTMLResponse)
def page_tenants(
    request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    tenants = get_all_tenants(db)
    fallback_mode, simulated_mode = _get_modes()
    return templates.TemplateResponse(
        request=request,
        name="tenants.html",
        context={
            "active_page": "tenants",
            "user": user,
            "tenants": tenants,
            "fallback_mode": fallback_mode,
            "simulated_mode": simulated_mode,
        },
    )


@pages_router.get("/experiment-config", response_class=HTMLResponse)
def page_config(request: Request, user: User = Depends(get_current_user)):
    fallback_mode, simulated_mode = _get_modes()
    return templates.TemplateResponse(
        request=request,
        name="experiment_config.html",
        context={
            "active_page": "config",
            "user": user,
            "fallback_mode": fallback_mode,
            "simulated_mode": simulated_mode,
        },
    )


@pages_router.get("/live-console", response_class=HTMLResponse)
def page_console(request: Request, user: User = Depends(get_current_user)):
    fallback_mode, simulated_mode = _get_modes()
    return templates.TemplateResponse(
        request=request,
        name="live_console.html",
        context={
            "active_page": "console",
            "user": user,
            "fallback_mode": fallback_mode,
            "simulated_mode": simulated_mode,
        },
    )


@pages_router.get("/compare", response_class=HTMLResponse)
def page_compare(
    request: Request,
    experiment_id: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    fallback_mode, simulated_mode = _get_modes()

    exp = None
    if experiment_id:
        exp = db.query(Experiment).filter(Experiment.id == experiment_id).first()
    if not exp:
        exp = (
            db.query(Experiment)
            .filter(Experiment.status.in_(["done", "completed"]))
            .order_by(Experiment.created_at.desc())
            .first()
        )

    metrics_summary: dict = {}
    if exp:
        runs = db.query(Run).filter(Run.experiment_id == exp.id).all()
        for r in runs:
            m = get_run_metrics(db, r.id)
            metrics_summary[r.policy] = {
                "jain": m.get("jain_fairness_primary", 0.0),
                "satis": m.get("mean_satisfaction", 0.0),
                "alloc_cpu": m.get("allocated_util_cpu", 0.0) * 100.0,
                "alloc_mem": m.get("allocated_util_mem", 0.0) * 100.0,
                "cons_cpu": m.get("consumed_util_cpu", 0.0) * 100.0,
                "cons_mem": m.get("consumed_util_mem", 0.0) * 100.0,
                "idle_cpu": m.get("idle_reserved_cpu", 0.0),
                "idle_mem": m.get("idle_reserved_mem_mb", 0.0),
                "churn": m.get("churn", 0.0),
                "changes_count": m.get("changes_count", 0.0),
                "solve_ms": m.get("solve_ms_avg", 0.0),
                "solve_epoch_pct": m.get("solve_time_epoch_pct", (m.get("solve_ms_avg", 0.0) / 1000.0) * 100.0),
                "on_time_ratio": m.get("on_time_ratio", 0.0) * 100.0,
                "completed": m.get("completed", 0.0),
                "throughput": m.get("throughput", 0.0),
                "completion_ratio": m.get("completion_ratio", 0.0),
                "throttle_time_ratio": m.get("throttle_time_ratio", 0.0) * 100.0,
                "deadline_miss_rate": m.get("deadline_miss_rate", 0.0) * 100.0,
                "deadline_misses": m.get("deadline_misses", 0.0),
                "mean_latency_ms": m.get("mean_latency_ms", 0.0),
                "p95_latency_ms": m.get("p95_latency_ms", 0.0),
                "censored": m.get("censored", 0.0),
                "excluded_count": m.get("excluded_count", 0.0),
                "oom_kills": m.get("oom_kills", 0.0),
                "non_tenant_cpu": m.get("non_tenant_cpu_cores_avg", 0.0),
            }

    return templates.TemplateResponse(
        request=request,
        name="compare.html",
        context={
            "active_page": "compare",
            "user": user,
            "experiment": exp,
            "metrics_summary": metrics_summary,
            "fallback_mode": fallback_mode,
            "simulated_mode": simulated_mode,
        },
    )


@pages_router.get("/history", response_class=HTMLResponse)
def page_history(
    request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    experiments = get_all_experiments(db, limit=20)
    fallback_mode, simulated_mode = _get_modes()
    return templates.TemplateResponse(
        request=request,
        name="history.html",
        context={
            "active_page": "history",
            "user": user,
            "experiments": experiments,
            "fallback_mode": fallback_mode,
            "simulated_mode": simulated_mode,
        },
    )


@pages_router.get("/architecture", response_class=HTMLResponse)
def page_architecture(request: Request, user: User = Depends(get_current_user)):
    fallback_mode, simulated_mode = _get_modes()
    return templates.TemplateResponse(
        request=request,
        name="architecture.html",
        context={
            "active_page": "architecture",
            "user": user,
            "fallback_mode": fallback_mode,
            "simulated_mode": simulated_mode,
        },
    )


@pages_router.get("/login", response_class=HTMLResponse)
def page_login(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={"error": None},
    )


@pages_router.post("/login")
def action_login(
    request: Request,
    response: Response,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.username == username).first()
    if not user or not verify_password(password, user.password_hash):
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"error": "Invalid username or password"},
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    token = create_session_token(user.username, user.role)
    resp = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    resp.set_cookie(
        key=config.session_cookie_name,
        value=token,
        max_age=config.session_max_age_s,
        httponly=True,
        samesite="strict",
    )
    return resp


@pages_router.get("/logout")
def action_logout():
    resp = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    resp.delete_cookie(key=config.session_cookie_name)
    return resp
