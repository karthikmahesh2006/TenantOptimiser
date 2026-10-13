"""Adaptive convex optimization resource allocator with stability filtering."""

import time
from dataclasses import dataclass

import numpy as np
from scipy.optimize import minimize

from alloc_platform.allocator.proportional import allocate_proportional
from alloc_platform.allocator.types import Plan, Pool, Tenant, TenantObs
from alloc_platform.allocator.validate import check_guarantee_feasibility, validate_inputs


@dataclass
class AdaptiveParams:
    """Hyperparameters and ablation flags for the Adaptive Allocator.

    Attributes:
        epoch_s: Allocation control loop period in seconds.
        ewma_alpha: Exponential moving average smoothing parameter [0, 1].
        headroom_h: Fractional headroom added to smoothed demand estimates.
        lambda_churn: Regularization parameter penalizing churn from previous plan.
        deadband: Relative change threshold below which updates are suppressed.
        min_dwell_s: Minimum dwell time before voluntary limit reduction.
        max_shrink_per_epoch: Maximum fractional contraction per epoch.
        memory_safety_mb: Margin in MB above current memory usage to prevent OOM.
        throttle_threshold: Throttle ratio triggering fast-up demand escalation.
        use_estimation: Ablation flag: whether to dynamically estimate demand.
        use_hysteresis: Ablation flag: whether to apply stability/churn filters.
        use_surplus: Ablation flag: whether to redistribute idle CPU to pressurized tenants.
    """

    epoch_s: float = 1.0
    ewma_alpha: float = 0.3
    headroom_h: float = 0.20
    lambda_churn: float = 0.10
    deadband: float = 0.05
    min_dwell_s: float = 3.0
    max_shrink_per_epoch: float = 0.20
    memory_safety_mb: float = 64.0
    throttle_threshold: float = 0.05
    use_estimation: bool = True
    use_hysteresis: bool = True
    use_surplus: bool = True


def _pseudo_huber(z: np.ndarray, delta: float = 1.0) -> np.ndarray:
    """Smooth pseudo-Huber loss function."""
    return delta**2 * (np.sqrt(1.0 + (z / delta) ** 2) - 1.0)


class AdaptiveAllocator:
    """Online adaptive resource optimizer with convex utility maximization."""

    def __init__(self, params: AdaptiveParams | None = None) -> None:
        self.params = params or AdaptiveParams()
        # State: EWMA usage per tenant and resource
        self.ewma_usage: dict[str, dict[str, float]] = {}
        # Last time limits were materially changed for tenant
        self.last_change_time: dict[str, float] = {}
        # Previous plan cache
        self.prev_plan: Plan | None = None
        # Previous backlog tracking for fast-up detection
        self.prev_backlog: dict[str, int] = {}

    def estimate_demand(
        self, tenants: list[Tenant], obs: dict[str, TenantObs] | None = None
    ) -> dict[str, dict[str, float]]:
        """Estimates effective workload demand using EWMA with fast-up / slow-down dynamics.

        If `use_estimation` is False, returns declared demand.
        Formula:
            dhat = min(demand, max(minimum, (1 + h) * ewma(usage)))
        If throttling exceeds threshold or backlog is growing, escalates estimate toward declared demand.
        """
        active_tenants = [t for t in tenants if t.active]
        if not self.params.use_estimation or not obs:
            return {
                t.id: {res: float(t.demand.get(res, 0.0)) for res in t.demand}
                for t in active_tenants
            }

        dhat: dict[str, dict[str, float]] = {}
        alpha = self.params.ewma_alpha
        h = self.params.headroom_h

        for t in active_tenants:
            t_obs = obs.get(t.id)
            dhat[t.id] = {}
            if t.id not in self.ewma_usage:
                self.ewma_usage[t.id] = {}

            # Check if tenant is under contention pressure
            is_pressured = False
            if t_obs:
                prev_b = self.prev_backlog.get(t.id, 0)
                if (
                    t_obs.throttle_ratio > self.params.throttle_threshold
                    or t_obs.backlog > prev_b
                    or t_obs.backlog > 0
                ):
                    is_pressured = True
                self.prev_backlog[t.id] = t_obs.backlog

            for res, dem_val in t.demand.items():
                min_val = float(t.minimum.get(res, 0.0))
                dem_val = float(dem_val)
                raw_used = float(t_obs.usage.get(res, 0.0)) if t_obs else min_val

                # Update EWMA
                prev_ewma = self.ewma_usage[t.id].get(res, raw_used)
                new_ewma = alpha * raw_used + (1.0 - alpha) * prev_ewma
                self.ewma_usage[t.id][res] = new_ewma

                # Base estimate
                est = (1.0 + h) * new_ewma
                est = max(min_val, min(dem_val, est))

                # Fast-up ramp if pressurized
                if is_pressured:
                    # Escalation toward declared demand
                    est = est + 0.5 * (dem_val - est)

                dhat[t.id][res] = round(est, 6)

        return dhat

    def solve_convex(
        self,
        pool: Pool,
        tenants: list[Tenant],
        dhat: dict[str, dict[str, float]],
        prev: Plan | None = None,
    ) -> tuple[dict[str, dict[str, float]], list[str], float]:
        """Maximizes log-utility penalized by churn using SLSQP.

        Objective:
            maximize sum_i w_i * log(s_i) - lambda * sum_i sum_r pseudo_huber(s_i * dhat_ir - prev_ir) / capacity_r
        Subject to:
            sum_i s_i * dhat_ir <= capacity_r for all r
            s_min_i <= s_i <= 1.0 for all i
        """
        start_t = time.perf_counter()
        active_tenants = [t for t in tenants if t.active]
        notes: list[str] = []
        n = len(active_tenants)

        if n == 0:
            return {}, notes, 0.0

        resources = list(pool.capacity.keys())
        # Tenant weights and minimum satisfaction factors
        weights = np.array([t.weight for t in active_tenants], dtype=np.float64)
        s_min = np.zeros(n, dtype=np.float64)

        for i, t in enumerate(active_tenants):
            ratios = [
                float(t.minimum.get(r, 0.0)) / dhat[t.id][r]
                for r in resources
                if dhat[t.id].get(r, 0.0) > 1e-9
            ]
            s_min[i] = min(1.0, max(ratios)) if ratios else 0.0

        # Construct feasible initial point using proportional allocator
        prop_plan = allocate_proportional(
            pool,
            [
                Tenant(
                    id=t.id,
                    name=t.name,
                    weight=t.weight,
                    priority=t.priority,
                    arrival_seq=t.arrival_seq,
                    active=t.active,
                    demand=dhat[t.id],
                    minimum=t.minimum,
                    workload_type=t.workload_type,
                )
                for t in active_tenants
            ],
        )

        s0 = np.ones(n, dtype=np.float64)
        for i, t in enumerate(active_tenants):
            p_alloc = prop_plan.alloc.get(t.id, {})
            ratios = [
                p_alloc.get(r, 0.0) / dhat[t.id][r]
                for r in resources
                if dhat[t.id].get(r, 0.0) > 1e-9
            ]
            s0[i] = max(s_min[i], min(1.0, min(ratios))) if ratios else 1.0

        # Extract prev allocations for churn penalty
        prev_alloc_arr = np.zeros((n, len(resources)), dtype=np.float64)
        if prev and prev.alloc:
            for i, t in enumerate(active_tenants):
                p_t = prev.alloc.get(t.id, {})
                for j, r in enumerate(resources):
                    prev_alloc_arr[i, j] = p_t.get(r, dhat[t.id].get(r, 0.0))
        else:
            for i, t in enumerate(active_tenants):
                for j, r in enumerate(resources):
                    prev_alloc_arr[i, j] = s0[i] * dhat[t.id].get(r, 0.0)

        dhat_matrix = np.zeros((n, len(resources)), dtype=np.float64)
        for i, t in enumerate(active_tenants):
            for j, r in enumerate(resources):
                dhat_matrix[i, j] = dhat[t.id].get(r, 0.0)

        caps = np.array([pool.capacity[r] for r in resources], dtype=np.float64)
        lam = self.params.lambda_churn

        # Objective function (negative utility for minimization)
        def objective(s_vec: np.ndarray) -> float:
            eps = 1e-8
            # Clip s to strictly positive for log
            s_clipped = np.clip(s_vec, eps, 1.0)
            log_utility = np.sum(weights * np.log(s_clipped))

            # Churn penalty: sum_i sum_r pseudo_huber(s_i * dhat_ir - prev_ir) / cap_r
            # Shape: (n, len(resources))
            alloc_matrix = s_vec[:, np.newaxis] * dhat_matrix
            diff = alloc_matrix - prev_alloc_arr
            ph = _pseudo_huber(diff)
            # Normalize by capacity
            churn_penalty = np.sum(ph / np.maximum(caps, 1e-6))

            # We maximize (utility - lam * churn) => minimize -(utility - lam * churn)
            return float(-(log_utility - lam * churn_penalty))

        # Constraints: sum_i s_i * dhat_ir <= capacity_r => capacity_r - sum_i s_i * dhat_ir >= 0
        constraints = []
        for j, r in enumerate(resources):

            def make_cap_constraint(res_idx: int):
                return {
                    "type": "ineq",
                    "fun": lambda s_vec, idx=res_idx: (
                        caps[idx] - np.sum(s_vec * dhat_matrix[:, idx])
                    ),
                }

            constraints.append(make_cap_constraint(j))

        # Bounds: s_min_i <= s_i <= 1.0
        bounds = [(float(s_min[i]), 1.0) for i in range(n)]

        # Run SLSQP optimizer
        try:
            res = minimize(
                fun=objective,
                x0=s0,
                method="SLSQP",
                bounds=bounds,
                constraints=constraints,
                options={"maxiter": 100, "ftol": 1e-5},
            )
            if res.success or res.status in (0, 9):  # 9 is iteration limit, often good enough
                s_opt = np.clip(res.x, s_min, 1.0)
                alloc: dict[str, dict[str, float]] = {}
                for i, t in enumerate(active_tenants):
                    alloc[t.id] = {}
                    for j, r in enumerate(resources):
                        alloc[t.id][r] = round(float(s_opt[i] * dhat_matrix[i, j]), 6)
                solve_ms = (time.perf_counter() - start_t) * 1000.0
                return alloc, notes, solve_ms
            else:
                notes.append(
                    f"SLSQP did not converge ({res.message}); falling back to proportional."
                )
        except Exception as e:
            notes.append(f"SLSQP error ({e!s}); falling back to proportional.")

        # Fallback to proportional plan
        alloc = {
            t.id: {r: round(prop_plan.alloc[t.id][r], 6) for r in resources} for t in active_tenants
        }
        solve_ms = (time.perf_counter() - start_t) * 1000.0
        return alloc, notes, solve_ms

    def redistribute_surplus(
        self,
        pool: Pool,
        tenants: list[Tenant],
        alloc: dict[str, dict[str, float]],
        obs: dict[str, TenantObs] | None = None,
    ) -> None:
        """Redistributes unused CPU capacity to throttled or backlogged tenants.

        Never redistributes memory (to avoid OOM hazards).
        Capped at declared demand.
        """
        if not self.params.use_surplus or not obs or "cpu" not in pool.capacity:
            return

        cpu_cap = pool.capacity["cpu"]
        total_cpu_alloc = sum(alloc[t.id].get("cpu", 0.0) for t in tenants if t.active)
        surplus_cpu = max(0.0, cpu_cap - total_cpu_alloc)

        if surplus_cpu <= 1e-4:
            return

        active_tenants = [t for t in tenants if t.active]
        # Identify pressured tenants with headroom below demand
        pressured: list[Tenant] = []
        for t in active_tenants:
            t_obs = obs.get(t.id)
            if not t_obs:
                continue
            has_headroom = alloc[t.id].get("cpu", 0.0) < float(t.demand.get("cpu", 0.0)) - 1e-4
            if has_headroom and (
                t_obs.throttle_ratio > self.params.throttle_threshold or t_obs.backlog > 0
            ):
                pressured.append(t)

        if not pressured:
            return

        total_weight = sum(t.weight for t in pressured)
        if total_weight <= 1e-9:
            return

        for t in pressured:
            share = surplus_cpu * (t.weight / total_weight)
            dem = float(t.demand.get("cpu", 0.0))
            current = alloc[t.id].get("cpu", 0.0)
            alloc[t.id]["cpu"] = round(min(dem, current + share), 6)

    def stability_filter(
        self,
        new_alloc: dict[str, dict[str, float]],
        prev_plan: Plan | None,
        tenants: list[Tenant],
        pool: Pool,
        obs: dict[str, TenantObs] | None = None,
        now: float = 0.0,
    ) -> dict[str, dict[str, float]]:
        """Applies hysteresis, dwell times, contraction limiting, and memory safety margins.

        Guarantees:
        - Memory limit never set below current usage + memory_safety_mb.
        - Rate limits capacity contraction to max_shrink_per_epoch.
        - Suppresses updates within deadband unless dwell time elapsed or constraints require it.
        - Pool capacity invariant strictly preserved.
        """
        active_tenants = [t for t in tenants if t.active]
        filtered: dict[str, dict[str, float]] = {
            t.id: dict(new_alloc.get(t.id, {})) for t in active_tenants
        }

        if not self.params.use_hysteresis or not prev_plan or not prev_plan.alloc:
            # Memory safety must apply regardless of hysteresis
            for t in active_tenants:
                t_obs = obs.get(t.id) if obs else None
                if t_obs and "mem_mb" in pool.capacity and "mem_mb" in filtered[t.id]:
                    used_mem = float(t_obs.usage.get("mem_mb", 0.0))
                    safe_mem = used_mem + self.params.memory_safety_mb
                    filtered[t.id]["mem_mb"] = max(filtered[t.id]["mem_mb"], safe_mem)
                    # Cannot exceed declared demand or capacity
                    dem_mem = float(t.demand.get("mem_mb", pool.capacity["mem_mb"]))
                    filtered[t.id]["mem_mb"] = min(dem_mem, filtered[t.id]["mem_mb"])
            return filtered

        for t in active_tenants:
            tid = t.id
            prev_t = prev_plan.alloc.get(tid)
            if not prev_t:
                continue

            dwell = now - self.last_change_time.get(tid, 0.0)
            is_material_change = False

            for res, cap in pool.capacity.items():
                p_val = prev_t.get(res, 0.0)
                n_val = filtered[tid].get(res, p_val)
                rel_change = abs(n_val - p_val) / max(cap, 1e-6)

                # Check deadband and dwell
                if rel_change <= self.params.deadband and dwell < self.params.min_dwell_s:
                    # Keep previous value
                    filtered[tid][res] = p_val
                else:
                    is_material_change = True

                # Rate-limit shrinking
                if filtered[tid][res] < p_val - 1e-6:
                    min_allowed = p_val * (1.0 - self.params.max_shrink_per_epoch)
                    filtered[tid][res] = max(filtered[tid][res], min_allowed)

                # Guarantee minimum enforcement
                min_req = float(t.minimum.get(res, 0.0))
                filtered[tid][res] = max(filtered[tid][res], min_req)

            # Memory safety margin
            t_obs = obs.get(tid) if obs else None
            if t_obs and "mem_mb" in pool.capacity:
                used_mem = float(t_obs.usage.get("mem_mb", 0.0))
                safe_mem = used_mem + self.params.memory_safety_mb
                filtered[tid]["mem_mb"] = max(filtered[tid]["mem_mb"], safe_mem)
                dem_mem = float(t.demand.get("mem_mb", pool.capacity["mem_mb"]))
                filtered[tid]["mem_mb"] = min(dem_mem, filtered[tid]["mem_mb"])

            if is_material_change:
                self.last_change_time[tid] = now

        # Invariant check: sum of filtered allocations cannot exceed capacity
        for res, cap in pool.capacity.items():
            total = sum(filtered[t.id].get(res, 0.0) for t in active_tenants)
            if total > cap + 1e-9:
                excess = total - cap
                # Shrink largest voluntary holders first (alloc - minimum)
                holders = sorted(
                    active_tenants,
                    key=lambda x: filtered[x.id].get(res, 0.0) - float(x.minimum.get(res, 0.0)),
                    reverse=True,
                )
                for h in holders:
                    margin = filtered[h.id].get(res, 0.0) - float(h.minimum.get(res, 0.0))
                    if margin > 0:
                        trim = min(excess, margin)
                        filtered[h.id][res] = round(filtered[h.id][res] - trim, 6)
                        excess -= trim
                        if excess <= 1e-9:
                            break

        return filtered

    def allocate(
        self,
        pool: Pool,
        tenants: list[Tenant],
        obs: dict[str, TenantObs] | None = None,
        prev: Plan | None = None,
        now: float = 0.0,
    ) -> Plan:
        """Executes full adaptive allocation pipeline."""
        total_start = time.perf_counter()
        validate_inputs(pool, tenants)

        # 1. Check feasibility
        feasible, admitted_ids, rejected_ids = check_guarantee_feasibility(pool, tenants)
        unmet_guarantees: list[tuple[str, str]] = []
        if not feasible:
            for rid in rejected_ids:
                for r in pool.capacity:
                    unmet_guarantees.append((rid, r))

        # 2. Demand estimation
        dhat = self.estimate_demand(tenants, obs)

        # 3. Solve convex problem
        opt_alloc, notes, solve_ms = self.solve_convex(pool, tenants, dhat, prev or self.prev_plan)

        # 4. Redistribute surplus CPU
        self.redistribute_surplus(pool, tenants, opt_alloc, obs)

        # 5. Stability filtering
        final_alloc = self.stability_filter(
            opt_alloc, prev or self.prev_plan, tenants, pool, obs, now
        )

        # Round all values
        for tid in final_alloc:
            for r in final_alloc[tid]:
                final_alloc[tid][r] = round(final_alloc[tid][r], 6)

        total_solve_ms = (time.perf_counter() - total_start) * 1000.0
        plan = Plan(
            alloc=final_alloc,
            unmet_guarantees=unmet_guarantees,
            notes=notes,
            solve_ms=round(total_solve_ms, 4),
        )
        self.prev_plan = plan
        return plan
