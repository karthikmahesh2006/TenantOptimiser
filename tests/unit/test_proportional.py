"""Unit tests for Proportional allocator with water-filling."""

import pytest

from alloc_platform.allocator.proportional import allocate_proportional
from alloc_platform.allocator.types import Pool, Tenant


def test_proportional_weighted_sharing():
    """Verify resources are split in proportion to weights."""
    pool = Pool(capacity={"cpu": 3.0, "mem_mb": 3000.0})
    tenants = [
        Tenant(
            id="t1",
            name="T1",
            weight=2.0,
            priority=1,
            arrival_seq=1,
            active=True,
            demand={"cpu": 3.0, "mem_mb": 3000.0},
            minimum={"cpu": 0.0, "mem_mb": 0.0},
        ),
        Tenant(
            id="t2",
            name="T2",
            weight=1.0,
            priority=1,
            arrival_seq=2,
            active=True,
            demand={"cpu": 3.0, "mem_mb": 3000.0},
            minimum={"cpu": 0.0, "mem_mb": 0.0},
        ),
    ]
    plan = allocate_proportional(pool, tenants)
    # T1 has 2x weight of T2: T1 gets 2.0, T2 gets 1.0
    assert pytest.approx(plan.alloc["t1"]["cpu"], rel=1e-5) == 2.0
    assert pytest.approx(plan.alloc["t2"]["cpu"], rel=1e-5) == 1.0


def test_proportional_water_filling_cap():
    """Verify water filling caps tenant at demand and redistributes remainder."""
    pool = Pool(capacity={"cpu": 3.0, "mem_mb": 3000.0})
    # T1 has weight 5.0 and demand 0.5 -> uncapped share exceeds 0.5
    # T2 has weight 1.0 and demand 3.0
    tenants = [
        Tenant(
            id="t1",
            name="T1",
            weight=5.0,
            priority=1,
            arrival_seq=1,
            active=True,
            demand={"cpu": 0.5, "mem_mb": 500.0},
            minimum={"cpu": 0.0, "mem_mb": 0.0},
        ),
        Tenant(
            id="t2",
            name="T2",
            weight=1.0,
            priority=1,
            arrival_seq=2,
            active=True,
            demand={"cpu": 3.0, "mem_mb": 3000.0},
            minimum={"cpu": 0.0, "mem_mb": 0.0},
        ),
    ]
    plan = allocate_proportional(pool, tenants)
    # T1 capped at 0.5; T2 absorbs remaining 2.5
    assert plan.alloc["t1"]["cpu"] == 0.5
    assert pytest.approx(plan.alloc["t2"]["cpu"], rel=1e-5) == 2.5


def test_proportional_infeasible_minimums():
    """Verify infeasible minimums are scaled down and reported."""
    pool = Pool(capacity={"cpu": 1.0, "mem_mb": 1000.0})
    tenants = [
        Tenant(
            id="t1",
            name="T1",
            weight=1.0,
            priority=1,
            arrival_seq=1,
            active=True,
            demand={"cpu": 1.0, "mem_mb": 1000.0},
            minimum={"cpu": 1.0, "mem_mb": 1000.0},
        ),
        Tenant(
            id="t2",
            name="T2",
            weight=1.0,
            priority=1,
            arrival_seq=2,
            active=True,
            demand={"cpu": 1.0, "mem_mb": 1000.0},
            minimum={"cpu": 1.0, "mem_mb": 1000.0},
        ),
    ]
    plan = allocate_proportional(pool, tenants)
    # Minimums sum to 2.0 CPU > 1.0 pool capacity: both scaled to 0.5
    assert plan.alloc["t1"]["cpu"] == 0.5
    assert plan.alloc["t2"]["cpu"] == 0.5
    assert len(plan.unmet_guarantees) > 0
