"""Dominant Resource Fairness (DRF) allocation policy."""

import time

from alloc_platform.allocator.types import Plan, Pool, Tenant, TenantObs
from alloc_platform.allocator.validate import validate_inputs


def allocate_drf(
    pool: Pool,
    tenants: list[Tenant],
    obs: dict[str, TenantObs] | None = None,
    prev: Plan | None = None,
) -> Plan:
    """Allocates resources using weighted Dominant Resource Fairness (DRF).

    Dominant share of tenant i: max_{r} (alloc_{i,r} / capacity_{r}).
    Weighted dominant share: dominant_share / weight.

    Progressive filling increases the satisfaction factor s_i in [s_min, 1] for tenants
    with the lowest weighted dominant share until:
      - A tenant hits s_i = 1 (demand fully satisfied), freezing the tenant.
      - A resource saturates (sum_i alloc_{i,r} == capacity_{r}), freezing all tenants consuming it.

    Minimum guarantees are honoured first. If minimums are infeasible, they are scaled down
    and recorded in `unmet_guarantees`.
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

    # Check minimum feasibility
    infeasible_res = []
    for res, cap in pool.capacity.items():
        total_min = sum(float(t.minimum.get(res, 0.0)) for t in active_tenants)
        if total_min > cap + 1e-9:
            infeasible_res.append(res)

    if infeasible_res:
        notes.append(
            f"Minimums infeasible for resources: {', '.join(infeasible_res)}; scaled down."
        )
        for res, cap in pool.capacity.items():
            total_min = sum(float(t.minimum.get(res, 0.0)) for t in active_tenants)
            scale = cap / total_min if total_min > cap else 1.0
            for t in active_tenants:
                req_min = float(t.minimum.get(res, 0.0))
                given = req_min * scale
                alloc[t.id][res] = round(given, 6)
                if given < req_min - 1e-9:
                    unmet_guarantees.append((t.id, res))
        solve_ms = (time.perf_counter() - start_time) * 1000.0
        return Plan(
            alloc=alloc,
            unmet_guarantees=unmet_guarantees,
            notes=notes,
            solve_ms=round(solve_ms, 4),
        )

    # Initial satisfaction factors from minimum guarantees
    # s_min_i = max over r of (minimum / demand) where demand > 0
    s: dict[str, float] = {}
    tenant_map = {t.id: t for t in active_tenants}

    for t in active_tenants:
        ratios = [
            float(t.minimum.get(r, 0.0)) / float(t.demand[r])
            for r in pool.capacity
            if float(t.demand.get(r, 0.0)) > 1e-9
        ]
        s[t.id] = max(ratios) if ratios else 0.0
        # Set initial allocation
        for r in pool.capacity:
            alloc[t.id][r] = s[t.id] * float(t.demand.get(r, 0.0))

    # Progressive filling loop
    # Unfrozen tenants can still receive more resources
    unfrozen: set[str] = {
        t.id
        for t in active_tenants
        if s[t.id] < 1.0 - 1e-9 and any(float(t.demand.get(r, 0.0)) > 1e-9 for r in pool.capacity)
    }

    # Saturated resources
    saturated_res: set[str] = set()

    step_limit = 2000
    step = 0

    while unfrozen and step < step_limit:
        step += 1

        # Calculate weighted dominant share for each unfrozen tenant
        # dominant share d_i = max_r (alloc_{i,r} / capacity_r)
        # weighted dominant share wds_i = d_i / weight_i
        wds: dict[str, float] = {}
        for tid in unfrozen:
            t = tenant_map[tid]
            shares = [
                alloc[tid][r] / pool.capacity[r] for r in pool.capacity if pool.capacity[r] > 1e-9
            ]
            d_i = max(shares) if shares else 0.0
            wds[tid] = d_i / t.weight

        min_wds = min(wds.values())
        # Find all tenants sharing the minimum weighted dominant share (within small tolerance)
        candidates = [tid for tid in unfrozen if abs(wds[tid] - min_wds) < 1e-7]

        # Determine how much min_wds can increase:
        # 1. Until the next lowest unfrozen tenant catches up
        other_wds = [val for tid, val in wds.items() if val > min_wds + 1e-7]
        delta_wds_catchup = (min(other_wds) - min_wds) if other_wds else float("inf")

        # 2. Until any candidate reaches s_i = 1.0
        # Delta in s_i for a candidate:
        # d_i = max_r (s_i * demand_{i,r} / capacity_r) = s_i * dom_ratio_i
        # wds_i = s_i * (dom_ratio_i / weight_i)
        # So delta_s_i = delta_wds * weight_i / dom_ratio_i
        delta_wds_s1 = float("inf")
        candidate_dom_ratios: dict[str, float] = {}
        for tid in candidates:
            t = tenant_map[tid]
            ratios = [
                float(t.demand.get(r, 0.0)) / pool.capacity[r]
                for r in pool.capacity
                if pool.capacity[r] > 1e-9
            ]
            dom_ratio = max(ratios) if ratios else 0.0
            candidate_dom_ratios[tid] = dom_ratio
            if dom_ratio > 1e-9:
                # rate of wds increase per unit s: dom_ratio / weight
                rate = dom_ratio / t.weight
                max_delta_s = 1.0 - s[tid]
                if max_delta_s > 0:
                    delta_wds_s1 = min(delta_wds_s1, max_delta_s * rate)

        # 3. Until any resource saturates
        # For resource r, current usage: sum_all alloc_{j,r}
        # Available capacity: pool.capacity[r] - sum_all alloc_{j,r}
        # In candidates, allocation increases by delta_s_i * demand_{i,r}
        # = delta_wds * (weight_i / dom_ratio_i) * demand_{i,r}
        delta_wds_sat = float("inf")
        for r, cap in pool.capacity.items():
            current_usage = sum(alloc[t.id][r] for t in active_tenants)
            headroom = cap - current_usage
            if headroom <= 1e-9:
                saturated_res.add(r)
                continue

            rate_r = 0.0
            for tid in candidates:
                t = tenant_map[tid]
                dom_ratio = candidate_dom_ratios[tid]
                if dom_ratio > 1e-9:
                    delta_s_per_wds = t.weight / dom_ratio
                    rate_r += delta_s_per_wds * float(t.demand.get(r, 0.0))

            if rate_r > 1e-9:
                delta_for_r = headroom / rate_r
                if delta_for_r < delta_wds_sat:
                    delta_wds_sat = delta_for_r

        # Choose the limiting delta_wds
        delta_wds = min(delta_wds_catchup, delta_wds_s1, delta_wds_sat)
        if delta_wds <= 1e-12 or delta_wds == float("inf"):
            break

        # Apply increase
        for tid in candidates:
            t = tenant_map[tid]
            dom_ratio = candidate_dom_ratios[tid]
            if dom_ratio > 1e-9:
                delta_s = delta_wds * (t.weight / dom_ratio)
                s[tid] = min(1.0, s[tid] + delta_s)
                for r in pool.capacity:
                    alloc[tid][r] = s[tid] * float(t.demand.get(r, 0.0))

        # Check newly frozen candidates (s_i == 1)
        for tid in list(candidates):
            if s[tid] >= 1.0 - 1e-9:
                unfrozen.discard(tid)

        # Check saturated resources and freeze tenants consuming them
        for r, cap in pool.capacity.items():
            current_usage = sum(alloc[t.id][r] for t in active_tenants)
            if current_usage >= cap - 1e-7:
                saturated_res.add(r)
                for tid in list(unfrozen):
                    if float(tenant_map[tid].demand.get(r, 0.0)) > 1e-9:
                        unfrozen.discard(tid)

    # Round final allocations and ensure they do not exceed capacity or demand
    for r, cap in pool.capacity.items():
        total_alloc = sum(alloc[t.id][r] for t in active_tenants)
        if total_alloc > cap:
            # Minor numerical excess trimming from largest recipient
            excess = total_alloc - cap
            for t in sorted(active_tenants, key=lambda x: -alloc[x.id][r]):
                trim = min(excess, alloc[t.id][r] - float(t.minimum.get(r, 0.0)))
                alloc[t.id][r] -= max(0.0, trim)
                excess -= max(0.0, trim)
                if excess <= 1e-9:
                    break

    for t in active_tenants:
        for r in pool.capacity:
            alloc[t.id][r] = round(min(alloc[t.id][r], float(t.demand.get(r, 0.0))), 6)
            req_min = float(t.minimum.get(r, 0.0))
            if alloc[t.id][r] < req_min - 1e-9:
                unmet_guarantees.append((t.id, r))

    solve_ms = (time.perf_counter() - start_time) * 1000.0
    return Plan(
        alloc=alloc,
        unmet_guarantees=unmet_guarantees,
        notes=notes,
        solve_ms=round(solve_ms, 4),
    )
