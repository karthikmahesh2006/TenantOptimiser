"""Inspect recorded fallback bundle."""

import json
import sys

def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "data/recorded_fallback.json"
    with open(path, "r", encoding="utf-8") as f:
        d = json.load(f)
    print("Recording Date:", d.get("recording_date"))
    print("Provenance:", d.get("provenance"))
    print("Instance Type:", d.get("instance_type"))
    exps = d.get("experiments", [])
    print(f"Experiments ({len(exps)}):")
    total_runs = 0
    all_dates = []
    for e in exps:
        runs = e.get("runs", [])
        total_runs += len(runs)
        c_at = e.get("created_at")
        if c_at:
            all_dates.append(c_at.split("T")[0])
        print(f"  - {e.get('id')} ({e.get('scenario_id')}): {len(runs)} runs, created_at={c_at}")
    print(f"Total runs across all experiments: {total_runs}")
    if all_dates:
        min_date = min(all_dates)
        max_date = max(all_dates)
        date_range = min_date if min_date == max_date else f"{min_date} to {max_date}"
        print(f"Derived date range: {date_range}")


if __name__ == "__main__":
    main()
