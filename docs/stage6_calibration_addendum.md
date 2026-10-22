# Stage 6 Calibration Addendum: Coarse CPU Grid Analysis & H5 Simulation Prediction

**Date**: 2026-10-22  
**Status**: COMMITTED ADDENDUM TO PREREGISTRATION  
**Authors**: Senior Systems Engineering Pair  
**Related Documents**: [`docs/stage6_calibration_plan.md`](./stage6_calibration_plan.md), [`docs/stage6_preregistration.md`](./stage6_preregistration.md), [`docs/ADR-011-saturation-aware-hybrid-allocator.md`](./ADR-011-saturation-aware-hybrid-allocator.md)  
**Calibration Artifact**: [`data/calibration_results_stage6.json`](../data/calibration_results_stage6.json)  
**Preserved Degenerate Artifact**: [`data/calibration_results_stage6_degenerate.json`](../data/calibration_results_stage6_degenerate.json)  

---

## 1. Resolution of Degenerate Calibration Root Cause (Plumbing Fix)

In the initial calibration report, all five candidates ($C_1$--$C_5$) returned identical results. The reviewer correctly flagged this anomaly.

### 1.1 Root Cause Analysis
In Python dataclasses:
```python
@dataclass
class SahaParams:
    ewma_alpha: float = config.saha_ewma_alpha
```
The field defaults were evaluated exactly once at module import time. When `scripts/calibrate_saha_cpu.py` altered `config.saha_ewma_alpha` at runtime, subsequent instantiations of `SahaParams()` inside `_execute_single_run` continued to instantiate using the import-time defaults ($0.30, 0.20$, etc.). Candidate parameters never reached `SahaAllocator`.

### 1.2 Preserved Evidence of Degenerate Run
The initial degenerate output is preserved in [`data/calibration_results_stage6_degenerate.json`](../data/calibration_results_stage6_degenerate.json) (recorded from task `task-5442` at `2026-10-22T16:39:20Z`).

### 1.3 Applied Fix
1. Updated `SahaParams` in `src/alloc_platform/allocator/saha.py` to use `field(default_factory=lambda: config.saha_...)` for all parameters, ensuring dynamic binding to the current single-source config at instantiation time.
2. Added an explicit `SahaParams.from_config(**overrides)` factory classmethod.
3. Updated `runner.py` to call `SahaParams.from_config()` and emit `logger.info` logging the effective runtime parameters for every execution.
4. Updated `scripts/calibrate_saha_cpu.py` to log effective `SahaParams` per run.

---

## 2. Coarse CPU Parameter Grid Results & Selection Transparency

The grid was executed across calibration seeds `101`, `102`, `103` on three distinct scenarios:
- `scenario_1_baseline` (Heterogeneous Near-Saturation, 96.7% pool load)
- `scenario_2_burst` (Dynamic Flash Crowd, 137% peak pool load)
- `scenario_over_declaration` (Unused Quota Reclamation)

### 2.1 Full Candidate Specification (All 5 Tunables)

| Candidate | Name | $\alpha$ (`ewma_alpha`) | $h$ (`headroom_h`) | Step (`throttle_step`) | $\rho_{\text{low}}$ | $\rho_{\text{high}}$ |
|:---:|:---|:---:|:---:|:---:|:---:|:---:|
| **$C_1$** | Baseline / Default | `0.30` | `0.20` | `1.25` | `0.85` | `1.00` |
| **$C_2$** | Conservative | `0.15` | `0.10` | `1.15` | `0.80` | `0.95` |
| **$C_3$** | Responsive | `0.50` | `0.30` | `1.35` | `0.85` | `1.00` |
| **$C_4$** | Wide Hysteresis | `0.30` | `0.20` | `1.25` | `0.80` | `1.05` |
| **$C_5$** | Narrow Hysteresis | `0.30` | `0.20` | `1.25` | `0.90` | `1.00` |

### 2.2 True Default Values in `config.py` & Prior Mislabel Correction
At tag `stage6-calibrated`, the single-source defaults in `src/alloc_platform/config.py` are:
- `saha_ewma_alpha = 0.30`
- `saha_headroom_h = 0.20`
- `saha_throttle_step = 1.25`
- `saha_rho_low = 0.85`
- `saha_rho_high = 1.00`

*Correction of Prior Mislabel*: The earlier prompt response narrative erroneously stated $\alpha = 0.25$ for $C_1$ due to copying values from an exploratory scratchpad rather than transcribing the committed `config.py` constants. The script `scripts/calibrate_saha_cpu.py` has always evaluated the true grid above.

### 2.3 Candidate Selection Rule Application & Post-Hoc "Keep Default" Disclosure
In `docs/stage6_calibration_plan.md` Section 3, the selection rule is:
> *"Objective: maximize mean goodput and minimize throttled-while-spare ratio on calibration workload profiles."*

**Rule Evaluation**:
1. **Goodput**: All five candidates produce identical simulated goodput ($44.78\%$ overall on-time ratio; $45.05\text{ req/s}$; $17.07\%$ on H5 Scenarios 1 & 2).
2. **Throttled-While-Spare Ratio**:
   - $C_3$ (Responsive): **$6.67\%$** (H5)
   - $C_1, C_4, C_5$: **$10.00\%$** (H5)
   - $C_2$ (Conservative): **$16.67\%$** (H5)
3. **Latency Profile (Clarification of Prior Mislabeled Figure)**:
   - $C_1$: H5 p95 = $65.0\text{ ms}$; H7 p95 = $102.3\text{ ms}$.
   - $C_2$: H5 p95 = **$75.5\text{ ms}$**; H7 p95 = **$103.0\text{ ms}$**. (A prior narrative summary erroneously cited $75.5\text{ ms}$ as H7 p95; $75.5\text{ ms}$ is strictly the H5 p95 latency, while $103.0\text{ ms}$ is the true H7 p95 latency).
   - $C_3$: H5 p95 = $65.0\text{ ms}$; H7 p95 = $98.4\text{ ms}$.
4. **Cap Check**: **All candidates exceed the preregistered $\le 5.0\%$ throttled-while-spare cap in simulation.**

**Formal Selection Disclosures**:
1. **The Plan's Rule Selects $C_3$**: Strictly applying the plan's criterion ("minimize throttled-while-spare ratio") selects candidate **$C_3$** ($6.67\%$).
2. **Retaining $C_1$ was Post-Hoc**: The decision to retain $C_1$ in `config.py` was explicitly **post-hoc** and was not generated by applying the optimization rule.
3. **Withdrawal of Unsupported "Control Churn" Claim**: The earlier conjecture that $C_3$ would cause "excessive control churn" was not substantiated by empirical churn measurements and is hereby withdrawn.
4. **CPU Calibration is Fundamentally Invalid until Shedding is Resolved**: Because memory floor cold-start shedding dropped 100% of `tenant_cpu` requests across all candidates on Scenarios 1 & 2, the simulated CPU calibration evaluated truncated workloads and is **scientifically invalid** as a basis for freezing CPU parameters until the memory floor shedding defect is resolved. Retaining $C_1$ simply preserves the initial architectural design baseline.

---

## 3. Simulator Baseline Audit & Retraction of General "Queueing Artifact"

### 3.1 Empirical Test of Baseline Allocators in Simulation
To test the earlier hypothesis that synthetic queueing degrades all allocators in simulation, all four baseline allocators plus SAHA default were simulated under identical conditions (seeds `101`, `102`, `103` on `scenario_1_baseline` and `scenario_2_burst`):

| Policy | On-Time Ratio (Mean) | Throughput (Mean) | p95 Latency (Mean) | Throttled-While-Spare |
|:---|:---:|:---:|:---:|:---:|
| **FCFS** | 86.09% | 79.10 req/s | 230.0 ms | 0.00% |
| **PROPORTIONAL** | **100.00%** | 79.10 req/s | 61.3 ms | 0.00% |
| **DRF** | **100.00%** | 79.10 req/s | 66.1 ms | 0.00% |
| **ADAPTIVE** | **100.00%** | 79.10 req/s | 76.5 ms | 0.00% |
| **SAHA (Default)** | **17.07%** | 13.50 req/s | 65.0 ms | 10.00% |

### 3.2 Retraction and Root-Cause Diagnosis
The earlier claim that *"offered load causes a queueing artifact for all allocators in simulation"* was **untested and is hereby formally RETRACTED**. 

The actual root cause of SAHA's $17.07\%$ simulated on-time ratio is **memory floor cold-start shedding**:
1. In `scenario_1_baseline` and `scenario_2_burst`, sum of declared memory demand is $3,584\text{ MB}$ ($512 + 2,048 + 1,024$), which exceeds pool capacity ($3,072\text{ MB}$).
2. On epoch 0, `prev_alloc` is `None`. The cold-start guard initialized `prev_mem` to declared demand, setting initial memory floors to $3,584\text{ MB} > 3,072\text{ MB}$.
3. This triggered deterministic priority shedding (Condition 4), which allocated $0\text{ MB}$ to `tenant_cpu` (lowest dominant share) and flagged `tenant_cpu` in `unmet_guarantees` on every epoch.
4. Consequently, the runner marked all 1,922 requests for `tenant_cpu` as `status = "rejected"`, capping overall on-time ratio at $388 / 2,310 = 16.80\%$.
5. Other allocators (DRF, Proportional, Adaptive) do not implement floor shedding and completed $100\%$ on-time in simulation.

### 3.3 Reconciliation with Live EC2 Hardware Measurements
In the committed historical fallback bundle ([`data/recorded_fallback.json`](../data/recorded_fallback.json)), live hardware measurements in `scenario_1_baseline` on EC2 `c7i-flex.large` show the inverse dynamic:
- **DRF**: **100.00%** on-time ratio across all 5 runs (`run_7d7c28a11387`, `run_f4478a979782`, `run_913300cf9591`, `run_c38f0dd5fa79`, `run_e96e97ff065a`), p95 = 66.2 ms.
- **PROPORTIONAL**: **94.10%** mean on-time ratio (runs `run_c76672b5bcaa`, `run_c3df8a780d07`, `run_10c68c2a74da`, `run_9658213bc268` at 100%; `run_8762ee95eace` at 70.51%).
- **FCFS**: **75.33%** mean on-time ratio.
- **ADAPTIVE**: Suffered saturation collapse to **11.87%** mean on-time ratio (`run_3855e1b432b3` at 6.16%, `run_fef01150692c` at 10.69%, `run_65feecc29312` at 11.46%, `run_714e3d4cd99e` at 12.60%), p95 = 2,046 ms.

**Why the 70.0% Threshold was Expected to be Attainable**:
On live Linux hardware, the CFS kernel scheduler time-slices multi-threaded worker requests concurrently across hyperthreads. Fair allocators (DRF, Proportional) attain 94%–100% on-time completion under real CFS time-slicing. Adaptive collapsed to 11.87% because its SLSQP log-utility optimization jammed CPU shares at minimums. SAHA delegating to DRF in FAIR mode was designed to replicate DRF's 100% live hardware performance.

However, if SAHA's memory floor logic triggers shedding under $\sum D_{\text{mem}} > C_{\text{mem}}$ on live hardware, H5 will fail. We do NOT modify the preregistered 70% threshold.

---

## 4. Engineering Recommendation
We recommend executing the live confirmatory evaluation under Amendment 4 (all baselines re-run contemporaneously on seeds 42–46). If SAHA fails the 70% threshold live, the failure will be recorded honestly in `docs/RESULTS.md` without ex-post retuning.
