"""Unit tests for FCFS allocator."""

from alloc_platform.allocator.fcfs import allocate_fcfs
from alloc_platform.allocator.types import Pool, Tenant


def test_fcfs_basic_order():
    """Verify earlier arrival sequence gets allocation first."""
    pool = Pool(capacity={"cpu": 2.0, "mem_mb": 2048.0})
    tenants = [
        Tenant(
            id="t2",
            name="Tenant 2",
            weight=1.0,
            priority=1,
            arrival_seq=2,
            active=True,
            demand={"cpu": 1.5, "mem_mb": 1500.0},
            minimum={"cpu": 0.5, "mem_mb": 500.0},
        ),
        Tenant(
            id="t1",
            name="Tenant 1",
            weight=1.0,
            priority=1,
            arrival_seq=1,
            active=True,
            demand={"cpu": 1.5, "mem_mb": 1500.0},
            minimum={"cpu": 0.5, "mem_mb": 500.0},
        ),
    ]

    plan = allocate_fcfs(pool, tenants)
    # t1 arrived first: gets 1.5 CPU, 1500 RAM
    assert plan.alloc["t1"]["cpu"] == 1.5
    assert plan.alloc["t1"]["mem_mb"] == 1500.0
    # t2 gets remaining: 0.5 CPU, 548 RAM
    assert plan.alloc["t2"]["cpu"] == 0.5
    assert plan.alloc["t2"]["mem_mb"] == 548.0


def test_fcfs_determinism():
    """Verify FCFS output is strictly deterministic regardless of input list order."""
    pool = Pool(capacity={"cpu": 2.0, "mem_mb": 2048.0})
    t1 = Tenant(
        id="t1",
        name="T1",
        weight=1.0,
        priority=1,
        arrival_seq=1,
        active=True,
        demand={"cpu": 1.0, "mem_mb": 1000.0},
        minimum={"cpu": 0.1, "mem_mb": 100.0},
    )
    t2 = Tenant(
        id="t2",
        name="T2",
        weight=1.0,
        priority=1,
        arrival_seq=2,
        active=True,
        demand={"cpu": 1.5, "mem_mb": 1500.0},
        minimum={"cpu": 0.1, "mem_mb": 100.0},
    )

    plan_a = allocate_fcfs(pool, [t1, t2])
    plan_b = allocate_fcfs(pool, [t2, t1])
    assert plan_a.alloc == plan_b.alloc


def test_fcfs_capacity_never_exceeded():
    """Verify pool capacity is never exceeded."""
    pool = Pool(capacity={"cpu": 1.0, "mem_mb": 1024.0})
    tenants = [
        Tenant(
            id=f"t{i}",
            name=f"T{i}",
            weight=1.0,
            priority=1,
            arrival_seq=i,
            active=True,
            demand={"cpu": 0.8, "mem_mb": 800.0},
            minimum={"cpu": 0.1, "mem_mb": 100.0},
        )
        for i in range(5)
    ]
    plan = allocate_fcfs(pool, tenants)
    tot_cpu = sum(plan.alloc[t.id]["cpu"] for t in tenants)
    tot_mem = sum(plan.alloc[t.id]["mem_mb"] for t in tenants)
    assert tot_cpu <= pool.capacity["cpu"] + 1e-9
    assert tot_mem <= pool.capacity["mem_mb"] + 1e-9
