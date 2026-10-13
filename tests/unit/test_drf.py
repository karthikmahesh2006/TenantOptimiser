"""Unit tests for Dominant Resource Fairness (DRF) allocator."""

import pytest

from alloc_platform.allocator.drf import allocate_drf
from alloc_platform.allocator.types import Pool, Tenant


def test_drf_textbook_ghodsi_2011_example():
    """Verifies standard 2-resource DRF example from Ghodsi et al. (NSDI 2011).

    System capacity: 9 CPU, 18 RAM.
    Tenant A: profile (1 CPU, 4 RAM) -> dominant resource is RAM (4/18 = 2/9 > 1/9).
    Tenant B: profile (3 CPU, 1 RAM) -> dominant resource is CPU (3/9 = 1/3 > 1/18).

    Exact analytical solution:
    CPU saturates at 9: Tenant A gets 3 tasks (3 CPU, 12 RAM), Tenant B gets 2 tasks (6 CPU, 2 RAM).
    Both obtain dominant share of 2/3 (66.7%).
    """
    pool = Pool(capacity={"cpu": 9.0, "mem_mb": 18.0})

    # Represent profiles scaled up to capacity bounds
    # Tenant A wants up to 4 tasks (4 CPU, 16 RAM)
    # Tenant B wants up to 3 tasks (9 CPU, 3 RAM)
    t_a = Tenant(
        id="t_a",
        name="Tenant A",
        weight=1.0,
        priority=1,
        arrival_seq=1,
        active=True,
        demand={"cpu": 4.0, "mem_mb": 16.0},
        minimum={"cpu": 0.0, "mem_mb": 0.0},
    )
    t_b = Tenant(
        id="t_b",
        name="Tenant B",
        weight=1.0,
        priority=1,
        arrival_seq=2,
        active=True,
        demand={"cpu": 9.0, "mem_mb": 3.0},
        minimum={"cpu": 0.0, "mem_mb": 0.0},
    )

    plan = allocate_drf(pool, [t_a, t_b])

    # Verify allocations match 3 tasks for A, 2 tasks for B
    assert pytest.approx(plan.alloc["t_a"]["cpu"], abs=1e-4) == 3.0
    assert pytest.approx(plan.alloc["t_a"]["mem_mb"], abs=1e-4) == 12.0

    assert pytest.approx(plan.alloc["t_b"]["cpu"], abs=1e-4) == 6.0
    assert pytest.approx(plan.alloc["t_b"]["mem_mb"], abs=1e-4) == 2.0

    # Dominant shares must be equal
    dom_share_a = max(plan.alloc["t_a"]["cpu"] / 9.0, plan.alloc["t_a"]["mem_mb"] / 18.0)
    dom_share_b = max(plan.alloc["t_b"]["cpu"] / 9.0, plan.alloc["t_b"]["mem_mb"] / 18.0)
    assert pytest.approx(dom_share_a, abs=1e-4) == 2.0 / 3.0
    assert pytest.approx(dom_share_b, abs=1e-4) == 2.0 / 3.0


def test_drf_weighted_shares():
    """Verify tenant with double weight receives double the dominant share."""
    pool = Pool(capacity={"cpu": 10.0, "mem_mb": 1000.0})
    t_w2 = Tenant(
        id="t_w2",
        name="T2",
        weight=2.0,
        priority=1,
        arrival_seq=1,
        active=True,
        demand={"cpu": 10.0, "mem_mb": 1000.0},
        minimum={"cpu": 0.0, "mem_mb": 0.0},
    )
    t_w1 = Tenant(
        id="t_w1",
        name="T1",
        weight=1.0,
        priority=1,
        arrival_seq=2,
        active=True,
        demand={"cpu": 10.0, "mem_mb": 1000.0},
        minimum={"cpu": 0.0, "mem_mb": 0.0},
    )

    plan = allocate_drf(pool, [t_w2, t_w1])
    # Dominant shares: t_w2 gets 2/3 (6.666 CPU), t_w1 gets 1/3 (3.333 CPU)
    assert pytest.approx(plan.alloc["t_w2"]["cpu"], abs=1e-4) == 20.0 / 3.0
    assert pytest.approx(plan.alloc["t_w1"]["cpu"], abs=1e-4) == 10.0 / 3.0
