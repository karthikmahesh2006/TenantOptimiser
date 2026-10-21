# Faculty Demonstration Runbook & Presentation Guide

## 1. Executive Summary & Demo Strategy

The live faculty demonstration presents the **Adaptive Multi-Tenant Cloud Resource Allocation Platform** deployed on AWS EC2 (`c7i-flex.large`, 2 vCPUs, 4 GiB RAM, Amazon Linux 2023, cgroup v2). 

> [!IMPORTANT]
> **Demo Strategy (2-Minute Live Run + Recorded Walkthrough)**:
> - **Live Execution (2 Minutes)**: Execute **ONLY** `scenario_over_declaration` live with three policies: **FCFS**, **Weighted Proportional (WP)**, and **Adaptive** (1 repeat, 30s each $\approx 1.5\text{ to } 2\text{ minutes}$). This showcases live real-time cgroup enforcement and dynamic quota reclamation.
> - **Recorded Results**: Show other scenarios (`scenario_honest_declaration`, `scenario_1_baseline`, ablations) using pre-recorded `MEASURED` data from real EC2 runs. Every recorded page clearly bears the amber banner: `"RECORDED real measurements from 2026-10-14, instance c7i-flex.large, not live"`.

---

## 2. Infrastructure Setup & Zero-Trust Access

### 2.1 Security & Networking
- **Zero Inbound Security Group Rules**: Port 22 (SSH) and Port 8000 are closed to the public Internet.
- **Access Protocol**: Access is exclusively mediated via AWS Systems Manager (SSM) Session Manager port forwarding.

### 2.2 Pre-Demo Port Forwarding Setup (Host Laptop)
Open a local terminal and establish the encrypted SSM tunnel:

```bash
# 1. Start SSM port forwarding tunnel
aws ssm start-session \
  --target i-011b23accbf304c75 \
  --document-name AWS-StartPortForwardingSession \
  --parameters '{"portNumber":["8000"],"localPortNumber":["8000"]}' \
  --region ap-south-1

# 2. Verify connection in a browser:
# Navigate to: http://127.0.0.1:8000
```

---

## 3. Minute-by-Minute Faculty Demo Script (10-Minute Presentation)

```mermaid
flowchart LR
    M1["Min 0-2: Overview & Host Pool"] --> M2["Min 2-4: Live Contention Run"]
    M2 --> M3["Min 4-6: Live Results & Reclamation"]
    M3 --> M4["Min 6-8: Regime Map & Honest Limits"]
    M4 --> M5["Min 8-10: Faculty Q&A"]
```

### Minute 0:00 – 2:00 | Architecture & Problem Formulation
- **Page**: `/` (Overview)
- **Talking Points**:
  - Point to the **Host Capacity vs Pool Budget** cards: Total host is 2 vCPUs, 4096 MiB RAM.
  - Explain the **75% Pool Invariant** ($1.50\text{ vCPU}$, $3072\text{ MiB}$): We strictly isolate tenant containers in a cgroup slice while reserving $25\%$ ($0.50\text{ vCPU}$) for the host kernel, API server, and telemetry samplers to prevent measurement skew.
  - State the core thesis: Cloud tenants routinely over-declare quotas to guarantee peak capacity, causing low utilization. Dynamic estimation allows harvesting unutilized quota while preserving performance isolation.

### Minute 2:00 – 4:00 | The Live Demonstration Run
- **Page**: `/experiment-config` $\to$ `/live-console`
- **Actions**:
  1. Select **Scenario**: `Scenario Over-Declaration: Unused Quota Reclamation`.
  2. Select **Policies**: Check `FCFS`, `Proportional`, and `Adaptive`.
  3. Set **Repeats**: `1`.
  4. Click **Start Experiment**.
  5. The UI automatically transitions to `/live-console`.
- **Talking Points**:
  - Show the live CPU allocation and usage charts updating epoch-by-epoch ($1\text{ s}$ intervals).
  - Point to Tenants A and B (over-declaring $1.2\text{c}$ but consuming only $0.2\text{c}$).
  - Point to Tenant C (genuinely requiring $0.66\text{c}$, $55\text{ rps}$).
  - Show Adaptive's EWMA estimator sensing idle headroom and surplus harvesting redistributing quota to Tenant C in real time.

### Minute 4:00 – 6:00 | Analyzing Live Results & Provenance
- **Page**: `/compare`
- **Talking Points**:
  - **Headline Metric (On-Time Ratio)**: Explain that requests completed within deadline divided by offered load is the headline metric. Rejections and timeouts count as failures.
  - Adaptive achieves $\approx 35.6\%$ on-time ratio vs Proportional $24.0\%$ (**+11.5 pp win**) and FCFS $9.8\%$ (**+25.7 pp win**).
  - Point to **Idle Reserved CPU**: Proportional reserves $0.66\text{c}$ idle while Tenant C throttles; Adaptive cuts idle reservation down to $0.46\text{c}$.
  - Point out **Metric Provenance Labels**: Show the explicit `MEASURED` tag on cgroup usage/throttling and the `CALCULATED` tag on satisfaction and Jain's index.

### Minute 6:00 – 8:00 | Operational Regime Map & Honest Reporting
- **Page**: `/regime-map`
- **Talking Points**:
  - *"We do not claim universal Pareto superiority. We have mapped the algorithm across three distinct operational regimes:"*
  - **Regime 1 (Over-Declaration)**: Adaptive wins by **+11.5 pp** through surplus reclamation.
  - **Regime 2 (Honest Declaration)**: Neutral goodput (**0.0 pp**), but Adaptive incurs a $-31.1\%$ contractual satisfaction deficit and $2.3\text{ ms}$ solve latency.
  - **Regime 3 (Near-Saturation Baseline)**: Adaptive loses (**-88.9 pp** vs DRF). Explain the mechanism frankly: at $96.7\%$ host saturation, burst throttling ratchets estimates to caps, collapsing SLSQP log-utility to static weights ($0.667\text{c}$) and starving the CPU worker, while DRF equalizes multi-resource dominant shares.

### Minute 8:00 – 10:00 | Faculty Q&A & Code Inspection
- Address questions using the FAQ playbook below.
- Show `/architecture` page if faculty ask about cgroup v2 hierarchy or SLSQP solver formulation.

---

## 4. Failure Recovery Playbook

| Issue Encountered | Immediate Root Cause | Recovery Procedure |
|---|---|---|
| **SSM port forwarding disconnects** | Transient network drop on host laptop. | Run `aws ssm start-session ...` in terminal. Tunnel re-establishes within 5 seconds; refresh browser. |
| **Live run fails or Docker hangs** | Stale container lock or daemon socket delay. | Navigate to `/history`, click on the pre-recorded `eval_scenario_o_3b13bb` run. State: *"The live SSM tunnel encountered latency; let us inspect the identical recorded hardware benchmark."* |
| **Recorded banner appears unexpectedly** | Fallback flag is active (`data/fallback_active.flag`). | Explain to faculty that the banner ensures absolute transparency about whether data was recorded or run live in the room. |

---

## 5. Likely Faculty Questions & Honest Answers

### Q1: Why does Adaptive lose so dramatically in Scenario 1 (11.1% vs DRF 100%)?
> **Answer**:
> "Under near-saturation ($96.7\%$ pool load), arrival burstiness induces transient CFS throttling across all containers. The demand estimator's fast-up escalation interprets this throttling as chronic under-provisioning and rapidly ratchets demand estimates toward declared caps, summing to $2.19\text{ cores}$ ($146\%$ of pool capacity).
> When total demand exceeds capacity, logarithmic utility maximization $\max \sum w_i \ln(s_i)$ mathematically collapses to allocating capacity strictly in proportion to tenant weights. With weights $2.0, 1.5, 1.0$, the CPU-heavy tenant receives only $0.667\text{ cores}$ despite needing $0.80\text{ cores}$ to service $66.8\text{ req/s}$, locking it into permanent compute starvation. 
> In contrast, DRF operates in multi-resource dominant share space. Because the memory worker requests $66.7\%$ of pool memory, DRF restricts its CPU allocation to match its dominant share, leaving $\approx 0.85\text{ cores}$ of CPU available for the CPU worker. This is a fundamental mathematical limitation of 1-D scalar log utility under saturation, which we document as a reported limitation."

### Q2: What is the difference between MEASURED and CALCULATED metrics in your system?
> **Answer**:
> "Every metric carries strict provenance metadata:
> - **MEASURED**: Data sampled directly from the Linux kernel and network stack without mathematical modeling: cgroup v2 hardware counters (`cpu.stat` throttled usec, `memory.current` RSS), host load, and load generator request completion timestamps.
> - **CALCULATED**: Metrics derived from allocator decisions relative to contractual definitions, such as Jain's fairness index and contractual demand satisfaction.
> - **SIMULATED**: Tagged exclusively on offline fluid simulations. No simulated data is ever shown in live or recorded EC2 evaluation runs."

### Q3: Why are the evaluation runs 30 seconds and 5 repeats rather than hours?
> **Answer**:
> "At closed-loop offered rates of $55\text{ to } 80\text{ req/s}$, each 30-second run processes over $2,400\text{ tasks}$, ensuring high statistical power per run. Running 5 randomized repeats ($N=5$) allows us to construct non-parametric 95% bootstrap confidence intervals while preventing thermal throttling drift on shared virtualized hardware."

### Q4: What does the "RECORDED" banner mean?
> **Answer**:
> "RECORDED means the data was executed on real AWS EC2 `c7i-flex.large` hardware under identical cgroup v2 isolation, but persisted into a validated JSON fallback bundle. This ensures that even if local networking or AWS SSM port forwarding degrades during the presentation, the faculty can examine genuine hardware measurements rather than simulations."

### Q5: Does the declared demand cap limit goodput for honest tenants?
> **Answer**:
> "Yes, and we have formally identified this as an architectural limitation. In `AdaptiveAllocator`, allocations and surplus redistribution are strictly capped at declared demand ($x_{i, r} \le D_{i, r}$). When Tenant C requires $0.66\text{ cores}$ but declared $0.50\text{ cores}$, the allocator cannot assign more than $0.50\text{c}$ during static epochs, even while $>0.38\text{ cores}$ of host CPU remain unallocated. This contract protect SLA boundaries but limits goodput when workloads under-declare."
