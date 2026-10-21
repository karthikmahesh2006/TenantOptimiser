"""Performance and fairness metrics for resource allocation evaluation.

All functions are pure, deterministic, and fully documented with mathematical definitions.
Primary fairness metric is Jain's index on (satisfaction_i / weight_i) over active tenants.
Secondary fairness metric is Jain's index on dominant shares over active tenants.
"""

from collections.abc import Callable, Sequence

import numpy as np

from alloc_platform.allocator.types import Plan


def jain_index(values: Sequence[float]) -> float:
    """Computes Jain's Fairness Index over a sequence of non-negative values.

    Formula:
        J(x_1, ..., x_n) = (sum_{i=1}^n x_i)^2 / (n * sum_{i=1}^n x_i^2)

    Properties:
        - Bounded in [1/n, 1.0].
        - J = 1.0 indicates perfect equality across all values.
        - If all values are 0 or list is empty, returns 1.0 (vacuously fair).

    Docstring specification:
        This function computes the normalized equality of the supplied array `values`.
        When evaluating fairness:
        - Primary metric: values = [satisfaction_i / weight_i for all active tenants i]
        - Secondary metric: values = [dominant_share_i / weight_i for all active tenants i]
    """
    if not values:
        return 1.0
    arr = np.asarray(values, dtype=np.float64)
    if np.all(arr <= 1e-12):
        return 1.0
    n = len(arr)
    sum_x = np.sum(arr)
    sum_x_sq = np.sum(arr**2)
    if sum_x_sq <= 1e-12:
        return 1.0
    index = float((sum_x**2) / (n * sum_x_sq))
    return max(0.0, min(1.0, index))


def satisfaction(alloc: dict[str, float], demand: dict[str, float]) -> float:
    r"""Computes bottleneck satisfaction factor s_i for a single tenant.

    Formula:
        s_i = min_{r: demand_r > 0} (alloc_r / demand_r)

    DENOMINATOR SPECIFICATION:
        The denominator `demand` is strictly the tenant's DECLARED demand (as specified
        in the tenant SLA/contract), NOT the dynamically estimated demand \hat{d}.
        This ensures uniform and unskewed comparative benchmarking across all policies.

    Bounded in [0.0, 1.0]. If all demands are 0, returns 1.0.
    """
    ratios = []
    for res, dem in demand.items():
        if dem > 1e-9:
            given = alloc.get(res, 0.0)
            ratios.append(given / dem)
    if not ratios:
        return 1.0
    return max(0.0, min(1.0, float(min(ratios))))


def unmet_demand(alloc: dict[str, float], demand: dict[str, float]) -> dict[str, float]:
    r"""Computes unmet demand per resource: max(0.0, demand_r - alloc_r).

    TARGET SPECIFICATION:
        The `demand` parameter represents the tenant's DECLARED demand (contractual SLA request),
        NOT the dynamically estimated demand \hat{d}.
    """
    return {res: max(0.0, float(demand.get(res, 0.0) - alloc.get(res, 0.0))) for res in demand}


def allocated_util(
    allocations: Sequence[dict[str, float]], capacity: dict[str, float]
) -> dict[str, float]:
    """Computes the fraction of pool capacity allocated (reserved) to tenants.

    Formula:
        U_{alloc, r} = (sum_i alloc_{i, r}) / capacity_r

    Reported separately from consumed utilisation to distinguish reserved quota from actual load.
    """
    util: dict[str, float] = {}
    for res, cap in capacity.items():
        if cap <= 1e-9:
            util[res] = 0.0
        else:
            total_alloc = sum(a.get(res, 0.0) for a in allocations)
            util[res] = total_alloc / cap
    return util


def consumed_util(
    consumptions: Sequence[dict[str, float]], capacity: dict[str, float]
) -> dict[str, float]:
    """Computes the fraction of pool capacity actually consumed (used) by tenants.

    Formula:
        U_{consumed, r} = (sum_i usage_{i, r}) / capacity_r

    Reported separately from allocated utilisation to measure real hardware expenditure.
    """
    util: dict[str, float] = {}
    for res, cap in capacity.items():
        if cap <= 1e-9:
            util[res] = 0.0
        else:
            total_used = sum(c.get(res, 0.0) for c in consumptions)
            util[res] = total_used / cap
    return util


def useful_util(useful_work_units: float, nominal_capacity_work_units: float) -> float:
    """Computes goodput efficiency: useful work completed strictly within deadline.

    Formula:
        U_{useful} = useful_work_completed / nominal_capacity_work_units
    """
    if nominal_capacity_work_units <= 1e-9:
        return 0.0
    return max(0.0, min(1.0, float(useful_work_units / nominal_capacity_work_units)))


def unused_reserved(
    allocations: Sequence[dict[str, float]], consumptions: Sequence[dict[str, float]]
) -> dict[str, float]:
    """Computes allocated headroom that remained idle (unconsumed by tenants).

    Formula:
        sum_i max(0.0, alloc_{i, r} - usage_{i, r})
    """
    return idle_reserved(allocations, consumptions)


def idle_reserved(
    allocations: Sequence[dict[str, float]], consumptions: Sequence[dict[str, float]]
) -> dict[str, float]:
    """Computes idle reserved capacity (allocated minus consumed) per resource.

    Formula:
        sum_i max(0.0, alloc_{i, r} - usage_{i, r})

    Provenance:
        CALCULATED (derived from allocations and measured usages).
    """
    diff: dict[str, float] = {}
    for a, c in zip(allocations, consumptions):
        for res, alloc_val in a.items():
            used_val = c.get(res, 0.0)
            diff[res] = diff.get(res, 0.0) + max(0.0, alloc_val - used_val)
    return diff


def guarantee_satisfaction(
    allocations: dict[str, dict[str, float]],
    minimums: dict[str, dict[str, float]],
    feasible_ids: set[str],
) -> tuple[float, float]:
    """Measures the proportion of guaranteed minimum requirements satisfied.

    Calculated separately for feasible and infeasible tenants:
    - feasible_satisfaction: fraction of minimum guarantees met for tenants in `feasible_ids`
    - infeasible_satisfaction: fraction of minimum guarantees met for tenants not in `feasible_ids`

    Returns:
        (feasible_rate, infeasible_rate)
    """
    feasible_met = 0
    feasible_total = 0
    infeasible_met = 0
    infeasible_total = 0

    for tid, reqs in minimums.items():
        alloc_res = allocations.get(tid, {})
        for res, min_val in reqs.items():
            if min_val <= 1e-9:
                continue
            is_met = alloc_res.get(res, 0.0) >= min_val - 1e-6
            if tid in feasible_ids:
                feasible_total += 1
                if is_met:
                    feasible_met += 1
            else:
                infeasible_total += 1
                if is_met:
                    infeasible_met += 1

    feas_rate = (feasible_met / feasible_total) if feasible_total > 0 else 1.0
    infeas_rate = (infeasible_met / infeasible_total) if infeasible_total > 0 else 0.0
    return float(feas_rate), float(infeas_rate)


def starvation_events(
    allocations_history: Sequence[dict[str, dict[str, float]]],
    demands_history: Sequence[dict[str, dict[str, float]]],
    threshold: float = 0.01,
) -> int:
    """Counts starvation occurrences where a tenant had non-zero demand but received < threshold."""
    starved_count = 0
    for alloc_map, demand_map in zip(allocations_history, demands_history):
        for tid, demands in demand_map.items():
            has_demand = any(d > threshold for d in demands.values())
            if has_demand:
                tenant_alloc = alloc_map.get(tid, {})
                is_starved = all(tenant_alloc.get(r, 0.0) < threshold for r in demands)
                if is_starved:
                    starved_count += 1
    return starved_count


def churn(plan_sequence: Sequence[Plan], capacity: dict[str, float]) -> float:
    """Computes total normalized allocation churn across consecutive epochs.

    Formula:
        (1 / (K - 1)) * sum_{k=1}^{K-1} sum_i sum_r (|alloc_{i, r}^{(k)} - alloc_{i, r}^{(k-1)}| / capacity_r)
    """
    if len(plan_sequence) < 2:
        return 0.0

    total_delta = 0.0
    k_transitions = len(plan_sequence) - 1

    for k in range(k_transitions):
        p_prev = plan_sequence[k].alloc
        p_curr = plan_sequence[k + 1].alloc
        all_tenants = set(p_prev.keys()) | set(p_curr.keys())

        for tid in all_tenants:
            curr_res = p_curr.get(tid, {})
            prev_res = p_prev.get(tid, {})
            for res, cap in capacity.items():
                if cap > 1e-9:
                    d = abs(curr_res.get(res, 0.0) - prev_res.get(res, 0.0))
                    total_delta += d / cap

    return float(total_delta / k_transitions)


def count_changes(plan_sequence: Sequence[Plan], threshold: float = 0.01) -> int:
    """Counts number of discrete tenant-resource limit updates with magnitude > threshold."""
    if len(plan_sequence) < 2:
        return 0

    change_count = 0
    for k in range(len(plan_sequence) - 1):
        p_prev = plan_sequence[k].alloc
        p_curr = plan_sequence[k + 1].alloc
        all_tenants = set(p_prev.keys()) | set(p_curr.keys())

        for tid in all_tenants:
            curr_res = p_curr.get(tid, {})
            prev_res = p_prev.get(tid, {})
            for res in set(curr_res.keys()) | set(prev_res.keys()):
                if abs(curr_res.get(res, 0.0) - prev_res.get(res, 0.0)) > threshold:
                    change_count += 1
    return change_count


def tenant_churn(
    plan_sequence: Sequence[Plan], capacity: dict[str, float], tenant_id: str
) -> float:
    """Computes normalized allocation churn for a single tenant across consecutive epochs.

    Formula:
        (1 / (K - 1)) * sum_{k=1}^{K-1} sum_r (|alloc_{tenant_id, r}^{(k)} - alloc_{tenant_id, r}^{(k-1)}| / capacity_r)
    """
    if len(plan_sequence) < 2:
        return 0.0

    total_delta = 0.0
    k_transitions = len(plan_sequence) - 1

    for k in range(k_transitions):
        curr_res = plan_sequence[k + 1].alloc.get(tenant_id, {})
        prev_res = plan_sequence[k].alloc.get(tenant_id, {})
        for res, cap in capacity.items():
            if cap > 1e-9:
                d = abs(curr_res.get(res, 0.0) - prev_res.get(res, 0.0))
                total_delta += d / cap

    return float(total_delta / k_transitions)


def tenant_count_changes(
    plan_sequence: Sequence[Plan], tenant_id: str, threshold: float = 0.01
) -> int:
    """Counts discrete limit updates with magnitude > threshold for a single tenant."""
    if len(plan_sequence) < 2:
        return 0

    change_count = 0
    for k in range(len(plan_sequence) - 1):
        curr_res = plan_sequence[k + 1].alloc.get(tenant_id, {})
        prev_res = plan_sequence[k].alloc.get(tenant_id, {})
        for res in set(curr_res.keys()) | set(prev_res.keys()):
            if abs(curr_res.get(res, 0.0) - prev_res.get(res, 0.0)) > threshold:
                change_count += 1
    return change_count


def pct_change(value: float, baseline: float, higher_is_better: bool = True) -> float:
    """Computes relative percentage change versus baseline: ((value - baseline) / baseline) * 100."""
    if abs(baseline) <= 1e-12:
        return 0.0
    pct = ((value - baseline) / abs(baseline)) * 100.0
    return float(pct)


def bootstrap_ci(
    samples: Sequence[float],
    stat: Callable[[np.ndarray], float] = np.mean,
    n: int = 2000,
    seed: int = 42,
    ci: float = 0.95,
) -> tuple[float, float, float]:
    """Computes non-parametric bootstrap confidence interval.

    Args:
        samples: Sequence of scalar measurements.
        stat: Statistical function operating on 1D numpy array (default np.mean).
        n: Number of bootstrap iterations (default 2000).
        seed: Fixed pseudo-random seed for determinism.
        ci: Confidence interval fraction (default 0.95 for 95% CI).

    Returns:
        (point_estimate, ci_lower, ci_upper)
    """
    arr = np.asarray(samples, dtype=np.float64)
    if len(arr) == 0:
        return 0.0, 0.0, 0.0
    if len(arr) == 1:
        val = float(arr[0])
        return val, val, val

    rng = np.random.default_rng(seed)
    point_estimate = float(stat(arr))
    boot_stats = np.empty(n, dtype=np.float64)

    for i in range(n):
        resample = rng.choice(arr, size=len(arr), replace=True)
        boot_stats[i] = stat(resample)

    alpha = (1.0 - ci) / 2.0
    ci_lower = float(np.percentile(boot_stats, 100.0 * alpha))
    ci_upper = float(np.percentile(boot_stats, 100.0 * (1.0 - alpha)))
    return point_estimate, ci_lower, ci_upper
