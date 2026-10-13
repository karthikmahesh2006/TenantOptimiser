# Faculty Demonstration Guide & Run Script

## 1. Executive Summary & Demo Objectives
This guide specifies the exact protocol for demonstrating the **Adaptive Multi-Tenant Cloud Resource Allocation Platform** to university faculty. The demonstration showcases real containers executing on an AWS EC2 instance, dynamic cgroup enforcement, and comparative empirical analysis across four resource allocation policies:
1. **First-Come, First-Served (FCFS)**
2. **Weighted Proportional with Water-Filling (WP)**
3. **Dominant Resource Fairness (DRF)**
4. **Adaptive Convex Optimizer (Proposed Method)**

---

## 2. Pre-Flight Checklist (T-15 Minutes)

- [ ] **AWS Host Operational**: EC2 instance state is `running` and status checks are `2/2 OK`.
  ```bash
  aws ec2 describe-instance-status --instance-ids "$IID" --query 'InstanceStatuses[0].InstanceStatus.Status'
  ```
- [ ] **System Services Healthy**: Confirm Docker, `dockerproxy`, and `alloc-api.service` are active.
  ```bash
  aws ssm send-command --instance-ids "$IID" --document-name AWS-RunShellScript \
    --parameters 'commands=["systemctl is-active alloc-api docker && docker ps --format {{.Names}}"]'
  ```
- [ ] **Orphan Sweep Clean**: Verify zero leftover tenant containers from previous tests.
  ```bash
  aws ssm send-command --instance-ids "$IID" --document-name AWS-RunShellScript \
    --parameters 'commands=["docker ps -q --filter label=project=alloc | wc -l"]'
  ```
- [ ] **Lock Available**: Verify no active experiment is running (`experiment_locks` table empty).
- [ ] **SSM Port-Forwarding Tunnel Active**:
  ```bash
  aws ssm start-session --target "$IID" \
    --document-name AWS-StartPortForwardingSession \
    --parameters '{"portNumber":["8000"],"localPortNumber":["8000"]}'
  ```
- [ ] **Web Browser Verified**: Navigate to `http://localhost:8000/api/v1/status` and verify JSON response with git commit SHA and system stats.
- [ ] **Admin Credentials Tested**: Successfully logged into the web console.
- [ ] **Fallback Bundle Ready**: `data/fallback_recorded_run.json` downloaded locally in case of AWS service disruption.

---

## 3. Minute-by-Minute Faculty Demo Script (15 Minutes Total)

### Minute 0–3: Architectural Context & Scientific Integrity
- **Talking Points**:
  - Point to the **System Overview** page.
  - Highlight Rule A1: Every displayed metric has a strict provenance badge:
    - `MEASURED`: Physical hardware telemetry from cgroup v2 (`cpu.stat`, `memory.current`) and procfs.
    - `CALCULATED`: Rigorous statistical aggregations (Jain's index, bootstrap 95% confidence intervals).
    - `CONFIGURED`: Declared tenant demands and pool limits.
    - `SIMULATED`: Strictly restricted to offline unit tests (never shown as live host data).
  - Highlight Zero-Inbound Host Isolation: The host security group accepts **zero inbound traffic**; demo data flows through AWS SSM websockets.

### Minute 3–6: Baseline Policies (FCFS, WP, DRF)
- **Talking Points**:
  - Explain why static cloud allocation fails under heterogeneous multi-tenant load.
  - **FCFS**: High starvation risk for later arriving tenants; guarantees ignored if capacity is depleted.
  - **Weighted Proportional**: Independent per-resource water-filling ignores cross-resource bottleneck dependencies.
  - **DRF**: Balances dominant shares equitably, but is static and oblivious to actual runtime usage and memory OOM hazards.

### Minute 6–10: The Proposed Adaptive Optimizer
- **Talking Points**:
  - Introduce the 4 stages of the Adaptive optimizer:
    1. **Dynamic Demand Estimation**: Fast-up escalation upon cgroup throttling or queue backpressure; slow-down EWMA smoothing.
    2. **Convex Formulation**: Maximizing weighted log-utility with smooth Pseudo-Huber churn penalty solved in $< 50\text{ ms}$ via SLSQP.
    3. **Surplus Harvesting**: Harvesting idle CPU (never memory) for throttled tenants.
    4. **Stability Filtering**: Hysteresis, deadbands, dwell times, and the **Memory Safety Margin** ($+64\text{ MB}$) preventing fatal OOM kills.

### Minute 10–13: Live Execution Demonstration
- Click **Start Demo Mode** (`POST /demo/start`):
  - 3 tenants: Tenant 1 (CPU-heavy SHA256), Tenant 2 (Memory-heavy bytearray), Tenant 3 (Mixed).
  - Open the **Live Console**:
    - Observe the real-time Server-Sent Events stream updating CPU quotas and memory limits.
    - Watch dynamic response to an injected mid-run workload burst.
    - Notice that Adaptive dynamically reassigns CPU quota from the memory-heavy tenant to the throttled CPU worker.

### Minute 13–15: Comparison View & Honest Accounting
- Navigate to the **Comparison** page:
  - Review side-by-side metric cards with 95% Bootstrap Confidence Intervals ($N=2000$).
  - **Honest Accounting**: Point out where Adaptive does **not** win:
    - Under steady, non-bursty traffic, FCFS and DRF have near-zero churn, whereas Adaptive incurs a minor optimization overhead ($< 15\text{ ms}$).
    - When all tenants are purely compute-bound with uniform profiles, DRF matches Adaptive fairness without parameter tuning.

---

## 4. Emergency Failure Playbook

### Scenario A: AWS SSM Disconnects
- **Action**: Rerun the port-forwarding command:
  ```bash
  aws ssm start-session --target "$IID" \
    --document-name AWS-StartPortForwardingSession \
    --parameters '{"portNumber":["8000"],"localPortNumber":["8000"]}'
  ```
- If the EC2 host was stopped or restarted, wait 30 seconds; systemd automatically recovers all services and sweeps orphaned containers.

### Scenario B: Cloud Network Outage / AWS Latency
- **Action**: Switch to **Recorded Fallback Mode**:
  1. Click **Load Recorded Fallback Data** in the UI navigation.
  2. Or run: `python scripts/import_fallback.py data/recorded_runs_c7iflex.json`
  3. The persistent warning banner will display:
     `RECORDED REAL RESULTS (Recorded on AWS c7i-flex.large, Account: 481374029754)`
  4. Proceed with presenting identical real recorded traces without live cloud execution.
