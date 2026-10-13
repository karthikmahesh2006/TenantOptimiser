"""Imports a fallback bundle and activates persistent RECORDED REAL RESULTS banner."""

import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from alloc_platform.db.models import Experiment, Run, RunMetric
from alloc_platform.db.session import SessionLocal, init_db


def import_bundle(input_path: str = "data/recorded_fallback.json") -> None:
    init_db()
    if not os.path.exists(input_path):
        print(f"Error: bundle file {input_path} not found.")
        return

    with open(input_path, "r", encoding="utf-8") as f:
        bundle = json.load(f)

    db = SessionLocal()
    try:
        for exp_data in bundle.get("experiments", []):
            existing = db.query(Experiment).filter(Experiment.id == exp_data["id"]).first()
            if not existing:
                e = Experiment(
                    id=exp_data["id"],
                    name=exp_data["name"],
                    scenario_id=exp_data["scenario_id"],
                    policies_json=json.dumps(exp_data["policies"]),
                    repeats=exp_data.get("repeats", 1),
                    status=exp_data["status"],
                )
                db.add(e)
                for r_data in exp_data.get("runs", []):
                    r = Run(
                        id=r_data["id"],
                        experiment_id=exp_data["id"],
                        policy=r_data["policy"],
                        repeat_index=r_data.get("repeat_index", 0),
                        status=r_data["status"],
                        is_valid=r_data.get("is_valid", True),
                    )
                    db.add(r)
                    for k, v in r_data.get("metrics", {}).items():
                        m = RunMetric(
                            run_id=r_data["id"],
                            metric_name=k,
                            metric_value=float(v),
                            provenance="MEASURED",
                        )
                        db.add(m)
        db.commit()

        # Set fallback active flag
        os.makedirs("data", exist_ok=True)
        with open("data/fallback_active.flag", "w") as f:
            f.write(f"Active fallback from {input_path}")

        print(f"Fallback bundle successfully imported from {input_path}. Banner activated.")
    finally:
        db.close()


if __name__ == "__main__":
    inp = sys.argv[1] if len(sys.argv) > 1 else "data/recorded_fallback.json"
    import_bundle(inp)
