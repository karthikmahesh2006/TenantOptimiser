# Preregistration: Stage 6 Extension - Saturation-Aware Hybrid Allocator (SAHA)

**Date**: 2026-10-22  
**Status**: PREREGISTERED (Prior to Calibration & Confirmatory Benchmarking)  
**Authors**: Senior Systems Engineering Pair  
**Baseline Git Commit**: `44f6802` (`v1.0-demo-baseline`)  
**Stage 6 Branch**: `stage6-saha`  

---

## 1. Experimental Methodology & Rigor

Under ADR-009, this preregistration document is committed, tagged, and its commit hash recorded **prior** to running any confirmatory benchmark experiments on hardware. All hypotheses, effect sizes, statistical tests, ablations, seed splits, and failure criteria are frozen herein.

### 1.1 Disjoint Seed Allocation
- **Coarse Calibration Seeds (Simulation Only)**: Seeds `101`, `102`, `103`.
  - Restricted strictly to the 5 CPU/mode tunables (`saha_ewma_alpha`, `saha_headroom_h`, `saha_throttle_step`, `saha_rho_low`, `saha_rho_high`).
- **Confirmatory Benchmark Seeds (Live Hardware on EC2)**: Seeds `42`, `43`, `44`, `45`, `46` ($n = 5$ repeats per policy per scenario).
  - Strictly disjoint from calibration seeds. Never inspected or simulated during tuning.

### 1.2 Metric Accounting & Inflation Guard (Part 2 Condition A)
To prevent metric inflation via selective request shedding:
1. All headline completion metrics (`on_time_ratio`, `completion_ratio`, `goodput`) are evaluated against **TOTAL OFFERED LOAD** ($len(samples)$), NOT completed-only:
   $$\text{on\_time\_ratio} = \frac{\text{completed on-time requests}}{\text{total offered requests}}$$
2. Shed requests receive `status = "rejected"` and count strictly as failed requests in the denominator.
3. Every reported table of goodput and `on_time_ratio` must report the **rejected fraction** ($\text{rejected\_count} / \text{total\_offered}$) in an adjacent column.
4. **`status == "error"` Sample Treatment**:
   - `error` samples (HTTP 5xx or connection drops) are included in `total_offered` (lowering goodput/on-time ratio).
   - `error` samples are excluded from completed p95 latency.
   - This accounting treatment is identical across FCFS, Proportional, DRF, Adaptive, and SAHA.

---

## 2. Checkable OOM Definitions & Detection Protocol

### 2.1 Definition of "Honest Load"
A tenant workload is defined as **Honest** if and only if its physical memory RSS remains strictly within its contractually declared memory demand at all times $t$:
$$\text{RSS}_i(t) \le D_{i, \text{mem}} \quad \forall t \in [0, T]$$
If a tenant violates this condition ($\text{RSS}_i(t) > D_{i, \text{mem}}$), it is classified as **Dishonest Load**.

### 2.2 Kernel Detection Method
OOM kills are monitored via Linux kernel cgroup v2 controller interface files:
- Path: `/sys/fs/cgroup/system.slice/docker-<cid>.scope/memory.events`
- Key: `oom_kill <integer>`
The orchestrator samples `memory.events` at every epoch ($1\text{ Hz}$) and collects final exit status upon container teardown.

### 2.3 Accounting Separation
- Any OOM kill occurring under Honest Load is classified as an **Allocator Safety Failure** (`honest_oom_kill`) and counts against the allocator's headline reliability metric.
- Any OOM kill occurring when $\text{RSS}_i(t) > D_{i, \text{mem}}$ is classified as a **Workload Violation** (`dishonest_oom_kill`) and reported in a separate telemetry column. It does not count as a policy failure.

---

## 3. Formal Hypotheses H5–H8

| Hypothesis | Description | Target Metric | Baseline Comparison | Effect-Size Threshold | Statistical Test | Failure Definition |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **H5** | Mitigate Saturation Collapse | `on_time_ratio` (over Total Offered Load) | Adaptive baseline in Scenario 1 ($7.1\%$) | $\text{SAHA} \ge 85.0\%$ ($\ge +75.0\text{ pp}$ over Adaptive) | 1-tailed Welch's $t$-test ($\alpha = 0.05$) across $n=5$ repeats | $\text{SAHA} < 80.0\%$ OR $p \ge 0.05$ vs Adaptive |
| **H6** | Zero Honest-Load OOM Kills | `oom_kills` under honest load | DRF baseline in Scenario 3 ($4/5 = 80\%$ runs terminated) | Exactly $0$ OOM kills across all $n=5$ runs ($0.0\%$ rate) | Fisher's Exact Test ($\alpha = 0.05$) comparing $0/5$ vs $4/5$ | Any container terminated by OOM under honest load ($\text{oom\_kills} > 0$) |
| **H7** | Preserve Over-Declaration Win | Honest Tenant C `on_time_ratio` & Throughput | Static Proportional in Scenario Over-Decl | $\text{SAHA} \ge \text{Proportional} + 15.0\text{ pp}$, within $5\text{ pp}$ of Adaptive | 1-tailed paired $t$-test ($\alpha = 0.05$) | Tenant C on-time ratio $< \text{Proportional} + 5.0\text{ pp}$ |
| **H8** | Rapid Host Capacity Contraction | CPU Convergence Epochs on Capacity Cut | Adaptive baseline in Scenario 3 ($\ge 3$ epochs) | Convergence in $\le 1$ epoch; zero capacity over-allocation | Wilcoxon signed-rank test ($\alpha = 0.05$) | Convergence $> 2$ epochs OR any epoch violating $\sum x_i \le C_{\text{cpu}}$ |

---

## 4. Pre-Declared Ablation Suite

To quantify the marginal contribution of each SAHA component, five structural ablations are pre-registered and will be executed under identical conditions:

1. `saha:no_declared_floor`:
   - Disables the 75% declared-demand floor rule (`use_declared_floor = False`, `mem_declared_floor_frac = 0.0`).
   - Control: Tests whether sliding-window p99 RSS alone is sufficient to prevent OOM kills under bursty workloads.
2. `saha:no_cut_latch`:
   - Disables the capacity cut bypass latching (`cut_latch_epochs = 0`).
   - Control: Tests whether transient telemetry delays cause host over-allocation.
3. `saha:no_throttle_escalation`:
   - Disables multiplicative throttle escalation (`throttle_step = 1.0`).
   - Control: Tests speed of recovery from CPU throttling under load spikes.
4. `saha:reclaim_only`:
   - Disables mode switching; forces permanent RECLAIM mode ($\rho_{\text{low}} = \infty$).
   - Control: Demonstrates the necessity of switching to DRF fair mode under heavy saturation.
5. `saha:fair_only`:
   - Disables mode switching; forces permanent FAIR mode ($\rho_{\text{high}} = 0.0$).
   - Control: Quantifies goodput loss from failing to harvest surplus memory under non-saturated regimes.

---

## 5. Run Invalidation & Exclusion Rules

A confirmatory benchmark run is declared **Invalid** and excluded from hypothesis testing if and only if host-level environment contamination occurs:
1. **CPU Steal:** Host CPU steal exceeds $5.0\%$ (`max_cpu_steal_pct`).
2. **Host Reservation Breach:** Non-tenant host CPU usage exceeds $0.50\text{ cores}$ ($25\%$ reservation).
3. **Hardware / Network Failure:** EC2 instance hardware fault, kernel crash, or dropped SSM session preventing metric collection.

*Crucially: Allocator decisions (request timeouts, latency spikes, CFS throttling, request rejections, or policy-induced OOM kills) NEVER invalidate a run. They are recorded directly as empirical policy performance.*

---

## 6. "What We Do If a Hypothesis Fails" Statement

In adherence to scientific integrity:
1. Every result—positive, negative, or neutral—will be reported in full in `docs/RESULTS.md` and committed to the repository.
2. If any hypothesis fails to meet its effect-size threshold or statistical significance bound:
   - The result will be explicitly classified as **FAILED**.
   - No post-hoc parameter adjustments, threshold retuning, or seed shopping will be performed.
   - A dedicated diagnostic analysis will be conducted to isolate the mathematical or operational root cause of the failure.
   - The failure will be preserved in the platform archive as an established empirical limitation of the SAHA architecture.
