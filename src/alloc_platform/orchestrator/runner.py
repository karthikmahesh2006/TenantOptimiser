"""Asynchronous experiment execution runner and state machine orchestrator."""

import asyncio
import json
import uuid
from datetime import datetime
from typing import Dict, List, Optional

import numpy as np
from sqlalchemy.orm import Session

from alloc_platform.allocator.adaptive import AdaptiveAllocator
from alloc_platform.allocator.drf import allocate_drf
from alloc_platform.allocator.fcfs import allocate_fcfs
from alloc_platform.allocator.metrics import (
    allocated_util,
    churn,
    count_changes,
    guarantee_satisfaction,
    jain_index,
    satisfaction,
)
from alloc_platform.allocator.proportional import allocate_proportional
from alloc_platform.allocator.types import Plan, Pool, Tenant, TenantObs
from alloc_platform.allocator.validate import check_guarantee_feasibility
from alloc_platform.api.sse import broadcaster
from alloc_platform.config import config
from alloc_platform.db.models import (
    AllocationEvent,
    Experiment,
    HostSample,
    Run,
    TenantSample,
)
from alloc_platform.db.repo import log_audit, save_run_metrics
from alloc_platform.db.session import SessionLocal
from alloc_platform.enforcement.docker_enforcer import DockerEnforcer, TenantContainerSpec
from alloc_platform.orchestrator.lock import acquire_experiment_lock, release_experiment_lock
from alloc_platform.orchestrator.scenario import ScenarioSpec, get_predefined_scenarios
from alloc_platform.orchestrator.state import ExperimentState
from alloc_platform.telemetry.host import HostMonitor

# In-memory cancellation events
_active_cancellations: Dict[str, asyncio.Event] = {}


def cancel_active_experiment(experiment_id: str) -> bool:
    event = _active_cancellations.get(experiment_id)
    if event:
        event.set()
        return True
    return False


async def execute_experiment_async(experiment_id: str) -> None:
    """Entry point for background task running an experiment."""
    db: Session = SessionLocal()
    try:
        exp = db.query(Experiment).filter(Experiment.id == experiment_id).first()
        if not exp:
            return

        if not acquire_experiment_lock(db, experiment_id):
            exp.status = ExperimentState.FAILED.value
            exp.error_message = (
                "Could not acquire host execution lock; another experiment is running."
            )
            db.commit()
            return

        cancel_event = asyncio.Event()
        _active_cancellations[experiment_id] = cancel_event

        exp.status = ExperimentState.PREPARING.value
        db.commit()
        await broadcaster.broadcast("experiment_state", {"id": experiment_id, "state": "preparing"})

        # Load scenario
        scenarios = get_predefined_scenarios()
        scen_spec = scenarios.get(exp.scenario_id, scenarios["scenario_1_baseline"])

        policies: List[str] = json.loads(exp.policies_json)
        rng = np.random.default_rng(scen_spec.seed)

        # Check docker availability
        docker_available = False
        enforcer: Optional[DockerEnforcer] = None
        try:
            enforcer = DockerEnforcer()
            enforcer.client.ping()
            docker_available = True
        except Exception:
            docker_available = False

        host_mon = HostMonitor()

        for rep in range(exp.repeats):
            # Shuffle policy order to eliminate thermal or caching bias
            shuffled_policies = list(policies)
            rng.shuffle(shuffled_policies)

            for pol_name in shuffled_policies:
                if cancel_event.is_set():
                    exp.status = ExperimentState.CANCELLED.value
                    db.commit()
                    return

                run_id = f"run_{uuid.uuid4().hex[:12]}"
                run_row = Run(
                    id=run_id,
                    experiment_id=experiment_id,
                    policy=pol_name,
                    repeat_index=rep,
                    status=ExperimentState.RUNNING.value,
                    is_valid=True,
                )
                db.add(run_row)
                db.commit()

                await broadcaster.broadcast(
                    "run_started",
                    {
                        "experiment_id": experiment_id,
                        "run_id": run_id,
                        "policy": pol_name,
                        "repeat": rep,
                    },
                )

                # Execute single run loop
                await _execute_single_run(
                    db=db,
                    run_id=run_id,
                    policy=pol_name,
                    scen_spec=scen_spec,
                    enforcer=enforcer if docker_available else None,
                    host_mon=host_mon,
                    cancel_event=cancel_event,
                )

        exp.status = ExperimentState.DONE.value
        exp.completed_at = datetime.utcnow()
        db.commit()
        await broadcaster.broadcast("experiment_completed", {"id": experiment_id})
        log_audit(
            db, "experiment_completed", details=f"Experiment {experiment_id} completed successfully"
        )

    except Exception as e:
        db.rollback()
        exp = db.query(Experiment).filter(Experiment.id == experiment_id).first()
        if exp:
            exp.status = ExperimentState.FAILED.value
            exp.error_message = str(e)
            db.commit()
        await broadcaster.broadcast("experiment_failed", {"id": experiment_id, "error": str(e)})
    finally:
        _active_cancellations.pop(experiment_id, None)
        release_experiment_lock(db, experiment_id)
        db.close()


async def _execute_single_run(
    db: Session,
    run_id: str,
    policy: str,
    scen_spec: ScenarioSpec,
    enforcer: Optional[DockerEnforcer],
    host_mon: HostMonitor,
    cancel_event: asyncio.Event,
) -> None:
    provenance = "MEASURED" if enforcer is not None else "SIMULATED"
    active_tenants = [
        Tenant(
            id=t.id,
            name=t.name,
            weight=t.weight,
            priority=t.priority,
            arrival_seq=t.arrival_seq,
            active=t.active,
            demand=dict(t.demand),
            minimum=dict(t.minimum),
            workload_type=t.workload_type,
        )
        for t in scen_spec.tenants
    ]
    curr_pool = Pool(capacity=dict(scen_spec.pool.capacity))
    adaptive_inst = AdaptiveAllocator()

    plans_history: List[Plan] = []
    prev_plan: Optional[Plan] = None
    container_ids: Dict[str, str] = {}

    # Setup containers if docker is live
    if enforcer:
        try:
            enforcer.ensure_network()
            for t in active_tenants:
                cid = enforcer.create_tenant(
                    TenantContainerSpec(
                        tenant_id=t.id,
                        run_id=run_id,
                        cpu_cores=max(0.1, float(t.minimum.get("cpu", 0.1))),
                        mem_mb=max(64.0, float(t.minimum.get("mem_mb", 64.0))),
                    )
                )
                container_ids[t.id] = cid
        except Exception as e:
            # Container creation failed
            run_row = db.query(Run).filter(Run.id == run_id).first()
            if run_row:
                run_row.status = "failed"
                run_row.invalidation_reason = f"Docker setup failed: {str(e)}"
                db.commit()
            return

    try:
        duration = scen_spec.duration_s
        for step in range(duration):
            if cancel_event.is_set():
                break

            step_time = float(step)

            # Apply timed scenario events
            for ev in scen_spec.events:
                if ev.t_sec == step:
                    if ev.event_type == "set_capacity":
                        curr_pool = Pool(
                            capacity=dict(ev.params.get("capacity", curr_pool.capacity))
                        )
                    elif ev.event_type == "set_demand":
                        tid = ev.params.get("tenant_id")
                        for idx, t in enumerate(active_tenants):
                            if t.id == tid:
                                new_dem = dict(ev.params.get("demand", t.demand))
                                active_tenants[idx] = Tenant(
                                    id=t.id,
                                    name=t.name,
                                    weight=t.weight,
                                    priority=t.priority,
                                    arrival_seq=t.arrival_seq,
                                    active=t.active,
                                    demand=new_dem,
                                    minimum=t.minimum,
                                    workload_type=t.workload_type,
                                )
                    elif ev.event_type == "tenant_arrive":
                        tid = ev.params.get("tenant_id")
                        for idx, t in enumerate(active_tenants):
                            if t.id == tid:
                                active_tenants[idx] = Tenant(
                                    id=t.id,
                                    name=t.name,
                                    weight=t.weight,
                                    priority=t.priority,
                                    arrival_seq=t.arrival_seq,
                                    active=True,
                                    demand=t.demand,
                                    minimum=t.minimum,
                                    workload_type=t.workload_type,
                                )
                    elif ev.event_type == "tenant_leave":
                        tid = ev.params.get("tenant_id")
                        for idx, t in enumerate(active_tenants):
                            if t.id == tid:
                                active_tenants[idx] = Tenant(
                                    id=t.id,
                                    name=t.name,
                                    weight=t.weight,
                                    priority=t.priority,
                                    arrival_seq=t.arrival_seq,
                                    active=False,
                                    demand=t.demand,
                                    minimum=t.minimum,
                                    workload_type=t.workload_type,
                                )

            # Gather runtime observation (TenantObs)
            obs: Dict[str, TenantObs] = {}
            for t in active_tenants:
                if not t.active:
                    continue
                # Simulated observation based on previous allocation and nominal demand
                nominal_cpu = float(t.demand.get("cpu", 1.0))
                alloc_cpu = (
                    prev_plan.alloc.get(t.id, {}).get("cpu", float(t.minimum.get("cpu", 0.1)))
                    if prev_plan
                    else float(t.minimum.get("cpu", 0.1))
                )
                used_cpu = min(nominal_cpu * 0.9, alloc_cpu)
                throttle = 0.1 if (nominal_cpu > alloc_cpu + 1e-4) else 0.0
                obs[t.id] = TenantObs(
                    usage={
                        "cpu": round(used_cpu, 4),
                        "mem_mb": round(float(t.demand.get("mem_mb", 256.0)) * 0.8, 2),
                    },
                    throttle_ratio=throttle,
                    backlog=1 if throttle > 0 else 0,
                )

                # Record tenant sample
                t_sample = TenantSample(
                    run_id=run_id,
                    timestamp_s=step_time,
                    tenant_id=t.id,
                    cpu_cores_used=used_cpu,
                    mem_used_mb=float(obs[t.id].usage["mem_mb"]),
                    throttle_ratio=throttle,
                    throttled_usec=int(throttle * 100000),
                    nr_throttled=1 if throttle > 0 else 0,
                    provenance=provenance,
                )
                db.add(t_sample)

            # Sample host metrics
            cpu_pct, mem_used, mem_tot, steal_pct = host_mon.sample_procfs()
            h_sample = HostSample(
                run_id=run_id,
                timestamp_s=step_time,
                host_cpu_pct=cpu_pct,
                host_mem_used_mb=mem_used,
                host_mem_total_mb=mem_tot,
                steal_pct=steal_pct,
                provenance=provenance,
            )
            db.add(h_sample)

            # Steal invalidation safety guard
            if steal_pct > config.max_cpu_steal_pct:
                run_row = db.query(Run).filter(Run.id == run_id).first()
                if run_row:
                    run_row.is_valid = False
                    run_row.invalidation_reason = (
                        f"High host steal {steal_pct:.1f}% exceeded {config.max_cpu_steal_pct}%"
                    )

            # Execute the allocator
            pol = policy.lower()
            if pol == "fcfs":
                plan = allocate_fcfs(curr_pool, active_tenants, obs, prev_plan)
            elif pol in ("proportional", "wp"):
                plan = allocate_proportional(curr_pool, active_tenants, obs, prev_plan)
            elif pol == "drf":
                plan = allocate_drf(curr_pool, active_tenants, obs, prev_plan)
            elif pol == "adaptive":
                plan = adaptive_inst.allocate(
                    curr_pool, active_tenants, obs, prev_plan, now=step_time
                )
            else:
                plan = allocate_proportional(curr_pool, active_tenants, obs, prev_plan)

            plans_history.append(plan)
            prev_plan = plan

            # Enforce limits if real docker
            apply_ms = 0.0
            if enforcer:
                for t in active_tenants:
                    cid = container_ids.get(t.id)
                    if cid and t.id in plan.alloc:
                        p_cpu = plan.alloc[t.id].get("cpu", 0.1)
                        p_mem = plan.alloc[t.id].get("mem_mb", 64.0)
                        res = enforcer.apply_limits(cid, p_cpu, p_mem)
                        apply_ms = max(apply_ms, res.apply_ms)

            # Save allocation events
            for tid, lims in plan.alloc.items():
                ev = AllocationEvent(
                    run_id=run_id,
                    epoch=step,
                    timestamp_s=step_time,
                    tenant_id=tid,
                    alloc_cpu=lims.get("cpu", 0.0),
                    alloc_mem_mb=lims.get("mem_mb", 0.0),
                    solve_ms=plan.solve_ms,
                    apply_ms=apply_ms,
                    provenance=provenance,
                )
                db.add(ev)

            db.commit()

            # Broadcast live epoch telemetry via SSE
            await broadcaster.broadcast(
                "telemetry_epoch",
                {
                    "run_id": run_id,
                    "epoch": step,
                    "policy": policy,
                    "alloc": plan.alloc,
                    "obs": {
                        k: {"cpu": v.usage["cpu"], "mem_mb": v.usage["mem_mb"]}
                        for k, v in obs.items()
                    },
                    "solve_ms": plan.solve_ms,
                },
            )

            # Paced epoch interval
            await asyncio.sleep(config.epoch_interval_s)

        # Compute summary metrics for run
        _compute_and_save_run_metrics(
            db, run_id, plans_history, active_tenants, curr_pool, provenance
        )

        run_row = db.query(Run).filter(Run.id == run_id).first()
        if run_row:
            run_row.status = "completed"
            run_row.completed_at = datetime.utcnow()
            db.commit()

    finally:
        # Guarantee container cleanup in finally
        if enforcer:
            enforcer.stop_and_remove_all(run_id)


def _compute_and_save_run_metrics(
    db: Session,
    run_id: str,
    plans: List[Plan],
    tenants: List[Tenant],
    pool: Pool,
    provenance: str,
) -> None:
    if not plans:
        return

    last_plan = plans[-1]
    active_tenants = [t for t in tenants if t.active]

    satisfactions = [satisfaction(last_plan.alloc.get(t.id, {}), t.demand) for t in active_tenants]
    # Primary: Jain on satisfaction / weight
    primary_values = [s / max(0.1, t.weight) for s, t in zip(satisfactions, active_tenants)]
    jain_primary = jain_index(primary_values)

    # Secondary: Jain on dominant shares
    dom_shares = []
    for t in active_tenants:
        al = last_plan.alloc.get(t.id, {})
        shares = [al.get(r, 0.0) / pool.capacity[r] for r in pool.capacity if pool.capacity[r] > 0]
        dom_shares.append(max(shares) / max(0.1, t.weight) if shares else 0.0)
    jain_secondary = jain_index(dom_shares)

    # Utilization
    flat_allocs = [al for p in plans for al in p.alloc.values()]
    u_alloc = allocated_util(flat_allocs, pool.capacity)

    churn_val = churn(plans, pool.capacity)
    changes_val = count_changes(plans)

    feasible, admitted_ids, _ = check_guarantee_feasibility(pool, tenants)
    minimums = {t.id: t.minimum for t in active_tenants}
    feas_rate, infeas_rate = guarantee_satisfaction(last_plan.alloc, minimums, set(admitted_ids))

    metrics_map = {
        "jain_fairness_primary": jain_primary,
        "jain_fairness_secondary": jain_secondary,
        "mean_satisfaction": float(np.mean(satisfactions)) if satisfactions else 0.0,
        "allocated_util_cpu": u_alloc.get("cpu", 0.0) / max(1, len(plans)),
        "allocated_util_mem": u_alloc.get("mem_mb", 0.0) / max(1, len(plans)),
        "churn": churn_val,
        "changes_count": float(changes_val),
        "guarantee_satisfaction_feasible": feas_rate,
        "guarantee_satisfaction_infeasible": infeas_rate,
        "solve_ms_avg": float(np.mean([p.solve_ms for p in plans])),
    }
    save_run_metrics(db, run_id, metrics_map, provenance=provenance)
