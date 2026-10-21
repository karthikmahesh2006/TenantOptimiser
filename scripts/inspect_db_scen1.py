"""Queries and prints the per-epoch telemetry for the Scenario 1 Adaptive run from alloc.db."""

import os
import sys

# Ensure root in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from alloc_platform.db.models import AllocationEvent, Experiment, Run, RunMetric, TenantSample
from alloc_platform.db.session import SessionLocal, init_db

init_db()
db = SessionLocal()

# Find the run_id for scenario_1_baseline adaptive
q = (
    db.query(Run, Experiment)
    .join(Experiment, Run.experiment_id == Experiment.id)
    .filter(Experiment.scenario_id == "scenario_1_baseline", Run.policy == "adaptive")
    .order_by(Run.created_at.desc())
    .first()
)

if not q:
    print("No Scenario 1 Adaptive run found in DB.")
    # Print all runs in DB to see what exists
    runs = db.query(Run).all()
    print(f"Total runs in DB: {len(runs)}")
    for r in runs:
        print(f"  Run: {r.id}, Exp: {r.experiment_id}, Policy: {r.policy}, Created: {r.created_at}")
    sys.exit(0)

run, exp = q

# Query on_time_ratio metric
m_on_time = db.query(RunMetric).filter(RunMetric.run_id == run.id, RunMetric.metric_name == "on_time_ratio").first()
on_time_val = m_on_time.metric_value if m_on_time else None
print(f"Found Run: {run.id} (Exp: {exp.id}, Scenario: {exp.scenario_id}, Policy: {run.policy}, On-Time: {on_time_val}, Created: {run.created_at})")

# Query per-epoch samples and allocations
samples = db.query(TenantSample).filter(TenantSample.run_id == run.id).order_by(TenantSample.timestamp_s.asc()).all()
alloc_events = db.query(AllocationEvent).filter(AllocationEvent.run_id == run.id).order_by(AllocationEvent.epoch.asc()).all()

print(f"Total TenantSample records: {len(samples)}")
print(f"Total AllocationEvent records: {len(alloc_events)}")

# Group by epoch
epochs = {}
for s in samples:
    ep = int(s.timestamp_s)
    if ep not in epochs:
        epochs[ep] = {"samples": {}, "allocs": {}}
    epochs[ep]["samples"][s.tenant_id] = {
        "used_cpu": s.cpu_cores_used,
        "throt": s.throttle_ratio,
        "throt_us": s.throttled_usec,
    }

for a in alloc_events:
    ep = a.epoch
    if ep not in epochs:
        epochs[ep] = {"samples": {}, "allocs": {}}
    epochs[ep]["allocs"][a.tenant_id] = {
        "alloc_cpu": a.alloc_cpu,
        "alloc_mem_mb": a.alloc_mem_mb,
    }

print("\n=== PER-EPOCH DB TRACE FOR SCENARIO 1 ADAPTIVE ===")
print(f"{'Epoch':>5} | {'tenant_cpu Alloc':>16} | {'tenant_cpu Used':>15} | {'Throt %':>7} | {'mix Alloc':>10} | {'mix Used':>9} | {'mem Alloc':>10} | {'mem Used':>9} | {'Total Alloc':>11}")
print("-" * 115)

for ep in sorted(epochs.keys()):
    d = epochs[ep]
    s_cpu = d["samples"].get("tenant_cpu", {})
    a_cpu = d["allocs"].get("tenant_cpu", {})
    c_alloc = a_cpu.get("alloc_cpu", 0.0)
    c_used = s_cpu.get("used_cpu", 0.0)
    c_throt = (s_cpu.get("throt", 0.0)) * 100.0

    s_mix = d["samples"].get("tenant_mix", {})
    a_mix = d["allocs"].get("tenant_mix", {})
    m_alloc = a_mix.get("alloc_cpu", 0.0)
    m_used = s_mix.get("used_cpu", 0.0)

    s_mem = d["samples"].get("tenant_mem", {})
    a_mem = d["allocs"].get("tenant_mem", {})
    mem_alloc = a_mem.get("alloc_cpu", 0.0)
    mem_used = s_mem.get("used_cpu", 0.0)

    tot_alloc = c_alloc + m_alloc + mem_alloc

    print(f"{ep:5d} | {c_alloc:16.4f} | {c_used:15.4f} | {c_throt:6.1f}% | {m_alloc:10.4f} | {m_used:9.4f} | {mem_alloc:10.4f} | {mem_used:9.4f} | {tot_alloc:11.4f}")
