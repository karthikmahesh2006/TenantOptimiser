"""Saturation-Aware Hybrid Allocator (SAHA) - Stage 6 Extension.

Addresses saturation collapse of 1-D log utility (Scenarios 1 & 2),
memory-squeeze OOM kills under honest contention, and sluggish capacity cut response,
while preserving quota reclamation wins under over-declaration.
"""

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from alloc_platform.allocator.drf import allocate_drf
from alloc_platform.allocator.types import Plan, Pool, Tenant, TenantObs
from alloc_platform.allocator.validate import validate_inputs
from alloc_platform.config import config


@dataclass
class SahaParams:
    """Hyperparameters and structural configuration for SahaAllocator.

    Tunable parameters (Stage 6 calibration set, exactly 12 tunables):
        ewma_alpha: EWMA smoothing factor for usage telemetry [0.0, 1.0].
        headroom_h: Fractional headroom added to smoothed demand estimates.
        throttle_step: Multiplicative demand increase factor when CFS throttled.
        rho_low: Saturation threshold below which RECLAIM mode is engaged.
        rho_high: Saturation threshold above which FAIR mode is engaged.
        mem_floor_mult: Multiplier applied to p99 RSS for memory floor.
        mem_margin_mb: Absolute memory margin in MB added to memory floor.
        mem_margin_frac: Fractional margin of live RSS added to memory floor.
        mem_window_s: Window duration in seconds for p99 RSS history.
        mem_contract_step: Maximum fractional memory contraction per epoch.
        cpu_contract_rate: Maximum fractional CPU contraction per epoch.
        deadband: Relative change threshold below which allocation updates are suppressed.

    Structural parameters (fixed, not tuned):
        reclaim_min_share_ratio: Guaranteed min share ratio for RECLAIM mode.
        mem_min_samples: Minimum RSS observation samples required before memory contraction.
        cut_latch_epochs: Number of epochs to latch the capacity-cut bypass.
        dhat_min_epsilon: Lower bound for demand estimates to avoid zero/singular division.
        cap_cut_tol: Relative tolerance for detecting host capacity reduction.
    """

    # Tunables
    ewma_alpha: float = field(default_factory=lambda: config.saha_ewma_alpha)
    headroom_h: float = field(default_factory=lambda: config.saha_headroom_h)
    throttle_step: float = field(default_factory=lambda: config.saha_throttle_step)
    rho_low: float = field(default_factory=lambda: config.saha_rho_low)
    rho_high: float = field(default_factory=lambda: config.saha_rho_high)
    mem_floor_mult: float = field(default_factory=lambda: config.saha_mem_floor_mult)
    mem_margin_mb: float = field(default_factory=lambda: config.saha_mem_margin_mb)
    mem_margin_frac: float = field(default_factory=lambda: config.saha_mem_margin_frac)
    mem_window_s: float = field(default_factory=lambda: config.saha_mem_window_s)
    mem_contract_step: float = field(default_factory=lambda: config.saha_mem_contract_step)
    cpu_contract_rate: float = field(default_factory=lambda: config.saha_cpu_contract_rate)
    deadband: float = field(default_factory=lambda: config.saha_deadband)

    # Structural parameters
    reclaim_min_share_ratio: float = field(default_factory=lambda: config.saha_reclaim_min_share_ratio)
    mem_min_samples: int = field(default_factory=lambda: config.saha_mem_min_samples)
    cut_latch_epochs: int = field(default_factory=lambda: config.saha_cut_latch_epochs)
    dhat_min_epsilon: float = field(default_factory=lambda: config.saha_dhat_min_epsilon)
    cap_cut_tol: float = field(default_factory=lambda: config.saha_cap_cut_tol)
    mem_declared_floor_frac: float = field(default_factory=lambda: config.saha_mem_declared_floor_frac)
    deadband_eps: float = field(default_factory=lambda: config.saha_deadband_eps)
    use_declared_floor: bool = True

    @classmethod
    def from_config(cls, **overrides) -> "SahaParams":
        """Instantiate SahaParams dynamically reading current values from config."""
        fields = {
            "ewma_alpha": config.saha_ewma_alpha,
            "headroom_h": config.saha_headroom_h,
            "throttle_step": config.saha_throttle_step,
            "rho_low": config.saha_rho_low,
            "rho_high": config.saha_rho_high,
            "mem_floor_mult": config.saha_mem_floor_mult,
            "mem_margin_mb": config.saha_mem_margin_mb,
            "mem_margin_frac": config.saha_mem_margin_frac,
            "mem_window_s": config.saha_mem_window_s,
            "mem_contract_step": config.saha_mem_contract_step,
            "cpu_contract_rate": config.saha_cpu_contract_rate,
            "deadband": config.saha_deadband,
            "reclaim_min_share_ratio": config.saha_reclaim_min_share_ratio,
            "mem_min_samples": config.saha_mem_min_samples,
            "cut_latch_epochs": config.saha_cut_latch_epochs,
            "dhat_min_epsilon": config.saha_dhat_min_epsilon,
            "cap_cut_tol": config.saha_cap_cut_tol,
            "mem_declared_floor_frac": config.saha_mem_declared_floor_frac,
            "deadband_eps": config.saha_deadband_eps,
        }
        fields.update(overrides)
        return cls(**fields)





class SahaAllocator:
    """Saturation-Aware Hybrid Allocator (SAHA) stateful allocator engine."""

    def __init__(self, params: Optional[SahaParams] = None) -> None:
        self.params = params or SahaParams()
        self.reset()

    def reset(self) -> None:
        """Resets all internal allocator state per run."""
        self.mode: str = "RECLAIM"
        self.ewma_usage: Dict[str, Dict[str, float]] = {}
        self.rss_history: Dict[str, List[Tuple[float, float]]] = {}  # tid -> list of (t_sec, rss_mb)
        self.mode_switches: List[Dict[str, Any]] = []
        self.prev_plan: Optional[Plan] = None
        self.prev_pool: Optional[Pool] = None
        self.cut_latch_counter: int = 0
        self.last_dhat: Dict[str, Dict[str, float]] = {}
        self.last_raw_alloc: Dict[str, Dict[str, float]] = {}
        self.last_rho: float = 0.0
        self.epoch_diagnostics: List[Dict[str, Any]] = []

    def estimate_demand(
        self,
        tenants: List[Tenant],
        obs: Optional[Dict[str, TenantObs]],
        pool: Pool,
    ) -> Dict[str, Dict[str, float]]:
        """Step 1: Throttle-aware demand estimation with EWMA and headroom."""
        dhat: Dict[str, Dict[str, float]] = {}
        active_tenants = [t for t in tenants if t.active]

        for t in active_tenants:
            dhat[t.id] = {}
            if t.id not in self.ewma_usage:
                self.ewma_usage[t.id] = {}

            t_obs = obs.get(t.id) if obs else None
            is_throttled = bool(t_obs and t_obs.throttle_ratio > config.throttle_threshold)

            for r in pool.capacity:
                used = float(t_obs.usage.get(r, 0.0)) if t_obs else 0.0
                if r not in self.ewma_usage[t.id]:
                    self.ewma_usage[t.id][r] = used
                else:
                    self.ewma_usage[t.id][r] = (
                        self.params.ewma_alpha * used
                        + (1.0 - self.params.ewma_alpha) * self.ewma_usage[t.id][r]
                    )

                # Base estimate with headroom
                est = self.ewma_usage[t.id][r] * (1.0 + self.params.headroom_h)

                # Throttle escalation: multiplicative step without jumping to cap
                if is_throttled and r == "cpu":
                    est = max(est, used) * self.params.throttle_step

                dec_dem = float(t.demand.get(r, 0.0))
                # Clamp strictly to (epsilon, declared]
                clamped = min(dec_dem, max(self.params.dhat_min_epsilon, est))
                dhat[t.id][r] = round(clamped, 6)

        return dhat

    def compute_saturation(
        self,
        dhat: Dict[str, Dict[str, float]],
        pool: Pool,
        active_tenants: List[Tenant],
    ) -> float:
        """Step 2: Saturation ratio rho = max_r (sum_i dhat_i,r / C_r)."""
        ratios = []
        for r, cap in pool.capacity.items():
            tot_dhat = sum(dhat[t.id].get(r, 0.0) for t in active_tenants)
            ratios.append(tot_dhat / max(1e-6, cap))
        return max(ratios) if ratios else 0.0

    def select_mode(self, rho: float, now: float) -> str:
        """Step 3: Mode selection with hysteresis on mode state only."""
        old_mode = self.mode
        if rho < self.params.rho_low:
            new_mode = "RECLAIM"
        elif rho > self.params.rho_high:
            new_mode = "FAIR"
        else:
            new_mode = old_mode

        if new_mode != old_mode:
            self.mode_switches.append(
                {
                    "timestamp": now,
                    "from_mode": old_mode,
                    "to_mode": new_mode,
                    "rho": round(rho, 4),
                }
            )
            self.mode = new_mode

        return self.mode

    def raw_allocation(
        self,
        mode: str,
        pool: Pool,
        active_tenants: List[Tenant],
        dhat: Dict[str, Dict[str, float]],
    ) -> Dict[str, Dict[str, float]]:
        """Step 4: Raw allocation by mode.
        - FAIR: DRF progressive filling directly over dhat (reused without logic duplication).
        - RECLAIM: surplus reclamation handing slack to constrained tenants.
        """
        raw: Dict[str, Dict[str, float]] = {
            t.id: {r: 0.0 for r in pool.capacity} for t in active_tenants
        }

        if mode == "FAIR":
            # Condition 1: Build synthetic Tenant copies with ALL other fields copied unchanged,
            # clamp dhat to (epsilon, declared]
            synthetic_tenants = [
                Tenant(
                    id=t.id,
                    name=t.name,
                    weight=t.weight,
                    priority=t.priority,
                    arrival_seq=t.arrival_seq,
                    active=t.active,
                    demand={
                        r: min(
                            float(t.demand.get(r, 0.0)),
                            max(
                                float(t.minimum.get(r, 0.0)),
                                max(self.params.dhat_min_epsilon, dhat[t.id].get(r, self.params.dhat_min_epsilon)),
                            ),
                        )
                        for r in pool.capacity
                    },

                    minimum=dict(t.minimum),
                    workload_type=t.workload_type,
                )
                for t in active_tenants
            ]
            drf_plan = allocate_drf(pool, synthetic_tenants)
            for t in active_tenants:
                for r in pool.capacity:
                    raw[t.id][r] = drf_plan.alloc.get(t.id, {}).get(r, 0.0)
            return raw

        # RECLAIM Mode
        for r, cap in pool.capacity.items():
            for t in active_tenants:
                req_min = float(t.minimum.get(r, 0.0))
                # Base allocation: min(dhat, declared) but at least min guarantee
                base = min(float(t.demand.get(r, 0.0)), max(req_min, dhat[t.id].get(r, 0.0)))
                raw[t.id][r] = base

            tot_base = sum(raw[t.id][r] for t in active_tenants)
            if tot_base > cap + 1e-9:
                scale = cap / tot_base if tot_base > 0 else 0.0
                for t in active_tenants:
                    raw[t.id][r] = round(raw[t.id][r] * scale, 6)
                continue

            slack = max(0.0, cap - tot_base)
            if slack > 1e-9:
                # Hand unallocated slack to tenants whose declared demand exceeds current allocation
                # and whose dhat exceeds current allocation (constrained tenants)
                unmet: Dict[str, float] = {}
                for t in active_tenants:
                    d_cap = float(t.demand.get(r, 0.0))
                    d_est = dhat[t.id].get(r, 0.0)
                    target = min(d_cap, d_est)
                    if target > raw[t.id][r] + 1e-9:
                        unmet[t.id] = target - raw[t.id][r]

                tot_unmet = sum(unmet.values())
                if tot_unmet > 1e-9:
                    for tid, diff in unmet.items():
                        give = min(diff, slack * (diff / tot_unmet))
                        raw[tid][r] += give
                        slack -= give
                        if slack <= 1e-9:
                            break

                # If slack still remains and tenants can absorb up to declared demand (CPU only; memory is reclaimed to floor)
                if slack > 1e-9 and r == "cpu":
                    unmet_decl = {
                        t.id: max(0.0, float(t.demand.get(r, 0.0)) - raw[t.id][r])
                        for t in active_tenants
                    }
                    tot_decl = sum(unmet_decl.values())
                    if tot_decl > 1e-9:
                        for tid, diff in unmet_decl.items():
                            give = min(diff, slack * (diff / tot_decl))
                            raw[tid][r] += give
                            slack -= give
                            if slack <= 1e-9:
                                break

        return raw

    def compute_memory_floors(
        self,
        active_tenants: List[Tenant],
        obs: Optional[Dict[str, TenantObs]],
        prev_alloc: Optional[Dict[str, Dict[str, float]]],
        now: float,
        pool: Optional[Pool] = None,
    ) -> Dict[str, float]:
        """Step 5: Memory floor calculation with sliding window RSS, cold-start guard, and declared-demand floor rule (Condition 7c)."""
        floors: Dict[str, float] = {}

        # Item 7c: Gated by sum of declared memory over active tenants <= current C_mem
        act = [t for t in active_tenants if t.active]
        c_mem = pool.capacity.get("mem_mb", 3072.0) if pool else 3072.0
        tot_decl_mem = sum(float(t.demand.get("mem_mb", 0.0)) for t in act)
        is_declared_floor_active = (
            self.params.use_declared_floor
            and (self.params.mem_declared_floor_frac > 0.0)
            and (tot_decl_mem <= c_mem + 1e-9)
        )

        for t in act:

            t_obs = obs.get(t.id) if obs else None
            curr_rss = float(t_obs.usage.get("mem_mb", 64.0)) if t_obs else 64.0

            if t.id not in self.rss_history:
                self.rss_history[t.id] = []
            self.rss_history[t.id].append((now, curr_rss))

            # Prune RSS samples older than window W
            self.rss_history[t.id] = [
                (ts, r_val)
                for (ts, r_val) in self.rss_history[t.id]
                if (now - ts) <= self.params.mem_window_s
            ]

            window_rss = [r_val for (_, r_val) in self.rss_history[t.id]]
            p99_rss = float(np.percentile(window_rss, 99)) if window_rss else curr_rss

            margin = max(self.params.mem_margin_mb, self.params.mem_margin_frac * curr_rss)
            calc_floor = p99_rss * self.params.mem_floor_mult + margin

            # Cold start guard (Condition 2)
            prev_mem = (
                prev_alloc.get(t.id, {}).get("mem_mb", float(t.demand.get("mem_mb", 512.0)))
                if prev_alloc
                else float(t.demand.get("mem_mb", 512.0))
            )
            is_cold = (len(self.rss_history[t.id]) < self.params.mem_min_samples) or (
                now < self.params.mem_window_s
            )

            if is_cold:
                # Do NOT contract memory below current/initial allocation
                floor_val = max(calc_floor, prev_mem)
            else:
                floor_val = calc_floor

            # Item 7c: Declared memory floor rule: max(p99 floor, frac * D) while gate is active
            decl_mem = float(t.demand.get("mem_mb", 512.0))
            if is_declared_floor_active:
                floor_val = max(floor_val, self.params.mem_declared_floor_frac * decl_mem)

            # Never exceed declared demand
            floors[t.id] = round(min(decl_mem, floor_val), 4)

        return floors


    def apply_memory_floors_and_infeasibility(
        self,
        raw_alloc: Dict[str, Dict[str, float]],
        floor_mem: Dict[str, float],
        pool: Pool,
        active_tenants: List[Tenant],
        is_cut_active: bool,
        prev_alloc: Optional[Dict[str, Dict[str, float]]],
    ) -> Tuple[Dict[str, Dict[str, float]], List[Tuple[str, str]], List[str]]:
        """Applies hard memory floors and handles infeasibility deterministically (Condition 4)."""
        alloc = {t.id: dict(raw_alloc[t.id]) for t in active_tenants}
        unmet_guarantees: List[Tuple[str, str]] = []
        notes: List[str] = []

        tot_floors = sum(floor_mem[t.id] for t in active_tenants)
        c_mem = pool.capacity.get("mem_mb", 3072.0)

        if tot_floors > c_mem + 1e-9:
            # Condition 4: Infeasibility deterministic priority order:
            # lowest dominant share first, ties broken by tenant id.
            notes.append(
                f"WARNING: Sum of memory floors ({tot_floors:.1f} MB) exceeds pool capacity ({c_mem:.1f} MB). "
                "Applying deterministic priority shedding (lowest dominant share first)."
            )
            # Dominant share over declared demand
            dom_shares = {
                t.id: max(
                    float(t.demand.get(r, 0.0)) / max(1e-6, pool.capacity.get(r, 1e-6))
                    for r in pool.capacity
                )
                for t in active_tenants
            }
            sorted_tenants = sorted(active_tenants, key=lambda t: (dom_shares[t.id], t.id))

            rem_mem = c_mem
            for t in sorted_tenants:
                req_floor = floor_mem[t.id]
                if rem_mem >= req_floor - 1e-9:
                    alloc[t.id]["mem_mb"] = req_floor
                    rem_mem -= req_floor
                else:
                    given = max(0.0, rem_mem)
                    alloc[t.id]["mem_mb"] = round(given, 6)
                    rem_mem = 0.0
                    unmet_guarantees.append((t.id, "mem_mb"))
                    notes.append(
                        f"WARNING: Memory floor infeasible for tenant {t.id}; allocated partial "
                        f"{given:.1f} MB < floor {req_floor:.1f} MB."
                    )
        else:
            # Feasible floors: honor memory floors
            for t in active_tenants:
                alloc[t.id]["mem_mb"] = max(alloc[t.id]["mem_mb"], floor_mem[t.id])

                # Memory contraction step (unless bypassed by capacity cut down to floor)
                if prev_alloc and t.id in prev_alloc:
                    prev_m = prev_alloc[t.id].get("mem_mb", alloc[t.id]["mem_mb"])
                    if not is_cut_active:
                        min_allowed_m = prev_m * (1.0 - self.params.mem_contract_step)
                        alloc[t.id]["mem_mb"] = max(alloc[t.id]["mem_mb"], min_allowed_m)
                    else:
                        # Condition 3b: May bypass mem_contract_step ONLY down to floor
                        alloc[t.id]["mem_mb"] = max(alloc[t.id]["mem_mb"], floor_mem[t.id])

        return alloc, unmet_guarantees, notes

    def apply_rate_limits_and_invariants(
        self,
        alloc: Dict[str, Dict[str, float]],
        floor_mem: Dict[str, float],
        pool: Pool,
        active_tenants: List[Tenant],
        is_cut_active: bool,
        prev_alloc: Optional[Dict[str, Dict[str, float]]],
    ) -> Dict[str, Dict[str, float]]:
        """Step 6: CPU rate limiting, deadband, and hard invariant enforcement (Condition 3c)."""
        final_alloc = {t.id: dict(alloc[t.id]) for t in active_tenants}

        # 1. CPU Contraction Rate Limit
        if prev_alloc and not is_cut_active:
            for t in active_tenants:
                if t.id in prev_alloc:
                    prev_c = prev_alloc[t.id].get("cpu", final_alloc[t.id]["cpu"])
                    min_allowed_c = prev_c * (1.0 - self.params.cpu_contract_rate)
                    final_alloc[t.id]["cpu"] = max(final_alloc[t.id]["cpu"], min_allowed_c)

        # 2. Deadband (soft limit)
        if prev_alloc:
            for t in active_tenants:
                if t.id in prev_alloc:
                    for r in pool.capacity:
                        prev_v = prev_alloc[t.id].get(r, final_alloc[t.id][r])
                        rel_change = abs(final_alloc[t.id][r] - prev_v) / max(1e-6, prev_v)
                        if rel_change < (self.params.deadband - self.params.deadband_eps):
                            final_alloc[t.id][r] = prev_v





        # 3. Hard Invariants Beat Soft Limits (Condition 3c):
        # sum_i x[i][r] <= C[r] for every resource and memory floors are hard.
        # If soft limits caused an overage, scale down only the above-floor/above-min portion.

        # Memory overage resolution
        c_mem = pool.capacity.get("mem_mb", 3072.0)
        tot_mem = sum(final_alloc[t.id].get("mem_mb", 0.0) for t in active_tenants)
        if tot_mem > c_mem + 1e-9:
            overage_m = tot_mem - c_mem
            above_floor = {
                t.id: max(0.0, final_alloc[t.id].get("mem_mb", 0.0) - floor_mem.get(t.id, 0.0))
                for t in active_tenants
            }
            tot_above_m = sum(above_floor.values())
            if tot_above_m > 1e-9:
                for t in active_tenants:
                    red_m = min(above_floor[t.id], overage_m * (above_floor[t.id] / tot_above_m))
                    final_alloc[t.id]["mem_mb"] -= red_m
            else:
                # If even floors total exceeds (already handled in infeasibility) or no above-floor slack
                scale = c_mem / tot_mem if tot_mem > 0 else 1.0
                for t in active_tenants:
                    final_alloc[t.id]["mem_mb"] *= scale

        # CPU overage resolution
        c_cpu = pool.capacity.get("cpu", 1.50)
        tot_cpu = sum(final_alloc[t.id].get("cpu", 0.0) for t in active_tenants)
        if tot_cpu > c_cpu + 1e-9:
            overage_c = tot_cpu - c_cpu
            above_min_c = {
                t.id: max(0.0, final_alloc[t.id].get("cpu", 0.0) - float(t.minimum.get("cpu", 0.0)))
                for t in active_tenants
            }
            tot_above_c = sum(above_min_c.values())
            if tot_above_c > 1e-9:
                for t in active_tenants:
                    red_c = min(above_min_c[t.id], overage_c * (above_min_c[t.id] / tot_above_c))
                    final_alloc[t.id]["cpu"] -= red_c
            else:
                scale_c = c_cpu / tot_cpu if tot_cpu > 0 else 1.0
                for t in active_tenants:
                    final_alloc[t.id]["cpu"] *= scale_c

        # Final Clamp to [0, demand] and round
        for t in active_tenants:
            for r in pool.capacity:
                dec = float(t.demand.get(r, 0.0))
                val = max(0.0, min(dec, final_alloc[t.id][r]))
                final_alloc[t.id][r] = round(val, 6)

        return final_alloc

    def allocate(
        self,
        pool: Pool,
        tenants: List[Tenant],
        obs: Optional[Dict[str, TenantObs]] = None,
        prev: Optional[Plan] = None,
        now: float = 0.0,
    ) -> Plan:
        """Executes full SAHA per-epoch allocation pipeline."""
        start_time = time.perf_counter()
        validate_inputs(pool, tenants)
        active_tenants = [t for t in tenants if t.active]

        if not active_tenants:
            return Plan(
                alloc={},
                unmet_guarantees=[],
                notes=["No active tenants."],
                solve_ms=round((time.perf_counter() - start_time) * 1000.0, 4),
            )

        prev_plan = prev or self.prev_plan
        prev_alloc = prev_plan.alloc if prev_plan else None

        # Capacity Cut Detection & Latch (Condition 3)
        is_cut_active = False
        if self.prev_pool is not None:
            cpu_cut = (
                (self.prev_pool.capacity.get("cpu", 0.0) - pool.capacity.get("cpu", 0.0))
                / max(1e-6, self.prev_pool.capacity.get("cpu", 0.0))
            ) > self.params.cap_cut_tol
            mem_cut = (
                (self.prev_pool.capacity.get("mem_mb", 0.0) - pool.capacity.get("mem_mb", 0.0))
                / max(1e-6, self.prev_pool.capacity.get("mem_mb", 0.0))
            ) > self.params.cap_cut_tol

            if cpu_cut or mem_cut:
                self.cut_latch_counter = self.params.cut_latch_epochs
            elif self.cut_latch_counter > 0:
                self.cut_latch_counter -= 1

        is_cut_active = self.cut_latch_counter > 0

        # Step 1: Demand Estimation
        dhat = self.estimate_demand(tenants, obs, pool)
        self.last_dhat = dhat

        # Step 2: Saturation rho
        rho = self.compute_saturation(dhat, pool, active_tenants)
        self.last_rho = rho

        # Step 3: Mode Selection
        mode = self.select_mode(rho, now)

        # Step 4: Raw Allocation
        raw = self.raw_allocation(mode, pool, active_tenants, dhat)
        self.last_raw_alloc = {t.id: dict(raw[t.id]) for t in active_tenants}

        # Step 5: Memory Floors
        floor_mem = self.compute_memory_floors(active_tenants, obs, prev_alloc, now, pool=pool)

        # Infeasibility & Floor Application (Step 5 & 7)
        alloc_with_floors, unmet_g, notes = self.apply_memory_floors_and_infeasibility(
            raw, floor_mem, pool, active_tenants, is_cut_active, prev_alloc
        )

        # Step 6: Rate Limiting, Deadband & Invariant Enforcement
        final_alloc = self.apply_rate_limits_and_invariants(
            alloc_with_floors, floor_mem, pool, active_tenants, is_cut_active, prev_alloc
        )

        solve_ms = (time.perf_counter() - start_time) * 1000.0

        plan = Plan(
            alloc=final_alloc,
            unmet_guarantees=unmet_g,
            notes=notes,
            solve_ms=round(solve_ms, 4),
        )

        self.prev_plan = plan
        self.prev_pool = Pool(capacity=dict(pool.capacity))

        self.epoch_diagnostics.append(
            {
                "timestamp": now,
                "rho": round(rho, 4),
                "mode": mode,
                "dhat": {t.id: dict(dhat[t.id]) for t in active_tenants},
                "raw_alloc": {t.id: dict(raw[t.id]) for t in active_tenants},
                "floor_mem": dict(floor_mem),
                "final_alloc": {t.id: dict(final_alloc[t.id]) for t in active_tenants},
                "is_cut_active": is_cut_active,
                "solve_ms": round(solve_ms, 4),
            }
        )

        return plan


def allocate_saha(
    pool: Pool,
    tenants: List[Tenant],
    obs: Optional[Dict[str, TenantObs]] = None,
    prev: Optional[Plan] = None,
    now: float = 0.0,
) -> Plan:
    """Functional convenience wrapper for SahaAllocator."""
    allocator = SahaAllocator()
    return allocator.allocate(pool, tenants, obs, prev, now=now)


SahaV1FrozenAllocator = SahaAllocator
allocate_saha_v1_frozen = allocate_saha

