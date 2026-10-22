# SAHA Calibration Plan (Stage 6 Extension)

**Status**: DRAFT / APPROVED FOR PREREGISTRATION  
**Author**: Systems Engineering Pair  
**Scope**: Saturation-Aware Hybrid Allocator (SAHA) parameter classification, calibration protocol, and boundary specifications.

---

## 1. Methodological Discipline & Scope

Under ADR-009, all allocator parameters must be single-sourced and calibrated prior to confirmatory benchmarking. No ex-post parameter tuning is permitted.

### 1.1 Strict Grounding Statement: Live Hardware vs. Simulation
> [!IMPORTANT]
> **The simulated orchestrator does NOT model Linux kernel cgroup v2 Out-Of-Memory (OOM) kills.**  
> In simulation mode, memory usage is tracked purely as a passive variable; memory shortage causes request service degradation or simulated rejection if memory floors are infeasible, but the simulated runtime cannot replicate kernel cgroup invocation of the Linux OOM killer.
> 
> Therefore:
> 1. **Memory-safety parameters are NEVER searched, tuned, or calibrated in simulation.**
> 2. **All OOM prevention and memory-guarantee claims are evaluated and graded strictly on LIVE HARDWARE under the Linux cgroup v2 enforcer.**
> 3. Simulation is restricted solely to coarse exploration and sensitivity checks of CPU-side dynamics and mode-selection boundaries.

---

## 2. Parameter Classification

Parameters are strictly bifurcated into **Searched Tunables** (CPU/mode-selection only) and **Structural Fixed Parameters** (memory safety, rate limiters, and safety floors).

### 2.1 Searched Parameters (CPU-Side & Mode-Selection Only)
These parameters govern CPU demand tracking, throttle escalation, and mode transitions. They are calibrated on independent calibration seeds disjoint from confirmatory benchmark seeds.

| Parameter | Baseline / Default | Calibration Grid | Provenance | Purpose |
| :--- | :--- | :--- | :--- | :--- |
| `saha_ewma_alpha` | `0.30` | `[0.15, 0.30, 0.50]` | CONFIGURED | CPU usage smoothing speed |
| `saha_headroom_h` | `0.20` | `[0.10, 0.20, 0.30]` | CONFIGURED | Proactive burst headroom |
| `saha_throttle_step` | `1.25` | `[1.15, 1.25, 1.35]` | CONFIGURED | Multiplicative throttle escalation |
| `saha_rho_low` | `0.85` | `[0.80, 0.85, 0.90]` | CONFIGURED | Upper boundary for RECLAIM mode |
| `saha_rho_high` | `1.00` | `[0.95, 1.00, 1.05]` | CONFIGURED | Lower boundary for FAIR mode |

**Calibration Seed Policy**:
- **Calibration Seeds**: Seeds `101`, `102`, `103` (independent synthetic runs).
- **Confirmatory Seeds**: Seeds `42`, `43`, `44`, `45` (strictly frozen and never touched during calibration).

---

### 2.2 Structural / Fixed Parameters (Memory Safety & Control Stability - NOT Tuned)
These parameters are structural invariants required to guarantee control stability, prevent memory squeeze OOMs, and enforce safety bounds. They are **fixed across all runs and never searched**.

| Parameter | Fixed Value | Provenance | Structural Rationale |
| :--- | :--- | :--- | :--- |
| `saha_mem_floor_mult` | `1.15` | CONFIGURED | 15% safety buffer above empirical p99 RSS. |
| `saha_mem_margin_mb` | `64.0` | CONFIGURED | Absolute lower bound buffer for runtime RSS jitter. |
| `saha_mem_margin_frac` | `0.10` | CONFIGURED | 10% relative RSS margin for high-memory containers. |
| `saha_mem_window_s` | `60.0` | CONFIGURED | 60-second sliding history window for empirical p99 RSS. |
| `saha_mem_contract_step` | `0.05` | CONFIGURED | 5% maximum memory contraction per epoch (prevents abrupt memory drops). |
| `saha_cpu_contract_rate` | `0.20` | CONFIGURED | 20% maximum CPU contraction per epoch (smooth de-escalation). |
| `saha_deadband` | `0.05` | CONFIGURED | 5% change threshold to suppress control churn. |
| `saha_mem_min_samples` | `10` | CONFIGURED | Minimum RSS observation samples required before cold-start lifts. |
| `saha_cut_latch_epochs` | `3` | CONFIGURED | Number of epochs to latch bypass upon host capacity cuts. |
| `saha_dhat_min_epsilon` | `1e-4` | CONFIGURED | Strictly positive lower bound for synthetic DRF demands. |
| `saha_cap_cut_tol` | `1e-3` | CONFIGURED | Relative tolerance for host capacity reduction detection. |
| `saha_mem_declared_floor_frac` | `0.75` | CONFIGURED | 75% declared-memory floor rule when $\sum D_{\text{mem}} \le C_{\text{mem}}$. |
| `saha_reclaim_min_share_ratio` | `0.0` | CONFIGURED | RECLAIM mode min share ratio (0.0 guarantees full slack reclamation). |

---

## 3. Calibration Protocol & Acceptance Criteria

1. **Grid Search Protocol**:
   - Run Cartesian product of the 5 searched CPU tunables across calibration seeds `101`, `102`, `103` in simulation.
   - Objective: maximize mean goodput and minimize throttled-while-spare ratio on calibration workload profiles.
2. **Boundary Validation**:
   - Verify that mode transitions display no rapid flapping within the hysteresis deadband $[\rho_{\text{low}}, \rho_{\text{high}}]$.
   - Confirm that capacity cuts trigger immediate CPU contraction rate bypass and memory latching.
3. **Freezing**:
   - Selected tunables are frozen into `src/alloc_platform/config.py`.
   - Confirmatory evaluation proceeds to live hardware on EC2 with zero post-run parameter adjustments.
