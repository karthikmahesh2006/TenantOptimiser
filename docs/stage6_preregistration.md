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
| **H6** | Zero Honest-Load OOM Kills | `oom_kills` under honest load | Contemporaneous live DRF in Scenario 3 (supersedes unverified historical narrative of "4/5 OOMs"; see Amendments 1 & 4) | Exactly $0$ OOM kills across all $n=5$ runs ($0.0\%$ rate) | Fisher's Exact Test ($\alpha = 0.05$) vs live DRF OOM rate | Any container terminated by OOM under honest load ($\text{oom\_kills} > 0$) |
| **H7** | Preserve Over-Declaration Win | Honest Tenant C `on_time_ratio` & Throughput | Static Proportional in Scenario Over-Decl | $\text{SAHA} \ge \text{Proportional} + 15.0\text{ pp}$, within $5\text{ pp}$ of Adaptive | 1-tailed paired $t$-test ($\alpha = 0.05$) | Tenant C on-time ratio $< \text{Proportional} + 5.0\text{ pp}$ |
| **H8** | Rapid Host Capacity Contraction | CPU Convergence Epochs on Capacity Cut | Adaptive baseline in Scenario 3 ($\ge 3$ epochs) | Convergence in $\le 1$ epoch; zero capacity over-allocation | Wilcoxon signed-rank test ($\alpha = 0.05$) | Convergence $> 2$ epochs OR any epoch violating $\sum x_i \le C_{\text{cpu}}$ |

> [!NOTE] **H5 Testing Reconciliation & Confirmatory Pass Criteria (Amendment 5)**:
> In accordance with Amendment 5 and Part 2 Condition D, the confirmatory statistical test for H5 is locked to a one-tailed Mann-Whitney U test comparing SAHA against contemporaneous live Adaptive across confirmatory seeds `42`–`46`:
> - **Full Pass**: $\text{On-Time}_{\text{SAHA}} \ge 85.0\%$ **AND** $p < 0.05$ vs contemporaneous live Adaptive.
> - **Partial Pass**: $80.0\% \le \text{On-Time}_{\text{SAHA}} < 85.0\%$ **AND** $p < 0.05$ vs contemporaneous live Adaptive.
> - **Failure**: $\text{On-Time}_{\text{SAHA}} < 80.0\%$ **OR** $p \ge 0.05$.
> All effect-size thresholds (85/80% on-time, 0 OOM, +15/+5 pp, $\le 1$ / $> 2$ epochs) remain strictly frozen.

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

---

## 7. Preregistration Amendments (Pre-Confirmatory Evaluation)

### Amendment 1: Contemporaneous Live DRF Baseline for Hypothesis H6
- **Context & Correction**: The preliminary preregistration draft cited DRF's historical "4/5 OOMs" from narrative observations in `docs/RESULTS.md` Section 1.1. However, the committed fallback bundle (`data/recorded_fallback.json`) contains only 108 runs across Scenarios 1, Over-Declaration, Honest-Declaration, and Demo; Scenario 3 raw runs were never exported to fallback. Evaluating H6 via Fisher's Exact Test against an unverified historical number is methodologically invalid.
- **Enacted Protocol**: During the confirmatory evaluation session on the ephemeral EC2 instance, DRF will be executed live alongside SAHA on `scenario_3_capacity_cut` across all 5 confirmatory seeds (`42`–`46`). H6 will evaluate SAHA's honest OOM rate against this contemporaneous, live DRF baseline within the exact same hardware session and environment.

### Amendment 2: Hard Auto-Termination Watchdog & Budget Cap
- The ephemeral EC2 instance will be launched with AWS parameter `InstanceInitiatedShutdownBehavior='terminate'`.
- A 75-minute hard shutdown watchdog will be initiated in instance user-data (50m workload execution across 85 runs + 25m setup/buffer margin):
  ```bash
  sudo shutdown -h +75 "Stage 6 evaluation budget cap reached"
  ```
  When the watchdog fires, the OS shutdown triggers immediate instance and root EBS volume termination, strictly capping total spend to $\le 75\text{ minutes}$ ($< \$0.11$).

### Amendment 3: Termination Security Group Verification & Artifact Capture
- At instance termination, the launch automation will query AWS EC2 for all inbound permissions on the security group:
  ```bash
  aws ec2 describe-security-groups --group-ids <SG_ID> --query "SecurityGroups[0].IpPermissions"
  ```
  The exact inbound rule count ($0$) will be asserted and committed into the final `data/stage6_live_metadata.json` artifact prior to instance termination.

### Amendment 4: Fully Contemporaneous Live Baselines, Paired Replication Protocol, Ablation Budget, and Reconciled Statistical Testing
- **Scope & Protocol**: To eliminate inter-session confounding across all hypotheses (H5–H8), all baseline allocators (Adaptive, DRF, Proportional) will be re-executed live contemporaneously alongside SAHA within the exact same ephemeral EC2 session on the identical seeds (`42`, `43`, `44`, `45`, `46`).
- **Seed Reuse & Paired Replication Disclosure**: Seeds `42`–`46` are identical to the baseline seeds used in the frozen v1.0 benchmark evaluations. Evaluating SAHA on seeds `42`–`46` constitutes a strict, controlled paired replication on known benchmark workloads, NOT an out-of-distribution independent confirmation.
- **Audit of Changed Tests and Metrics (Correction of Prior "Zero Test Wording Changed" Statement)**:
  The prior statement that "zero test wording changed" was incorrect and is withdrawn. Relative to the frozen Section 3 table, Amendment 4 makes the following specific adjustments:
  1. **H5 Comparison**: Compares SAHA against contemporaneous live Adaptive runs on seeds 42–46 (rather than frozen historical 7.1%). The statistical test is specified as a one-tailed two-sample Mann-Whitney U test (or Welch's t-test).
  2. **H6 Comparison**: Replaces the unverified historical "4/5 DRF OOMs" narrative with contemporaneous live DRF runs on Scenario 3. Primary criterion is made absolute; Fisher's exact test is secondary.
  3. **H7 Comparison**: Replaces static comparison with contemporaneous live Proportional and Adaptive runs. Primary metric remains Tenant C on-time ratio; throughput is added as supplementary.
  4. **H8 Comparison**: Evaluates contraction convergence against contemporaneous live Adaptive on Scenario 3.
- **Reversion of Pass Criteria (Part 2 Condition D Compliance)**:
  - **H5**: The threshold criterion is **MANDATORY**, not a "descriptive target". H5 passes if and only if **BOTH** the effect-size threshold ($\text{On-Time}_{\text{SAHA}} \ge 80.0\%$) **AND** the statistical significance test ($p < 0.05$ vs contemporaneous Adaptive) hold.
  - **H7**: Primary metric remains strictly **Honest Tenant C on-time ratio** ($\ge \text{Proportional} + 15.0\text{ pp}$; failure defined as $< \text{Proportional} + 5.0\text{ pp}$). Overall throughput is classified strictly as a **Supplementary/Secondary Metric**.
- **H6 Absolute Criterion & Fisher's Exact Power Disclosure (Part 2 Condition E Compliance)**:
  - **Primary Criterion (Absolute)**: SAHA must experience exactly $0$ honest-load OOM kills across all $n=5$ runs ($0.0\%$ rate). Any honest OOM kill is an immediate failure of H6.
  - **Secondary Criterion (Comparative)**: Fisher's Exact Test ($\alpha = 0.05$, one-tailed) comparing SAHA vs contemporaneous live DRF in Scenario 3.
  - **Power Disclosure**: With $n=5$ repeats per policy, Fisher's Exact Test achieves $p < 0.05$ if and only if contemporaneous live DRF experiences $\ge 4/5$ OOM kills ($0/5$ vs $4/5 \implies p = 0.0238$; $0/5$ vs $3/5 \implies p = 0.0833 > 0.05$). If live DRF experiences $\le 3$ OOM kills, the comparative test cannot reject $H_0$ at $\alpha = 0.05$.
- **Test Structure, Multiplicity, and Minimum Attainable p-Values (Part 2 Condition F Compliance)**:
  - **H5 Testing Structure**: Scenario 1 and Scenario 2 are evaluated both individually ($n=5$ each) and pooled ($N=10$ per policy). For individual testing, Bonferroni correction applies for the 2 scenario tests: $\alpha_{\text{adj}} = 0.05 / 2 = 0.025$. For pooled testing, $\alpha = 0.05$.
  - **Minimum Attainable p-Values at $n=5$**:
    - Mann-Whitney U test (one-tailed, $n_1=5, n_2=5$): $p_{\min} = \frac{1}{\binom{10}{5}} = \frac{1}{252} \approx 0.003968$ ($< \alpha_{\text{adj}} = 0.025$). (Pooled $n_1=10, n_2=10$: $p_{\min} \approx 0.000091$).
    - Wilcoxon signed-rank test (one-tailed, $n=5$ pairs): $p_{\min} = \left(\frac{1}{2}\right)^5 = 0.03125$. (Requires all 5 pairs to strictly favor SAHA to reach $p < 0.05$).
    - Fisher's Exact Test (one-tailed, $0/5$ vs $k/5$): $k=3 \implies p=0.0833$, $k=4 \implies p=0.0238$, $k=5 \implies p=0.003968$.
- **Run Count, Ablation Budget & Timing Validation (Part 2 Condition G Compliance)**:
  - **Core Baseline & Hypothesis Runs**:
    - Scenario 1 (Baseline, 30s): SAHA (5), Adaptive (5), DRF (5) = 15 runs.
    - Scenario 2 (Burst, 30s): SAHA (5), Adaptive (5), DRF (5) = 15 runs.
    - Scenario 3 (Capacity Cut, 30s): SAHA (5), Adaptive (5), DRF (5) = 15 runs.
    - Scenario Over-Declaration (30s): SAHA (5), Adaptive (5), Proportional (5) = 15 runs.
    - Subtotal: 60 runs.
  - **Pre-Declared Ablation Runs (Section 4)**:
    - Executed on `scenario_1_baseline` across seeds `42`–`46` ($n=5$ each):
      1. `saha:no_declared_floor`: 5 runs.
      2. `saha:no_cut_latch`: 5 runs.
      3. `saha:no_throttle_escalation`: 5 runs.
      4. `saha:reclaim_only`: 5 runs.
      5. `saha:fair_only`: 5 runs.
    - Subtotal: 25 ablation runs.
  - **Negative Control Arm (`saha_v1_frozen`, Section 7 Amendment 5)**:
    - Executed across seeds `42`–`46` ($n=5$ each) on Scenarios 1, 2, 3, and Over-Declaration: 20 runs.
  - **Total Session Runs (Option C Protocol)**: $60 + 25 + 20 = \mathbf{105\text{ live benchmark runs}}$.
  - **Wall-clock Runtime (Option b 120s Scenarios)**: 105 runs × ~125s (120s duration + 5s overhead) = $13,125\text{ s}$ ($\sim 218.8\text{ minutes}$ / $3.65\text{ hours}$) active execution.
  - **Instance Watchdog & Buffer Margin**: 245 minutes hard watchdog (5m boot/SSM + 219m workload execution + 21m artifact export and verification buffer).
  - **Financial Budget**: 245 minutes ($4.08\text{ hours}$) on EC2 `c7i-flex.large` ($0.088/\text{hr}$) = $\mathbf{\$0.359}$ (strictly $\le \$1.00$ cap).

### Amendment 5: Cold-Start Allocator Bug Resolution, 2x2 Attribution Matrix, Scenario Extension (Option b), and Negative Control Arm (Option C Protocol)

#### 1. Root-Cause Analysis: Cold-Start Memory Floor Over-Allocation Bug
During initial simulation on oversubscribed scenarios (Scenario 1 Baseline: Pool 1.5c / 3072 MB, declared demand 3584 MB), SAHA exhibited 17.07% on-time ratio while baselines achieved 86–100%. Diagnostic inspection revealed:
- In `src/alloc_platform/allocator/saha.py:346-356`, during cold start ($t < 60\text{ s}$ or $< 10$ samples), `prev_mem` fell back to declared demand `t.demand["mem_mb"]` whenever `prev_alloc` was `None` (epoch 0).
- When declared demand exceeded pool memory capacity ($\sum D_{\text{mem}} > C_{\text{mem}}$), ADR-011 §2.2 specifies that the declared-floor gate turns OFF and floors safely revert to empirical p99 RSS with bootstrap fallback ($1.15 \times \text{curr\_rss} + \text{margin}$).
- However, initializing `prev_mem = demand` forced `floor_val = max(calc_floor, prev_mem) = demand`, creating an artificial oversubscription where $\sum \text{floor} = 3584\text{ MB} > 3072\text{ MB}$.
- This erroneously triggered Condition 4 priority shedding at epoch 0, rejecting up to 1,922 requests per run.

#### 2. Attribution: 2x2 Factorial Matrix & Interaction Analysis
To rigorously isolate whether the 17.07% on-time metric was an allocator defect, a simulator artifact, or both, a 2x2 factorial matrix was executed locally on `scenario_1_baseline` across diagnosis seeds `101`, `102`, `103`:

| Test Cell | Allocator Policy | Synthetic RSS Frac | Mean On-Time Ratio | Mean Rejected Samples | Mean Unmet Guarantees | Mean Throttled-Spare Ratio |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: |
| **Cell 1** | Unpatched SAHA (`saha_v1_frozen`) | $0.80 \times D$ | **17.07%** | 1,968.0 | 0.0 | 10.00% |
| **Cell 2** | Unpatched SAHA (`saha_v1_frozen`) | $0.25 \times D$ | **17.07%** | 1,968.0 | 0.0 | 3.33% |
| **Cell 3** | Patched SAHA (`saha`) | $0.80 \times D$ | **17.07%** | 1,968.0 | 0.0 | 10.00% |
| **Cell 4** | Patched SAHA (`saha`) | $0.25 \times D$ | **100.00%** | **0.0** | **0.0** | **3.33%** |

- **Attribution Finding (Interaction Model)**:
  - The 17.07% failure is an **interaction** where the **allocator defect** (cold-start floor over-allocation) AND the **synthetic RSS artifact** ($0.80 \times D$ demand inflation) are each **independently sufficient** to cause 17.07% on-time shedding failure:
    - **Cell 2** shows that the unpatched allocator alone suffices to cause 17.07% failure even under realistic physical RSS ($0.25 \times D$).
    - **Cell 3** shows that the synthetic RSS artifact alone suffices to cause 17.07% failure even with the patched allocator ($1.15 \times 0.80 \times D + 0.10 \times 0.80 \times D = 1.00 \times D$, inducing artificial infeasibility).
    - **Cell 4** demonstrates that BOTH the allocator defect and the simulator artifact must be resolved together for the simulated run to achieve 100% on-time.
- **Unmet Guarantees Column Explanation**:
  - The "Unmet Guarantees" column counts solver-level minimum guarantee violations (`len(plan.unmet_guarantees)`).
  - In the preliminary report, this column showed `0.0` because `_compute_and_save_run_metrics` in `runner.py` did not persist `"unmet_guarantees_count"` as a key in `RunMetric`, causing queries to default to 0.0. The actual request-level shedding was captured in the request sample metrics as `rejected = 1968.0` (reflecting Condition 4 priority shedding for tenants whose memory floor was shed). In the current codebase, `unmet_guarantees_count` is explicitly recorded in `RunMetric`.
- **Explicit Disclaimer on Simulation vs Live Hardware**:
  > [!WARNING]
  > **Simulated 100% on-time does NOT imply or guarantee live EC2 success.**
  > The open-loop simulator models nominal service times and speedup functions, but does NOT model physical CPU saturation, Linux CFS runqueue queuing delay, context switching overhead, socket buffer exhaustion, or real kernel memory pressure under cgroup v2. Live confirmatory evaluation on EC2 is required to empirically test hypotheses H5–H8.

#### 3. Code Fix & Pinned Pre-Fix Snapshot
- **Fix (Commit `4d6cdb6`)**: In `saha.py:346-350`, when `prev_alloc is None` and declared-floor gate is inactive (`is_declared_floor_active is False`), `prev_mem` initializes to `calc_floor` ($1.15 \times \text{curr\_rss} + \text{margin}$) per ADR-011 §2.2, NOT declared demand.
- **Pinned Snapshot**: The pre-fix allocator code was snapshotted as `saha_v1_frozen` in `src/alloc_platform/allocator/saha_v1_frozen.py` (commit `b09cd20` state) and registered in `runner.py` dispatch.

#### 4. Confirmatory Seed Isolation
- Diagnosis, parameter verification, and matrix benchmarking strictly utilized **seeds `101`, `102`, `103` ONLY**.
- Confirmatory seeds `42`, `43`, `44`, `45`, `46` were strictly held out and have **NEVER** been simulated locally for SAHA.

#### 5. Phase 3 Scenario Duration & Rescaled Event Timing (Option b Selected)
- **Problem**: In 30s benchmark scenarios, the 60s cold-start sliding window ($W=60\text{ s}$, `mem_min_samples=10`) spans 100% of the run duration, preventing post-cold-start steady-state evaluation.
- **Decision Rule Application**: Option (a) kept 30s scenarios and shortened $W$ to 10s. Option (b) retained physical $W=60\text{ s}$ and extended scenario durations to 120s ($50\%$ steady-state operation). Per the governing decision rule, because the estimated AWS EC2 cost of Option (b) is $\le \$1.00$ ($4.08\text{ hr} \times \$0.088/\text{hr} = \$0.359$), **Option (b) is strictly selected**.
- **Scenario Event Rescaling ($t \ge W=60\text{ s}$)**:
  - At 30s:
    - `scenario_1_baseline`: No events.
    - `scenario_2_burst`: $t=10\text{ s}$ (burst to 1.8c), $t=20\text{ s}$ (burst end to 1.2c).
    - `scenario_3_capacity_cut`: $t=15\text{ s}$ (capacity cut to 0.90c / 2048 MB).
    - `scenario_4_churn`: $t=10\text{ s}$ (tenant arrive), $t=20\text{ s}$ (tenant leave).
    - `scenario_over_declaration` & `scenario_honest_declaration`: No events.
  - At 120s:
    - `scenario_1_baseline`: No events.
    - `scenario_2_burst`: $t=70\text{ s}$ (burst on to 1.8c), $t=95\text{ s}$ (burst end to 1.2c).
    - `scenario_3_capacity_cut`: $t=70\text{ s}$ (capacity cut to 0.90c / 2048 MB).
    - `scenario_4_churn`: $t=70\text{ s}$ (tenant arrive), $t=95\text{ s}$ (tenant leave).
    - `scenario_over_declaration` & `scenario_honest_declaration`: No events.
- **Hypothesis H8 & Scenario 2 (S2) Implications**:
  - **H8 (S3 Capacity Cut Contraction)**: Rescaling the capacity cut from $t=15\text{ s}$ to $t=70\text{ s} \ge W$ ensures that the allocator has completed its 60-second cold-start window and settled into steady-state EWMA/p99 floors before the cut occurs. If the cut occurred at $t=15\text{ s}$, contraction dynamics would be confounded with cold-start transients. At $t=70\text{ s}$, H8 cleanly tests whether SAHA latches and contracts within $\le 1$ epoch under genuine steady-state capacity degradation.
  - **S2 (Flash Crowd Dynamic Burst)**: Rescaling the burst to $t=70\text{ s}$ ensures that SAHA's headroom tracking and fast-up escalation are evaluated from a converged steady state, directly testing rapid scaling under load spikes without cold-start interference.
- **Contemporaneous Baseline Invariance**: Since all baselines (Adaptive, DRF, Proportional) are executed contemporaneously within the exact same ephemeral EC2 run, extending scenario duration introduces zero baseline drift or comparability bias.

#### 6. Confirmatory Protocol & Option C Matrix (105 Runs)
To conclusively evaluate the allocator fix in live hardware evaluation, Option C matrix protocol is adopted, adding a pinned `saha_v1_frozen` negative control arm:
- **Core Baseline & Hypothesis Runs (120s, seeds 42–46)**:
  - Scenario 1 (Baseline): SAHA (5), Adaptive (5), DRF (5) = 15 runs.
  - Scenario 2 (Burst): SAHA (5), Adaptive (5), DRF (5) = 15 runs.
  - Scenario 3 (Capacity Cut): SAHA (5), Adaptive (5), DRF (5) = 15 runs.
  - Scenario Over-Declaration: SAHA (5), Adaptive (5), Proportional (5) = 15 runs.
  - Subtotal: 60 runs.
- **Pre-Declared Ablation Runs (120s, seeds 42–46 on Scenario 1)**:
  - 5 ablations (`no_declared_floor`, `no_cut_latch`, `no_throttle_escalation`, `reclaim_only`, `fair_only`) × 5 seeds = 25 runs.
- **Negative Control Arm (120s, seeds 42–46)**:
  - `saha_v1_frozen` across Scenario 1 (5), Scenario 2 (5), Scenario 3 (5), Over-Declaration (5) = 20 runs.
- **Total Session Runs**: $60 + 25 + 20 = \mathbf{105\text{ runs}}$.
- **Active Execution Runtime**: 105 runs × (120s duration + 5s overhead) = $13,125\text{ s} = 218.75\text{ minutes}$ (~3.65 hours).
- **Instance Watchdog & Buffer**: 245 minutes hard watchdog (covering 218.8m execution + 26.2m bootstrap/export buffer).
- **Financial Budget**: 245 minutes (4.08 hours) on `c7i-flex.large` ($0.088/hr) = $\mathbf{\$0.359} \le \$1.00$.

---

### Amendment 6 (2026-10-23): Reduced Exploratory Live Evaluation Protocol (30 Runs)

#### 1. Scope & Nature of Evaluation
This live evaluation is explicitly declared **EXPLORATORY**, not the confirmatory 105-run design specified in Amendment 5 §6. The objective is to gather empirical hardware evidence on core allocator dynamics within a bounded execution budget, while allowing detached execution without requiring a client laptop to remain open.

#### 2. Evaluated Hypotheses (H5 and H7 Only)
Only two hypotheses from Section 3 are evaluated in this run:
1. **Hypothesis H5 (Mitigate Saturation Collapse in Scenario 1)**:
   - Target Metric: `on_time_ratio` (over Total Offered Load).
   - Baseline: Contemporaneous live Adaptive in `scenario_1_baseline`.
   - Statistical Test: One-tailed Mann-Whitney U test ($\alpha = 0.05$) across $n=5$ repeats (seeds `42`–`46`).
   - Effect-Size Thresholds (strictly frozen):
     - **Full Pass**: $\text{On-Time}_{\text{SAHA}} \ge 85.0\%$ AND $p < 0.05$ vs contemporaneous live Adaptive.
     - **Partial Pass**: $80.0\% \le \text{On-Time}_{\text{SAHA}} < 85.0\%$ AND $p < 0.05$ vs contemporaneous live Adaptive.
     - **Failure**: $\text{On-Time}_{\text{SAHA}} < 80.0\%$ OR $p \ge 0.05$.
2. **Hypothesis H7 (Preserve Over-Declaration Win in Scenario Over-Declaration)**:
   - Target Metric: Honest Tenant C `on_time_ratio` and throughput.
   - Baseline: Contemporaneous live Proportional and contemporaneous live Adaptive in `scenario_over_declaration`.
   - Statistical Evaluation: $\text{SAHA} \ge \text{Proportional} + 15.0\text{ pp}$ and within $5.0\text{ pp}$ of Adaptive. Failure defined as Tenant C on-time $< \text{Proportional} + 5.0\text{ pp}$.

#### 3. Omitted Hypotheses & Scope Exclusions
- **Hypothesis H6 (Zero Honest-Load OOM Kills in Scenario 3)**: **NOT TESTED**. Scenario 3 is omitted; no empirical claims are made regarding H6 in this evaluation.
- **Hypothesis H8 (Rapid Host Capacity Contraction in Scenario 3)**: **NOT TESTED**. Scenario 3 is omitted; no empirical claims are made regarding H8 in this evaluation.
- **Omitted Arms**:
  - `saha_v1_frozen` negative control arm (20 runs across S1-S4) is omitted.
  - Pre-declared ablation suite (25 runs on S1) is omitted.
  - Scenario 2 (Burst) and Scenario 3 (Capacity Cut) are omitted.
- **Rationale for Scope Reduction**: Time, complexity, and operational risk. Running 105 runs requires ~4 hours on live EC2; the reduced 30-run suite executes in ~62.5 minutes (88-minute watchdog buffer), minimizing cloud costs, watchdog risk, and providing rapid detached validation of primary allocator claims.

#### 4. Reduced 30-Run Preset Composition
- Preset: `--preset reduced30`
- Duration: 120 seconds per run
- Seeds: `42`, `43`, `44`, `45`, `46` ($n = 5$ per cell)
- Cells:
  1. `scenario_1_baseline` × {`saha`, `adaptive`, `drf`} (15 runs)
  2. `scenario_over_declaration` × {`saha`, `adaptive`, `proportional`} (15 runs)
- Total Runs: **30 runs**
- Watchdog Cap: $30 \times (120\text{ s} + 5.0\text{ s}) = 3750\text{ s} (62.50\text{ m}) + 25\text{ m} = 87.50\text{ m} \to \mathbf{88\text{ minutes}}$.

#### 5. Scenario Event Timestamps (Item 1.3 Audit)
- At 30s:
  - `scenario_1_baseline`: No events (steady-state).
  - `scenario_2_burst`: $t=10\text{ s}$, $t=20\text{ s}$.
  - `scenario_3_capacity_cut`: $t=15\text{ s}$.
  - `scenario_4_churn`: $t=10\text{ s}$, $t=20\text{ s}$.
  - `scenario_over_declaration`: No events (steady-state).
- At 120s:
  - `scenario_1_baseline`: No events (steady-state).
  - `scenario_2_burst`: $t=70\text{ s}$, $t=95\text{ s}$ (both $\ge W = 60\text{ s}$, post cold-start).
  - `scenario_3_capacity_cut`: $t=70\text{ s}$ ($\ge W = 60\text{ s}$, post cold-start).
  - `scenario_4_churn`: $t=70\text{ s}$, $t=95\text{ s}$ ($\ge W = 60\text{ s}$, post cold-start).
  - `scenario_over_declaration`: No events (steady-state).
- **Preset Audit Outcome**: The two scenarios in the `reduced30` preset (`scenario_1_baseline` and `scenario_over_declaration`) contain zero timed perturbation events; both evaluate steady-state multi-tenant allocation and quota reclamation. All other scenarios (S2, S3, S4) have been rescaled to occur strictly after cold start ($t \ge 60\text{ s}$).



