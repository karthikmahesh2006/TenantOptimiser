"""Scenario specifications, validation, and predefined benchmark suites."""

from dataclasses import dataclass, field
from typing import Any, Dict, List

from alloc_platform.allocator.types import Pool, Tenant
from alloc_platform.config import config


@dataclass
class TimedEvent:
    t_sec: int
    event_type: str  # set_capacity, set_demand, tenant_arrive, tenant_leave
    params: Dict[str, Any]


@dataclass
class ScenarioSpec:
    name: str
    duration_s: int
    seed: int
    pool: Pool
    tenants: List[Tenant]
    events: List[TimedEvent] = field(default_factory=list)

    def validate(self) -> None:
        if len(self.tenants) > config.max_tenants:
            raise ValueError(
                f"Tenant count {len(self.tenants)} exceeds maximum {config.max_tenants}"
            )
        if self.duration_s > config.max_duration_s or self.duration_s < 5:
            raise ValueError(
                f"Duration {self.duration_s} must be between 5s and {config.max_duration_s}s"
            )
        for t in self.tenants:
            cpu_dem = t.demand.get("cpu", 0.0)
            mem_dem = t.demand.get("mem_mb", 0.0)
            if cpu_dem > config.max_container_cpu or cpu_dem < config.min_container_cpu:
                raise ValueError(
                    f"Tenant {t.id} CPU demand {cpu_dem} outside [{config.min_container_cpu}, {config.max_container_cpu}]"
                )
            if mem_dem > config.max_container_mem_mb or mem_dem < config.min_container_mem_mb:
                raise ValueError(
                    f"Tenant {t.id} Memory demand {mem_dem} outside [{config.min_container_mem_mb}, {config.max_container_mem_mb}]"
                )


def get_predefined_scenarios(duration_s: int = 30) -> Dict[str, ScenarioSpec]:
    """Returns the standard benchmark scenarios 1-4 and demo scenario.

    When duration_s == 120, event times are rescaled to occur after the
    cold-start sliding window (t >= W = 60s) to evaluate steady-state dynamics.
    """
    # Scenario 1: Heterogeneous Workload Baseline (Pool budget <= 75% of host capacity)
    scen1_pool = Pool(
        capacity={"cpu": config.default_pool_cpu, "mem_mb": config.default_pool_mem_mb}
    )
    scen1_tenants = [
        Tenant(
            id="tenant_cpu",
            name="CPU Intensive Worker",
            weight=2.0,
            priority=2,
            arrival_seq=1,
            active=True,
            demand={"cpu": 1.2, "mem_mb": 512.0},
            minimum={"cpu": 0.3, "mem_mb": 128.0},
            workload_type="cpu_heavy",
        ),
        Tenant(
            id="tenant_mem",
            name="Memory Cache Worker",
            weight=1.0,
            priority=1,
            arrival_seq=2,
            active=True,
            demand={"cpu": 0.4, "mem_mb": 2048.0},
            minimum={"cpu": 0.1, "mem_mb": 512.0},
            workload_type="memory_heavy",
        ),
        Tenant(
            id="tenant_mix",
            name="Web Mixed Service",
            weight=1.5,
            priority=1,
            arrival_seq=3,
            active=True,
            demand={"cpu": 0.8, "mem_mb": 1024.0},
            minimum={"cpu": 0.2, "mem_mb": 256.0},
            workload_type="mixed",
        ),
    ]

    if duration_s == 120:
        # Rescaled event timings for 120s duration: post cold-start (t >= W = 60s)
        scen2_events = [
            TimedEvent(
                t_sec=70,
                event_type="set_demand",
                params={"tenant_id": "tenant_cpu", "demand": {"cpu": 1.8, "mem_mb": 512.0}},
            ),
            TimedEvent(
                t_sec=95,
                event_type="set_demand",
                params={"tenant_id": "tenant_cpu", "demand": {"cpu": 1.2, "mem_mb": 512.0}},
            ),
        ]
        scen3_events = [
            TimedEvent(
                t_sec=70,
                event_type="set_capacity",
                params={"capacity": {"cpu": 0.9, "mem_mb": 2048.0}},
            ),
        ]
        scen4_events = [
            TimedEvent(t_sec=70, event_type="tenant_arrive", params={"tenant_id": "tenant_mix"}),
            TimedEvent(t_sec=95, event_type="tenant_leave", params={"tenant_id": "tenant_mix"}),
        ]
    else:
        # Standard event timings for 30s duration
        scen2_events = [
            TimedEvent(
                t_sec=10,
                event_type="set_demand",
                params={"tenant_id": "tenant_cpu", "demand": {"cpu": 1.8, "mem_mb": 512.0}},
            ),
            TimedEvent(
                t_sec=20,
                event_type="set_demand",
                params={"tenant_id": "tenant_cpu", "demand": {"cpu": 1.2, "mem_mb": 512.0}},
            ),
        ]
        scen3_events = [
            TimedEvent(
                t_sec=15,
                event_type="set_capacity",
                params={"capacity": {"cpu": 0.9, "mem_mb": 2048.0}},
            ),
        ]
        scen4_events = [
            TimedEvent(t_sec=10, event_type="tenant_arrive", params={"tenant_id": "tenant_mix"}),
            TimedEvent(t_sec=20, event_type="tenant_leave", params={"tenant_id": "tenant_mix"}),
        ]

    return {
        "scenario_1_baseline": ScenarioSpec(
            name="Scenario 1: Heterogeneous Workload Baseline",
            duration_s=duration_s,
            seed=42,
            pool=scen1_pool,
            tenants=scen1_tenants,
        ),
        "scenario_2_burst": ScenarioSpec(
            name="Scenario 2: Flash Crowd Workload Burst",
            duration_s=duration_s,
            seed=42,
            pool=scen1_pool,
            tenants=scen1_tenants,
            events=scen2_events,
        ),
        "scenario_3_capacity_cut": ScenarioSpec(
            name="Scenario 3: Physical Capacity Cut Under Contention",
            duration_s=duration_s,
            seed=42,
            pool=scen1_pool,
            tenants=scen1_tenants,
            events=scen3_events,
        ),
        "scenario_4_churn": ScenarioSpec(
            name="Scenario 4: Tenant Churn & Dynamic Arrival",
            duration_s=duration_s,
            seed=42,
            pool=scen1_pool,
            tenants=scen1_tenants,
            events=scen4_events,
        ),
        "demo_scenario": ScenarioSpec(
            name="Demo Scenario: Rapid Multi-Tenant Benchmark",
            duration_s=duration_s,
            seed=1337,
            pool=scen1_pool,
            tenants=scen1_tenants,
            events=scen2_events,
        ),
        "scenario_over_declaration": ScenarioSpec(
            name="Scenario Over-Declaration: Unused Quota Reclamation",
            duration_s=duration_s,
            seed=42,
            pool=scen1_pool,
            tenants=[
                Tenant(
                    id="tenant_a_over",
                    name="Tenant A (Over-declaring)",
                    weight=1.0,
                    priority=1,
                    arrival_seq=1,
                    active=True,
                    demand={"cpu": 1.2, "mem_mb": 512.0},
                    minimum={"cpu": 0.1, "mem_mb": 128.0},
                    workload_type="low_cpu",
                ),
                Tenant(
                    id="tenant_b_over",
                    name="Tenant B (Over-declaring)",
                    weight=1.0,
                    priority=1,
                    arrival_seq=2,
                    active=True,
                    demand={"cpu": 1.2, "mem_mb": 512.0},
                    minimum={"cpu": 0.1, "mem_mb": 128.0},
                    workload_type="low_cpu",
                ),
                Tenant(
                    id="tenant_c_honest",
                    name="Tenant C (Genuine Need)",
                    weight=1.0,
                    priority=1,
                    arrival_seq=3,
                    active=True,
                    demand={"cpu": 1.0, "mem_mb": 1024.0},
                    minimum={"cpu": 0.2, "mem_mb": 256.0},
                    workload_type="cpu_heavy",
                ),
            ],
        ),
        "scenario_honest_declaration": ScenarioSpec(
            name="Scenario Honest-Declaration: Equal Demand Contention",
            duration_s=duration_s,
            seed=42,
            pool=scen1_pool,
            tenants=[
                Tenant(
                    id="tenant_a_honest",
                    name="Tenant A (Honest)",
                    weight=1.0,
                    priority=1,
                    arrival_seq=1,
                    active=True,
                    demand={"cpu": 0.5, "mem_mb": 512.0},
                    minimum={"cpu": 0.1, "mem_mb": 128.0},
                    workload_type="mixed",
                ),
                Tenant(
                    id="tenant_b_honest",
                    name="Tenant B (Honest)",
                    weight=1.0,
                    priority=1,
                    arrival_seq=2,
                    active=True,
                    demand={"cpu": 0.5, "mem_mb": 512.0},
                    minimum={"cpu": 0.1, "mem_mb": 128.0},
                    workload_type="mixed",
                ),
                Tenant(
                    id="tenant_c_honest",
                    name="Tenant C (Honest)",
                    weight=1.0,
                    priority=1,
                    arrival_seq=3,
                    active=True,
                    demand={"cpu": 0.5, "mem_mb": 512.0},
                    minimum={"cpu": 0.1, "mem_mb": 128.0},
                    workload_type="mixed",
                ),
            ],
        ),
    }

