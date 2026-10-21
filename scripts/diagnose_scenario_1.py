"""Diagnostic execution and trace extraction for Scenario 1 Adaptive run."""

import asyncio
import json
import os
import sys
import uuid

# Ensure root in path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from alloc_platform.config import config
from alloc_platform.db.models import Experiment, PoolModel, Run, RunMetric, ScenarioModel, TenantSample
from alloc_platform.db.repo import upsert_pool
from alloc_platform.db.session import SessionLocal, init_db
from alloc_platform.orchestrator.runner import execute_experiment_async
from alloc_platform.orchestrator.scenario import get_predefined_scenarios


async def run_scenario_1_diagnosis():
    init_db()
    db = SessionLocal()

    # Ensure default_pool exists
    if not db.query(PoolModel).filter(PoolModel.id == "default_pool").first():
        upsert_pool(
            db,
            "default_pool",
            "Host Primary Pool",
            cpu=config.default_pool_cpu,
            mem_mb=config.default_pool_mem_mb,
        )

    # Ensure predefined scenarios exist
    scenarios = get_predefined_scenarios()
    for sc_id, sc in scenarios.items():
        if not db.query(ScenarioModel).filter(ScenarioModel.id == sc_id).first():
            db.add(
                ScenarioModel(
                    id=sc_id,
                    name=sc.name,
                    duration_s=sc.duration_s,
                    seed=sc.seed,
                    pool_id="default_pool",
                    spec_json=json.dumps({"name": sc.name}),
                )
            )
    db.commit()

    exp_id = f"diag_s1_{uuid.uuid4().hex[:6]}"
    exp = Experiment(
        id=exp_id,
        name="Scenario 1 Bounded Diagnosis",
        scenario_id="scenario_1_baseline",
        policies_json=json.dumps(["adaptive"]),
        repeats=1,
        status="pending",
    )
    db.add(exp)
    db.commit()
    db.close()

    print(f"=== [1/3] Executing Scenario 1 Adaptive Run on EC2 ({exp_id}) ===")
    await execute_experiment_async(exp_id)

    # Find the run
    db = SessionLocal()
    run = db.query(Run).filter(Run.experiment_id == exp_id, Run.policy == "adaptive").first()
    if not run:
        print("ERROR: Run not found in database!")
        return

    print(f"\n=== [2/3] Run Complete: {run.id} (Status: {run.status}, Valid: {run.is_valid}) ===")

    # Load diagnostic JSON if dumped
    diag_file = os.path.join(config.data_dir, "diagnostics", f"{run.id}.json")
    diag_data = []
    if os.path.exists(diag_file):
        with open(diag_file, "r", encoding="utf-8") as f:
            diag_data = json.load(f)

    # Load tenant samples from DB
    samples = db.query(TenantSample).filter(TenantSample.run_id == run.id).order_by(TenantSample.timestamp_s.asc()).all()
    samples_by_ep = {}
    for s in samples:
        ep = int(s.timestamp_s)
        if ep not in samples_by_ep:
            samples_by_ep[ep] = {}
        samples_by_ep[ep][s.tenant_id] = {
            "usage_cpu": s.cpu_cores_used,
            "throttle_ratio": s.throttle_ratio,
        }

    # Print the detailed diagnosis table
    print("\n" + "=" * 125)
    print("SCENARIO 1 ADAPTIVE PER-EPOCH DIAGNOSTIC TRACE: CPU-HEAVY TENANT & POOL TOTALS")
    print("=" * 125)
    print(
        f"{'Epoch':>5} | {'Alloc (c)':>9} | {'Usage (c)':>9} | {'Throt %':>7} | "
        f"{'dhat (c)':>8} | {'s_i':>6} | {'Sum dhat':>8} | {'Pool Cap':>8} | "
        f"{'Mix Alloc':>9} | {'Mix dhat':>8} | {'Mem Alloc':>9} | {'Mem dhat':>8}"
    )
    print("-" * 125)

    for i, d in enumerate(diag_data):
        ep = d.get("epoch", i)
        t_cpu_obs = d.get("obs", {}).get("tenant_cpu", {})
        u_cpu = t_cpu_obs.get("usage", {}).get("cpu", 0.0)
        throt = t_cpu_obs.get("throttle_ratio", 0.0) * 100.0

        d_cpu = d.get("dhat", {}).get("tenant_cpu", {}).get("cpu", 0.0)
        s_i_cpu = d.get("solver_s", {}).get("tenant_cpu", 0.0)
        final_cpu = d.get("final_alloc", {}).get("tenant_cpu", {}).get("cpu", 0.0)

        sum_dhat = d.get("sum_dhat_cpu", 0.0)
        pool_cap = d.get("pool_cpu", 1.50)

        mix_alloc = d.get("final_alloc", {}).get("tenant_mix", {}).get("cpu", 0.0)
        mix_dhat = d.get("dhat", {}).get("tenant_mix", {}).get("cpu", 0.0)

        mem_alloc = d.get("final_alloc", {}).get("tenant_mem", {}).get("cpu", 0.0)
        mem_dhat = d.get("dhat", {}).get("tenant_mem", {}).get("cpu", 0.0)

        print(
            f"{ep:5.0f} | {final_cpu:9.4f} | {u_cpu:9.4f} | {throt:6.1f}% | "
            f"{d_cpu:8.4f} | {s_i_cpu:6.4f} | {sum_dhat:8.4f} | {pool_cap:8.4f} | "
            f"{mix_alloc:9.4f} | {mix_dhat:8.4f} | {mem_alloc:9.4f} | {mem_dhat:8.4f}"
        )

    # Print summary metrics from RunMetric
    metrics = db.query(RunMetric).filter(RunMetric.run_id == run.id).all()
    print("\n=== [3/3] RUN SUMMARY METRICS ===")
    for m in sorted(metrics, key=lambda x: x.metric_name):
        print(f"  {m.metric_name} [{m.provenance}]: {m.metric_value:.4f}")

    db.close()


if __name__ == "__main__":
    asyncio.run(run_scenario_1_diagnosis())
