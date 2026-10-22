"""Coarse CPU Parameter Calibration for SAHA (Stage 6 Extension).

Evaluates the 5 CPU/mode tunables across calibration seeds 101, 102, 103 in simulation.
Seeds 101, 102, 103 are strictly disjoint from confirmatory benchmark seeds (42-46).
Memory safety parameters are structural and fixed (never searched in simulation).
"""

import asyncio
import json
import logging
import os
import sys
import time
from typing import Dict, List

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from alloc_platform.allocator.saha import SahaAllocator, SahaParams
from alloc_platform.config import config
from alloc_platform.db.models import Base, Experiment, PoolModel, Run, RunMetric
from alloc_platform.orchestrator.runner import _execute_single_run
from alloc_platform.orchestrator.scenario import get_predefined_scenarios
from alloc_platform.telemetry.host import HostMonitor

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("calibrate_saha_cpu")


def run_calibration_grid():
    print("=" * 80)
    print("SAHA COARSE CPU PARAMETER CALIBRATION (SIMULATION GRID)")
    print("Seeds: 101, 102, 103 (strictly disjoint from confirmatory seeds 42-46)")
    print("Scenarios: scenario_1_baseline, scenario_2_burst, scenario_over_declaration")
    print("=" * 80)

    # Coarse grid of representative parameter tuples:
    # (ewma_alpha, headroom_h, throttle_step, rho_low, rho_high)
    candidate_configs = [
        # Baseline / default candidate
        {"name": "C1_Baseline_Default", "ewma_alpha": 0.30, "headroom_h": 0.20, "throttle_step": 1.25, "rho_low": 0.85, "rho_high": 1.00},
        # Conservative (lower headroom, slower throttle step, lower switching bounds)
        {"name": "C2_Conservative", "ewma_alpha": 0.15, "headroom_h": 0.10, "throttle_step": 1.15, "rho_low": 0.80, "rho_high": 0.95},
        # Responsive (higher alpha, higher headroom, aggressive throttle step)
        {"name": "C3_Responsive", "ewma_alpha": 0.50, "headroom_h": 0.30, "throttle_step": 1.35, "rho_low": 0.85, "rho_high": 1.00},
        # High-hysteresis threshold
        {"name": "C4_Wide_Hysteresis", "ewma_alpha": 0.30, "headroom_h": 0.20, "throttle_step": 1.25, "rho_low": 0.80, "rho_high": 1.05},
        # Low-hysteresis threshold
        {"name": "C5_Narrow_Hysteresis", "ewma_alpha": 0.30, "headroom_h": 0.20, "throttle_step": 1.25, "rho_low": 0.90, "rho_high": 1.00},
    ]

    calib_seeds = [101, 102, 103]
    scenarios_to_test = ["scenario_1_baseline", "scenario_2_burst", "scenario_over_declaration"]

    results = []

    # Fast in-memory execution: set epoch interval to 0.0 for simulation
    config.epoch_interval_s = 0.0

    for cand in candidate_configs:
        cand_runs = []
        print(f"\n=======================================================")
        print(f"Evaluating Candidate: {cand['name']}")
        print(f"  Target Params: alpha={cand['ewma_alpha']}, h={cand['headroom_h']}, step={cand['throttle_step']}, rho_low={cand['rho_low']}, rho_high={cand['rho_high']}")
        print(f"=======================================================")

        for seed in calib_seeds:
            for scen_name in scenarios_to_test:
                # Apply candidate parameters to global single-source config
                config.saha_ewma_alpha = cand["ewma_alpha"]
                config.saha_headroom_h = cand["headroom_h"]
                config.saha_throttle_step = cand["throttle_step"]
                config.saha_rho_low = cand["rho_low"]
                config.saha_rho_high = cand["rho_high"]

                # Verify dynamic SahaParams bindings
                active_params = SahaParams.from_config()
                print(
                    f"  [RUN LOG] Cand={cand['name']:<22} Seed={seed} Scen={scen_name:<26} | "
                    f"Effective SahaParams(alpha={active_params.ewma_alpha:.2f}, h={active_params.headroom_h:.2f}, "
                    f"step={active_params.throttle_step:.2f}, rho_low={active_params.rho_low:.2f}, rho_high={active_params.rho_high:.2f})"
                )

                # Setup in-memory isolated DB
                engine = create_engine("sqlite:///:memory:")
                Base.metadata.create_all(engine)
                db = sessionmaker(bind=engine)()

                exp_id = f"exp_calib_{cand['name']}_{seed}_{scen_name}"
                run_id = f"run_{cand['name']}_{seed}_{scen_name}"

                db.add(PoolModel(id="default_pool", name="Default Pool", cpu_cores=config.default_pool_cpu, mem_mb=config.default_pool_mem_mb))
                db.add(Experiment(id=exp_id, name=f"Calib {cand['name']}", scenario_id=scen_name, status="running", policies_json='["saha"]', repeats=1))
                db.add(Run(id=run_id, experiment_id=exp_id, policy="saha", repeat_index=0, status="pending", is_valid=True))
                db.commit()

                scen = get_predefined_scenarios()[scen_name]
                scen.seed = seed
                scen.duration_s = 30  # 30-epoch simulation duration

                cancel_event = asyncio.Event()
                host_mon = HostMonitor()

                asyncio.run(_execute_single_run(
                    db=db,
                    run_id=run_id,
                    policy="saha",
                    scen_spec=scen,
                    enforcer=None,  # Pure simulation mode
                    host_mon=host_mon,
                    cancel_event=cancel_event,
                ))

                metrics_rows = db.query(RunMetric).filter(RunMetric.run_id == run_id).all()
                m_dict = {m.metric_name: m.metric_value for m in metrics_rows}
                cand_runs.append({
                    "seed": seed,
                    "scenario": scen_name,
                    "on_time_ratio": m_dict.get("on_time_ratio", 0.0),
                    "throughput": m_dict.get("throughput", 0.0),
                    "p95_latency_ms": m_dict.get("p95_latency_ms", 0.0),
                    "rejected": m_dict.get("rejected", 0.0),
                    "throttled_while_spare_ratio": m_dict.get("throttled_while_spare_ratio", 0.0),
                    "effective_params": {
                        "ewma_alpha": active_params.ewma_alpha,
                        "headroom_h": active_params.headroom_h,
                        "throttle_step": active_params.throttle_step,
                        "rho_low": active_params.rho_low,
                        "rho_high": active_params.rho_high,
                    },
                })
                db.close()

        # Compute separate H5 metrics (Scenarios 1 and 2 ONLY) vs H7 (over-declaration)
        h5_runs = [r for r in cand_runs if r["scenario"] in ("scenario_1_baseline", "scenario_2_burst")]
        h7_runs = [r for r in cand_runs if r["scenario"] == "scenario_over_declaration"]

        h5_on_time = sum(m["on_time_ratio"] for m in h5_runs) / len(h5_runs)
        h5_tp = sum(m["throughput"] for m in h5_runs) / len(h5_runs)
        h5_p95 = sum(m["p95_latency_ms"] for m in h5_runs) / len(h5_runs)
        h5_spare_throttle = sum(m["throttled_while_spare_ratio"] for m in h5_runs) / len(h5_runs)

        h7_on_time = sum(m["on_time_ratio"] for m in h7_runs) / len(h7_runs)
        h7_tp = sum(m["throughput"] for m in h7_runs) / len(h7_runs)
        h7_p95 = sum(m["p95_latency_ms"] for m in h7_runs) / len(h7_runs)

        print(f"\nSummary for {cand['name']}:")
        print(f"  H5 Metric Set (Scenarios 1 & 2 ONLY):")
        print(f"    On-Time Ratio: {h5_on_time*100:.2f}% (Threshold: >= 70%)")
        print(f"    Throttled-While-Spare: {h5_spare_throttle*100:.2f}% (Cap: <= 5%)")
        print(f"    Throughput: {h5_tp:.1f} req/s | p95 Latency: {h5_p95:.1f} ms")
        print(f"  H7 Metric Set (Scenario Over-Declaration):")
        print(f"    On-Time Ratio: {h7_on_time*100:.2f}% (Adaptive win preservation)")
        print(f"    Throughput: {h7_tp:.1f} req/s | p95 Latency: {h7_p95:.1f} ms")

        results.append({
            "candidate": cand,
            "h5_metrics": {
                "mean_on_time_ratio": h5_on_time,
                "mean_throughput": h5_tp,
                "mean_p95_latency_ms": h5_p95,
                "mean_throttled_while_spare_ratio": h5_spare_throttle,
            },
            "h7_metrics": {
                "mean_on_time_ratio": h7_on_time,
                "mean_throughput": h7_tp,
                "mean_p95_latency_ms": h7_p95,
            },
            "runs": cand_runs,
        })

    # Reset config back to default
    config.saha_ewma_alpha = 0.30
    config.saha_headroom_h = 0.20
    config.saha_throttle_step = 1.25
    config.saha_rho_low = 0.85
    config.saha_rho_high = 1.00
    config.epoch_interval_s = 1.0

    # Save calibration results
    out_dir = os.path.join(os.path.dirname(__file__), "..", "data")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "calibration_results_stage6.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)

    print("\n" + "=" * 80)
    print(f"Calibration complete! Results saved to {out_path}")
    print("=" * 80)


if __name__ == "__main__":
    run_calibration_grid()
