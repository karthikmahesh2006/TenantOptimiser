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
        id="default", name="Default Pool", cpu_cores=2.0, mem_mb=4096.0
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
            "tenants": tenants,
            "active_tenants_count": active_count,
            "total_tenants_count": len(tenants),
            "experiments": experiments,
            "latest_jain": latest_jain,
            "fallback_mode": fallback_mode,
            "simulated_mode": simulated_mode,
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
    request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    fallback_mode, simulated_mode = _get_modes()
    return templates.TemplateResponse(
        request=request,
        name="compare.html",
        context={
            "active_page": "compare",
            "user": user,
            "metrics_summary": {},
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
