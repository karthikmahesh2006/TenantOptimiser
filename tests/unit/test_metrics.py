"""Unit tests for allocator metrics and fairness evaluation."""

import numpy as np
import pytest

from alloc_platform.allocator.metrics import (
    allocated_util,
    bootstrap_ci,
    churn,
    count_changes,
    guarantee_satisfaction,
    jain_index,
    satisfaction,
    unmet_demand,
)
from alloc_platform.allocator.types import Plan


def test_jain_index_known_answers():
    """Verify Jain's index against mathematically exact analytical values."""
    # 1. Perfect equality: J([10, 10, 10]) = (30)^2 / (3 * 300) = 900 / 900 = 1.0
    assert pytest.approx(jain_index([10.0, 10.0, 10.0]), rel=1e-6) == 1.0

    # 2. Maximum inequality: one has all resources, 2 have zero: J([10, 0, 0]) = 100 / (3 * 100) = 1/3
    assert pytest.approx(jain_index([10.0, 0.0, 0.0]), rel=1e-6) == 1.0 / 3.0

    # 3. Known unequal sequence: J([1, 2, 3, 4])
    # sum = 10 -> (10)^2 = 100; sum_sq = 1 + 4 + 9 + 16 = 30
    # J = 100 / (4 * 30) = 100 / 120 = 5/6 approx 0.833333
    assert pytest.approx(jain_index([1.0, 2.0, 3.0, 4.0]), rel=1e-6) == 5.0 / 6.0

    # 4. Vacuously fair cases: empty or all zeros
    assert jain_index([]) == 1.0
    assert jain_index([0.0, 0.0, 0.0]) == 1.0


def test_satisfaction():
    """Verify tenant satisfaction factor calculation."""
    demand = {"cpu": 2.0, "mem_mb": 1024.0}
    # Half CPU, full RAM -> satisfaction is min(1.0/2.0, 1024/1024) = 0.5
    alloc = {"cpu": 1.0, "mem_mb": 1024.0}
    assert satisfaction(alloc, demand) == 0.5

    # Zero demand returns 1.0
    assert satisfaction({}, {}) == 1.0


def test_unmet_demand():
    """Verify unmet demand calculation per resource."""
    demand = {"cpu": 2.0, "mem_mb": 1024.0}
    alloc = {"cpu": 1.5, "mem_mb": 512.0}
    unmet = unmet_demand(alloc, demand)
    assert pytest.approx(unmet["cpu"], rel=1e-6) == 0.5
    assert pytest.approx(unmet["mem_mb"], rel=1e-6) == 512.0


def test_allocated_and_consumed_util():
    """Verify pool capacity utilization calculations."""
    capacity = {"cpu": 4.0, "mem_mb": 4096.0}
    allocs = [
        {"cpu": 1.0, "mem_mb": 1024.0},
        {"cpu": 2.0, "mem_mb": 1024.0},
    ]
    util = allocated_util(allocs, capacity)
    assert pytest.approx(util["cpu"], rel=1e-6) == 3.0 / 4.0
    assert pytest.approx(util["mem_mb"], rel=1e-6) == 2048.0 / 4096.0


def test_guarantee_satisfaction():
    """Verify guarantee satisfaction split between feasible and infeasible tenants."""
    minimums = {
        "t1": {"cpu": 1.0},
        "t2": {"cpu": 1.0},
        "t3": {"cpu": 2.0},
    }
    allocs = {
        "t1": {"cpu": 1.0},
        "t2": {"cpu": 0.5},
        "t3": {"cpu": 1.0},
    }
    # t1 and t2 are feasible; t3 was deemed infeasible
    feas_rate, infeas_rate = guarantee_satisfaction(allocs, minimums, feasible_ids={"t1", "t2"})
    assert feas_rate == 0.5  # t1 met, t2 missed
    assert infeas_rate == 0.0  # t3 missed


def test_churn_and_changes():
    """Verify churn metric and discrete count of updates."""
    cap = {"cpu": 2.0}
    p1 = Plan(alloc={"t1": {"cpu": 1.0}})
    p2 = Plan(alloc={"t1": {"cpu": 1.5}})
    p3 = Plan(alloc={"t1": {"cpu": 1.5}})

    assert count_changes([p1, p2, p3]) == 1
    # Churn: epoch 1 delta is 0.5 / 2.0 = 0.25; epoch 2 delta is 0.0; average over 2 transitions = 0.125
    assert pytest.approx(churn([p1, p2, p3], cap), rel=1e-6) == 0.125


def test_bootstrap_ci():
    """Verify deterministic non-parametric bootstrap confidence interval."""
    data = [10.0, 11.0, 9.0, 10.5, 9.5] * 20
    point, lo, hi = bootstrap_ci(data, stat=np.mean, n=1000, seed=42)
    assert 9.8 < point < 10.2
    assert lo <= point <= hi


def test_idle_reserved():
    """Verify idle reserved capacity (allocated minus consumed) calculation."""
    from alloc_platform.allocator.metrics import idle_reserved

    allocs = [
        {"cpu": 1.5, "mem_mb": 1024.0},
        {"cpu": 1.0, "mem_mb": 512.0},
    ]
    usages = [
        {"cpu": 0.5, "mem_mb": 800.0},
        {"cpu": 1.2, "mem_mb": 400.0},
    ]
    res = idle_reserved(allocs, usages)
    # Step 0: cpu idle = 1.0, mem idle = 224.0
    # Step 1: cpu idle = max(0, 1.0 - 1.2) = 0.0, mem idle = 112.0
    assert pytest.approx(res["cpu"], rel=1e-6) == 1.0
    assert pytest.approx(res["mem_mb"], rel=1e-6) == 336.0


def test_tenant_churn_and_changes():
    """Verify per-tenant churn and change counter."""
    from alloc_platform.allocator.metrics import tenant_churn, tenant_count_changes

    cap = {"cpu": 2.0}
    p1 = Plan(alloc={"t1": {"cpu": 1.0}, "t2": {"cpu": 0.5}})
    p2 = Plan(alloc={"t1": {"cpu": 1.5}, "t2": {"cpu": 0.5}})
    p3 = Plan(alloc={"t1": {"cpu": 1.5}, "t2": {"cpu": 0.5}})

    assert tenant_count_changes([p1, p2, p3], "t1") == 1
    assert tenant_count_changes([p1, p2, p3], "t2") == 0
    assert pytest.approx(tenant_churn([p1, p2, p3], cap, "t1"), rel=1e-6) == 0.125
    assert tenant_churn([p1, p2, p3], cap, "t2") == 0.0
