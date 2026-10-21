# Research & Evaluation Methodology

## 1. Problem Formulation & Multi-Tenant Model
The Adaptive Multi-Tenant Cloud Resource Allocation Platform compares resource allocation algorithms on a shared compute host partitioned among $N$ competing tenants.
Each tenant $i \in \{1, \dots, N\}$ possesses:
- Declared demand vector: $D_i = (D_{i, \text{cpu}}, D_{i, \text{mem}}) \in \mathbb{R}_+^2$
- Guaranteed minimum requirement: $M_i = (M_{i, \text{cpu}}, M_{i, \text{mem}}) \in \mathbb{R}_+^2$ with $M_i \le D_i$
- Weight: $w_i > 0$
- Priority: $p_i \in \mathbb{Z}_+$
- Arrival sequence: $a_i \in \mathbb{N}$

The shared host pool has capacity $C = (C_{\text{cpu}}, C_{\text{mem}})$.
The allocator produces an allocation vector $x_i = (x_{i, \text{cpu}}, x_{i, \text{mem}})$ satisfying:
$$\sum_{i=1}^N x_{i, r} \le C_r \quad \forall r \in \{\text{cpu}, \text{mem}\}$$
$$x_{i, r} \le D_{i, r} \quad \forall i, r$$

## 2. Allocation Policies Compared

### 2.1 First-Come, First-Served (FCFS)
Tenants are sorted by arrival sequence $a_i$. Each tenant receives $\min(D_{i, r}, C_r - \sum_{j < i} x_{j, r})$ in order. Minimum guarantees are honoured only if sufficient capacity remains when the tenant's turn arrives.

### 2.2 Weighted Proportional with Water-Filling (WP)
Performs independent water-filling per resource:
1. Admitted minimums $M_{i, r}$ are allocated first.
2. If minimums exceed capacity, they are scaled down proportionally: $x_{i, r} = M_{i, r} \cdot (C_r / \sum M_{j, r})$.
3. Unallocated capacity $R_r = C_r - \sum M_{i, r}$ is distributed in proportion to $w_i \cdot (D_{i, r} - M_{i, r})$. Tenants reaching $D_{i, r}$ are capped, and remaining capacity is iteratively water-filled across uncapped tenants until fully absorbed.

### 2.3 Dominant Resource Fairness (DRF)
Generalizes max-min fairness to multi-resource environments (Ghodsi et al., 2011).
- Dominant share of tenant $i$:
  $$d_i = \max_{r \in \mathcal{R}} \frac{x_{i, r}}{C_r}$$
- Weighted dominant share:
  $$\widetilde{d}_i = \frac{d_i}{w_i}$$
Progressive filling continuously raises the lowest $\widetilde{d}_i$ by scaling task satisfaction $s_i \in [s_{\min, i}, 1.0]$. A tenant freezes when $s_i = 1.0$ or when a consumed resource saturates.

### 2.4 Proposed Adaptive Convex Optimizer
The adaptive policy balances dynamic efficiency with operational stability:
1. **Dynamic Demand Estimation**:
   $$\hat{d}_{i, r} = \min\left( D_{i, r}, \max(M_{i, r}, (1 + h) \cdot \text{EWMA}(u_{i, r})) \right)$$
   If throttling exceeds threshold ($\ge 5\%$, `config.throttle_threshold = 0.05`) or worker queue backlog grows, the estimate escalates aggressively toward declared demand (fast up, slow down).
2. **Convex Formulation**:
   Maximizes log-utility penalized by churn using SLSQP:
   $$\max_{s} \sum_{i=1}^N w_i \ln(s_i) - \lambda \sum_{i=1}^N \sum_{r} \frac{\phi_{\delta}(s_i \hat{d}_{i, r} - x_{i, r}^{(\text{prev})})}{C_r}$$
   Subject to $\sum_i s_i \hat{d}_{i, r} \le C_r$ and $s_{\min, i} \le s_i \le 1.0$.
   Where $\phi_{\delta}(z) = \delta^2 (\sqrt{1 + (z/\delta)^2} - 1)$ is the smooth Pseudo-Huber loss.
3. **Surplus Harvesting**:
   Idle CPU capacity (never memory) is redistributed to throttled/backlogged tenants in proportion to weights.
4. **Stability Filtering & Single-Source Constants**:
   - Updates suppressed if relative change is within deadband ($< 5\%$, `config.deadband = 0.05`) and minimum dwell time ($3\text{ s}$, `config.min_dwell_s = 3.0`) has not elapsed.
   - Contraction rate-limited to at most $20\%$ (`config.max_shrink_per_epoch = 0.20`) shrink per epoch.
   - Memory safety: $x_{i, \text{mem}} \ge u_{i, \text{mem}} + 64\text{ MB}$ (`config.memory_safety_mb = 64.0`) protects containers from fatal OOM kills.
   - Allocation control loop period is $1.0\text{ s}$ (`config.epoch_interval_s = 1.0`).
   - Tenant pool budget is restricted to at most $75\%$ (`config.default_host_cpu_ratio = 0.75`, `config.default_host_mem_ratio = 0.75`) of host vCPU and RAM, reserving $25\%$ outside the pool for the host OS, API server, load generator, and cgroup sampler.

## 3. Metrics & Provenance Definitions
 
| Metric | Provenance | Mathematical Definition | Interpretation |
|---|---|---|---|
| On-Time Ratio (Headline) | `MEASURED` | $\frac{\text{Completed within Deadline}}{\text{Total Offered Requests}}$ | Headline efficiency metric. Requests rejected (429) or censored (timeout) count as failures. |
| Hardware Throttle Ratio | `MEASURED` | $\frac{\text{cgroup throttled\_usec}}{\text{throttled\_usec} + \text{usage\_usec}}$ | Fraction of CPU active work time tenant was constrained by CFS. |
| Deadline Miss Rate | `MEASURED` | $\frac{\text{Completed with Latency} > \text{Deadline}}{\text{Total Completed Requests}}$ | Application-level SLA miss rate for serviced requests. |
| Jain's Satisfaction Index | `CALCULATED` | $J(s_1/w_1, \dots, s_N/w_N) = \frac{(\sum s_i/w_i)^2}{N \sum (s_i/w_i)^2}$ | Evaluates equal satisfaction per unit weight against contractual declared demand. |
| Mean Satisfaction | `CALCULATED` | $\frac{1}{N} \sum_{i=1}^N \min_r (x_{i, r} / D_{i, r})$ | Fractional contractual demand fulfillment. Denominator is DECLARED demand. |
| Consumed Utilization | `MEASURED` | $\sum_i u_{i, r} / C_r$ | Real physical usage from cgroup v2. |
| Allocated Utilization | `CALCULATED` | $\sum_i x_{i, r} / C_r$ | Quota reserved by allocator relative to pool capacity. |
| Idle Reserved Capacity | `CALCULATED` | $\sum_i \max(0, x_{i, r} - u_{i, r})$ | Quota allocated but unconsumed by container. |
| Allocation Churn | `CALCULATED` | $\frac{1}{K-1} \sum_k \sum_{i, r} \frac{\|x_{i, r}^{(k)} - x_{i, r}^{(k-1)}\|}{C_r}$ | Control loop volatility between consecutive epochs. |
| Solve Time | `MEASURED` | Absolute solver duration (ms) and fraction of epoch (%) | Algorithmic execution latency per epoch. |
| Throttled-While-Unallocated | `MEASURED` | Fraction of epochs where a tenant is throttled while pool allocation $< 95\%$ | Hardware inefficiency metric: spare capacity left idle while containers throttle. |
| Time-to-Convergence | `MEASURED` | Epochs until allocation delta stays within deadband after start or event | Control stabilization latency. |

## 4. Run Invalidation & Experimental Controls
To ensure repeatable scientific rigor:
- **Host Reservation Guard**: Non-tenant CPU (API, loadgen, telemetry sampler) must remain $\le 25\%$ of host capacity ($0.50\text{ vCPU}$). Runs exceeding this threshold are flagged `INVALID (non-tenant overhead exceeded host reservation)`.
- **Steal Monitoring**: If host CPU steal percentage exceeds $5.0\%$ during an epoch, the run is flagged `INVALID (excessive noisy neighbor steal)`.
- **Unexpected OOM**: Container terminations with exit code 137 are recorded and invalidate the run.
- **Randomized Policy Ordering**: Repeat blocks shuffle allocator execution order to prevent thermal throttling bias.

## 5. Pre-Stated Scientific Hypotheses

The experimental evaluation tests four formal hypotheses:

- **Hypothesis H1 (Over-Declaration & Usage-Aware Efficiency)**:
  Under over-declared demand (static demand overestimating true utilization), usage-aware dynamic allocation (Adaptive) reclaims unutilized capacity from idle tenants and redistributes it to constrained tenants, achieving significantly higher on-time completion ($\text{on\_time\_ratio}$), higher throughput, and lower p95 latency compared to static demand-based baselines (FCFS, WP, DRF).
  *Favours Adaptive.*

- **Hypothesis H2 (Workload Burst Adaptation)**:
  During unexpected workload bursts, dynamic estimation with fast-up escalation and surplus harvesting tracks transient demand spikes, reducing throttle duration and deadline misses compared to static allocations.

- **Hypothesis H3 (Honest Declaration Neutrality or Degradation)**:
  When tenants declare honest demand matching their true usage under heavy load, static baselines (DRF, WP) operate near their optimal theoretical allocations. Under these conditions, Adaptive dynamic estimation provides no surplus to reclaim, and the overhead of dynamic tracking (solve latency, stability damping, contraction caps) yields neutral or slightly worse goodput/latency than static baselines.
  *Neutral or favours baselines. Reported with equal prominence.*

- **Hypothesis H4 (Bounded Stability & Low Overhead)**:
  Dynamic limit adjustments in Adaptive can be bounded by stability filtering (deadband, min dwell, contraction cap) such that churn remains low, solve time stays well below $1\%$ of the control epoch ($< 10\text{ ms}$ on a $1000\text{ ms}$ epoch), and container thrashing or memory OOMs are completely avoided.

## 6. Predefined Evaluation Scenarios

| Scenario Identifier | Focus & Hypothesis Tested | Tenant Configurations | Expected Outcome |
|---|---|---|---|
| `scenario_1_baseline` | Heterogeneous multi-resource baseline | 3 tenants: CPU-heavy ($1.2\text{c}$), Memory-heavy ($2048\text{M}$), Mixed ($0.8\text{c}$) | DRF balances dominant shares; Adaptive tracks physical usage. |
| `scenario_2_burst` | Hypothesis H2: Flash crowd burst | CPU worker bursts from $1.2\text{c}$ to $1.8\text{c}$ at $t=10\text{s}$, returns at $t=20\text{s}$ | Adaptive tracks burst via fast-up; baselines remain static. |
| `scenario_3_capacity_cut` | Robustness under host degradation | Physical host pool cut from $1.5\text{c}$ to $0.9\text{c}$ at $t=15\text{s}$ | All policies downscale; tests minimum guarantee preservation. |
| `scenario_4_churn` | Dynamic tenant lifecycle | Mixed service arrives at $t=10\text{s}$, departs at $t=20\text{s}$ | Tests admission control, churn penalty, and deallocation speed. |
| `scenario_over_declaration` | Hypothesis H1: Asymmetric over-declaration | Tenants A and B declare $1.2\text{c}$ each but use $\approx 0.2\text{c}$; Tenant C genuinely needs $\approx 1.0\text{c}$ | Baselines waste $\approx 0.66\text{c}$ idle on A & B while C throttles. Adaptive reclaims idle capacity for C. |
| `scenario_honest_declaration` | Hypothesis H3: Saturated honest load | Tenants A, B, C each declare $0.5\text{c}$ and use $\approx 0.5\text{c}$ (total = $1.5\text{c} = 100\%$ pool) | Baselines allocate optimally ($0.5\text{c}$ each). Adaptive has no surplus to harvest; neutral or slightly worse. |

