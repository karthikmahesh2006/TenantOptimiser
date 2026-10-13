"""Offline fluid simulator for resource allocation parameter sweeps and offline tests.

PROVENANCE: All outputs produced by this module are strictly marked SIMULATED.
"""

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from alloc_platform.allocator.adaptive import AdaptiveAllocator
from alloc_platform.allocator.drf import allocate_drf
from alloc_platform.allocator.fcfs import allocate_fcfs
from alloc_platform.allocator.metrics import (
    churn,
    jain_index,
    satisfaction,
)
from alloc_platform.allocator.proportional import allocate_proportional
from alloc_platform.allocator.types import Plan, Pool, Tenant, TenantObs


@dataclass
class SimScenario:
    """Scenario specification for offline simulation."""

    name: str
    duration_s: int
    pool: Pool
    tenants: list[Tenant]
    # Map of t_second -> list of modifier functions: lambda tenants, pool: ...
    events: dict[int, Any] = field(default_factory=dict)


@dataclass
class SimResult:
    """Results from an offline fluid simulation run."""

    provenance: str = "SIMULATED"
    policy_name: str = ""
    epochs: list[int] = field(default_factory=list)
    plans: list[Plan] = field(default_factory=list)
    observations: list[dict[str, TenantObs]] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def simulate(
    policy: str,
    scenario: SimScenario,
    seed: int = 42,
    adaptive_allocator: AdaptiveAllocator | None = None,
) -> SimResult:
    """Executes a deterministic offline fluid simulation for testing and sweeps.

    Args:
        policy: Name of the allocation policy ('fcfs', 'proportional', 'drf', 'adaptive').
        scenario: SimScenario defining pool, tenants, duration, and events.
        seed: Random seed for arrival variations.
        adaptive_allocator: Optional customized instance of AdaptiveAllocator.

    Returns:
        SimResult with provenance strictly set to 'SIMULATED'.
    """
    rng = np.random.default_rng(seed)
    allocator_inst = adaptive_allocator or AdaptiveAllocator()

    plans: list[Plan] = []
    observations: list[dict[str, TenantObs]] = []
    epochs: list[int] = []

    # Fluid model state: per-tenant backlog
    backlog: dict[str, float] = {t.id: 0.0 for t in scenario.tenants}

    prev_plan: Plan | None = None
    current_tenants = [
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
        for t in scenario.tenants
    ]
    current_pool = Pool(capacity=dict(scenario.pool.capacity))

    for t_step in range(scenario.duration_s):
        epochs.append(t_step)

        # Apply any scheduled scenario events
        if t_step in scenario.events:
            event_fn = scenario.events[t_step]
            if callable(event_fn):
                event_fn(current_pool, current_tenants)

        # Construct TenantObs from fluid state
        current_obs: dict[str, TenantObs] = {}
        for t in current_tenants:
            if not t.active:
                continue

            # Fluid arrival of workload: nominal demand with small pseudo-random noise
            t_demand_cpu = float(t.demand.get("cpu", 1.0))
            arrival_flux = t_demand_cpu * (0.8 + 0.4 * rng.uniform(0.0, 1.0))

            # Available allocated quota from previous plan (or minimum if t=0)
            avail_cpu = (
                prev_plan.alloc.get(t.id, {}).get("cpu", float(t.minimum.get("cpu", 0.1)))
                if prev_plan
                else float(t.minimum.get("cpu", 0.1))
            )

            # Service work
            work_served = min(arrival_flux + backlog[t.id], avail_cpu)
            unserved = max(0.0, (arrival_flux + backlog[t.id]) - work_served)
            backlog[t.id] = unserved

            # Throttling ratio
            throttle_ratio = min(1.0, unserved / max(arrival_flux, 1e-4)) if unserved > 0 else 0.0

            # Usage tracking
            u_mem = float(t.demand.get("mem_mb", 256.0)) * (0.7 + 0.3 * rng.uniform(0.0, 1.0))
            current_obs[t.id] = TenantObs(
                usage={"cpu": round(work_served, 4), "mem_mb": round(u_mem, 2)},
                throttle_ratio=round(throttle_ratio, 4),
                backlog=int(np.ceil(unserved)),
            )

        observations.append(current_obs)

        # Call the requested allocator
        pol = policy.lower()
        if pol == "fcfs":
            plan = allocate_fcfs(current_pool, current_tenants, current_obs, prev_plan)
        elif pol in ("proportional", "wp"):
            plan = allocate_proportional(current_pool, current_tenants, current_obs, prev_plan)
        elif pol == "drf":
            plan = allocate_drf(current_pool, current_tenants, current_obs, prev_plan)
        elif pol == "adaptive":
            plan = allocator_inst.allocate(
                current_pool, current_tenants, current_obs, prev_plan, now=float(t_step)
            )
        else:
            raise ValueError(f"Unknown allocation policy: {policy}")

        plans.append(plan)
        prev_plan = plan

    # Compute summary metrics over simulation run
    active_tenants = [t for t in current_tenants if t.active]
    satisfactions = [satisfaction(plans[-1].alloc.get(t.id, {}), t.demand) for t in active_tenants]
    norm_fairness = [s / t.weight for s, t in zip(satisfactions, active_tenants) if t.weight > 0]
    jain_fairness = jain_index(norm_fairness)
    sim_churn = churn(plans, current_pool.capacity)

    metrics = {
        "jain_fairness_primary": round(jain_fairness, 4),
        "mean_satisfaction": round(float(np.mean(satisfactions)), 4) if satisfactions else 0.0,
        "churn": round(sim_churn, 4),
        "final_plan": plans[-1].alloc if plans else {},
    }

    return SimResult(
        provenance="SIMULATED",
        policy_name=policy,
        epochs=epochs,
        plans=plans,
        observations=observations,
        metrics=metrics,
        notes=["Offline fluid model simulation."],
    )
