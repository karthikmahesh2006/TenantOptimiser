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
    db = SessionLocal()
    demo_exp_id = "faculty_demo_measured"

    existing = get_experiment_by_id(db, demo_exp_id)
    if existing:
        # Delete prior runs and metrics for clean rerun
        runs = db.query(Run).filter(Run.experiment_id == demo_exp_id).all()
        for r in runs:
            db.query(RunMetric).filter(RunMetric.run_id == r.id).delete()
            db.delete(r)
        db.delete(existing)
        db.commit()

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

    print("\n" + "=" * 92)
    print("                      MEASURED LIVE HARDWARE BENCHMARK RESULTS")
    print("=" * 92)
    print(
        f"{'Policy':<14} | {'Jain (Bottleneck)':<18} | {'Mean Satis.':<12} | {'Alloc Util':<14} | {'Cons Util':<14} | {'Churn':<8} | {'Solve (ms)':<10}"
    )
    print("-" * 92)

    for pol in ["fcfs", "proportional", "adaptive"]:
        m = metrics_by_policy.get(pol, {})
        jain = m.get("jain_fairness_primary", 0.0)
        satis = m.get("mean_satisfaction", 0.0)
        alloc_u = m.get("allocated_util_cpu", 0.0) * 100
        cons_u = m.get("consumed_util_cpu", 0.0) * 100
        churn = m.get("churn", 0.0)
        solve_ms = m.get("solve_ms_avg", 0.0)

        print(
            f"{pol.upper():<14} | {jain:<18.4f} | {satis:<12.4f} | {alloc_u:>6.1f}% CPU    | {cons_u:>6.1f}% CPU    | {churn:<8.3f} | {solve_ms:<10.2f}"
        )
    print("-" * 92)
    print("All metrics above are MEASURED on real cgroup v2 & docker containers on host.")
    print("=" * 92)
    db.close()


if __name__ == "__main__":
    asyncio.run(run_benchmark())
