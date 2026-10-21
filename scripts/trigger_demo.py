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

    print("\n" + "=" * 145)
    print("                                      MEASURED LIVE HARDWARE BENCHMARK RESULTS")
    print("                       (Queried strictly from SQLite run_metrics rows for this execution run)")
    print("=" * 145)
    print(
        f"{'Policy':<13} | {'Jain (CALC)':<12} | {'Satis (CALC)':<12} | {'On-Time (Headline)':<18} | {'Unmet (Throt|Miss)':<20} | {'Idle CPU':<10} | {'Cons Util':<11} | {'Solve Time':<18} | {'Throughput':<12} | {'p95 Lat [Excl]':<18}"
    )
    print("-" * 145)

    for pol in ["fcfs", "proportional", "adaptive"]:
        m = metrics_by_policy.get(pol, {})
        jain = m.get("jain_fairness_primary", 0.0)
        satis = m.get("mean_satisfaction", 0.0)
        on_time = m.get("on_time_ratio", 1.0) * 100.0
        throt = m.get("throttle_time_ratio", 0.0) * 100.0
        miss = m.get("deadline_miss_rate", 0.0) * 100.0
        idle_c = m.get("idle_reserved_cpu", 0.0)
        cons_u = m.get("consumed_util_cpu", 0.0) * 100.0
        solve_ms = m.get("solve_ms_avg", 0.0)
        solve_pct = m.get("solve_time_epoch_pct", (solve_ms / 1000.0) * 100.0)
        tp = m.get("throughput", 0.0)
        p95_lat = m.get("p95_latency_ms", 0.0)
        excl = m.get("excluded_count", 0.0)

        on_time_str = f"{on_time:>5.1f}%"
        unmet_str = f"{throt:>4.1f}% | {miss:>4.1f}%"
        solve_str = f"{solve_ms:>5.2f}ms ({solve_pct:>4.2f}%)"
        p95_str = f"{p95_lat:>6.1f}ms [{excl:.0f} excl]"

        print(
            f"{pol.upper():<13} | {jain:<12.4f} | {satis:<12.4f} | {on_time_str:<18} | {unmet_str:<20} | {idle_c:>5.2f} cores | {cons_u:>5.1f}% CPU   | {solve_str:<18} | {tp:>6.2f} req/s | {p95_str:<18}"
        )
    print("-" * 145)
    print("Provenance: Jain, Satis, Idle CPU are CALCULATED; Cons Util, On-Time, Unmet Need, Solve, Throughput, Latency are MEASURED.")
    print("=" * 145)
    db.close()


if __name__ == "__main__":
    asyncio.run(run_benchmark())
