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



