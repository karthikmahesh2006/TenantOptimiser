"""Unit tests for Adaptive Allocator."""

import time

from alloc_platform.allocator.adaptive import AdaptiveAllocator, AdaptiveParams
from alloc_platform.allocator.types import Pool, Tenant, TenantObs


def test_adaptive_memory_safety():
    """Verify adaptive allocator never sets memory below current usage + safety margin."""
    pool = Pool(capacity={"cpu": 4.0, "mem_mb": 4096.0})
    tenants = [
        Tenant(
            id="t1",
            name="T1",
            weight=1.0,
            priority=1,
            arrival_seq=1,
            active=True,
            demand={"cpu": 2.0, "mem_mb": 2048.0},
            minimum={"cpu": 0.2, "mem_mb": 256.0},
        )
    ]
    # Current usage is 1000 MB. Safety margin is 64 MB.
    obs = {"t1": TenantObs(usage={"cpu": 0.5, "mem_mb": 1000.0}, throttle_ratio=0.0, backlog=0)}

    allocator = AdaptiveAllocator(AdaptiveParams(memory_safety_mb=64.0))
    plan = allocator.allocate(pool, tenants, obs, now=10.0)

    # Must be at least 1000 + 64 = 1064 MB
    assert plan.alloc["t1"]["mem_mb"] >= 1064.0 - 1e-6


def test_adaptive_demand_estimation_fast_up():
    """Verify pressurized tenant (throttled) gets escalated demand estimate."""
    tenants = [
        Tenant(
            id="t1",
            name="T1",
            weight=1.0,
            priority=1,
            arrival_seq=1,
            active=True,
            demand={"cpu": 2.0, "mem_mb": 2048.0},
            minimum={"cpu": 0.2, "mem_mb": 256.0},
        )
    ]
    obs_calm = {"t1": TenantObs(usage={"cpu": 0.4, "mem_mb": 500.0}, throttle_ratio=0.0, backlog=0)}
    obs_throttled = {
        "t1": TenantObs(usage={"cpu": 0.4, "mem_mb": 500.0}, throttle_ratio=0.25, backlog=5)
    }

    allocator_calm = AdaptiveAllocator(AdaptiveParams(headroom_h=0.2))
    est_calm = allocator_calm.estimate_demand(tenants, obs_calm)

    allocator_pressured = AdaptiveAllocator(AdaptiveParams(headroom_h=0.2))
    est_pressured = allocator_pressured.estimate_demand(tenants, obs_throttled)

    # Pressurized estimate should be substantially higher than calm estimate
    assert est_pressured["t1"]["cpu"] > est_calm["t1"]["cpu"]


def test_adaptive_solve_time_10_tenants_gate1():
    """Gate 1 criterion: Solve time for 10 tenants must be strictly under 50 ms."""
    pool = Pool(capacity={"cpu": 8.0, "mem_mb": 16384.0})
    tenants = [
        Tenant(
            id=f"t{i}",
            name=f"Tenant {i}",
            weight=1.0 + (i % 3) * 0.5,
            priority=1,
            arrival_seq=i,
            active=True,
            demand={"cpu": 1.0, "mem_mb": 1500.0},
            minimum={"cpu": 0.1, "mem_mb": 100.0},
        )
        for i in range(10)
    ]
    obs = {
        t.id: TenantObs(
            usage={"cpu": 0.6 + 0.02 * i, "mem_mb": 800.0 + 50.0 * i},
            throttle_ratio=0.01 * (i % 4),
            backlog=i % 2,
        )
        for i, t in enumerate(tenants)
    }

    allocator = AdaptiveAllocator()
    # Warm up JIT/import
    allocator.allocate(pool, tenants, obs, now=0.0)

    # Measure 5 solver iterations
    times = []
    for step in range(1, 6):
        start = time.perf_counter()
        plan = allocator.allocate(pool, tenants, obs, now=float(step))
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        times.append(elapsed_ms)
        # Check plan sanity
        tot_cpu = sum(plan.alloc[t.id]["cpu"] for t in tenants)
        tot_mem = sum(plan.alloc[t.id]["mem_mb"] for t in tenants)
        assert tot_cpu <= pool.capacity["cpu"] + 1e-4
        assert tot_mem <= pool.capacity["mem_mb"] + 1e-4

    mean_time_ms = sum(times) / len(times)
    # Gate 1 benchmark
    assert mean_time_ms < 50.0, f"Mean solve time {mean_time_ms:.2f} ms exceeded 50 ms budget"


def test_adaptive_surplus_and_throttling_bypass():
    """Verify multi-pass surplus redistribution and that upward expansion is not blocked by deadband when throttled."""
    pool = Pool(capacity={"cpu": 2.0, "mem_mb": 2048.0})
    tenants = [
        Tenant(
            id="t_idle",
            name="Idle Tenant",
            weight=1.0,
            priority=1,
            arrival_seq=1,
            active=True,
            demand={"cpu": 1.0, "mem_mb": 512.0},
            minimum={"cpu": 0.1, "mem_mb": 128.0},
        ),
        Tenant(
            id="t_busy",
            name="Throttled Tenant",
            weight=1.0,
            priority=1,
            arrival_seq=2,
            active=True,
            demand={"cpu": 1.5, "mem_mb": 512.0},
            minimum={"cpu": 0.1, "mem_mb": 128.0},
        ),
    ]
    # t_idle uses very little; t_busy is heavily throttled
    obs = {
        "t_idle": TenantObs(usage={"cpu": 0.1, "mem_mb": 128.0}, throttle_ratio=0.0, backlog=0),
        "t_busy": TenantObs(usage={"cpu": 0.8, "mem_mb": 256.0}, throttle_ratio=0.20, backlog=3),
    }

    allocator = AdaptiveAllocator()
    plan = allocator.allocate(pool, tenants, obs, now=1.0)

    # t_busy should receive surplus CPU well above its proportional static share (which would be 1.0)
    assert plan.alloc["t_busy"]["cpu"] > 1.2
    assert plan.alloc["t_idle"]["cpu"] + plan.alloc["t_busy"]["cpu"] <= pool.capacity["cpu"] + 1e-4

