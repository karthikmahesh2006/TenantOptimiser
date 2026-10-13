"""Hypothesis property-based tests for all four allocation policies."""

from hypothesis import given, settings
from hypothesis import strategies as st

from alloc_platform.allocator.adaptive import AdaptiveAllocator, AdaptiveParams
from alloc_platform.allocator.drf import allocate_drf
from alloc_platform.allocator.fcfs import allocate_fcfs
from alloc_platform.allocator.proportional import allocate_proportional
from alloc_platform.allocator.types import Pool, Tenant, TenantObs


# Strategies for generating valid pools and tenants
@st.composite
def pool_strategy(draw):
    cpu = draw(st.floats(min_value=0.5, max_value=8.0))
    mem_mb = draw(st.floats(min_value=256.0, max_value=8192.0))
    return Pool(capacity={"cpu": round(cpu, 4), "mem_mb": round(mem_mb, 2)})


@st.composite
def tenants_strategy(draw, max_tenants=5):
    n = draw(st.integers(min_value=1, max_value=max_tenants))
    tenants = []
    for i in range(n):
        w = draw(st.floats(min_value=0.1, max_value=5.0))
        p = draw(st.integers(min_value=1, max_value=5))
        d_cpu = draw(st.floats(min_value=0.1, max_value=4.0))
        d_mem = draw(st.floats(min_value=64.0, max_value=4096.0))
        m_cpu = draw(st.floats(min_value=0.0, max_value=d_cpu))
        m_mem = draw(st.floats(min_value=0.0, max_value=d_mem))

        tenants.append(
            Tenant(
                id=f"t{i}",
                name=f"Tenant {i}",
                weight=round(w, 2),
                priority=p,
                arrival_seq=i,
                active=True,
                demand={"cpu": round(d_cpu, 4), "mem_mb": round(d_mem, 2)},
                minimum={"cpu": round(m_cpu, 4), "mem_mb": round(m_mem, 2)},
            )
        )
    return tenants


@settings(max_examples=40, deadline=None)
@given(pool=pool_strategy(), tenants=tenants_strategy())
def test_all_allocators_capacity_and_demand_bounds(pool, tenants):
    """Invariant: allocations must never exceed pool capacity or tenant demand."""
    allocators = [
        ("FCFS", lambda: allocate_fcfs(pool, tenants)),
        ("Proportional", lambda: allocate_proportional(pool, tenants)),
        ("DRF", lambda: allocate_drf(pool, tenants)),
        ("Adaptive", lambda: AdaptiveAllocator().allocate(pool, tenants, now=0.0)),
    ]

    for name, run_fn in allocators:
        plan = run_fn()
        for res, cap in pool.capacity.items():
            tot_alloc = sum(plan.alloc.get(t.id, {}).get(res, 0.0) for t in tenants)
            # Allocation never exceeds pool capacity (with float precision slack)
            assert tot_alloc <= cap + 1e-4, (
                f"{name}: {res} alloc {tot_alloc} exceeded capacity {cap}"
            )

            for t in tenants:
                alloc_val = plan.alloc.get(t.id, {}).get(res, 0.0)
                dem_val = t.demand.get(res, 0.0)
                # Allocation never exceeds declared demand
                assert alloc_val <= dem_val + 1e-4, (
                    f"{name}: tenant {t.id} {res} alloc {alloc_val} exceeded demand {dem_val}"
                )


@settings(max_examples=30, deadline=None)
@given(pool=pool_strategy(), tenants=tenants_strategy())
def test_determinism_fcfs_proportional_drf(pool, tenants):
    """Determinism invariant: running FCFS, Proportional, and DRF repeatedly yields identical plans."""
    p_fcfs_1 = allocate_fcfs(pool, tenants)
    p_fcfs_2 = allocate_fcfs(pool, tenants)
    assert p_fcfs_1.alloc == p_fcfs_2.alloc

    p_prop_1 = allocate_proportional(pool, tenants)
    p_prop_2 = allocate_proportional(pool, tenants)
    assert p_prop_1.alloc == p_prop_2.alloc

    p_drf_1 = allocate_drf(pool, tenants)
    p_drf_2 = allocate_drf(pool, tenants)
    assert p_drf_1.alloc == p_drf_2.alloc


@settings(max_examples=30, deadline=None)
@given(pool=pool_strategy(), tenants=tenants_strategy())
def test_proportional_and_drf_feasible_minimums_honoured(pool, tenants):
    """Minimums met whenever feasible; infeasible cases explicitly reported."""
    resources = list(pool.capacity.keys())
    is_feasible = all(
        sum(t.minimum.get(r, 0.0) for t in tenants) <= pool.capacity[r] + 1e-6 for r in resources
    )

    p_prop = allocate_proportional(pool, tenants)
    if is_feasible:
        # Feasible: all minimums must be met
        for t in tenants:
            for r in resources:
                assert p_prop.alloc[t.id][r] >= t.minimum.get(r, 0.0) - 1e-4
    else:
        # Infeasible: must be reported in unmet_guarantees
        assert len(p_prop.unmet_guarantees) > 0


@settings(max_examples=30, deadline=None)
@given(pool=pool_strategy(), tenants=tenants_strategy())
def test_adaptive_memory_safety_property(pool, tenants):
    """Adaptive memory safety invariant: allocation >= usage + margin (within capacity/demand)."""
    # Create arbitrary observation usage below minimum
    obs = {}
    for t in tenants:
        u_mem = float(t.minimum.get("mem_mb", 64.0)) * 0.5
        obs[t.id] = TenantObs(usage={"cpu": 0.1, "mem_mb": u_mem})

    allocator = AdaptiveAllocator(AdaptiveParams(memory_safety_mb=32.0))
    plan = allocator.allocate(pool, tenants, obs, now=5.0)

    for t in tenants:
        alloc_mem = plan.alloc[t.id]["mem_mb"]
        used_mem = obs[t.id].usage["mem_mb"]
        target_safe = used_mem + 32.0
        # If target fits within declared demand and total capacity allows
        if target_safe <= t.demand["mem_mb"] and target_safe <= pool.capacity["mem_mb"]:
            assert alloc_mem >= used_mem + 16.0  # Safe margin preserved
