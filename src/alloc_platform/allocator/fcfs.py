"""First-Come, First-Served (FCFS) resource allocation policy."""

import time

from alloc_platform.allocator.types import Plan, Pool, Tenant, TenantObs
from alloc_platform.allocator.validate import validate_inputs


def allocate_fcfs(
    pool: Pool,
    tenants: list[Tenant],
    obs: dict[str, TenantObs] | None = None,
    prev: Plan | None = None,
) -> Plan:
    """Allocates resources strictly by tenant arrival sequence.

    Deterministic baseline algorithm. Active tenants are sorted by `arrival_seq` ascending.
    Each tenant receives min(demand, remaining_capacity) per resource in sequence until
    capacity is exhausted.

    NOTE: Minimum guarantees are honoured only if sufficient capacity remains when the
    tenant's turn arrives. Infeasible minimums are listed in `unmet_guarantees`.
    """
    start_time = time.perf_counter()
    validate_inputs(pool, tenants)

    active_tenants = [t for t in tenants if t.active]
    sorted_tenants = sorted(active_tenants, key=lambda t: (t.arrival_seq, t.id))

    remaining = {res: float(cap) for res, cap in pool.capacity.items()}
    alloc: dict[str, dict[str, float]] = {
        t.id: {res: 0.0 for res in pool.capacity} for t in sorted_tenants
    }
    unmet_guarantees: list[tuple[str, str]] = []
    notes: list[str] = [
        "FCFS allocates strictly by arrival sequence; minimum guarantees are honoured only if capacity remains."
    ]

    for t in sorted_tenants:
        for res, cap in pool.capacity.items():
            dem = float(t.demand.get(res, 0.0))
            given = min(dem, max(0.0, remaining[res]))
            alloc[t.id][res] = round(given, 6)
            remaining[res] -= given

            req_min = float(t.minimum.get(res, 0.0))
            if alloc[t.id][res] < req_min - 1e-9:
                unmet_guarantees.append((t.id, res))

    solve_ms = (time.perf_counter() - start_time) * 1000.0
    return Plan(
        alloc=alloc,
        unmet_guarantees=unmet_guarantees,
        notes=notes,
        solve_ms=round(solve_ms, 4),
    )
