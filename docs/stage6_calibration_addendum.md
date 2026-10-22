# Stage 6 Calibration Addendum: Coarse CPU Grid Analysis & H5 Simulation Prediction

**Date**: 2026-10-22  
**Status**: COMMITTED ADDENDUM TO PREREGISTRATION  
**Authors**: Senior Systems Engineering Pair  
**Related Documents**: [`docs/stage6_calibration_plan.md`](./stage6_calibration_plan.md), [`docs/stage6_preregistration.md`](./stage6_preregistration.md), [`docs/ADR-011-saturation-aware-hybrid-allocator.md`](./ADR-011-saturation-aware-hybrid-allocator.md)  
**Calibration Artifact**: [`data/calibration_results_stage6.json`](../data/calibration_results_stage6.json)  

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

### 1.2 Applied Fix
1. Updated `SahaParams` in `src/alloc_platform/allocator/saha.py` to use `field(default_factory=lambda: config.saha_...)` for all parameters, ensuring dynamic binding to the current single-source config at instantiation time.
2. Added an explicit `SahaParams.from_config(**overrides)` factory classmethod.
3. Updated `runner.py` to call `SahaParams.from_config()` and emit `logger.info` logging the effective runtime parameters for every execution.
4. Updated `scripts/calibrate_saha_cpu.py` to log effective `SahaParams` per run.

---

## 2. Coarse CPU Parameter Grid Results

The grid was re-executed across calibration seeds `101`, `102`, `103` on three distinct scenarios:
- `scenario_1_baseline` (Heterogeneous Near-Saturation, 96.7% pool load)
- `scenario_2_burst` (Dynamic Flash Crowd, 137% peak pool load)
- `scenario_over_declaration` (Unused Quota Reclamation)

### 2.1 Candidate Configurations
- **$C_1$ (Baseline Default)**: $\alpha = 0.30$, $h = 0.20$, $\text{step} = 1.25$, $\rho_{\text{low}} = 0.85$, $\rho_{\text{high}} = 1.00$
- **$C_2$ (Conservative)**: $\alpha = 0.15$, $h = 0.10$, $\text{step} = 1.15$, $\rho_{\text{low}} = 0.80$, $\rho_{\text{high}} = 0.95$
- **$C_3$ (Responsive)**: $\alpha = 0.50$, $h = 0.30$, $\text{step} = 1.35$, $\rho_{\text{low}} = 0.85$, $\rho_{\text{high}} = 1.00$
- **$C_4$ (Wide Hysteresis)**: $\alpha = 0.30$, $h = 0.20$, $\text{step} = 1.25$, $\rho_{\text{low}} = 0.80$, $\rho_{\text{high}} = 1.05$
- **$C_5$ (Narrow Hysteresis)**: $\alpha = 0.30$, $h = 0.20$, $\text{step} = 1.25$, $\rho_{\text{low}} = 0.90$, $\rho_{\text{high}} = 1.00$

### 2.2 Formal Metric Separation (H5 vs H7)
Per Part 1 Item 2, H5 covers Scenarios 1 and 2 only; `scenario_over_declaration` evaluates H7 and is strictly separated.

| Candidate | H5 On-Time Ratio (Scen 1 & 2) | H5 Throttled-Spare Ratio (Scen 1 & 2) | H5 p95 Latency (Scen 1 & 2) | H7 On-Time Ratio (Over-Decl) | H7 p95 Latency (Over-Decl) |
| :--- | :---: | :---: | :---: | :---: | :---: |
| **$C_1$ (Baseline Default)** | **17.07%** | **10.00%** | **65.0 ms** | **100.0%** | **102.3 ms** |
| **$C_2$ (Conservative)** | 17.07% | 16.67% | 75.5 ms | 100.0% | 103.0 ms |
| **$C_3$ (Responsive)** | 17.07% | 6.67% | 65.0 ms | 100.0% | 98.3 ms |
| **$C_4$ (Wide Hysteresis)** | 17.07% | 10.00% | 65.0 ms | 100.0% | 99.7 ms |
| **$C_5$ (Narrow Hysteresis)** | 17.07% | 10.00% | 65.0 ms | 100.0% | 102.3 ms |

### 2.3 Demonstration of Parameter Impact
With the plumbing verified:
1. **$C_2$ (Conservative)**: Lower headroom ($h=0.10$) and sluggish throttle escalation ($\text{step}=1.15$) caused throttled-while-spare to increase from $10.00\%$ to **$16.67\%$**, and degraded H5 p95 latency from $65.0\text{ ms}$ to **$75.5\text{ ms}$**.
2. **$C_3$ (Responsive)**: Higher headroom ($h=0.30$) and faster throttle step ($1.35$) reduced throttled-while-spare to **$6.67\%$** and improved H7 p95 latency from $102.3\text{ ms}$ to **$98.3\text{ ms}$**.
3. **On-Time Ratio Invariance in Simulation**: All candidates produced identical $17.07\%$ on-time ratio in Scenarios 1 and 2 because aggregate CPU offered load ($2.40\text{c}$ in Scenario 1, $3.00\text{c}$ in Scenario 2) exceeds pool capacity ($1.50\text{c}$) by $160\%\text{--}200\%$.

---

## 3. Explicit Disclosure: Simulation Predicts an H5 Failure

The preregistered criteria for Hypothesis H5 are:
- **On-Time Ratio**: $\ge 70.0\%$ (vs Adaptive baseline $7.1\%\text{--}11.1\%$)
- **Throttled-While-Spare Ratio**: $\le 5.0\%$

### 3.1 Simulated Values vs Preregistered Thresholds
- **Simulated Mean On-Time Ratio (Scenarios 1 & 2)**: **$17.07\%$** $\ll \mathbf{70.0\%}$ (**FAIL** by $-52.93\text{ pp}$)
- **Simulated Throttled-While-Spare Ratio**: **$6.67\%\text{--}16.67\%$** $> \mathbf{5.0\%}$ (**FAIL** by $+1.67\text{ pp}$ to $+11.67\text{ pp}$)

### 3.2 Mechanism of the Simulated Failure
In the offline Python simulator:
1. Request service time scales inversely with assigned CPU:
   $$\text{speedup} = \frac{x_{\text{cpu}}}{d_{\text{cpu}}}, \quad \text{latency} = \frac{\text{nominal\_service\_ms}}{\text{speedup}} + 5.0\text{ ms}$$
2. In Scenario 1, pool capacity is $1.50\text{ cores}$ while sum of tenant demands is $2.40\text{ cores}$. Even under optimal Dominant Resource Fairness, `tenant_cpu` receives $\le 0.85\text{ cores}$ ($71\%$ of its $1.20\text{c}$ demand).
3. The resulting speedup ($0.71$) increases nominal service time from $15\text{ ms}$ to $21.1\text{ ms} + 5.0\text{ ms} = 26.1\text{ ms}$, exceeding the tight $25.0\text{ ms}$ deadline. The simulator flags every such request as `status = "deadline_missed"`.
4. In simulation, no CPU parameter tuning can overcome the mathematical reality of $2.40\text{c}$ offered load on a $1.50\text{c}$ capacity limit without shedding load.

### 3.3 Simulator vs Live Hardware Discrepancy
In the frozen v1.0 hardware evaluation (`docs/RESULTS.md` line 22):
- On live EC2 Linux hardware, **DRF achieved 100.0% on-time ratio** in Scenario 1 and Scenario 2, while Adaptive suffered saturation collapse ($11.1\%$ and $10.7\%$).
- Live hardware CFS time-slicing across hyperthreads and socket concurrency allowed containers to complete within deadline, whereas the offline simulator's deterministic serial model does not capture kernel CFS queueing dynamics.
- **Scientific Caveat**: While live hardware may yield higher on-time completion than the simulator (as observed for DRF in Stage 5), the scientific team formally records that **the offline simulation predicts an H5 failure**. Per preregistration rules, the $70.0\%$ and $5.0\%$ thresholds remain frozen.

---

## 4. Engineering Recommendation for Live Evaluation

The systems engineering pair provides the following recommendation for the platform reviewer:

1. **Option A (Proceed to Live Confirmatory Run)**:
   - Launch the clean ephemeral EC2 instance and execute confirmatory benchmarks for H5–H8 on seeds `42--46`.
   - **Rationale**: The research platform's primary purpose is evaluating real container enforcement on Linux cgroup v2. If SAHA on hardware replicates DRF's 100% on-time completion in FAIR mode (as designed), H5 will be empirically tested against reality. If live hardware also fails the $70\%$ threshold, the failure will be recorded with equal prominence in `docs/RESULTS.md` without post-hoc tuning.
2. **Option B (Halt and Conclude on Simulation Failure)**:
   - Halt prior to live EC2 deployment and grade H5 as Not Supported based on simulation.
3. **Recommendation**: We recommend **Option A**. The ephemeral evaluation is budgeted at $\le 30\text{ minutes}$ ($< \$0.06$). Running live provides honest empirical evidence directly answering whether SAHA's FAIR-mode DRF delegation resolves the real Linux kernel CFS saturation collapse.
