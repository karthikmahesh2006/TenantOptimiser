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
   If throttling exceeds threshold ($\ge 5\%$) or worker queue backlog grows, the estimate escalates aggressively toward declared demand (fast up, slow down).
2. **Convex Formulation**:
   Maximizes log-utility penalized by churn using SLSQP:
   $$\max_{s} \sum_{i=1}^N w_i \ln(s_i) - \lambda \sum_{i=1}^N \sum_{r} \frac{\phi_{\delta}(s_i \hat{d}_{i, r} - x_{i, r}^{(\text{prev})})}{C_r}$$
   Subject to $\sum_i s_i \hat{d}_{i, r} \le C_r$ and $s_{\min, i} \le s_i \le 1.0$.
   Where $\phi_{\delta}(z) = \delta^2 (\sqrt{1 + (z/\delta)^2} - 1)$ is the smooth Pseudo-Huber loss.
3. **Surplus Harvesting**:
   Idle CPU capacity (never memory) is redistributed to throttled/backlogged tenants in proportion to weights.
4. **Stability Filtering**:
   - Updates suppressed if relative change is within deadband ($< 5\%$) and minimum dwell time ($3\text{ s}$) has not elapsed.
   - Contraction rate-limited to at most $20\%$ shrink per epoch.
   - Memory safety: $x_{i, \text{mem}} \ge u_{i, \text{mem}} + 64\text{ MB}$ protects containers from fatal OOM kills.

## 3. Metrics & Provenance Definitions

| Metric | Provenance | Mathematical Definition | Interpretation |
|---|---|---|---|
| Jain's Satisfaction Index (Primary) | `CALCULATED` | $J(s_1/w_1, \dots, s_N/w_N) = \frac{(\sum s_i/w_i)^2}{N \sum (s_i/w_i)^2}$ | Evaluates equal satisfaction per unit weight. |
| Jain's Dominant Share (Secondary) | `CALCULATED` | $J(\widetilde{d}_1, \dots, \widetilde{d}_N)$ | Evaluates classical DRF share equality. |
| Bottleneck Satisfaction | `CALCULATED` | $s_i = \min_{r: D_{i, r} > 0} (x_{i, r} / D_{i, r})$ | Fractional demand fulfillment. |
| Consumed Utilization | `MEASURED` | $\sum_i u_{i, r} / C_r$ | Real physical usage from cgroup v2. |
| Useful Goodput | `MEASURED` | Completed tasks within deadline / Total requests | Real workload efficiency. |
| Allocation Churn | `CALCULATED` | $\frac{1}{K-1} \sum_k \sum_{i, r} \frac{\|x_{i, r}^{(k)} - x_{i, r}^{(k-1)}\|}{C_r}$ | Control loop volatility. |

## 4. Run Invalidation & Experimental Controls
To ensure repeatable scientific rigor:
- **Steal Monitoring**: If host CPU steal percentage exceeds $5.0\%$ during an epoch, the run is flagged `INVALID (excessive noisy neighbor steal)`.
- **Burstable Credits**: On burstable instances, runs falling below 10 CPU credits are flagged invalid.
- **Unexpected OOM**: Container terminations with exit code 137 are recorded and invalidate the run.
- **Randomized Policy Ordering**: Repeat blocks shuffle allocator execution order to prevent thermal throttling bias.
