# Architecture & Engineering Decision Records (ADR)

## ADR-001: Local Development & Python 3.11 Runtime
- **Context:** Project requires Python 3.11+, pydantic v2, scipy, and modern async stack. The developer machine runs Windows with an initial Python 2.7 legacy binary on path.
- **Decision:** Installed Python 3.11.9 locally in user scope (`%LOCALAPPDATA%\Programs\Python\Python311`) without requiring admin elevation, created dedicated virtual environment (`.venv`), and configured dependencies matching the Amazon Linux 2023 host deployment.
- **Consequences:** Clean separation between development toolchain and system paths.

## ADR-002: AWS EC2 Instance Selection
- **Context:** Section F1 specifies: "prefer a 2-vCPU, 4 GB non-burstable flex type (for example c7i-flex.large or m7i-flex.large) if it appears in the list and in this region; otherwise t3.small".
- **Decision:** Queried AWS Free Tier eligible instances in region `ap-south-1`. Confirmed `c7i-flex.large` (2 vCPU, 4096 MiB RAM, dedicated/flex compute) is offering-available and eligible. Selected `c7i-flex.large` over `t3.small` to eliminate burstable CPU credit throttling noise and provide consistent hardware benchmarks.
- **Consequences:** Avoids CPU credit starvation during multi-minute benchmark runs; steal percentage and credit telemetry are still maintained as safety guards.

## ADR-003: Priority and Tie-Breaking Order
- **Context:** Feasibility and admission control require unambiguous tie-breaking.
- **Decision:** Priority is represented as integer where higher value = higher precedence. Guarantee admission breaks ties strictly in the order:
  1. `priority` (descending)
  2. `weight` (descending)
  3. `arrival_seq` (ascending, earliest arrival first)
- **Consequences:** Deterministic, predictable admission behavior.

## ADR-004: Strict Provenance Model
- **Context:** Rule A1 states: "No fabricated data. Every number in the UI and reports comes from real measurements or is labelled CALCULATED, CONFIGURED or SIMULATED."
- **Decision:** Implemented typed provenance enum across all telemetry samples, derived run metrics, and simulator outputs:
  - `MEASURED`: Raw telemetry read from cgroup v2 / procfs / HTTP loadgen latencies.
  - `CALCULATED`: Statistical derivations (Jain's index, percentiles, bootstrap CI, utils).
  - `CONFIGURED`: Inputs from pool limits, tenant demands, quotas, scenarios.
  - `SIMULATED`: Synthetic traces produced solely by `allocator/simulator.py`.
- **Consequences:** Zero risk of synthetic data masquerading as real benchmarks.

## ADR-005: Docker Socket Security & Host Isolation
- **Context:** The web application needs to adjust container cgroups dynamically but must not be granted unrestricted root Docker privileges.
- **Decision:** Placed `tecnativa/docker-socket-proxy` between the orchestrator and dockerd listening exclusively on `127.0.0.1:2375`. Disabled exec, volumes, and arbitrary image pulling. Restricted network access for tenant containers to an isolated bridge network (`alloc-tenants`) with internal routing only (no public egress).
- **Consequences:** Fully satisfies least-privilege security posture.

## ADR-006: Execution Modes & Network Ingress Security
- **Context:** User instruction updated requirement: "public IPv4 allowed, security group has zero inbound rules, access only via SSM port forwarding." Local development without Docker Desktop requires simulation, while EC2 deployments must execute live container workloads without silent fallbacks.
- **Decision:** Public IPv4 is assigned to the EC2 instance to permit outbound package downloads without requiring an expensive NAT Gateway. The Security Group enforces ZERO inbound rules (no open inbound ports; `IpPermissions: []`). Inbound access to the application web console (port 8000) is conducted exclusively through AWS Systems Manager (SSM) Session Manager port forwarding. Execution mode is controlled by `ALLOC_MODE=live|sim|auto`. On the EC2 instance, `ALLOC_MODE=live` is strictly enforced: if Docker or cgroup v2 is unavailable, runs fail immediately with an explicit error. Simulated runs display a persistent banner.
- **Consequences:** Zero internet ingress exposure, zero NAT Gateway/ALB charges, and complete data provenance transparency.

## ADR-007: Asynchronous Orchestration and Event-Driven Live UI
- **Context:** Benchmark experiments run across multiple repeats and epochs, while web clients observe real-time telemetry graphs and allocation changes without full-page reloads.
- **Decision:** Background experiment runs are managed as asynchronous coroutines (`execute_experiment_async`) with non-blocking cooperative pacing (`config.epoch_interval_s`). Route handlers triggering experiments are declared as `async def` to ensure event loop attachment. Telemetry is streamed to HTMX/vanilla JavaScript clients via Server-Sent Events (`/api/v1/events`), while mutual exclusion is enforced by a single-experiment database lock.
- **Consequences:** Responsive web UI, instant live chart rendering, and complete prevention of concurrent experiment resource contamination.

## ADR-008: Packaging Asset Discovery & Optimizer Guarantee Admission
- **Context:** Deployments packaged into standard wheel distributions (`pip install .`) excluded non-code assets by default, triggering missing directory errors on fresh remote environments. Additionally, edge-case tenant configurations generated randomized minimum guarantees exceeding physical pool capacity.
- **Decision:**
  1. Configured `tool.setuptools.package-data` and `MANIFEST.in` to recursively package HTML templates and static assets into the wheel distribution, with automatic fallback path discovery in `main.py` and `routes_pages.py`.
  2. In `AdaptiveAllocator.solve_convex`, lower bound constraints (`s_min`) are filtered strictly by `admitted_ids` from `check_guarantee_feasibility`. This guarantees that lower bounds never exceed pool capacity, ensuring the SLSQP optimization problem remains strictly feasible. A final capacity clamp in `stability_filter` guarantees the physical capacity invariant is never violated regardless of hysteresis or dwell states.
- **Consequences:** Deterministic zero-configuration deployment across any fresh Linux host, and mathematical stability across all extreme or infeasible guarantee configurations.

## ADR-009: Pre-Stage 4 Parameter Freeze (`params-frozen`)
- **Context:** Rule A5 requires freezing all tunables and scenario parameters before beginning Stage 4 replication benchmarks to prevent ex-post retuning.
- **Decision:** All hyperparameters, thresholds, pool budgets, and calibrated offered rates are strictly frozen as of commit tag `params-frozen`:
  - **Host & Pool Budget:** Host = 2.0 vCPU, 4096 MiB RAM. Pool budget = 75% (`default_host_cpu_ratio = 0.75`, `default_host_mem_ratio = 0.75`), reserving 25% (0.50 vCPU, 1024 MiB RAM) strictly outside the pool for OS, API server, load generator, and sampler.
  - **Optimizer Hyperparameters:**
    - `lambda_churn = 0.10` (Pseudo-Huber churn penalty weight in SLSQP utility objective)
    - `headroom_h = 0.20` (20% demand estimation headroom above EWMA)
    - `ewma_alpha = 0.30` (smoothing factor for usage observation)
    - `epoch_interval_s = 1.0` (control loop period)
  - **Stability & Safety Constants:**
    - `memory_safety_mb = 64.0` (minimum headroom above physical usage to prevent OOM)
    - `throttle_threshold = 0.05` (5% CFS throttling threshold triggering fast-up escalation)
    - `max_shrink_per_epoch = 0.20` (maximum 20% limit contraction per epoch)
    - `deadband = 0.05` (5% of pool capacity change threshold for update suppression)
    - `min_dwell_s = 3.0` (minimum dwell duration before voluntary limit reduction)
  - **Run Invalidation Guard Thresholds:**
    - Max host CPU steal: `max_cpu_steal_pct = 5.0%`
    - Max non-tenant CPU: `0.50 vCPU` (25% host reservation)
    - Max unexpected OOM kills: `0`
  - **Calibrated Scenario Offered Rates (from hardware calibration seeds 701-704):**
    - `scenario_1_baseline`: `tenant_cpu` = 66.8 req/s, `tenant_mix` = 10.6 req/s, `tenant_mem` = 2.7 req/s (Total demand = 1.45 cores = 96.7% pool)
    - `scenario_over_declaration`: `tenant_a_over` = 11.7 req/s, `tenant_b_over` = 11.7 req/s, `tenant_c_honest` = 55.0 req/s (Total demand = 1.06-1.20 cores)
    - `scenario_honest_declaration`: `tenant_a_honest` = 11.7 req/s, `tenant_b_honest` = 11.7 req/s, `tenant_c_honest` = 11.7 req/s (Total demand = 1.50 cores = 100.0% pool)
- **Consequences:** Any subsequent modification to these parameters must be documented with explicit justification in ADR records, retaining earlier benchmark results without overwriting.

## ADR-010: Bounded Diagnosis of Scenario 1 (Adaptive On-Time 7.1% vs DRF 100%) and Calibration Justification
- **Context:** Under `scenario_1_baseline` on EC2 hardware, Adaptive achieved an on-time completion ratio of 7.1% (8.2% on diagnostic re-test) while DRF achieved 100.0%. A bounded diagnostic was ordered to identify whether this discrepancy constitutes an implementation artifact/bug or a fundamental algorithmic limitation, and to verify the commit provenance of the over-declaration specification.
- **Diagnostic Findings & Mechanism Evidence:**
  1. **Censored-Demand Feedback Loop (Ratchet):**
     - Total offered load is 1.45 cores (96.7% of the 1.50-core pool).
     - Natural Poisson arrival burstiness induces transient CFS throttling across all three tenant containers.
     - The demand estimator's fast-up escalation (`est = est + 0.5 * (dem_val - est)`) interprets burst throttling as chronic capacity deficit, rapidly ratcheting all three demand estimates toward their declared demand caps (`tenant_cpu`: 1.20c cap $\to$ 1.05c estimate; `tenant_mix`: 0.80c cap $\to$ 0.75c estimate; `tenant_mem`: 0.40c cap $\to$ 0.40c estimate).
     - Summed demand estimates reach 2.19 cores, which is 146% of the 1.50-core pool capacity.
  2. **SLSQP Log-Utility Collapse to Static Weighted Sharing:**
     - When the capacity constraint $\sum_i s_i \hat{d}_i \le C$ binds, logarithmic utility maximization $\max \sum_i w_i \ln(s_i)$ mathematically collapses to allocating resources strictly in proportion to tenant weights:
       $$\text{alloc}_i = \frac{w_i}{\sum_k w_k} C$$
     - With weights $w_{\text{cpu}}=2.0, w_{\text{mix}}=1.5, w_{\text{mem}}=1.0$ (total weight = 4.5), the solver assigns:
       - `tenant_cpu`: $(2.0 / 4.5) \times 1.50 = 0.667\text{ cores}$
       - `tenant_mix`: $(1.5 / 4.5) \times 1.50 = 0.500\text{ cores}$
       - `tenant_mem`: $(1.0 / 4.5) \times 1.50 = 0.333\text{ cores}$
  3. **Persistent Starvation & Ratchet Lock:**
     - To service 66.8 req/s with a service time of 0.0120 s/req, `tenant_cpu` requires 0.80 cores.
     - Because it is allocated only 0.667 cores, it experiences permanent 17% compute starvation. It is continuously throttled (20% to 39% CFS throttle ratio each epoch), which in turn keeps its demand estimate ratcheted high, locking the solver into the 0.667-core allocation.
     - Meanwhile, `tenant_mem` is allocated 0.34 cores while only needing 0.20 cores. However, surplus harvesting cannot reallocate this idle quota because the solver already allocated 100% of the pool ($0.667 + 0.500 + 0.333 = 1.500\text{c}$), leaving zero unallocated surplus (`surplus_cpu = 0.0`).
  4. **Why DRF Succeeds (100% On-Time):**
     - DRF operates in dominant share space. `tenant_mem`'s dominant resource request is memory (2048 MiB / 3072 MiB = 66.7%).
     - By equalizing dominant shares, DRF grants `tenant_mem` memory but restricts its CPU allocation to its dominant ratio, naturally leaving $\approx 0.85\text{ cores}$ of CPU available for `tenant_cpu`.
- **Classification & Decision:**
  - This is a **genuine algorithmic limitation** of scalar 1-D log-utility with fast-up demand estimation under near-saturation conditions ($>95\%$ pool load).
  - No ex-post parameter tuning is permitted. This outcome is documented as a reported empirical limitation with equal prominence to Adaptive's wins.
- **Spec Commitment & Tenant C Rate Justification:**
  - `scenario_over_declaration` was committed in `b8071ed` before its first run.
  - In `scenario_over_declaration`, Tenant C declares 0.50c and requires 0.66c (55 req/s on calibration seed 701) rather than the 1.0c (83 req/s) mentioned in the original preliminary sketch. At 83 req/s, the client load generator process consumed $>0.55\text{ cores}$ on the 2-vCPU host, violating the $0.50\text{ core}$ host reservation guard and triggering automatic run invalidation. Calibrating Tenant C to 55 req/s (0.66c demand) guarantees the host reservation invariant holds while preserving the intended contention dynamic (0.66c true demand vs 0.50c static allocation).
- **Consequences:** All on-time ratio differences are reported in percentage points (`pp`). Memory capacity is formally acknowledged as non-binding in current test workloads. Formal ablations (`no_smoothing`, `no_estimation_declared`, `no_hysteresis`, `no_surplus`) are evaluated in Stage 4.




