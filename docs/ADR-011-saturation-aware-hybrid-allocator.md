# ADR-011: Saturation-Aware Hybrid Allocator (SAHA) Architecture & Governance

**Status**: PROPOSED / ACCEPTED FOR PREREGISTRATION  
**Date**: 2026-10-22  
**Scope**: Stage 6 ("v2") Extension to the Adaptive Multi-Tenant Cloud Resource Allocation Platform  
**Authors**: Senior Systems Engineering Pair  

---

## 1. Context & Problem Statement

Stages 1–5 established empirical baselines across FCFS, Proportional (Weighted Proportional), Dominant Resource Fairness (DRF), and Adaptive (SLSQP 1-D Log-Utility) allocators on AWS EC2 `c7i-flex.large` (2 vCPU, 4 GiB memory, Linux cgroup v2, Amazon Linux 2023). Under ADR-009, Stage 1–5 parameters, database runs, and results were frozen.

The frozen evaluation revealed four fundamental failure modes:
1. **Saturation Collapse of 1-D Log-Utility (Scenarios 1 & 2):** Under near-saturated offered load ($>95\%$ pool capacity), Poisson request bursts cause transient CFS throttling. The fast-up demand estimator ratchets demand estimates up to declared demand caps ($\sum \hat{d}_{i, r} > C_r$). Logarithmic utility maximization mathematically collapses to static weighted sharing ($\text{alloc}_i = \frac{w_i}{\sum w} C$), starving latency-sensitive CPU tenants and collapsing on-time completion ratio from 100% to 7.1%.
2. **Memory-Squeeze OOM Kills under Honest Load (Scenario 3):** Under a host capacity cut ($1.50\text{c} / 3072\text{ MB} \to 0.90\text{c} / 2048\text{ MB}$), DRF equalized dominant shares by scaling memory down to 512 MB. Container Tenant B (requiring 1500 MB) was terminated by the Linux kernel OOM killer (exit code 137) in 80% of runs (4 out of 5).
3. **Goodput Cost of Global Hysteresis & Dwell:** Global dwell locks quotas against voluntary contraction, preventing the allocator from re-harvesting surplus memory or CPU from idle tenants when sudden demand shifts occur.
4. **Sluggish CPU Contraction on Host Cuts:** When host capacity is cut, standard 20% per-epoch contraction limits take 3+ epochs to shed quota, causing host oversubscription and CFS throttling.

---

## 2. Decision: The SAHA Architecture

We introduce ONE new allocator, **SAHA (Saturation-Aware Hybrid Allocator)**, implementing the frozen allocator interface `allocate(pool, tenants, obs, prev, now) -> Plan`.

```
                    +-----------------------------+
                    |  Throttle-Aware Estimation  |
                    |   (EWMA + Headroom + Step)  |
                    +--------------+--------------+
                                   |
                                   v
                    +-----------------------------+
                    | Compute Saturation rho =    |
                    |   max_r (sum dhat / C_r)    |
                    +--------------+--------------+
                                   |
                     +-------------+-------------+
                     |                           |
                     v (rho <= 0.85)             v (rho >= 1.00)
             +---------------+           +---------------+
             | RECLAIM Mode  |           |   FAIR Mode   |
             | (Usage-Prop)  |           |  (DRF on dhat)|
             +-------+-------+           +-------+-------+
                     |                           |
                     +-------------+-------------+
                                   | (Hysteresis: retain if 0.85 < rho < 1.00)
                                   v
                    +-----------------------------+
                    |   Compute Memory Floors     |
                    | (p99 RSS + margin + 75% D)  |
                    +--------------+--------------+
                                   |
                                   v
                    +-----------------------------+
                    | Infeasibility Priority Shed |
                    | (Lowest dominant share 1st) |
                    +--------------+--------------+
                                   |
                                   v
                    +-----------------------------+
                    | Rate Limits & Cut Latching  |
                    | (Hard Invariants Beat Soft) |
                    +-----------------------------+
```

### 2.1 Two-Tier Dual-Mode State Machine
1. **Saturation Metric ($\rho$):**
   $$\rho = \max_{r \in \{\text{cpu}, \text{mem}\}} \left( \frac{\sum_{i \in \text{active}} \hat{d}_{i, r}}{C_r} \right)$$
2. **Mode Switching with Hysteresis:**
   - **RECLAIM Mode ($\rho \le \rho_{\text{low}} = 0.85$):** Non-saturated regime. Allocates base resources proportional to estimated demand $\hat{d}_{i, r}$. Surplus CPU slack is shared work-conservingly. Memory is strictly reclaimed down to the memory floor.
   - **FAIR Mode ($\rho \ge \rho_{\text{high}} = 1.00$):** Saturated regime. Bypasses SLSQP log-utility optimization and reuses `allocate_drf` over synthetic `Tenant` copies with demand clamped to $(\epsilon, D_i]$. Originals are never mutated. This guarantees dominant resource fairness and prevents starvation collapse.
   - **Hysteresis Band ($0.85 < \rho < 1.00$):** Retains the previous epoch's mode, eliminating limit flapping.

### 2.2 Memory Safety & Floor Governance
Memory is non-work-conserving. An abrupt memory contraction triggers unrecoverable kernel OOM kills.
1. **Empirical Sliding Window:** Each tenant maintains an RSS history over $W = 60.0\text{ s}$. The base floor is:
   $$\text{floor}_{\text{p99}, i} = 1.15 \times \text{p99}(\text{RSS}_i) + \max(64.0\text{ MB}, 0.10 \times \text{RSS}_i)$$
2. **Cold-Start Guard:** For the first $t < 60.0\text{ s}$ or until $\ge 10$ samples are collected, memory allocation is never contracted below initial/current allocation.
3. **75% Declared-Demand Floor Rule (`saha_mem_declared_floor_frac = 0.75`):**
   - **Gated Condition:** Active strictly while $\sum_{i \in \text{active}} D_{i, \text{mem}} \le C_{\text{mem}}$.
   - **Design Rationale (Existence):** The existence of a declared-memory floor while $\sum D_{\text{mem}} \le C_{\text{mem}}$ is design-reasoned: memory is non-work-conserving, and sudden working-set expansions cannot wait for a 60-second sliding-window estimator without triggering unrecoverable kernel OOM kills.
   - **Untuned Guessed Structural Value (0.75):** While the existence of the floor is design-reasoned, the specific fraction 0.75 ($0.75 \times D_{i, \text{mem}}$) is an untuned guess representing an arbitrary compromise between burst protection and platform multiplexing. It was never derived, searched, or calibrated against traces.
   - **Experimental Control:** The `no_declared_floor` ablation is the experimental control that directly tests whether this guessed 75% floor provides value versus pure empirical p99 RSS.
   - **Automatic Gating:** Under heavy memory contention (Scenarios 1 & 3), $\sum D_{\text{mem}} > C_{\text{mem}}$, causing the gate to flip OFF automatically and safely reverting floors to empirical p99 RSS.

### 2.3 Hard Invariants Beat Soft Limits & Deterministic Shedding
1. **Hard Invariants:** $\sum_i x_{i, r} \le C_r$ and memory floors are hard.
2. **Soft Limits:** Rate contraction limits (5% memory, 20% CPU) and deadband (5%) are soft. If soft limits would cause pool over-allocation, the above-floor portion is scaled down proportionally.
3. **Deterministic Floor Shedding:** If $\sum \text{floor}_i > C_{\text{mem}}$, memory is infeasible. Tenants are prioritized by lowest dominant share first (ties broken by tenant ID). Tenants that cannot receive their full floor are placed in `Plan.unmet_guarantees` and allocated partial memory.
4. **Addressing Metric Inflation (Risk A):**
   - In the live cgroup path and simulation, requests for tenants with unmet memory guarantees receive `status = "rejected"`.
   - `on_time_ratio` is calculated against **TOTAL OFFERED LOAD** ($on\_time\_count / len(samples)$).
   - Shedding requests decreases goodput; an allocator cannot improve its scores by shedding. The rejected fraction is reported alongside all goodput metrics.

### 2.4 Capacity Cut Detection & Latch
Host capacity cut ($\Delta C / C > 10^{-3}$) immediately bypasses the 20% CPU contraction limit and latches for `saha_cut_latch_epochs = 3` epochs to avoid missing lagging telemetry. Memory contracts immediately down to the floor.

---

## 3. Known Limitations & Accounting Treatment

1. **`status == "error"` Sample Accounting:**
   - Under the frozen Stage 1–5 metric engine, HTTP 500 or connection error samples are counted in `total_offered` (lowering `on_time_ratio`), but are omitted from `completed_samples` and `excluded_count`.
   - This accounting is kept strictly frozen to preserve comparability.
   - Treatment is identical across FCFS, Proportional, DRF, Adaptive, and SAHA.
2. **Single-Node Scope:**
   - SAHA is an intra-host cgroup v2 resource allocator; cluster-wide scheduling remains out of scope.

---

## 4. Single-Source Parameter Table

All 13 parameters are defined in `src/alloc_platform/config.py` and instantiated via `SahaParams`:

| Parameter | Type | Value | Scope |
| :--- | :---: | :---: | :--- |
| `saha_ewma_alpha` | Tunable (CPU) | `0.30` | EWMA smoothing factor |
| `saha_headroom_h` | Tunable (CPU) | `0.20` | Demand headroom |
| `saha_throttle_step` | Tunable (CPU) | `1.25` | Multiplicative throttle escalation |
| `saha_rho_low` | Tunable (Mode) | `0.85` | Saturation lower threshold |
| `saha_rho_high` | Tunable (Mode) | `1.00` | Saturation upper threshold |
| `saha_mem_floor_mult` | Fixed Structural | `1.15` | p99 floor multiplier |
| `saha_mem_margin_mb` | Fixed Structural | `64.0` | Minimum floor margin (MB) |
| `saha_mem_margin_frac` | Fixed Structural | `0.10` | Relative floor margin fraction |
| `saha_mem_window_s` | Fixed Structural | `60.0` | Sliding window duration (s) |
| `saha_mem_contract_step` | Fixed Structural | `0.05` | 5% per epoch memory contraction limit |
| `saha_cpu_contract_rate` | Fixed Structural | `0.20` | 20% per epoch CPU contraction limit |
| `saha_deadband` | Fixed Structural | `0.05` | 5% change deadband suppression |
| `saha_deadband_eps` | Fixed Structural | `1e-7` | Numerical epsilon for deadband float equality |
| `saha_mem_min_samples` | Fixed Structural | `10` | Cold start sample count threshold |
| `saha_cut_latch_epochs` | Fixed Structural | `3` | Capacity cut latch epoch count |
| `saha_dhat_min_epsilon` | Fixed Structural | `1e-4` | Demand estimate lower bound |
| `saha_cap_cut_tol` | Fixed Structural | `1e-3` | Capacity cut detection tolerance |
| `saha_mem_declared_floor_frac` | Fixed Structural | `0.75` | Declared memory floor fraction |
| `saha_reclaim_min_share_ratio` | Fixed Structural | `0.0` | Reclaim mode minimum share ratio |
