"""Diagnose Adaptive OOM kills in scenario_honest_declaration.

Inspects runs run_732728d8afd2 and run_6ac917d18c24, querying tenant_samples, allocation_events,
and diagnostics to print memory usage, limit, and allocation per epoch around the kill.
"""

import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from alloc_platform.db.models import AllocationEvent, Run, RunMetric, TenantSample
from alloc_platform.db.session import SessionLocal, init_db


def diagnose():
    init_db()
    db = SessionLocal()

    oom_runs = ["run_732728d8afd2", "run_6ac917d18c24"]

    print("=" * 100)
    print("DIAGNOSIS OF ADAPTIVE OOM TERMINATIONS IN SCENARIO_HONEST_DECLARATION")
    print("=" * 100)

    for run_id in oom_runs:
        run = db.query(Run).filter(Run.id == run_id).first()
        if not run:
            print(f"Run {run_id} not found in DB")
            continue

        print(f"\nRun ID: {run.id} | Policy: {run.policy} | Repeat: {run.repeat_index} | Valid: {run.is_valid} | Reason: {run.invalidation_reason}")

        # Metrics
        metrics = {m.metric_name: m.metric_value for m in db.query(RunMetric).filter(RunMetric.run_id == run_id).all()}
        print(f"  Recorded Metrics: on_time_ratio={metrics.get('on_time_ratio')}, throughput={metrics.get('throughput')}, oom_kills={metrics.get('oom_kills')}")

        # Check diagnostic JSON if present
        diag_candidates = [
            os.path.join("data", "diagnostics", f"{run_id}.json"),
            os.path.join("/opt/alloc/current/data/diagnostics", f"{run_id}.json"),
        ]
        diag_path = next((p for p in diag_candidates if os.path.exists(p)), None)
        if diag_path:
            with open(diag_path, "r") as f:
                trace = json.load(f)
            print(f"  Diagnostic Trace found ({len(trace)} epochs):")
            for ep in trace[-10:]:
                print(f"    Epoch {ep.get('t', ep.get('epoch'))}: {ep}")

        # Query TenantSamples from database
        samples = db.query(TenantSample).filter(TenantSample.run_id == run_id).order_by(TenantSample.timestamp_s).all()
        print(f"  Tenant Samples in DB: {len(samples)}")
        if samples:
            by_tenant = {}
            for s in samples:
                by_tenant.setdefault(s.tenant_id, []).append(s)
            for t_id, s_list in by_tenant.items():
                print(f"\n    Tenant: {t_id} (last {min(10, len(s_list))} samples):")
                print(f"    {'Time(s)':<8} | {'CPU Used':<10} | {'Mem Used(MB)':<14} | {'Throt %':<10}")
                print("    " + "-" * 50)
                for s in s_list[-10:]:
                    print(f"    {s.timestamp_s:<8.1f} | {s.cpu_cores_used:<10.3f} | {s.mem_used_mb:<14.1f} | {s.throttle_ratio*100:<10.1f}%")

        # Query AllocationEvents from database
        allocs = db.query(AllocationEvent).filter(AllocationEvent.run_id == run_id).order_by(AllocationEvent.epoch).all()
        print(f"\n  Alloc Events in DB: {len(allocs)}")
        if allocs:
            by_tenant_alloc = {}
            for a in allocs:
                by_tenant_alloc.setdefault(a.tenant_id, []).append(a)
            for t_id, a_list in by_tenant_alloc.items():
                print(f"\n    Alloc Events for {t_id} (last {min(10, len(a_list))} epochs):")
                print(f"    {'Epoch':<6} | {'Alloc CPU':<12} | {'Alloc Mem(MB)':<14}")
                print("    " + "-" * 38)
                for a in a_list[-10:]:
                    print(f"    {a.epoch:<6} | {a.alloc_cpu:<12.3f} | {a.alloc_mem_mb:<14.1f}")

    db.close()


if __name__ == "__main__":
    diagnose()
