"""Rigorous evaluation suite executing Scenarios 1-4 with 5 repeats, noise floor, and ablations.

Computes 95% bootstrap confidence intervals and percent differences versus all baselines.
Queried strictly from SQLite database rows.
"""

import asyncio
import json
import os
import sys
import uuid
from typing import Any, Dict, List

import numpy as np

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from alloc_platform.allocator.metrics import bootstrap_ci, pct_change
from alloc_platform.db.models import Experiment, Run, RunMetric
from alloc_platform.db.repo import get_experiment_by_id, get_run_metrics
from alloc_platform.db.session import SessionLocal, init_db
from alloc_platform.orchestrator.runner import execute_experiment_async
from alloc_platform.orchestrator.scenario import get_predefined_scenarios


async def run_scenario_benchmark(
    scenario_id: str,
    policies: List[str],
    repeats: int = 5,
    exp_name: str | None = None,
) -> str:
    db = SessionLocal()
    exp_id = f"eval_{scenario_id[:10]}_{uuid.uuid4().hex[:6]}"
    scen_name = exp_name or f"Rigorous Evaluation: {scenario_id} ({repeats} repeats)"

    exp = Experiment(
        id=exp_id,
        name=scen_name,
        scenario_id=scenario_id,
        policies_json=json.dumps(policies),
        repeats=repeats,
        status="pending",
    )
    db.add(exp)
    db.commit()
    db.close()

    print(f"\n[EXEC] Launching {exp_id} ({scenario_id}) with policies {policies}, repeats={repeats}...")
    await execute_experiment_async(exp_id)

    db = SessionLocal()
    exp_res = get_experiment_by_id(db, exp_id)
    print(f"[STATUS] {exp_id}: {exp_res.status.upper()}")
    if exp_res.error_message:
        print(f"[ERROR] {exp_res.error_message}")
    db.close()
    return exp_id


def analyze_experiment(exp_id: str) -> Dict[str, Any]:
    db = SessionLocal()
    all_runs = db.query(Run).filter(Run.experiment_id == exp_id).all()
    valid_runs = [r for r in all_runs if r.is_valid]
    invalid_runs = [r for r in all_runs if not r.is_valid]
    exclusions = [
        {"run_id": r.id, "policy": r.policy, "repeat": r.repeat_index, "reason": r.invalidation_reason}
        for r in invalid_runs
    ]

    # Collect per-policy metric values across repeats for valid runs
    data_by_policy: Dict[str, Dict[str, List[float]]] = {}
    for r in valid_runs:
        m = get_run_metrics(db, r.id)
        if r.policy not in data_by_policy:
            data_by_policy[r.policy] = {}
        for k, v in m.items():
            if k not in data_by_policy[r.policy]:
                data_by_policy[r.policy][k] = []
            data_by_policy[r.policy][k].append(v)
    db.close()

    # Compute bootstrap CI per metric
    stats_by_policy: Dict[str, Dict[str, Dict[str, float]]] = {}
    for pol, metrics in data_by_policy.items():
        stats_by_policy[pol] = {}
        for m_name, values in metrics.items():
            pt, lo, hi = bootstrap_ci(values, n=2000, seed=42)
            stats_by_policy[pol][m_name] = {
                "mean": pt,
                "ci_lower": lo,
                "ci_upper": hi,
                "count": len(values),
                "std": float(np.std(values)) if len(values) > 1 else 0.0,
            }

    return {
        "exp_id": exp_id,
        "stats": stats_by_policy,
        "raw": data_by_policy,
        "total_runs": len(all_runs),
        "valid_runs": len(valid_runs),
        "exclusions": exclusions,
    }


def print_comparison_table(scenario_name: str, analysis: Dict[str, Any]):
    stats = analysis["stats"]
    exclusions = analysis.get("exclusions", [])
    total_runs = analysis.get("total_runs", 0)
    valid_runs = analysis.get("valid_runs", 0)

    print("\n" + "=" * 140)
    print(f"  SCENARIO EVALUATION RESULTS: {scenario_name}")
    print("  (Mean with 95% Bootstrap CI over repeats; queried strictly from SQLite DB rows)")
    if exclusions:
        print(f"  [RUN AUDIT] Exclusions / Invalid Runs: {len(exclusions)} of {total_runs} excluded:")
        for ex in exclusions:
            print(f"    - Run {ex['run_id']} ({ex['policy'].upper()} rep {ex['repeat']}): {ex['reason']}")
    else:
        print(f"  [RUN AUDIT] All {valid_runs} runs valid (host reservation <= 25%, steal <= 5.0%, OOM kills == 0)")
    print("=" * 140)
    print(
        f"{'Policy':<18} | {'Jain (CALC)':<20} | {'Satisfaction (CALC)':<22} | {'Unmet (Throt|Miss)':<20} | {'Idle CPU (CALC)':<16} | {'Consumed CPU':<20} | {'Throughput':<18} | {'Solve Time':<24}"
    )
    print("-" * 140)

    from alloc_platform.config import config
    epoch_ms = config.epoch_interval_s * 1000.0

    for pol, m_dict in stats.items():
        jain = m_dict.get("jain_fairness_primary", {"mean": 0.0, "ci_lower": 0.0, "ci_upper": 0.0})
        satis = m_dict.get("mean_satisfaction", {"mean": 0.0, "ci_lower": 0.0, "ci_upper": 0.0})
        throt = m_dict.get("throttle_time_ratio", {"mean": 0.0, "ci_lower": 0.0, "ci_upper": 0.0})
        miss = m_dict.get("deadline_miss_rate", {"mean": 0.0, "ci_lower": 0.0, "ci_upper": 0.0})
        idle_c = m_dict.get("idle_reserved_cpu", {"mean": 0.0, "ci_lower": 0.0, "ci_upper": 0.0})
        cons = m_dict.get("consumed_util_cpu", {"mean": 0.0, "ci_lower": 0.0, "ci_upper": 0.0})
        tp = m_dict.get("throughput", {"mean": 0.0, "ci_lower": 0.0, "ci_upper": 0.0})
        solve = m_dict.get("solve_ms_avg", {"mean": 0.0, "ci_lower": 0.0, "ci_upper": 0.0})

        # Deterministic formula-based metrics for baselines: show as exact formula properties without CI
        is_deterministic_baseline = pol.lower() in ("fcfs", "proportional", "drf", "wp")
        if is_deterministic_baseline:
            jain_str = f"{jain['mean']:.3f} (CALCULATED)"
            satis_str = f"{satis['mean']:.3f} (CALCULATED)"
        else:
            jain_str = f"{jain['mean']:.3f} [{jain['ci_lower']:.3f}, {jain['ci_upper']:.3f}]"
            satis_str = f"{satis['mean']:.3f} [{satis['ci_lower']:.3f}, {satis['ci_upper']:.3f}]"

        unmet_str = f"{throt['mean']*100:.1f}% | {miss['mean']*100:.1f}%"
        idle_str = f"{idle_c['mean']:.2f} cores"
        cons_str = f"{cons['mean']*100:.1f}% [{cons['ci_lower']*100:.1f}%, {cons['ci_upper']*100:.1f}%]"
        tp_str = f"{tp['mean']:.2f} [{tp['ci_lower']:.2f}, {tp['ci_upper']:.2f}] req/s"
        solve_pct = (solve['mean'] / max(1.0, epoch_ms)) * 100.0
        solve_str = f"{solve['mean']:.2f} ms ({solve_pct:.2f}% epoch)"

        print(
            f"{pol.upper():<18} | {jain_str:<20} | {satis_str:<22} | {unmet_str:<20} | {idle_str:<16} | {cons_str:<20} | {tp_str:<18} | {solve_str:<24}"
        )

    print("-" * 140)

    # Relative comparison vs baselines (Item 7 & A6: no relative solve overhead percentage)
    if "adaptive" in stats:
        print("  RELATIVE PERCENTAGE DIFFERENCE (Adaptive vs Baselines, including trade-offs & losses):")
        ad_jain = stats["adaptive"].get("jain_fairness_primary", {}).get("mean", 0.0)
        ad_satis = stats["adaptive"].get("mean_satisfaction", {}).get("mean", 0.0)
        ad_tp = stats["adaptive"].get("throughput", {}).get("mean", 0.0)
        ad_solve = stats["adaptive"].get("solve_ms_avg", {}).get("mean", 0.0)
        ad_solve_pct = (ad_solve / max(1.0, epoch_ms)) * 100.0

        for base in ["fcfs", "proportional", "drf"]:
            if base in stats:
                b_jain = stats[base].get("jain_fairness_primary", {}).get("mean", 0.0)
                b_satis = stats[base].get("mean_satisfaction", {}).get("mean", 0.0)
                b_tp = stats[base].get("throughput", {}).get("mean", 0.0)

                d_jain = pct_change(ad_jain, b_jain)
                d_satis = pct_change(ad_satis, b_satis)
                d_tp = pct_change(ad_tp, b_tp)

                print(
                    f"  vs {base.upper():<12}: Satisfaction: {d_satis:>+6.1f}% | Jain: {d_jain:>+6.1f}% | Throughput: {d_tp:>+6.1f}%"
                )

        print(f"  Adaptive Solver Latency: {ad_solve:.2f} ms ({ad_solve_pct:.2f}% of {config.epoch_interval_s:.1f}s epoch)")

    print("=" * 140)


async def main():
    import argparse

    parser = argparse.ArgumentParser(description="Rigorous Evaluation Suite")
    parser.add_argument(
        "--scenario",
        type=str,
        default=None,
        help="Specific scenario ID to run (e.g. scenario_1_baseline, scenario_2_burst, scenario_3_capacity_cut, scenario_4_churn)",
    )
    parser.add_argument("--repeats", type=int, default=5, help="Number of repeats per policy (default: 5)")
    parser.add_argument("--ablations", action="store_true", help="Run Adaptive ablation study on Scenario 2")
    parser.add_argument("--analyze-only", type=str, default=None, help="Analyze and print existing experiment ID")
    args = parser.parse_args()

    if args.analyze_only:
        analysis = analyze_experiment(args.analyze_only)
        print_comparison_table(f"Experiment {args.analyze_only}", analysis)
        return

    print("=" * 120)
    print("         ADAPTIVE PLATFORM COMPREHENSIVE EMPIRICAL EVALUATION SUITE")
    print(f"         Repeats: {args.repeats} per policy | Noise Floor Included | Strict DB Provenance")
    print("=" * 120)

    scenarios = get_predefined_scenarios()
    policies_core = ["fcfs", "proportional", "drf", "adaptive", "adaptive_noise"]

    if args.ablations:
        print("\n" + "=" * 120)
        print("  RUNNING ADAPTIVE ABLATIONS ON SCENARIO 2 (BURST)")
        print("=" * 120)
        ablation_policies = ["adaptive", "adaptive_no_est", "adaptive_no_hyst", "adaptive_no_surplus"]
        abl_exp_id = await run_scenario_benchmark(
            "scenario_2_burst",
            ablation_policies,
            repeats=args.repeats,
            exp_name="Adaptive Ablation Study: Burst Workload",
        )
        abl_analysis = analyze_experiment(abl_exp_id)
        print_comparison_table("Adaptive Ablation Study (Scenario 2 Burst)", abl_analysis)
        return

    scen_keys = [args.scenario] if args.scenario else [
        "scenario_1_baseline",
        "scenario_2_burst",
        "scenario_3_capacity_cut",
        "scenario_4_churn",
    ]

    for sk in scen_keys:
        if sk in scenarios:
            exp_id = await run_scenario_benchmark(sk, policies_core, repeats=args.repeats)
            analysis = analyze_experiment(exp_id)
            print_comparison_table(scenarios[sk].name, analysis)

    print("\nEvaluation completed successfully. All data queried from SQLite DB rows.")


if __name__ == "__main__":
    asyncio.run(main())

