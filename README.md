# Adaptive Multi-Tenant Cloud Resource Allocation Platform

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![Architecture: cgroup v2](https://img.shields.io/badge/cgroup-v2-green.svg)](https://docs.kernel.org/admin-guide/cgroup-v2.html)
[![Security: 0-Inbound SSM](https://img.shields.io/badge/security-0--inbound--SSM-orange.svg)](docs/SECURITY.md)

An empirical research platform comparing cloud resource allocation algorithms on real Docker containers executing on an AWS EC2 host under cgroup v2. The platform measures physical consumption, throttling, latency, and goodput, and provides an interactive web UI with strict provenance tracking.

---

## 1. Features & Algorithms Compared

- **First-Come, First-Served (FCFS)**: Sequential greedy baseline respecting arrival order.
- **Weighted Proportional (WP)**: Multi-resource water-filling with proportional sharing.
- **Dominant Resource Fairness (DRF)**: Continuous progressive filling equalizing weighted dominant shares.
- **Adaptive Convex Optimizer (Proposed)**:
  - Online utility maximization solved via SLSQP ($< 50\text{ ms}$).
  - Dynamic demand estimation with fast-up escalation under contention.
  - Idle CPU surplus harvesting.
  - Stability filtering with hysteresis, deadband, dwell times, and **Memory Safety Margins** preventing container OOM kills.
- **Real Enforcement**: Real CFS CPU quotas (`cpu.cfs_quota_us`) and memory limits (`memory.max`) applied via container updates and verified through cgroup v2 readback.
- **Strict Data Provenance**: Every metric carries a visible badge: `MEASURED`, `CALCULATED`, `CONFIGURED`, or `SIMULATED`.
- **Zero-Inbound Security**: The host EC2 instance security group accepts zero inbound network traffic. Access is mediated exclusively over encrypted AWS SSM Session Manager port forwarding.

---

## 2. Technology Stack

- **Runtime**: Python 3.11+
- **Host OS**: Amazon Linux 2023 (cgroup v2, systemd)
- **Container Isolation**: Docker with `tecnativa/docker-socket-proxy` restricted API
- **Web & API**: FastAPI, Uvicorn, Jinja2, HTMX, local Chart.js (no CDN dependency)
- **Database**: SQLite 3 with Write-Ahead Logging (`WAL`), SQLAlchemy 2.0
- **Optimization & Math**: NumPy, SciPy (`SLSQP`)
- **Testing**: pytest, pytest-asyncio, Hypothesis (property-based testing), Ruff

---

## 3. Quick Start (Local Development)

### Setup Virtual Environment
```bash
python3.11 -m venv .venv
# On Linux/macOS:
source .venv/bin/activate
# On Windows:
.venv\Scripts\Activate.ps1

pip install -e ".[dev]"
```

### Running Test Suite
```bash
# Run unit tests and property tests
pytest tests/unit tests/property -v

# Run lint checks
ruff check .
```

---

## 4. Documentation Directory

- **[System Architecture](docs/ARCHITECTURE.md)**: Detailed component breakdown and Mermaid architecture diagrams.
- **[Architectural Decisions (ADR)](docs/DECISIONS.md)**: Rationale for instance types, cgroup drivers, security constraints, and solver designs.
- **[Security Architecture](docs/SECURITY.md)**: Threat model, zero-inbound policy, and Docker socket proxy controls.
- **[Methodology & Metrics](docs/METHODOLOGY.md)**: Mathematical formulations of fairness, satisfaction, utilization, and invalidation rules.
- **[AWS Operations Runbook](docs/AWS_RUNBOOK.md)**: Step-by-step AWS CLI deployment, configuration, budgeting, and clean teardown.
- **[Faculty Demonstration Guide](docs/DEMO_GUIDE.md)**: Minute-by-minute demo script, pre-flight checklist, and emergency fallback playbook.

---

## 5. Live Demonstration & Verification Gates

The project follows strict verification gates:
- **Gate 1**: Core allocators (FCFS, WP, DRF, Adaptive), metrics, simulator, known-answer tests, and solve time $< 50\text{ ms}$.
- **Gate 2**: Worker image, Docker enforcement, cgroup v2 verification, telemetry sampler, open-loop load generator, and host calibration.
- **Gate 3**: Orchestrator state machine, mutual exclusion lock, API endpoints, and authentication.
- **Gate 4**: Web UI, live SSE console, side-by-side comparison with bootstrap confidence intervals.
- **Gate 5**: Clean AWS automated deployment via SSM with verified zero inbound exposure.
