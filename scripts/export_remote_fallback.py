import json
import os
import sys
from datetime import datetime

sys.path.insert(0, "/opt/alloc/current/src")

from alloc_platform.db.models import Experiment, Run, RunMetric
from alloc_platform.db.session import SessionLocal, init_db


def export_bundle(output_path: str = "/opt/alloc/current/data/recorded_fallback.json") -> None:
    init_db()
    db = SessionLocal()
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

    try:
        experiments = (
            db.query(Experiment)
            .filter(Experiment.status.in_(["done", "completed"]))
            .all()
        )
        exp_list = []
        for e in experiments:
            runs = db.query(Run).filter(Run.experiment_id == e.id).all()
            run_list = []
            for r in runs:
                metric_rows = db.query(RunMetric).filter(RunMetric.run_id == r.id).all()
                metrics = {m.metric_name: m.metric_value for m in metric_rows}
                run_prov = (
                    "MEASURED"
                    if (
                        metric_rows
                        and all(m.provenance in ("MEASURED", "CALCULATED") for m in metric_rows)
                        and any(m.provenance == "MEASURED" for m in metric_rows)
                    )
                    else "SIMULATED"
                )
                is_oom = bool(
                    metrics.get("oom_kills", 0.0) > 0
                    or (r.invalidation_reason and "OOM" in r.invalidation_reason)
                )
                run_list.append(
                    {
                        "id": r.id,
                        "policy": r.policy,
                        "repeat_index": r.repeat_index,
                        "status": r.status,
                        "is_valid": r.is_valid,
                        "is_oom": is_oom,
                        "invalidation_reason": r.invalidation_reason,
                        "provenance": run_prov,
                        "metrics": metrics,
                    }
                )
            exp_list.append(
                {
                    "id": e.id,
                    "name": e.name,
                    "scenario_id": e.scenario_id,
                    "policies": json.loads(e.policies_json),
                    "repeats": e.repeats,
                    "status": e.status,
                    "created_at": e.created_at.isoformat(),
                    "completed_at": e.completed_at.isoformat() if e.completed_at else None,
                    "runs": run_list,
                }
            )

        all_runs = [r for exp in exp_list for r in exp["runs"]]
        bundle_prov = "MEASURED" if (all_runs and all(r["provenance"] == "MEASURED" for r in all_runs)) else "SIMULATED"

        bundle = {
            "recording_date": datetime.utcnow().isoformat(),
            "instance_type": "c7i-flex.large",
            "account_label": "aws-account-placeholder",
            "region": "ap-south-1",
            "run_count": len(all_runs),
            "provenance": bundle_prov,
            "experiments": exp_list,
        }

        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(bundle, f, indent=2)

        oom_runs = [r for r in all_runs if r.get("is_oom")]
        print(f"Fallback bundle successfully exported to {output_path} ({len(exp_list)} experiments, {len(all_runs)} runs)")
        print(f"  Flagged OOM runs: {len(oom_runs)} runs -> {[r['id'] for r in oom_runs]}")
        print(f"  Provenance: {bundle_prov}")
    finally:
        db.close()


if __name__ == "__main__":
    export_bundle()
