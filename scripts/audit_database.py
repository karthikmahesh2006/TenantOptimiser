"""Audit all experiments, runs, and invalidations in the database."""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from alloc_platform.db.models import Experiment, Run, RunMetric
from alloc_platform.db.session import SessionLocal, init_db


def audit():
    init_db()
    db = SessionLocal()

    exps = db.query(Experiment).all()
    print(f"Total experiments: {len(exps)}")

    for exp in exps:
        runs = db.query(Run).filter(Run.experiment_id == exp.id).all()
        valid_runs = [r for r in runs if r.is_valid]
        invalid_runs = [r for r in runs if not r.is_valid]
        print(f"\nExp ID: {exp.id} | Scenario: {exp.scenario_id} | Status: {exp.status} | Total Runs: {len(runs)} (Valid: {len(valid_runs)}, Invalid: {len(invalid_runs)})")
        if invalid_runs:
            for r in invalid_runs:
                print(f"  [INVALID] Run {r.id} | Policy: {r.policy} | Rep: {r.repeat_index} | Reason: {r.invalidation_reason}")

        # Check OOM kills recorded in metrics
        policy_runs = {}
        for r in runs:
            policy_runs.setdefault(r.policy, []).append(r)
        for pol, prs in policy_runs.items():
            oom_counts = []
            for r in prs:
                m_oom = db.query(RunMetric).filter(RunMetric.run_id == r.id, RunMetric.metric_name == "oom_kills").first()
                if m_oom:
                    oom_counts.append(m_oom.metric_value)
            total_oom = sum(oom_counts) if oom_counts else 0
            print(f"  Policy {pol:15s}: {len(prs)} runs | Total OOMs recorded: {total_oom}")

    db.close()


if __name__ == "__main__":
    audit()
