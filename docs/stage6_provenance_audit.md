# Stage 6 Historical Fallback Bundle Provenance Audit

**Date**: 2026-10-23  
**Status**: AUDITED (Recommendations Proposed; Historical Bundle Preserved Unmodified per Hard Rule 2)  
**Target File Audited**: [`data/recorded_fallback.json`](../data/recorded_fallback.json) (108 runs)  

---

## 1. Executive Summary

An exhaustive provenance audit was conducted on all 108 runs in `data/recorded_fallback.json` to determine whether request latency metrics (`mean_latency_ms`, `p95_latency_ms`, `completed`, `on_time_ratio`) originated from live HTTP network measurements (`_dispatch_single_request` in `loadgen.py`) or from the deterministic synthetic fallback path in `runner.py` (`if not req_samples and trace:`).

### Key Finding
- **80 Runs**: Confirmed originated from **Real HTTP Samples** (variable latencies, non-zero timeout/censoring, asynchronous arrival dropouts).
- **28 Runs**: Identified as originating from the **Synthetic Fallback Path** (`runner.py:617-658`), despite being labeled with `provenance: "MEASURED"`.
- **Root Cause of Mislabeling**: When Docker containers were present (`enforcer is not None`), `runner.py:223` set `provenance = "MEASURED"`. If live request collection failed or returned zero samples (`not req_samples`), the runner fell back to synthetic evaluation over the open-loop trace, but stamped the resulting `RequestSample` objects with the outer `provenance` variable (`"MEASURED"`).

---

## 2. Methodology & Hallmarks of Synthetic vs Real Samples

In `runner.py`, request execution follows two distinct branches:
1. **Real-Sample Path (`run_load` via `loadgen.py`)**:
   - HTTP POST requests dispatched asynchronously to tenant containers over container network.
   - Latency includes operating system scheduling, network stack traversal, and server handling:
     $$\text{latency\_ms} = (\text{completion\_time} - \text{scheduled\_time}) \times 1000.0$$
   - Timeouts and socket errors trigger `is_censored = True` and produce variable completion counts ($len(\text{samples}) < len(\text{trace})$).
2. **Synthetic Fallback Path (`runner.py:617-658`)**:
   - Evaluated deterministically when `req_samples` is empty.
   - Latency is computed mathematically from nominal trace service time and allocation speedup:
     $$\text{speedup} = \min\left(2.0, \max\left(0.2, \frac{x_{\text{cpu}}}{d_{\text{cpu}}}\right)\right), \quad \text{service\_ms} = \frac{\text{nominal\_service\_ms}}{\text{speedup}}, \quad \text{latency\_ms} = \text{service\_ms} + 5.0$$
   - Each sample's latency is explicitly rounded: `round(latency_ms, 2)`.
   - Total sample count strictly equals the trace length ($2,452$ for Scenario 1; $2,360$ for Honest; $2,425$ for Over-Declaration).
   - `censored` is strictly $0.0$ (censored logic does not exist in the synthetic fallback branch).
   - For static policies (DRF, Proportional, FCFS), all repeats produce mathematically identical mean and p95 values down to floating-point precision.

---

## 3. Provenance Audit Classification Table

| Scenario | Policy | Total Runs | Real HTTP Runs | Synthetic Fallback Runs | Classification Evidence |
| :--- | :--- | :---: | :---: | :---: | :--- |
| **`scenario_1_baseline`** | `drf` | 5 | 0 | **5** | All 5 runs: completed = 2452, p95 = 66.2 ms, mean = 54.641835 ms (exact match to rounded synthetic formula, diff = 0.000e+00). |
| **`scenario_1_baseline`** | `proportional` | 5 | 1 | **4** | Reps 0, 1, 3, 4: completed = 2452, p95 = 61.1945 ms (exact synthetic formula match). Rep 2: real HTTP (comp = 2347, cens = 14, p95 = 2213.99 ms). |
| **`scenario_1_baseline`** | `fcfs` | 5 | 1 | **4** | Reps 0, 1, 2, 4: completed = 2452, p95 = 230.0 ms (exact synthetic formula match). Rep 3: real HTTP (comp = 2215, rej = 176, p95 = 4168.92 ms). |
| **`scenario_1_baseline`** | `adaptive` | 8 | 8 | 0 | Variable completions (2150–2201), non-deterministic p95 (1885–2154 ms), live HTTP network variance. |
| **`scenario_1_baseline`** | `adaptive_noise` | 5 | 5 | 0 | Variable completions (2166–2200), non-deterministic p95 (2027–2063 ms). |
| **`scenario_1_baseline`** | *Ablations (4)* | 12 | 12 | 0 | All 12 runs: variable completions (2144–2230), non-deterministic p95s. |
| **`scenario_honest_declaration`** | `drf` | 5 | 0 | **5** | All 5 runs: completed = 2360, p95 = 58.1 ms, mean = 49.922089 ms (exact match to rounded synthetic formula). |
| **`scenario_honest_declaration`** | `proportional` | 5 | 0 | **5** | All 5 runs: completed = 2360, p95 = 58.1 ms, mean = 49.922089 ms (exact match to rounded synthetic formula). |
| **`scenario_honest_declaration`** | `fcfs` | 5 | 0 | **5** | All 5 runs: completed = 2360, p95 = 58.1 ms, mean = 49.922089 ms (exact match to rounded synthetic formula). |
| **`scenario_honest_declaration`** | `adaptive` | 5 | 5 | 0 | Variable p95 (58.19–72.95 ms), live HTTP network variance. |
| **`scenario_honest_declaration`** | `adaptive_noise` | 5 | 5 | 0 | Variable p95 (58.28–58.64 ms). |
| **`scenario_over_declaration`** | `adaptive` | 8 | 8 | 0 | Variable completions (2134–2191), variable rejected (126–180), live HTTP loadgen. |
| **`scenario_over_declaration`** | `adaptive_noise` | 5 | 5 | 0 | Variable completions (2145–2183), variable rejected (136–193). |
| **`scenario_over_declaration`** | `drf` | 5 | 5 | 0 | Variable completions (2039–2091), variable rejected (251–303), live HTTP latency. |
| **`scenario_over_declaration`** | `proportional` | 5 | 5 | 0 | Variable completions (2061–2078), variable rejected (263–280). |
| **`scenario_over_declaration`** | `fcfs` | 5 | 5 | 0 | Variable completions (1152–1203), severe rejection (1052–1133), live HTTP CFS throttling. |
| **`scenario_over_declaration`** | *Ablations (4)* | 12 | 12 | 0 | Variable completions (2051–2425), variable rejected. |
| **`demo_scenario`** | *All (3)* | 3 | 3 | 0 | Completed 9–11 requests, variable latencies. |
| **TOTAL** | — | **108** | **80** | **28** | **28 runs identified as synthetic fallback.** |

---

## 4. DRF Reconstruction Discrepancy Resolution (Phase 1.2)

- **Recorded Mean in Fallback Bundle**: `54.641835236541596 ms`
- **Re-Simulated Unrounded Mean**: `54.641899539041546 ms` (Raw difference: $+0.000064302\text{ ms}$)
- **Re-Simulated Rounded Mean (`round(lat, 2)`)**: `54.641835236541596 ms`
- **Exact Difference**:
  $$\text{Difference} = 54.641835236541596 - 54.641835236541596 = \mathbf{0.000000000000000\text{ ms}}$$
- **Mathematical Explanation**: In `runner.py:653`, the synthetic fallback path rounds every request sample's latency to two decimal places:
  ```python
  RequestSample(
      ...
      latency_ms=round(latency_ms, 2),
      service_ms=round(service_ms, 2),
      status=status,
      provenance=provenance,
  )
  ```
  The arithmetic mean of these 2,452 rounded numbers produces `54.641835236541596 ms` identically. This completely accounts for the $6.43 \times 10^{-5}\text{ ms}$ difference.

---

## 5. Formal Recommendation for Provenance Label Corrections

Per **Hard Rule 2**, `data/recorded_fallback.json` is preserved unmodified to guarantee frozen audit integrity.

When exporting future bundles or presenting historical data in `docs/RESULTS.md`:
1. The 28 runs listed above must be reported with provenance **`SIMULATED_FALLBACK`** (or `CALCULATED`) rather than `MEASURED`.
2. The contemporaneous live confirmatory benchmark runs executed under Stage 6 will supersede these historical fallback baselines with 100% verified live measurements on real cgroups.
