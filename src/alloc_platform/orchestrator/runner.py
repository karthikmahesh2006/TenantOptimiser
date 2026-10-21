"""Asynchronous experiment execution runner and state machine orchestrator."""

import asyncio
import json
import os
import uuid
from datetime import datetime
from typing import Dict, List, Optional

import numpy as np
from sqlalchemy.orm import Session

from alloc_platform.allocator.adaptive import AdaptiveAllocator, AdaptiveParams
from alloc_platform.allocator.drf import allocate_drf
from alloc_platform.allocator.fcfs import allocate_fcfs
from alloc_platform.allocator.metrics import (
    allocated_util,
    churn,
    consumed_util,
    count_changes,
    guarantee_satisfaction,
    idle_reserved,
    jain_index,
    satisfaction,
    tenant_churn,
    tenant_count_changes,
    unmet_demand,
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
    RequestSampleModel,
    Run,
    TenantSample,
)
from alloc_platform.db.repo import log_audit, save_run_metrics
from alloc_platform.db.session import SessionLocal
from alloc_platform.enforcement.cgroup import CgroupReader
from alloc_platform.enforcement.docker_enforcer import DockerEnforcer, TenantContainerSpec
from alloc_platform.orchestrator.lock import acquire_experiment_lock, release_experiment_lock
from alloc_platform.orchestrator.scenario import ScenarioSpec, get_predefined_scenarios
from alloc_platform.orchestrator.state import ExperimentState
from alloc_platform.telemetry.host import HostMonitor
from alloc_platform.workload.loadgen import RequestSample, run_load
from alloc_platform.workload.traces import build_trace

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

        # Check execution mode: live | recorded | sim | auto
        alloc_mode = config.alloc_mode.lower()
        if alloc_mode not in ("live", "recorded", "sim", "auto"):
            raise ValueError(f"Invalid ALLOC_MODE={config.alloc_mode}. Must be 'live', 'recorded', 'sim', or 'auto'.")

        docker_available = False
        enforcer: Optional[DockerEnforcer] = None
        try:
            enforcer = DockerEnforcer()
            enforcer.client.ping()
            docker_available = True
        except Exception:
            docker_available = False


        if alloc_mode == "live":
            if not docker_available:
                raise RuntimeError(
                    f"ALLOC_MODE=live requires active Docker daemon at {config.docker_host}, "
                    "but connection failed. Run aborted."
                )
            import os
            if os.name != "nt" and not os.path.exists(os.path.join(config.cgroup_fs_root, "cgroup.controllers")):
                raise RuntimeError(
                    f"ALLOC_MODE=live requires cgroup v2 mount at {config.cgroup_fs_root}, "
                    "but cgroup.controllers was not found. Run aborted."
                )
            active_enforcer = enforcer
        elif alloc_mode in ("recorded", "sim"):
            active_enforcer = None
        else:  # "auto" for local development
            active_enforcer = enforcer if docker_available else None

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
                        "simulated": active_enforcer is None,
                    },
                )

                # Execute single run loop
                await _execute_single_run(
                    db=db,
                    run_id=run_id,
                    policy=pol_name,
                    scen_spec=scen_spec,
                    enforcer=active_enforcer,
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

    # Ablation configuration
    pol = policy.lower()
    adaptive_params = AdaptiveParams()
    if "no_smoothing" in pol:
        adaptive_params.ewma_alpha = 1.0
        adaptive_params.headroom_h = 0.0
    elif "no_estimation_declared" in pol or "no_est" in pol:
        adaptive_params.use_estimation = False
    if "no_hysteresis" in pol or "no_hyst" in pol:
        adaptive_params.use_hysteresis = False
    if "no_surplus" in pol:
        adaptive_params.use_surplus = False
    adaptive_inst = AdaptiveAllocator(params=adaptive_params)

    plans_history: List[Plan] = []
    usages_history: List[List[Dict[str, float]]] = []
    prev_plan: Optional[Plan] = None
    container_ids: Dict[str, str] = {}
    tenant_endpoints: Dict[str, str] = {}

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

            # Resolve tenant container IP addresses
            for t in active_tenants:
                cid = container_ids.get(t.id)
                if cid:
                    try:
                        c_obj = enforcer.client.containers.get(cid)
                        c_obj.reload()
                        ip = (
                            c_obj.attrs.get("NetworkSettings", {})
                            .get("Networks", {})
                            .get(config.tenant_network_name, {})
                            .get("IPAddress")
                        )
                        if ip:
                            tenant_endpoints[t.id] = f"http://{ip}:8000"
                    except Exception:
                        pass
        except Exception as e:
            # Container creation failed
            run_row = db.query(Run).filter(Run.id == run_id).first()
            if run_row:
                run_row.status = "failed"
                run_row.invalidation_reason = f"Docker setup failed: {str(e)}"
                db.commit()
            return

    # Build reproducible open-loop workload trace using calibrated rates if available
    calibrated_rates_path = os.path.join(config.data_dir, "calibrated_rates.json")
    calibrated_rates: Dict[str, float] = {}
    if os.path.exists(calibrated_rates_path):
        try:
            with open(calibrated_rates_path, "r", encoding="utf-8") as f:
                cal_data = json.load(f)
                calibrated_rates = cal_data.get("calibrated_rates", {})
        except Exception:
            calibrated_rates = {}

    trace_rate = calibrated_rates if calibrated_rates else 2.0
    trace = build_trace(
        active_tenants,
        duration_s=scen_spec.duration_s,
        seed=scen_spec.seed,
        base_rate_rps=trace_rate,
    )
    load_stop_event = asyncio.Event()
    load_task: Optional[asyncio.Task] = None
    if tenant_endpoints:
        load_task = asyncio.create_task(
            run_load(trace, tenant_endpoints, run_id, load_stop_event)
        )

    prev_cgroup_stats: Dict[str, Dict[str, float]] = {}
    oom_kills_per_tenant: Dict[str, int] = {t.id: 0 for t in active_tenants}
    cgroup_usec_sum: Dict[str, float] = {t.id: 0.0 for t in active_tenants}
    cgroup_throttled_sum: Dict[str, float] = {t.id: 0.0 for t in active_tenants}
    usages_by_tenant_history: List[Dict[str, Dict[str, float]]] = []
    non_tenant_cores_history: List[float] = []
    alloc_history_by_epoch: List[Dict[str, float]] = []
    throttled_while_spare_epochs = 0
    max_steal_pct: float = 0.0
    prev_step_time = 0.0

    try:
        duration = scen_spec.duration_s
        for step in range(duration):
            if cancel_event.is_set():
                break

            step_time = float(step)
            dt = max(0.1, step_time - prev_step_time) if step > 0 else 1.0
            prev_step_time = step_time

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

                cid = container_ids.get(t.id)
                cgroup_stats = None
                if cid:
                    try:
                        reader = CgroupReader(cid, config.cgroup_fs_root)
                        cgroup_stats = reader.read_stats()
                    except Exception:
                        cgroup_stats = None

                if cgroup_stats and cgroup_stats.get("usage_usec", 0.0) > 0:
                    prev_st = prev_cgroup_stats.get(t.id, {})
                    delta_usec = max(0.0, cgroup_stats["usage_usec"] - prev_st.get("usage_usec", 0.0))
                    used_cpu = delta_usec / (dt * 1_000_000.0)
                    used_mem = cgroup_stats.get("memory_current_mb", 64.0)

                    delta_throttled = max(0.0, cgroup_stats["throttled_usec"] - prev_st.get("throttled_usec", 0.0))
                    throttle_ratio = min(1.0, delta_throttled / max(1.0, delta_usec + delta_throttled))
                    nr_throttled = int(max(0.0, cgroup_stats["nr_throttled"] - prev_st.get("nr_throttled", 0.0)))
                    oom_kills_per_tenant[t.id] = int(cgroup_stats.get("oom_kill", 0.0))
                    prev_cgroup_stats[t.id] = dict(cgroup_stats)
                    cgroup_usec_sum[t.id] = cgroup_usec_sum.get(t.id, 0.0) + delta_usec
                    cgroup_throttled_sum[t.id] = cgroup_throttled_sum.get(t.id, 0.0) + delta_throttled
                else:
                    nominal_cpu = float(t.demand.get("cpu", 1.0))
                    alloc_cpu = (
                        prev_plan.alloc.get(t.id, {}).get("cpu", float(t.minimum.get("cpu", 0.1)))
                        if prev_plan
                        else float(t.minimum.get("cpu", 0.1))
                    )
                    used_cpu = min(nominal_cpu * 0.9, alloc_cpu)
                    throttle_ratio = 0.1 if (nominal_cpu > alloc_cpu + 1e-4) else 0.0
                    delta_throttled = throttle_ratio * 100000.0
                    delta_usec = used_cpu * dt * 1_000_000.0
                    nr_throttled = 1 if throttle_ratio > 0 else 0
                    used_mem = round(float(t.demand.get("mem_mb", 256.0)) * 0.8, 2)
                    cgroup_usec_sum[t.id] = cgroup_usec_sum.get(t.id, 0.0) + delta_usec
                    cgroup_throttled_sum[t.id] = cgroup_throttled_sum.get(t.id, 0.0) + delta_throttled

                obs[t.id] = TenantObs(
                    usage={
                        "cpu": round(used_cpu, 4),
                        "mem_mb": round(used_mem, 2),
                    },
                    throttle_ratio=round(throttle_ratio, 4),
                    backlog=1 if throttle_ratio > 0 else 0,
                )

                # Record tenant sample
                t_sample = TenantSample(
                    run_id=run_id,
                    timestamp_s=step_time,
                    tenant_id=t.id,
                    cpu_cores_used=round(used_cpu, 4),
                    mem_used_mb=round(used_mem, 2),
                    throttle_ratio=round(throttle_ratio, 4),
                    throttled_usec=int(delta_throttled),
                    nr_throttled=nr_throttled,
                    provenance=provenance,
                )
                db.add(t_sample)

            # Record step usages for consumed utilisation audit
            step_usages = [dict(obs[t.id].usage) for t in active_tenants if t.active and t.id in obs]
            usages_history.append(step_usages)
            usages_by_tenant_history.append(
                {t.id: dict(obs[t.id].usage) for t in active_tenants if t.active and t.id in obs}
            )

            # Sample host metrics
            cpu_pct, mem_used, mem_tot, steal_pct = host_mon.sample_procfs()
            max_steal_pct = max(max_steal_pct, steal_pct)
            host_cores_used = (cpu_pct / 100.0) * config.host_vcpus
            tenant_cores_used = sum(obs[t.id].usage["cpu"] for t in active_tenants if t.active and t.id in obs)
            non_tenant_cores = max(0.0, host_cores_used - tenant_cores_used)
            non_tenant_cores_history.append(non_tenant_cores)

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
            if pol == "fcfs":
                plan = allocate_fcfs(curr_pool, active_tenants, obs, prev_plan)
            elif pol in ("proportional", "wp"):
                plan = allocate_proportional(curr_pool, active_tenants, obs, prev_plan)
            elif pol == "drf":
                plan = allocate_drf(curr_pool, active_tenants, obs, prev_plan)
            elif pol.startswith("adaptive") or pol.startswith("no_"):
                plan = adaptive_inst.allocate(
                    curr_pool, active_tenants, obs, prev_plan, now=step_time
                )
            else:
                plan = allocate_proportional(curr_pool, active_tenants, obs, prev_plan)

            plans_history.append(plan)
            prev_plan = plan

            # Item 11: Check if tenant throttled while pool capacity unallocated
            tot_alloc_cpu = sum(lims.get("cpu", 0.0) for lims in plan.alloc.values())
            pool_cpu_cap = curr_pool.capacity.get("cpu", config.default_pool_cpu)
            has_spare_capacity = (pool_cpu_cap - tot_alloc_cpu) > 0.05
            is_step_throttled = any(
                obs[t.id].throttle_ratio > config.throttle_threshold
                for t in active_tenants if t.active and t.id in obs
            )
            if has_spare_capacity and is_step_throttled:
                throttled_while_spare_epochs += 1

            alloc_history_by_epoch.append(
                {t.id: plan.alloc.get(t.id, {}).get("cpu", 0.0) for t in active_tenants if t.active}
            )

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
                    provenance="CALCULATED" if provenance == "MEASURED" else "SIMULATED",
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
                    "provenance": provenance,
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

        # Collect requests and finalize workload metrics
        load_stop_event.set()
        req_samples: List[RequestSample] = []
        if load_task:
            try:
                req_samples = await asyncio.wait_for(load_task, timeout=5.0)
            except Exception:
                req_samples = []

        # If no live samples returned (e.g. simulation or endpoints unreachable), simulate requests deterministically
        if not req_samples and trace:
            for arr in trace:
                epoch_idx = min(len(plans_history) - 1, max(0, int(arr.scheduled_time_s)))
                plan_at_t = plans_history[epoch_idx] if plans_history else prev_plan
                p_cpu = plan_at_t.alloc.get(arr.tenant_id, {}).get("cpu", 0.1) if plan_at_t else 0.1
                p_mem = plan_at_t.alloc.get(arr.tenant_id, {}).get("mem_mb", 64.0) if plan_at_t else 64.0

                cpu_demand = max(0.1, arr.cpu_units)
                speedup = min(2.0, max(0.2, p_cpu / cpu_demand))
                service_ms = arr.nominal_service_ms / speedup
                latency_ms = service_ms + 5.0

                if p_mem < arr.mem_mb:
                    status = "error"
                elif latency_ms > arr.deadline_ms:
                    status = "deadline_missed"
                else:
                    status = "ok"

                req_samples.append(
                    RequestSample(
                        run_id=run_id,
                        arrival_id=arr.arrival_id,
                        tenant_id=arr.tenant_id,
                        scheduled_time_s=arr.scheduled_time_s,
                        dispatch_time_s=round(arr.scheduled_time_s + 0.002, 4),
                        completion_time_s=round(arr.scheduled_time_s + latency_ms / 1000.0, 4),
                        latency_ms=round(latency_ms, 2),
                        service_ms=round(service_ms, 2),
                        status=status,
                        provenance=provenance,
                    )
                )

        # Persist request samples to DB
        for s in req_samples:
            db.add(
                RequestSampleModel(
                    run_id=run_id,
                    arrival_id=s.arrival_id,
                    tenant_id=s.tenant_id,
                    scheduled_time_s=s.scheduled_time_s,
                    dispatch_time_s=s.dispatch_time_s,
                    completion_time_s=s.completion_time_s,
                    latency_ms=s.latency_ms,
                    service_ms=s.service_ms,
                    status=s.status,
                    provenance=provenance,
                )
            )
        db.commit()

        total_oom_kills = sum(oom_kills_per_tenant.values())
        non_tenant_cpu_cores_avg = (
            float(np.mean(non_tenant_cores_history)) if non_tenant_cores_history else 0.0
        )
        max_non_tenant_cores = config.host_vcpus * (1.0 - config.default_host_cpu_ratio)

        run_row = db.query(Run).filter(Run.id == run_id).first()
        if run_row:
            if non_tenant_cpu_cores_avg > max_non_tenant_cores:
                run_row.is_valid = False
                run_row.invalidation_reason = (
                    f"Non-tenant CPU {non_tenant_cpu_cores_avg:.3f} cores exceeded host reservation ({max_non_tenant_cores:.2f} cores)"
                )
            elif max_steal_pct > config.max_cpu_steal_pct:
                run_row.is_valid = False
                run_row.invalidation_reason = (
                    f"High host steal {max_steal_pct:.1f}% exceeded threshold of {config.max_cpu_steal_pct}%"
                )
            db.commit()

        # Convergence calculation across startup and timed events (A4)
        event_times = sorted(list(set([0] + [ev.t_sec for ev in scen_spec.events if ev.t_sec < duration])))
        conv_epochs_list = []
        pool_ref_cpu = max(0.1, curr_pool.capacity.get("cpu", 1.5))
        for ev_t in event_times:
            conv_step = duration - ev_t
            for t_idx in range(ev_t + 1, len(alloc_history_by_epoch)):
                prev_vec = alloc_history_by_epoch[t_idx - 1]
                curr_vec = alloc_history_by_epoch[t_idx]
                max_delta = max((abs(curr_vec.get(tid, 0.0) - prev_vec.get(tid, 0.0)) for tid in curr_vec), default=0.0)
                if (max_delta / pool_ref_cpu) <= config.deadband:
                    conv_step = t_idx - ev_t
                    break
            conv_epochs_list.append(conv_step)

        convergence_initial = float(conv_epochs_list[0]) if conv_epochs_list else 1.0
        convergence_avg = float(np.mean(conv_epochs_list)) if conv_epochs_list else 1.0
        throttled_while_spare_ratio = throttled_while_spare_epochs / max(1.0, float(duration))

        # Compute summary metrics for run
        _compute_and_save_run_metrics(
            db=db,
            run_id=run_id,
            plans=plans_history,
            usages_history=usages_history,
            usages_by_tenant_history=usages_by_tenant_history,
            tenants=active_tenants,
            pool=curr_pool,
            provenance=provenance,
            req_samples=req_samples,
            duration_s=float(scen_spec.duration_s),
            oom_kills_count=total_oom_kills,
            oom_kills_per_tenant=oom_kills_per_tenant,
            cgroup_usec_sum=cgroup_usec_sum,
            cgroup_throttled_sum=cgroup_throttled_sum,
            non_tenant_cpu_cores_avg=non_tenant_cpu_cores_avg,
            throttled_while_spare_ratio=throttled_while_spare_ratio,
            convergence_initial=convergence_initial,
            convergence_avg=convergence_avg,
        )

        if hasattr(adaptive_inst, "epoch_diagnostics") and adaptive_inst.epoch_diagnostics:
            diag_dir = os.path.join(config.data_dir, "diagnostics")
            os.makedirs(diag_dir, exist_ok=True)
            diag_file = os.path.join(diag_dir, f"{run_id}.json")
            try:
                with open(diag_file, "w", encoding="utf-8") as f:
                    json.dump(adaptive_inst.epoch_diagnostics, f, indent=2)
            except Exception:
                pass

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
    usages_history: List[List[Dict[str, float]]],
    usages_by_tenant_history: Optional[List[Dict[str, Dict[str, float]]]] = None,
    tenants: Optional[List[Tenant]] = None,
    pool: Optional[Pool] = None,
    provenance: str = "MEASURED",
    req_samples: Optional[List[RequestSample]] = None,
    duration_s: float = 60.0,
    oom_kills_count: int = 0,
    oom_kills_per_tenant: Optional[Dict[str, int]] = None,
    cgroup_usec_sum: Optional[Dict[str, float]] = None,
    cgroup_throttled_sum: Optional[Dict[str, float]] = None,
    non_tenant_cpu_cores_avg: float = 0.0,
    throttled_while_spare_ratio: float = 0.0,
    convergence_initial: float = 1.0,
    convergence_avg: float = 1.0,
) -> None:
    if not plans or not tenants or not pool:
        return

    last_plan = plans[-1]
    active_tenants = [t for t in tenants if t.active]

    # Bottleneck satisfaction evaluated strictly against DECLARED demand (SLA contract)
    satisfactions = [satisfaction(last_plan.alloc.get(t.id, {}), t.demand) for t in active_tenants]
    primary_values = [s / max(0.1, t.weight) for s, t in zip(satisfactions, active_tenants)]
    jain_primary = jain_index(primary_values)

    # Secondary: Jain on dominant shares
    dom_shares = []
    for t in active_tenants:
        al = last_plan.alloc.get(t.id, {})
        shares = [al.get(r, 0.0) / pool.capacity[r] for r in pool.capacity if pool.capacity[r] > 0]
        dom_shares.append(max(shares) / max(0.1, t.weight) if shares else 0.0)
    jain_secondary = jain_index(dom_shares)

    # Unmet demand evaluated against declared demand
    unmet_cpu = sum(
        max(0.0, float(t.demand.get("cpu", 0.0) - last_plan.alloc.get(t.id, {}).get("cpu", 0.0)))
        for t in active_tenants
    )
    unmet_mem = sum(
        max(0.0, float(t.demand.get("mem_mb", 0.0) - last_plan.alloc.get(t.id, {}).get("mem_mb", 0.0)))
        for t in active_tenants
    )

    # 1. Allocated (reserved) utilisation
    flat_allocs = [al for p in plans for al in p.alloc.values()]
    u_alloc = allocated_util(flat_allocs, pool.capacity)

    # 2. Consumed (actual usage) utilisation
    flat_usages = [u for step_u in usages_history for u in step_u]
    u_consumed = consumed_util(flat_usages, pool.capacity)

    churn_val = churn(plans, pool.capacity)
    changes_val = count_changes(plans)

    feasible, admitted_ids, _ = check_guarantee_feasibility(pool, tenants)
    minimums = {t.id: t.minimum for t in active_tenants}
    feas_rate, infeas_rate = guarantee_satisfaction(last_plan.alloc, minimums, set(admitted_ids))

    # Throttle time ratio (unmet actual need component 1 - MEASURED)
    cgroup_u = cgroup_usec_sum or {}
    cgroup_th = cgroup_throttled_sum or {}
    tot_usec = sum(cgroup_u.values())
    tot_th = sum(cgroup_th.values())
    overall_throttle_ratio = (tot_th / (tot_usec + tot_th)) if (tot_usec + tot_th) > 0.0 else 0.0

    # Solve time in absolute ms and percentage of epoch (Item 7 & A6)
    solve_ms = float(np.mean([p.solve_ms for p in plans])) if plans else 0.0
    epoch_ms = config.epoch_interval_s * 1000.0
    solve_epoch_pct = (solve_ms / max(1.0, epoch_ms)) * 100.0

    metrics_map: Dict[str, float] = {
        "jain_fairness_primary": jain_primary,
        "jain_fairness_secondary": jain_secondary,
        "mean_satisfaction": float(np.mean(satisfactions)) if satisfactions else 0.0,
        "unmet_demand_cpu": unmet_cpu,
        "unmet_demand_mem": unmet_mem,
        "allocated_util_cpu": u_alloc.get("cpu", 0.0) / max(1, len(plans)),
        "allocated_util_mem": u_alloc.get("mem_mb", 0.0) / max(1, len(plans)),
        "consumed_util_cpu": u_consumed.get("cpu", 0.0) / max(1, len(usages_history)),
        "consumed_util_mem": u_consumed.get("mem_mb", 0.0) / max(1, len(usages_history)),
        "churn": churn_val,
        "changes_count": float(changes_val),
        "guarantee_satisfaction_feasible": feas_rate,
        "guarantee_satisfaction_infeasible": infeas_rate,
        "solve_ms_avg": solve_ms,
        "solve_time_epoch_pct": solve_epoch_pct,
        "throttle_time_ratio": float(overall_throttle_ratio),
        "throttled_while_spare_ratio": float(throttled_while_spare_ratio),
        "convergence_initial": float(convergence_initial),
        "convergence_avg": float(convergence_avg),
        "non_tenant_cpu_cores_avg": float(non_tenant_cpu_cores_avg),
    }

    # Idle reserved capacity (allocated minus consumed - CALCULATED)
    usages_by_t = usages_by_tenant_history or []
    for t in active_tenants:
        idle_cpu_steps = []
        idle_mem_steps = []
        for k in range(len(plans)):
            alloc_k = plans[k].alloc.get(t.id, {})
            usage_k = usages_by_t[k].get(t.id, {}) if k < len(usages_by_t) else {}
            idle_cpu_steps.append(max(0.0, alloc_k.get("cpu", 0.0) - usage_k.get("cpu", 0.0)))
            idle_mem_steps.append(max(0.0, alloc_k.get("mem_mb", 0.0) - usage_k.get("mem_mb", 0.0)))
        metrics_map[f"tenant:{t.id}:idle_reserved_cpu"] = float(np.mean(idle_cpu_steps)) if idle_cpu_steps else 0.0
        metrics_map[f"tenant:{t.id}:idle_reserved_mem_mb"] = float(np.mean(idle_mem_steps)) if idle_mem_steps else 0.0

    metrics_map["idle_reserved_cpu"] = sum(
        metrics_map.get(f"tenant:{t.id}:idle_reserved_cpu", 0.0) for t in active_tenants
    )
    metrics_map["idle_reserved_mem_mb"] = sum(
        metrics_map.get(f"tenant:{t.id}:idle_reserved_mem_mb", 0.0) for t in active_tenants
    )

    # Request-level metrics
    samples = req_samples or []
    total_offered = len(samples)
    completed_samples = [s for s in samples if s.status in ("ok", "deadline_missed")]
    completed_count = len(completed_samples)
    on_time_count = sum(1 for s in completed_samples if s.status == "ok")
    missed_count = sum(1 for s in samples if s.status == "deadline_missed")
    rejected_count = sum(1 for s in samples if s.status == "rejected")
    censored_count = sum(1 for s in samples if getattr(s, "is_censored", False) or s.status == "timeout")
    excluded_count = rejected_count + censored_count
    latencies = [s.latency_ms for s in completed_samples]

    on_time_ratio = (on_time_count / total_offered) if total_offered > 0 else 1.0
    tp = completed_count / max(1.0, duration_s)
    cr = (completed_count / total_offered) if total_offered > 0 else 1.0
    mr = (missed_count / completed_count) if completed_count > 0 else 0.0
    mean_lat = float(np.mean(latencies)) if latencies else 0.0
    p95_lat = float(np.percentile(latencies, 95)) if latencies else 0.0

    metrics_map.update(
        {
            "on_time_ratio": float(on_time_ratio),
            "completed": float(completed_count),
            "throughput": tp,
            "completion_ratio": cr,
            "deadline_misses": float(missed_count),
            "deadline_miss_rate": mr,
            "rejected": float(rejected_count),
            "censored": float(censored_count),
            "excluded_count": float(excluded_count),
            "mean_latency_ms": mean_lat,
            "p95_latency_ms": p95_lat,
            "oom_kills": float(oom_kills_count),
        }
    )

    # Per-tenant request-level metrics
    oom_per_t = oom_kills_per_tenant or {}
    for t in active_tenants:
        t_samples = [s for s in samples if s.tenant_id == t.id]
        t_total_offered = len(t_samples)
        t_completed = [s for s in t_samples if s.status in ("ok", "deadline_missed")]
        t_comp_count = len(t_completed)
        t_on_time_count = sum(1 for s in t_completed if s.status == "ok")
        t_miss_count = sum(1 for s in t_samples if s.status == "deadline_missed")
        t_rej_count = sum(1 for s in t_samples if s.status == "rejected")
        t_censored_count = sum(1 for s in t_samples if getattr(s, "is_censored", False) or s.status == "timeout")
        t_excluded_count = t_rej_count + t_censored_count
        t_lats = [s.latency_ms for s in t_completed]

        t_on_time_ratio = (t_on_time_count / t_total_offered) if t_total_offered > 0 else 1.0
        t_tp = t_comp_count / max(1.0, duration_s)
        t_cr = (t_comp_count / t_total_offered) if t_total_offered > 0 else 1.0
        t_mr = (t_miss_count / t_comp_count) if t_comp_count > 0 else 0.0
        t_mean_lat = float(np.mean(t_lats)) if t_lats else 0.0
        t_p95_lat = float(np.percentile(t_lats, 95)) if t_lats else 0.0
        t_oom = float(oom_per_t.get(t.id, 0))

        t_u = cgroup_u.get(t.id, 0.0)
        t_th = cgroup_th.get(t.id, 0.0)
        t_throttle_ratio = (t_th / (t_u + t_th)) if (t_u + t_th) > 0.0 else 0.0

        metrics_map[f"tenant:{t.id}:on_time_ratio"] = float(t_on_time_ratio)
        metrics_map[f"tenant:{t.id}:completed"] = float(t_comp_count)
        metrics_map[f"tenant:{t.id}:throughput"] = t_tp
        metrics_map[f"tenant:{t.id}:completion_ratio"] = t_cr
        metrics_map[f"tenant:{t.id}:deadline_misses"] = float(t_miss_count)
        metrics_map[f"tenant:{t.id}:deadline_miss_rate"] = t_mr
        metrics_map[f"tenant:{t.id}:rejected"] = float(t_rej_count)
        metrics_map[f"tenant:{t.id}:censored"] = float(t_censored_count)
        metrics_map[f"tenant:{t.id}:excluded_count"] = float(t_excluded_count)
        metrics_map[f"tenant:{t.id}:mean_latency_ms"] = t_mean_lat
        metrics_map[f"tenant:{t.id}:p95_latency_ms"] = t_p95_lat
        metrics_map[f"tenant:{t.id}:oom_kills"] = t_oom
        metrics_map[f"tenant:{t.id}:throttle_time_ratio"] = float(t_throttle_ratio)
        metrics_map[f"tenant:{t.id}:churn"] = tenant_churn(plans, pool.capacity, t.id)
        metrics_map[f"tenant:{t.id}:changes_count"] = float(tenant_count_changes(plans, t.id))

    save_run_metrics(db, run_id, metrics_map, default_provenance=provenance)
