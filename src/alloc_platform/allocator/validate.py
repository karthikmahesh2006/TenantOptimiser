"""Input validation and feasibility checking for resource allocation."""

import math

from alloc_platform.allocator.types import Pool, Tenant
from alloc_platform.config import config


def validate_inputs(pool: Pool, tenants: list[Tenant]) -> None:
    """Validates pool and tenant specifications against physical and structural invariants.

    Raises:
        ValueError: On negative values, NaN/infinity, minimum > demand, duplicate IDs,
                    tenant count exceeding platform max, or non-positive weights.
    """
    if len(tenants) > config.max_tenants:
        raise ValueError(
            f"Tenant count ({len(tenants)}) exceeds configured maximum of {config.max_tenants}"
        )

    seen_ids = set()
    for tenant in tenants:
        if tenant.id in seen_ids:
            raise ValueError(f"Duplicate tenant ID detected: {tenant.id}")
        seen_ids.add(tenant.id)

        if math.isnan(tenant.weight) or math.isinf(tenant.weight) or tenant.weight <= 0:
            raise ValueError(
                f"Tenant {tenant.id} weight must be positive and finite, got {tenant.weight}"
            )

        # Validate demands and minimums
        for res, val in tenant.demand.items():
            if math.isnan(val) or math.isinf(val) or val < 0:
                raise ValueError(
                    f"Tenant {tenant.id} demand for {res} must be non-negative and finite, got {val}"
                )

        for res, min_val in tenant.minimum.items():
            if math.isnan(min_val) or math.isinf(min_val) or min_val < 0:
                raise ValueError(
                    f"Tenant {tenant.id} minimum for {res} must be non-negative and finite, got {min_val}"
                )
            dem_val = tenant.demand.get(res, 0.0)
            if min_val > dem_val + 1e-9:
                raise ValueError(
                    f"Tenant {tenant.id} minimum {min_val} exceeds declared demand {dem_val} for {res}"
                )

    # Validate pool capacity
    for res, cap in pool.capacity.items():
        if math.isnan(cap) or math.isinf(cap) or cap < 0:
            raise ValueError(f"Pool capacity for {res} must be non-negative and finite, got {cap}")


def check_guarantee_feasibility(
    pool: Pool, tenants: list[Tenant]
) -> tuple[bool, list[str], list[str]]:
    """Checks whether guaranteed minimums of active tenants can be simultaneously satisfied.

    If sum of minimums exceeds pool capacity for any resource, tenants are prioritized
    and admitted by (priority DESC, weight DESC, arrival_seq ASC). Tenants that cannot be
    accommodated without exceeding capacity are returned as rejected.

    Returns:
        (feasible, admitted_ids, rejected_ids)
    """
    active_tenants = [t for t in tenants if t.active]
    if not active_tenants:
        return True, [], []

    # Check sum of minimums per resource
    resources = list(pool.capacity.keys())
    total_mins = {r: sum(t.minimum.get(r, 0.0) for t in active_tenants) for r in resources}

    is_feasible = all(total_mins[r] <= pool.capacity[r] + 1e-9 for r in resources)
    if is_feasible:
        return True, [t.id for t in active_tenants], []

    # Infeasible: rank by (priority DESC, weight DESC, arrival_seq ASC)
    sorted_tenants = sorted(
        active_tenants,
        key=lambda t: (-t.priority, -t.weight, t.arrival_seq),
    )

    remaining_cap = dict(pool.capacity)
    admitted_ids: list[str] = []
    rejected_ids: list[str] = []

    for t in sorted_tenants:
        # Check if tenant's minimums fit within remaining capacity across all resources
        fits = all(t.minimum.get(r, 0.0) <= remaining_cap[r] + 1e-9 for r in resources)
        if fits:
            admitted_ids.append(t.id)
            for r in resources:
                remaining_cap[r] -= t.minimum.get(r, 0.0)
        else:
            rejected_ids.append(t.id)

    return False, admitted_ids, rejected_ids
