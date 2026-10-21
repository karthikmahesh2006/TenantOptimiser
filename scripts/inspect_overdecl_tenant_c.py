"""Inspects Tenant C allocation vs declared cap per epoch in over-declaration benchmark."""

import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from alloc_platform.db.models import Experiment, Run, TenantSample
from alloc_platform.db.session import SessionLocal, init_db


def inspect():
    init_db()
    db = SessionLocal()

    # Find over-declaration experiments
    exps = db.query(Experiment).filter(Experiment.scenario_id == "scenario_over_declaration").all()
    print(f"Found {len(exps)} over-declaration experiments.")

    for exp in exps:
        print(f"\nExperiment: {exp.id} ({exp.name}) - Status: {exp.status}")
        runs = db.query(Run).filter(Run.experiment_id == exp.id, Run.policy == "adaptive", Run.is_valid == True).all()
        if not runs:
            continue

        run = runs[0]
        print(f"Inspecting Adaptive Run: {run.id}")

        # Check diagnostic JSON
        diag_path = os.path.join("data", "diagnostics", f"{run.id}.json")
        if not os.path.exists(diag_path):
            alt_path = os.path.join("/opt/alloc/current/data/diagnostics", f"{run.id}.json")
            if os.path.exists(alt_path):
                diag_path = alt_path

        if os.path.exists(diag_path):
            with open(diag_path, "r", encoding="utf-8") as f:
                trace = json.load(f)
            print(f"Loaded {len(trace)} epochs from {diag_path}")
            print(f"{'Epoch':>5} | {'Tenant C Cap':>12} | {'Tenant C Alloc':>14} | {'Tenant C Usage':>14} | {'Tenant C Throt%':>15} | {'Pool Spare':>10}")
            print("-" * 80)
            for d in trace:
                ep = d.get("epoch", 0)
                allocs = d.get("final_alloc", {})
                obs = d.get("obs", {})
                tc_alloc = allocs.get("tenant_c_honest", {}).get("cpu", 0.0)
                tc_obs = obs.get("tenant_c_honest", {})
                tc_usage = tc_obs.get("usage", {}).get("cpu", 0.0)
                tc_throt = tc_obs.get("throttle_ratio", 0.0) * 100.0
                total_alloc = sum(a.get("cpu", 0.0) for a in allocs.values())
                spare = max(0.0, 1.50 - total_alloc)
                print(f"{ep:>5} | {'0.500 c':>12} | {tc_alloc:>12.3f} c | {tc_usage:>12.3f} c | {tc_throt:>14.1f}% | {spare:>8.3f} c")
        else:
            # Query from AllocEvent and TenantSample tables
            from alloc_platform.db.models import AllocationEvent
            events = db.query(AllocationEvent).filter(AllocationEvent.run_id == run.id, AllocationEvent.tenant_id == "tenant_c_honest").order_by(AllocationEvent.epoch.asc()).all()
            events_by_epoch = {e.epoch: e for e in events}
            samples = db.query(TenantSample).filter(TenantSample.run_id == run.id, TenantSample.tenant_id == "tenant_c_honest").order_by(TenantSample.timestamp_s.asc()).all()
            print(f"Loaded {len(events)} alloc events, {len(samples)} samples from database.")
            print(f"{'Epoch':>5} | {'Tenant C Cap':>12} | {'Alloc (c)':>10} | {'Usage (c)':>10} | {'Throt %':>9}")
            print("-" * 60)
            for i, s in enumerate(samples):
                ev = events_by_epoch.get(i) or events_by_epoch.get(int(s.timestamp_s))
                alloc_val = ev.alloc_cpu if ev else 0.0
                print(f"{i:>5} | {'0.500 c':>12} | {alloc_val:>10.3f} c | {s.cpu_cores_used:>10.3f} c | {s.throttle_ratio * 100:>8.1f}%")

    db.close()


if __name__ == "__main__":
    inspect()
