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
