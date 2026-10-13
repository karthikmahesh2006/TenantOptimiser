"""Weighted Proportional resource allocation policy with iterative water-filling."""

import time

from alloc_platform.allocator.types import Plan, Pool, Tenant, TenantObs
from alloc_platform.allocator.validate import validate_inputs


def allocate_proportional(
    pool: Pool,
    tenants: list[Tenant],
    obs: dict[str, TenantObs] | None = None,
    prev: Plan | None = None,
) -> Plan:
    """Allocates resources proportionally to weight * (demand - minimum) with water-filling.

    Per resource:
    1. Grant minimum guarantees first.
    2. If minimums exceed pool capacity, scale down proportionally and record unmet guarantees.
    3. If capacity remains, distribute remainder proportionally to weight * (demand - minimum)
       using iterative water-filling (cap at declared demand, redistribute surplus among
       uncapped tenants until all surplus is absorbed or all tenants reach demand).
    """
    start_time = time.perf_counter()
    validate_inputs(pool, tenants)

    active_tenants = [t for t in tenants if t.active]
    if not active_tenants:
        return Plan(
            alloc={},
            unmet_guarantees=[],
            notes=["No active tenants."],
            solve_ms=(time.perf_counter() - start_time) * 1000.0,
        )

    alloc: dict[str, dict[str, float]] = {
        t.id: {res: 0.0 for res in pool.capacity} for t in active_tenants
    }
    unmet_guarantees: list[tuple[str, str]] = []
    notes: list[str] = []

    for res, cap in pool.capacity.items():
        total_min = sum(float(t.minimum.get(res, 0.0)) for t in active_tenants)

        if total_min > cap + 1e-9:
            # Minimums are infeasible: scale down proportionally
            notes.append(
                f"Resource {res} minimums ({total_min:.2f}) exceed capacity ({cap:.2f}); scaled down proportionally."
            )
            scale = cap / total_min if total_min > 0 else 0.0
            for t in active_tenants:
                req_min = float(t.minimum.get(res, 0.0))
                given = req_min * scale
                alloc[t.id][res] = round(given, 6)
                if given < req_min - 1e-9:
                    unmet_guarantees.append((t.id, res))
            # No remainder left for water-filling
            continue

        # Minimums are feasible: grant minimums first
        for t in active_tenants:
            alloc[t.id][res] = float(t.minimum.get(res, 0.0))

        remaining = cap - total_min
        # Water-filling for remainder
        uncapped: set[str] = {
            t.id for t in active_tenants if float(t.demand.get(res, 0.0)) > alloc[t.id][res] + 1e-9
        }
        tenant_map = {t.id: t for t in active_tenants}

        while remaining > 1e-9 and uncapped:
            # Calculate sum of weight * remaining_need for uncapped
            weights = {}
            for tid in uncapped:
                t = tenant_map[tid]
                need = float(t.demand.get(res, 0.0)) - float(t.minimum.get(res, 0.0))
                if need <= 1e-9:
                    need = 1.0  # Fallback to pure weight if demand == minimum
                weights[tid] = t.weight * need

            total_w = sum(weights.values())
            if total_w <= 1e-9:
                break

            # Find if any tenant would exceed their demand with this step's share
            capped_this_round = set()
            for tid in list(uncapped):
                t = tenant_map[tid]
                share = remaining * (weights[tid] / total_w)
                dem = float(t.demand.get(res, 0.0))
                if alloc[tid][res] + share >= dem - 1e-9:
                    capped_this_round.add(tid)

            if not capped_this_round:
                # All uncapped tenants can absorb their full proportional share
                for tid in uncapped:
                    share = remaining * (weights[tid] / total_w)
                    alloc[tid][res] += share
                remaining = 0.0
                break
            else:
                # Cap the tenants that hit their limit, subtract given amount from remaining, repeat
                for tid in capped_this_round:
                    t = tenant_map[tid]
                    dem = float(t.demand.get(res, 0.0))
                    delta = dem - alloc[tid][res]
                    alloc[tid][res] = dem
                    remaining = max(0.0, remaining - delta)
                    uncapped.remove(tid)

        # Round allocations
        for t in active_tenants:
            alloc[t.id][res] = round(alloc[t.id][res], 6)

    solve_ms = (time.perf_counter() - start_time) * 1000.0
    return Plan(
        alloc=alloc,
        unmet_guarantees=unmet_guarantees,
        notes=notes,
        solve_ms=round(solve_ms, 4),
    )
