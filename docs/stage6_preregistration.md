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
  - **Total Session Runs**: $60 + 25 = 85$ live benchmark runs.
  - **Wall-clock Runtime**: 85 runs × ~35s (30s scenario duration + 5s container create/teardown overhead) = 2,975s (~49.6 minutes) active execution.
  - **Instance Watchdog & Buffer Margin**: 75 minutes hard watchdog (5m boot/SSM + 50m workload execution + 20m artifact export and verification buffer).
  - **Financial Budget**: 75 minutes on EC2 `c7i-flex.large` ($0.088/hr) = $\$0.110$ (well within research budget cap).

