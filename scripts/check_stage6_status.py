#!/usr/bin/env python3
"""Stage 6 Detached Execution Status Checker.

Runnable from any machine with AWS credentials (read-only):
- Queries EC2 instance state and lifecycle
- Asserts Security Group inbound rule count (must be 0)
- Fetches and displays progress.json and done.json from S3 under backups/stage6/<commit12>/
- Counts completed <run_id>.json files in S3
- Displays pinned commit hash and execution log availability
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
from typing import Any, Dict, List, Optional


def load_state() -> Dict[str, str]:
    """Loads state from infra/state/resources.env if present."""
    state_file = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "infra", "state", "resources.env")
    )
    state: Dict[str, str] = {}
    if os.path.exists(state_file):
        with open(state_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    state[k.strip()] = v.strip()
    return state


def run_aws_json(cmd: List[str]) -> Optional[Any]:
    """Runs an AWS CLI command and parses JSON output, returning None on error."""
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return json.loads(res.stdout) if res.stdout.strip() else None
    except Exception:
        return None


def fetch_s3_json(bucket: str, s3_key: str, region: str) -> Optional[Dict[str, Any]]:
    """Fetches a JSON file from S3 into memory."""
    s3_uri = f"s3://{bucket}/{s3_key}"
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".json") as f:
        tmp_name = f.name
    try:
        cmd = ["aws", "s3", "cp", s3_uri, tmp_name, "--region", region]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode == 0 and os.path.exists(tmp_name) and os.path.getsize(tmp_name) > 0:
            with open(tmp_name, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    finally:
        if os.path.exists(tmp_name):
            os.remove(tmp_name)
    return None


def list_s3_run_files(bucket: str, s3_prefix: str, region: str) -> List[str]:
    """Lists all run_*.json files under the S3 prefix."""
    s3_uri = f"s3://{bucket}/{s3_prefix.rstrip('/')}/"
    cmd = ["aws", "s3", "ls", s3_uri, "--region", region]
    run_files = []
    try:
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode == 0:
            for line in res.stdout.splitlines():
                parts = line.strip().split()
                if len(parts) >= 4:
                    fname = parts[3]
                    if fname.startswith("run_") and fname.endswith(".json"):
                        run_files.append(fname)
    except Exception:
        pass
    return sorted(run_files)


def check_status(
    instance_id: Optional[str] = None,
    bucket: Optional[str] = None,
    s3_prefix: Optional[str] = None,
    region: Optional[str] = None,
    sg_id: Optional[str] = None,
    commit_hash: Optional[str] = None,
) -> None:
    state = load_state()
    region = region or state.get("REGION", "ap-south-1")
    bucket = bucket or state.get("BUCKET", "alloc-platform-481374029754-ap-south-1")
    sg_id = sg_id or state.get("SG_ID")

    print("============================================================")
    print(" STAGE 6 DETACHED EXECUTION STATUS")
    print("============================================================")
    print(f"Region:     {region}")
    print(f"S3 Bucket:  {bucket}")

    # 1. Instance Details
    inst_sg_id = sg_id
    inst_commit = commit_hash
    if instance_id:
        print(f"\n--- [1] EC2 Instance: {instance_id} ---")
        inst_info = run_aws_json([
            "aws", "ec2", "describe-instances",
            "--instance-ids", instance_id,
            "--region", region,
            "--query", "Reservations[0].Instances[0]",
            "--output", "json"
        ])
        if inst_info:
            inst_state = inst_info.get("State", {}).get("Name", "unknown")
            launch_time = inst_info.get("LaunchTime", "unknown")
            tags = {t["Key"]: t["Value"] for t in inst_info.get("Tags", [])}
            sgs = [g.get("GroupId") for g in inst_info.get("SecurityGroups", [])]
            if sgs:
                inst_sg_id = sgs[0]
            if not inst_commit and "Commit" in tags:
                inst_commit = tags["Commit"]

            print(f"  State:       {inst_state}")
            print(f"  Launch Time: {launch_time}")
            print(f"  Tags:        {tags}")
            print(f"  Security Grp:{sgs}")
        else:
            print(f"  Could not describe instance {instance_id} (may already be terminated or not found).")
    else:
        print("\n--- [1] EC2 Instance: Not specified (querying S3 only) ---")

    # 2. Determine exact S3 prefix
    if s3_prefix:
        prefix = s3_prefix.rstrip("/")
    else:
        base_prefix = "backups/stage6"
        if inst_commit:
            commit12 = inst_commit[:12]
            prefix = f"{base_prefix}/{commit12}"
        else:
            prefix = base_prefix

    print(f"S3 Prefix:  {prefix}")

    # 3. Security Group Zero-Inbound Check
    if inst_sg_id:
        print(f"\n--- [2] Security Group Zero-Inbound Audit: {inst_sg_id} ---")
        sg_info = run_aws_json([
            "aws", "ec2", "describe-security-groups",
            "--group-ids", inst_sg_id,
            "--region", region,
            "--query", "SecurityGroups[0].IpPermissions",
            "--output", "json"
        ])
        if sg_info is not None:
            rule_count = len(sg_info)
            if rule_count == 0:
                print(f"  PASS: Security group {inst_sg_id} has exactly 0 inbound rules (zero-inbound verified).")
            else:
                print(f"  FAIL: SECURITY VIOLATION: Security group has {rule_count} inbound rules!")
        else:
            print(f"  Could not query security group {inst_sg_id}.")

    # 4. S3 Progress and Run Files
    print(f"\n--- [3] S3 Execution Artifacts (s3://{bucket}/{prefix}/) ---")

    run_files = list_s3_run_files(bucket, prefix, region)
    print(f"  Completed Run Files in S3: {len(run_files)} files")
    for rf in run_files[:5]:
        print(f"    - {rf}")
    if len(run_files) > 5:
        print(f"    ... and {len(run_files) - 5} more")

    progress = fetch_s3_json(bucket, f"{prefix}/progress.json", region)
    if progress:
        print(f"\n  Latest Progress (progress.json):")
        print(f"    Done/Total:  {progress.get('done')}/{progress.get('total')} runs")
        print(f"    Invalid:     {progress.get('invalid')} runs")
        print(f"    Last Run ID: {progress.get('last_run_id')}")
        print(f"    Preset:      {progress.get('preset')}")
        print(f"    Commit:      {progress.get('commit_hash')}")
        print(f"    Timestamp:   {progress.get('timestamp')}")
    else:
        print("  progress.json not found in S3 (run may be starting, initializing, or prefix not yet written).")

    done = fetch_s3_json(bucket, f"{prefix}/done.json", region)
    if done:
        print(f"\n  Completion Status (done.json):")
        print(f"    Status:      {done.get('status')}")
        print(f"    Runs:        {done.get('run_count')}/{done.get('total_planned')}")
        print(f"    Invalid:     {done.get('invalid_count')}")
        print(f"    Reason:      {done.get('reason')}")
        print(f"    Commit:      {done.get('commit_hash')}")
        print(f"    Preset:      {done.get('preset')}")
        print(f"    Timestamp:   {done.get('timestamp')}")
    else:
        print("  done.json not found in S3 (execution still in progress or not yet completed).")

    print("============================================================\n")


def main():
    parser = argparse.ArgumentParser(description="Check Stage 6 Detached Execution Status")
    parser.add_argument("--instance-id", type=str, help="EC2 Instance ID to check.")
    parser.add_argument("--commit-hash", type=str, help="Commit hash to inspect under backups/stage6/<commit12>/.")
    parser.add_argument("--bucket", type=str, help="S3 bucket name.")
    parser.add_argument("--s3-prefix", type=str, default=None, help="S3 artifact prefix override.")
    parser.add_argument("--region", type=str, help="AWS region.")
    parser.add_argument("--sg-id", type=str, help="Security Group ID to check.")
    args = parser.parse_args()

    check_status(
        instance_id=args.instance_id,
        bucket=args.bucket,
        s3_prefix=args.s3_prefix,
        region=args.region,
        sg_id=args.sg_id,
        commit_hash=args.commit_hash,
    )


if __name__ == "__main__":
    main()
