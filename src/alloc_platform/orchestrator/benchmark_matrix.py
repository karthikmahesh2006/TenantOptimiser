"""Benchmark matrix runner CLI for Stage 6 confirmatory evaluation.

Supports executing:
(a) A single (policy, scenario, seed) run;
(b) The full 60-run core hypothesis matrix;
(c) The 25-run ablation suite;
(d) The 20-run pinned pre-fix negative control arm (saha_v1_frozen);
(e) Total 105-run Option C protocol with --count-only and incremental S3 upload.
"""

import argparse
import asyncio
import json
import logging
import os
import subprocess
import sys
from typing import Dict, List, Optional, Tuple

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from alloc_platform.config import config
from alloc_platform.db.models import Base, Experiment, PoolModel, Run, RunMetric
from alloc_platform.orchestrator.runner import _execute_single_run
from alloc_platform.orchestrator.scenario import get_predefined_scenarios
from alloc_platform.telemetry.host import HostMonitor

logger = logging.getLogger(__name__)


def build_plan_tuples(
    scenarios: Optional[List[str]] = None,
    policies: Optional[List[str]] = None,
    ablations: Optional[List[str]] = None,
    negative_controls: Optional[List[str]] = None,
    seeds: Optional[List[int]] = None,
    single_policy: Optional[str] = None,
    single_scenario: Optional[str] = None,
    single_seed: Optional[int] = None,
) -> List[Tuple[str, str, int]]:
    """Builds the exact list of (policy, scenario_id, seed) tuples."""
    if single_policy and single_scenario and single_seed is not None:
        return [(single_policy, single_scenario, single_seed)]

    seeds_list = seeds if seeds is not None else [42, 43, 44, 45, 46]
    plan: List[Tuple[str, str, int]] = []

    # 1. Core baseline & hypothesis runs (60 runs)
    core_mapping = {
        "scenario_1_baseline": ["saha", "adaptive", "drf"],
        "scenario_2_burst": ["saha", "adaptive", "drf"],
        "scenario_3_capacity_cut": ["saha", "adaptive", "drf"],
        "scenario_over_declaration": ["saha", "adaptive", "proportional"],
    }
    target_scenarios = scenarios or list(core_mapping.keys())
    for sc_id in target_scenarios:
        sc_pols = core_mapping.get(sc_id, ["saha", "adaptive", "drf"])
        for pol in sc_pols:
            for s in seeds_list:
                plan.append((pol, sc_id, s))

    # 2. Pre-declared ablation suite (25 runs on scenario_1_baseline)
    ablation_pols = ablations if ablations is not None else [
        "saha:no_declared_floor",
        "saha:no_cut_latch",
        "saha:no_throttle_escalation",
        "saha:reclaim_only",
        "saha:fair_only",
    ]
    for ab_pol in ablation_pols:
        for s in seeds_list:
            plan.append((ab_pol, "scenario_1_baseline", s))

    # 3. Pinned pre-fix negative control arm (20 runs across all 4 scenarios)
    neg_pols = negative_controls if negative_controls is not None else ["saha_v1_frozen"]
    for neg_pol in neg_pols:
        for sc_id in target_scenarios:
            for s in seeds_list:
                plan.append((neg_pol, sc_id, s))

    return plan


def upload_run_to_s3(run_id: str, run_data: dict, bucket: str, region: str) -> None:
    """Incrementally uploads a single run result to S3."""
    import tempfile
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".json") as f:
        json.dump(run_data, f, indent=2)
        tmp_path = f.name
    try:
        s3_uri = f"s3://{bucket}/runs/{run_id}.json"
        cmd = ["aws", "s3", "cp", tmp_path, s3_uri, "--region", region]
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        logger.info("Successfully uploaded run %s to %s", run_id, s3_uri)
    except Exception as e:
        logger.warning("Incremental S3 upload failed for run %s: %s", run_id, e)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def main():
    parser = argparse.ArgumentParser(description="Stage 6 Benchmark Matrix Runner")
    parser.add_argument("--count-only", action="store_true", help="Print total run count and tuples without executing.")
    parser.add_argument("--scenarios", nargs="+", help="Subset of scenarios.")
    parser.add_argument("--policies", nargs="+", help="Subset of core policies.")
    parser.add_argument("--ablations", nargs="+", help="Ablation policies.")
    parser.add_argument("--negative-controls", nargs="+", help="Negative control policies.")
    parser.add_argument("--seeds", nargs="+", type=int, help="Seed list.")
    parser.add_argument("--duration", type=int, default=120, help="Scenario duration in seconds (default: 120s).")
    parser.add_argument("--repeats", type=int, default=5, help="Repeats per cell.")
    parser.add_argument("--single-policy", type=str, help="Single policy execution.")
    parser.add_argument("--single-scenario", type=str, help="Single scenario execution.")
    parser.add_argument("--single-seed", type=int, help="Single seed execution.")
    parser.add_argument("--s3-bucket", type=str, help="S3 bucket for incremental run exports.")
    parser.add_argument("--region", type=str, default="ap-south-1", help="AWS region.")
    parser.add_argument("--db-path", type=str, default="alloc.db", help="SQLite database path.")
    args = parser.parse_args()

    plan = build_plan_tuples(
        scenarios=args.scenarios,
        policies=args.policies,
        ablations=args.ablations,
        negative_controls=args.negative_controls,
        seeds=args.seeds,
        single_policy=args.single_policy,
        single_scenario=args.single_scenario,
        single_seed=args.single_seed,
    )

    if args.count_only:
        print(f"Planned Benchmark Runs (Count: {len(plan)}):")
        for idx, (pol, sc, seed) in enumerate(plan, 1):
            print(f"  [{idx:3d}/{len(plan)}] policy='{pol}', scenario='{sc}', seed={seed}")
        print(f"\nTotal runs: {len(plan)}")
        return

    # Execution path
    engine = create_engine(f"sqlite:///{args.db_path}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    scenarios_dict = get_predefined_scenarios(duration_s=args.duration)

    print(f"Starting execution of {len(plan)} benchmark runs (duration={args.duration}s)...")
    host_mon = HostMonitor()
    invalid_runs_count = 0

    for idx, (pol, sc_id, seed) in enumerate(plan, 1):
        db = Session()
        run_id = f"run_{sc_id}_{pol.replace(':', '_')}_s{seed}_{idx}"
        exp_id = f"exp_{sc_id}_{pol.replace(':', '_')}"
        print(f"\n--- [{idx}/{len(plan)}] Executing: {pol} on {sc_id} (seed={seed}) ---")

        # Ensure experiment exists
        exp = db.query(Experiment).filter(Experiment.id == exp_id).first()
        if not exp:
            exp = Experiment(id=exp_id, name=f"Exp {sc_id} {pol}", scenario_id=sc_id, status="running", policies_json=json.dumps([pol]), repeats=1)
            db.add(exp)
            db.commit()

        run_rec = Run(id=run_id, experiment_id=exp_id, policy=pol, repeat_index=0, status="pending", is_valid=True)
        db.add(run_rec)
        db.commit()

        scen_spec = scenarios_dict.get(sc_id)
        if not scen_spec:
            print(f"ERROR: Scenario {sc_id} not found!")
            db.close()
            continue

        scen_copy = scen_spec.__class__(**vars(scen_spec))
        scen_copy.seed = seed
        scen_copy.duration_s = args.duration

        cancel_ev = asyncio.Event()
        asyncio.run(_execute_single_run(
            db=db,
            run_id=run_id,
            policy=pol,
            scen_spec=scen_copy,
            enforcer=None,
            host_mon=host_mon,
            cancel_event=cancel_ev,
        ))

        # Collect metrics
        run_row = db.query(Run).filter(Run.id == run_id).first()
        if run_row and not run_row.is_valid:
            invalid_runs_count += 1
            print(f"  WARNING: Run {run_id} marked INVALID ({invalid_runs_count}/2 allowed): {run_row.invalidation_reason}")
            if invalid_runs_count >= 2:
                db.close()
                raise RuntimeError(
                    f"Aborting benchmark_matrix: Encountered {invalid_runs_count} invalid runs with loadgen/sample failures (latest: {run_id})."
                )

        metrics = {m.metric_name: m.metric_value for m in db.query(RunMetric).filter(RunMetric.run_id == run_id).all()}
        print(f"  Completed {run_id}: on_time={metrics.get('on_time_ratio', 0.0):.2%}, p95={metrics.get('p95_latency_ms', 0.0):.2f}ms")

        if args.s3_bucket:
            run_summary = {
                "run_id": run_id,
                "policy": pol,
                "scenario": sc_id,
                "seed": seed,
                "duration_s": args.duration,
                "metrics": metrics,
            }
            upload_run_to_s3(run_id, run_summary, bucket=args.s3_bucket, region=args.region)

        db.close()

    print("\nBenchmark matrix execution complete.")


if __name__ == "__main__":
    main()
