"""Triggers the Faculty Demo benchmark and outputs the measured comparison metrics."""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from alloc_platform.db.models import Experiment, Run, RunMetric
from alloc_platform.db.repo import get_experiment_by_id, get_run_metrics
from alloc_platform.db.session import SessionLocal
from alloc_platform.orchestrator.runner import execute_experiment_async


async def run_benchmark():
    import uuid

    db = SessionLocal()
    demo_exp_id = f"faculty_demo_{uuid.uuid4().hex[:6]}"


    exp = Experiment(
        id=demo_exp_id,
        name="Faculty Live Demo: 3-Tenant Comparison with Dynamic Burst",
        scenario_id="demo_scenario",
        policies_json=json.dumps(["fcfs", "proportional", "adaptive"]),
        repeats=1,
        status="pending",
    )
    db.add(exp)
    db.commit()
    db.close()

    print("=" * 80)
    print("  LAUNCHING LIVE FACULTY DEMO BENCHMARK ON REAL DOCKER HOST (ap-south-1)")
    print(f"  Experiment ID: {demo_exp_id}")
    print("  Policies: FCFS, Weighted Proportional, Adaptive Optimiser")
    print("=" * 80)

    await execute_experiment_async(demo_exp_id)

    db = SessionLocal()
    exp = get_experiment_by_id(db, demo_exp_id)
    print(f"\nExperiment Status: {exp.status.upper()}")
    if exp.error_message:
        print(f"Error Message: {exp.error_message}")

    runs = db.query(Run).filter(Run.experiment_id == demo_exp_id).all()
    metrics_by_policy = {}
    for r in runs:
        m = get_run_metrics(db, r.id)
        metrics_by_policy[r.policy] = m

    print("\n" + "=" * 110)
    print("                      MEASURED LIVE HARDWARE BENCHMARK RESULTS")
    print("            (Queried strictly from SQLite run_metrics rows for this execution run)")
    print("=" * 110)
    print(
        f"{'Policy':<13} | {'Jain (CALC)':<12} | {'Satis (CALC)':<12} | {'Alloc Util':<12} | {'Cons Util':<12} | {'Churn':<7} | {'Solve':<9} | {'Throughput':<14} | {'Latency (p95)':<14}"
    )
    print("-" * 110)

    for pol in ["fcfs", "proportional", "adaptive"]:
        m = metrics_by_policy.get(pol, {})
        jain = m.get("jain_fairness_primary", 0.0)
        satis = m.get("mean_satisfaction", 0.0)
        alloc_u = m.get("allocated_util_cpu", 0.0) * 100
        cons_u = m.get("consumed_util_cpu", 0.0) * 100
        churn = m.get("churn", 0.0)
        solve_ms = m.get("solve_ms_avg", 0.0)
        tp = m.get("throughput", 0.0)
        p95_lat = m.get("p95_latency_ms", 0.0)

        print(
            f"{pol.upper():<13} | {jain:<12.4f} | {satis:<12.4f} | {alloc_u:>5.1f}% CPU   | {cons_u:>5.1f}% CPU   | {churn:<7.3f} | {solve_ms:>6.2f} ms | {tp:>7.2f} req/s   | {p95_lat:>8.1f} ms"
        )
    print("-" * 110)
    print("Provenance: Jain, Satis, Alloc Util, Churn are CALCULATED; Cons Util, Solve, Throughput, Latency are MEASURED.")
    print("=" * 110)
    db.close()


if __name__ == "__main__":
    asyncio.run(run_benchmark())
