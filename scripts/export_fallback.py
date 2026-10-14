"""Exports database runs and raw metrics to a portable JSON fallback bundle."""

import json
import os
import sys
from datetime import datetime

# Add project root to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from alloc_platform.db.models import (
    Experiment,
    Run,
    RunMetric,
)
from alloc_platform.db.session import SessionLocal, init_db


def export_bundle(
    output_path: str = "data/recorded_fallback.json", experiment_id: str | None = None
) -> None:
    init_db()
    db = SessionLocal()
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

    try:
        if experiment_id:
            experiments = db.query(Experiment).filter(Experiment.id == experiment_id).all()
        else:
            all_exps = db.query(Experiment).all()
            experiments = [e for e in all_exps if e.id == "faculty_demo_measured"] or all_exps
        exp_list = []
        for e in experiments:
            runs = db.query(Run).filter(Run.experiment_id == e.id).all()
            run_list = []
            for r in runs:

                metric_rows = db.query(RunMetric).filter(RunMetric.run_id == r.id).all()
                metrics = {m.metric_name: m.metric_value for m in metric_rows}
                # Determine provenance from metrics or default to SIMULATED
                run_prov = "MEASURED" if (metric_rows and all(m.provenance == "MEASURED" for m in metric_rows)) else "SIMULATED"
                run_list.append(
                    {
                        "id": r.id,
                        "policy": r.policy,
                        "repeat_index": r.repeat_index,
                        "status": r.status,
                        "is_valid": r.is_valid,
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
            "account_label": "481374029754",
            "region": "ap-south-1",
            "provenance": bundle_prov,
            "experiments": exp_list,
        }

        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(bundle, f, indent=2)

        print(
            f"Fallback bundle successfully exported to {output_path} ({len(exp_list)} experiments)"
        )
    finally:
        db.close()


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "data/recorded_fallback.json"
    exp_id = sys.argv[2] if len(sys.argv) > 2 else "faculty_demo_measured"
    export_bundle(out, exp_id)

