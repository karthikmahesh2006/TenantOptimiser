"""Recompute evaluation tables with policy-induced OOM runs included as failures.

Per scientific protocol:
- Invalidate only environment faults: non-tenant CPU > 0.50c, steal > 5.0%.
- Policy-induced OOM kills are NOT environment faults; they are failures of the allocation policy.
- OOM runs are included in analysis with on_time_ratio = 0.0 (or whatever goodput was completed prior to OOM).
- Report OOM count and rate per policy per scenario.
"""

import os
import sys
from typing import Dict, List, Any
import numpy as np

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from alloc_platform.allocator.metrics import bootstrap_ci
from alloc_platform.db.models import Experiment, Run, RunMetric
from alloc_platform.db.session import SessionLocal, init_db


def recompute_all():
    init_db()
    db = SessionLocal()

    exps = db.query(Experiment).all()
    print("=" * 100)
    print("RECOMPUTING ALL EXPERIMENTS (OOM runs included as valid policy evaluations)")
    print("=" * 100)

    for exp in exps:
        runs = db.query(Run).filter(Run.experiment_id == exp.id).all()
        if not runs or len(runs) < 3:
            continue

        print(f"\nExperiment: {exp.id} | Scenario: {exp.scenario_id} | Name: {exp.name}")
        print(f"Total runs: {len(runs)}")

        # Audit invalidations and classify environment vs policy
        policy_runs: Dict[str, List[Run]] = {}
        env_invalid_runs = []
        policy_oom_runs = []

        for r in runs:
            if not r.is_valid:
                # Check reason
                reason = r.invalidation_reason or ""
                if "Non-tenant CPU" in reason or "steal" in reason:
                    env_invalid_runs.append(r)
                elif "OOM" in reason:
                    policy_oom_runs.append(r)
                else:
                    env_invalid_runs.append(r)

        print(f"  Environment faults (excluded): {len(env_invalid_runs)}")
        for r in env_invalid_runs:
            print(f"    - Excluded Run {r.id} ({r.policy} rep {r.repeat_index}): {r.invalidation_reason}")

        print(f"  Policy OOM runs (now included as valid policy failures): {len(policy_oom_runs)}")
        for r in policy_oom_runs:
            print(f"    - Included OOM Run {r.id} ({r.policy} rep {r.repeat_index}): {r.invalidation_reason}")

        # Group runs by policy (including valid runs and policy_oom_runs, excluding only env_invalid_runs)
        for r in runs:
            if r in env_invalid_runs:
                continue
            policy_runs.setdefault(r.policy, []).append(r)

        print("\n  Per-Policy Summary (with OOM runs included):")
        print(f"  {'Policy':<16} | {'Total n':<8} | {'OOMs':<6} | {'OOM Rate':<10} | {'On-Time Ratio (Mean & 95% CI)':<35} | {'Throughput':<20}")
        print("  " + "-" * 95)

        for pol, prs in sorted(policy_runs.items()):
            on_times = []
            throughputs = []
            oom_count = 0

            for r in prs:
                metrics = {m.metric_name: m.metric_value for m in db.query(RunMetric).filter(RunMetric.run_id == r.id).all()}
                
                # Check if this run was an OOM failure
                is_oom = (r in policy_oom_runs) or (metrics.get("oom_kills", 0) > 0)
                if is_oom:
                    oom_count += 1

                # If OOM caused failure, use measured on_time_ratio or 0.0 if incomplete
                ot = metrics.get("on_time_ratio", 0.0)
                tp = metrics.get("throughput", 0.0)
                on_times.append(ot)
                throughputs.append(tp)

            n = len(prs)
            oom_rate = (oom_count / n * 100.0) if n > 0 else 0.0

            if on_times:
                ot_mean, ot_lo, ot_hi = bootstrap_ci(on_times, n=2000, seed=42)
                tp_mean, tp_lo, tp_hi = bootstrap_ci(throughputs, n=2000, seed=42)
                ot_str = f"{ot_mean*100:.1f}% [{ot_lo*100:.1f}%, {ot_hi*100:.1f}%]"
                tp_str = f"{tp_mean:.2f} [{tp_lo:.2f}, {tp_hi:.2f}] req/s"
            else:
                ot_str = "N/A"
                tp_str = "N/A"

            print(f"  {pol.upper():<16} | {n:<8} | {oom_count:<6} | {oom_rate:>8.1f}% | {ot_str:<35} | {tp_str:<20}")

    db.close()


if __name__ == "__main__":
    recompute_all()
