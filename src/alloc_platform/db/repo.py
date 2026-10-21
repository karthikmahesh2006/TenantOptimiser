"""Data access repositories for pools, tenants, scenarios, experiments, and metrics."""

from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from alloc_platform.db.models import (
    AuditLog,
    Experiment,
    PoolModel,
    RunMetric,
    ScenarioModel,
    TenantModel,
)


def log_audit(
    db: Session, action: str, username: str = "system", details: Optional[str] = None
) -> None:
    audit = AuditLog(action=action, username=username, details=details)
    db.add(audit)
    db.commit()


def get_all_pools(db: Session) -> List[PoolModel]:
    return db.query(PoolModel).all()


def get_pool_by_id(db: Session, pool_id: str) -> Optional[PoolModel]:
    return db.query(PoolModel).filter(PoolModel.id == pool_id).first()


def upsert_pool(db: Session, pool_id: str, name: str, cpu: float, mem_mb: float) -> PoolModel:
    p = get_pool_by_id(db, pool_id)
    if not p:
        p = PoolModel(id=pool_id, name=name, cpu_cores=cpu, mem_mb=mem_mb)
        db.add(p)
    else:
        p.name = name
        p.cpu_cores = cpu
        p.mem_mb = mem_mb
    db.commit()
    db.refresh(p)
    return p


def get_all_tenants(db: Session, active_only: bool = False) -> List[TenantModel]:
    q = db.query(TenantModel)
    if active_only:
        q = q.filter(TenantModel.active.is_(True))
    return q.order_by(TenantModel.arrival_seq.asc()).all()


def get_tenant_by_id(db: Session, tenant_id: str) -> Optional[TenantModel]:
    return db.query(TenantModel).filter(TenantModel.id == tenant_id).first()


def upsert_tenant(
    db: Session,
    tenant_id: str,
    name: str,
    weight: float,
    priority: int,
    arrival_seq: int,
    active: bool,
    demand_cpu: float,
    demand_mem_mb: float,
    min_cpu: float,
    min_mem_mb: float,
    workload_type: str = "mixed",
) -> TenantModel:
    t = get_tenant_by_id(db, tenant_id)
    if not t:
        t = TenantModel(
            id=tenant_id,
            name=name,
            weight=weight,
            priority=priority,
            arrival_seq=arrival_seq,
            active=active,
            demand_cpu=demand_cpu,
            demand_mem_mb=demand_mem_mb,
            min_cpu=min_cpu,
            min_mem_mb=min_mem_mb,
            workload_type=workload_type,
        )
        db.add(t)
    else:
        t.name = name
        t.weight = weight
        t.priority = priority
        t.arrival_seq = arrival_seq
        t.active = active
        t.demand_cpu = demand_cpu
        t.demand_mem_mb = demand_mem_mb
        t.min_cpu = min_cpu
        t.min_mem_mb = min_mem_mb
        t.workload_type = workload_type
    db.commit()
    db.refresh(t)
    return t


def get_all_scenarios(db: Session) -> List[ScenarioModel]:
    return db.query(ScenarioModel).all()


def get_scenario_by_id(db: Session, scenario_id: str) -> Optional[ScenarioModel]:
    return db.query(ScenarioModel).filter(ScenarioModel.id == scenario_id).first()


def get_experiment_by_id(db: Session, experiment_id: str) -> Optional[Experiment]:
    return db.query(Experiment).filter(Experiment.id == experiment_id).first()


def get_all_experiments(db: Session, limit: int = 20) -> List[Experiment]:
    return db.query(Experiment).order_by(Experiment.created_at.desc()).limit(limit).all()


ALLOCATION_DERIVED_METRIC_PREFIXES = (
    "jain_fairness",
    "mean_satisfaction",
    "unmet_demand",
    "allocated_util",
    "idle_reserved",
    "churn",
    "changes_count",
    "guarantee_satisfaction",
)


def is_allocation_derived(metric_name: str) -> bool:
    return any(p in metric_name for p in ALLOCATION_DERIVED_METRIC_PREFIXES)


def get_run_metrics(db: Session, run_id: str) -> Dict[str, float]:
    metrics = db.query(RunMetric).filter(RunMetric.run_id == run_id).all()
    return {m.metric_name: m.metric_value for m in metrics}


def get_run_metrics_detailed(db: Session, run_id: str) -> Dict[str, Dict[str, Any]]:
    metrics = db.query(RunMetric).filter(RunMetric.run_id == run_id).all()
    return {m.metric_name: {"value": m.metric_value, "provenance": m.provenance} for m in metrics}


def save_run_metrics(
    db: Session,
    run_id: str,
    metrics: Dict[str, Any],
    default_provenance: str = "MEASURED",
) -> None:
    for k, v in metrics.items():
        if isinstance(v, (tuple, list)) and len(v) == 2:
            val, prov = float(v[0]), str(v[1])
        else:
            val = float(v)
            if default_provenance == "SIMULATED":
                prov = "SIMULATED"
            elif is_allocation_derived(k):
                prov = "CALCULATED"
            else:
                prov = "MEASURED"
        m = RunMetric(run_id=run_id, metric_name=k, metric_value=val, provenance=prov)
        db.add(m)
    db.commit()
