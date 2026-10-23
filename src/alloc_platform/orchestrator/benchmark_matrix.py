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
import math
import os
import subprocess
import sys
import time
from typing import Dict, List, Optional, Tuple

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from alloc_platform.config import config
from alloc_platform.db.models import Base, Experiment, PoolModel, Run, RunMetric
from alloc_platform.orchestrator.runner import _execute_single_run
from alloc_platform.orchestrator.scenario import get_predefined_scenarios
from alloc_platform.telemetry.host import HostMonitor

logger = logging.getLogger(__name__)


def compute_watchdog_minutes(
    runs: int, duration_s: int, overhead_s: float = 5.0, buffer_min: int = 25
) -> Tuple[int, str]:
    """Computes the watchdog timeout in minutes with transparent arithmetic.

    Watchdog = runs x (duration + measured_overhead) + 25 min buffer.
    """
    exec_sec = runs * (duration_s + overhead_s)
    exec_min = exec_sec / 60.0
    total_min = math.ceil(exec_min + buffer_min)
    explanation = (
        f"Watchdog Arithmetic: {runs} runs x ({duration_s}s duration + {overhead_s}s overhead) = "
        f"{exec_sec:.1f}s ({exec_min:.2f}m) + {buffer_min}m buffer = {exec_min + buffer_min:.2f}m -> {total_min} minutes"
    )
    return total_min, explanation


def build_plan_tuples(
    scenarios: Optional[List[str]] = None,
    policies: Optional[List[str]] = None,
    ablations: Optional[List[str]] = None,
    negative_controls: Optional[List[str]] = None,
    seeds: Optional[List[int]] = None,
    single_policy: Optional[str] = None,
    single_scenario: Optional[str] = None,
    single_seed: Optional[int] = None,
    preset: Optional[str] = None,
) -> List[Tuple[str, str, int]]:
    """Builds the exact list of (policy, scenario_id, seed) tuples."""
    if single_policy and single_scenario and single_seed is not None:
        return [(single_policy, single_scenario, single_seed)]

    if preset == "reduced30":
        # Exactly 30 runs:
        # scenario_1_baseline x {saha, adaptive, drf} x seeds 42-46 (15 runs)
        # scenario_over_declaration x {saha, adaptive, proportional} x seeds 42-46 (15 runs)
        seeds_list = seeds if seeds is not None else [42, 43, 44, 45, 46]
        plan: List[Tuple[str, str, int]] = []
        for pol in ["saha", "adaptive", "drf"]:
            for s in seeds_list:
                plan.append((pol, "scenario_1_baseline", s))
        for pol in ["saha", "adaptive", "proportional"]:
            for s in seeds_list:
                plan.append((pol, "scenario_over_declaration", s))
        return plan

    if preset == "smoke3":
        # Exactly 3 runs for detached smoke test:
        # scenario_1_baseline x {saha, adaptive, drf} x seed 9001 (throwaway non-confirmatory seed)
        seeds_list = seeds if seeds is not None else [9001]
        plan = []
        for pol in ["saha", "adaptive", "drf"]:
            for s in seeds_list:
                plan.append((pol, "scenario_1_baseline", s))
        return plan

    seeds_list = seeds if seeds is not None else [42, 43, 44, 45, 46]
    plan = []

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


def upload_json_to_s3(data: dict, s3_uri: str, region: str) -> None:
    """Uploads a dictionary as JSON to S3 via aws s3 cp."""
    import tempfile
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".json") as f:
        json.dump(data, f, indent=2)
        tmp_path = f.name
    try:
        cmd = ["aws", "s3", "cp", tmp_path, s3_uri, "--region", region]
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def write_and_upload_done_file(
    status: str,
    run_count: int,
    invalid_count: int,
    total_planned: int,
    reason: Optional[str] = None,
    done_file: str = "/tmp/stage6_done.json",
    s3_bucket: Optional[str] = None,
    s3_prefix: str = "backups/stage6",
    region: str = "ap-south-1",
    commit_hash: Optional[str] = None,
    preset: Optional[str] = None,
) -> None:
    """Writes done.json locally and uploads it to S3."""
    payload = {
        "status": status,
        "run_count": run_count,
        "invalid_count": invalid_count,
        "total_planned": total_planned,
        "reason": reason,
        "commit_hash": commit_hash,
        "preset": preset,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    dir_name = os.path.dirname(os.path.abspath(done_file))
    if dir_name:
        os.makedirs(dir_name, exist_ok=True)
    with open(done_file, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    if s3_bucket:
        s3_uri = f"s3://{s3_bucket}/{s3_prefix.rstrip('/')}/done.json"
        try:
            upload_json_to_s3(payload, s3_uri, region)
        except Exception as e:
            logger.warning("Failed to upload done.json: %s", e)


def main():
    parser = argparse.ArgumentParser(description="Stage 6 Benchmark Matrix Runner")
    parser.add_argument("--count-only", action="store_true", help="Print total run count and tuples without executing.")
    parser.add_argument(
        "--preset",
        type=str,
        choices=["full105", "reduced30", "smoke3"],
        help="Predefined benchmark suite (reduced30: 30 runs, smoke3: 3 runs, full105: 105 runs).",
    )
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
    parser.add_argument("--s3-prefix", type=str, default="backups/stage6", help="S3 prefix (default: backups/stage6).")
    parser.add_argument("--overhead", type=float, default=5.0, help="Measured per-run overhead in seconds for watchdog (default: 5.0s).")
    parser.add_argument("--commit-hash", type=str, default=None, help="Pinned git commit hash running on instance.")
    parser.add_argument("--done-file", type=str, default="/tmp/stage6_done.json", help="Local done.json path.")
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
        preset=args.preset,
    )

    if args.count_only:
        watchdog_min, explanation = compute_watchdog_minutes(
            runs=len(plan),
            duration_s=args.duration,
            overhead_s=args.overhead,
        )
        print(f"Planned Benchmark Runs (Count: {len(plan)}):")
        for idx, (pol, sc, seed) in enumerate(plan, 1):
            print(f"  [{idx:3d}/{len(plan)}] policy='{pol}', scenario='{sc}', seed={seed}")
        print(f"\nTotal runs: {len(plan)}")
        print(f"\n{explanation}")
        return

    # Execution path
    engine = create_engine(f"sqlite:///{args.db_path}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    scenarios_dict = get_predefined_scenarios(duration_s=args.duration)

    # Initialize DockerEnforcer in live mode
    enforcer = None
    if config.alloc_mode.lower() == "live":
        from alloc_platform.enforcement.docker_enforcer import DockerEnforcer
        try:
            enforcer = DockerEnforcer()
            enforcer.client.ping()
            print("  [LIVE] Connected to Docker daemon via DockerEnforcer.")
        except Exception as e:
            raise RuntimeError(f"FATAL: ALLOC_MODE=live but DockerEnforcer failed: {e}")

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
            enforcer=enforcer,
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
                write_and_upload_done_file(
                    status="failed",
                    run_count=idx,
                    invalid_count=invalid_runs_count,
                    total_planned=len(plan),
                    reason=f"Aborted: Encountered {invalid_runs_count} invalid runs (latest: {run_id}).",
                    done_file=args.done_file,
                    s3_bucket=args.s3_bucket,
                    s3_prefix=args.s3_prefix,
                    region=args.region,
                    commit_hash=args.commit_hash,
                    preset=args.preset,
                )
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
                "is_valid": run_row.is_valid if run_row else True,
                "invalidation_reason": run_row.invalidation_reason if run_row else None,
                "metrics": metrics,
                "commit_hash": args.commit_hash,
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            s3_run_uri = f"s3://{args.s3_bucket}/{args.s3_prefix.rstrip('/')}/{run_id}.json"
            try:
                upload_json_to_s3(run_summary, s3_run_uri, args.region)
                print(f"  Uploaded run artifact to {s3_run_uri}")
            except Exception as e:
                print(f"  ERROR: S3 upload failed for {run_id}: {e}")
                if idx == 1:
                    db.close()
                    write_and_upload_done_file(
                        status="failed",
                        run_count=1,
                        invalid_count=invalid_runs_count,
                        total_planned=len(plan),
                        reason=f"First S3 upload failed for {run_id}: {e}",
                        done_file=args.done_file,
                        s3_bucket=args.s3_bucket,
                        s3_prefix=args.s3_prefix,
                        region=args.region,
                        commit_hash=args.commit_hash,
                        preset=args.preset,
                    )
                    raise RuntimeError(f"FATAL: First S3 upload failed ({e}). Aborting run to avoid running without saving.")

            # Update progress.json
            progress_data = {
                "done": idx,
                "total": len(plan),
                "invalid": invalid_runs_count,
                "last_run_id": run_id,
                "commit_hash": args.commit_hash,
                "preset": args.preset,
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            s3_progress_uri = f"s3://{args.s3_bucket}/{args.s3_prefix.rstrip('/')}/progress.json"
            try:
                upload_json_to_s3(progress_data, s3_progress_uri, args.region)
            except Exception as e:
                logger.warning("Failed to upload progress.json: %s", e)

        db.close()

    print("\nBenchmark matrix execution complete.")
    write_and_upload_done_file(
        status="completed",
        run_count=len(plan),
        invalid_count=invalid_runs_count,
        total_planned=len(plan),
        reason="All scheduled benchmark runs completed cleanly.",
        done_file=args.done_file,
        s3_bucket=args.s3_bucket,
        s3_prefix=args.s3_prefix,
        region=args.region,
        commit_hash=args.commit_hash,
        preset=args.preset,
    )



if __name__ == "__main__":
    main()
