"""Stage 6 Ephemeral Confirmatory Benchmark Launcher & Watchdog Harness.

Provisions an ephemeral c7i-flex.large EC2 instance in ap-south-1 under zero-inbound SG rules,
executes the 60-run confirmatory benchmark matrix for Hypotheses H5-H8 (Amendments 1-4),
extracts all database and fallback artifacts to local storage, asserts 0 inbound SG rules,
and terminates the instance within a strict 50-minute hard watchdog budget.
"""

import json
import os
import subprocess
import sys
import time
from typing import Dict, List, Tuple

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from scripts.ssm_run import run_ssm


def load_state() -> Dict[str, str]:
    state_file = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "infra", "state", "resources.env")
    )
    state = {}
    if os.path.exists(state_file):
        with open(state_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    state[k.strip()] = v.strip()
    return state


def verify_security_group_zero_inbound(sg_id: str, region: str) -> int:
    """Verifies that the security group has strictly zero inbound rules."""
    cmd = [
        "aws",
        "ec2",
        "describe-security-groups",
        "--group-ids",
        sg_id,
        "--region",
        region,
        "--query",
        "SecurityGroups[0].IpPermissions",
        "--output",
        "json",
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, check=True)
    inbound_rules = json.loads(res.stdout)
    count = len(inbound_rules)
    if count != 0:
        raise RuntimeError(
            f"SECURITY VIOLATION: Security group {sg_id} has {count} inbound rules (must be 0)!"
        )
    return count


def launch_instance(
    image_id: str,
    instance_type: str,
    subnet_id: str,
    sg_id: str,
    iam_profile: str,
    region: str,
    watchdog_minutes: int = 50,
) -> str:
    """Launches an ephemeral EC2 instance with InstanceInitiatedShutdownBehavior=terminate and a hard watchdog."""
    print(f"=== [1/6] Launching Ephemeral Confirmatory Instance ({instance_type}) ===")
    verify_security_group_zero_inbound(sg_id, region)

    user_data = f"""#!/bin/bash
set -euo pipefail
# Hard watchdog: terminate-on-shutdown enforces budget limit
echo "Setting {watchdog_minutes}-minute hard auto-termination watchdog..."
sudo shutdown -h +{watchdog_minutes} "Stage 6 evaluation budget cap reached"
"""

    cmd = [
        "aws",
        "ec2",
        "run-instances",
        "--image-id",
        image_id,
        "--instance-type",
        instance_type,
        "--subnet-id",
        subnet_id,
        "--security-group-ids",
        sg_id,
        "--iam-instance-profile",
        f"Name={iam_profile}",
        "--instance-initiated-shutdown-behavior",
        "terminate",
        "--user-data",
        user_data,
        "--tag-specifications",
        "ResourceType=instance,Tags=[{Key=Project,Value=alloc-platform},{Key=Name,Value=alloc-stage6-ephemeral}]",
        "--region",
        region,
        "--query",
        "Instances[0].InstanceId",
        "--output",
        "text",
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, check=True)
    instance_id = res.stdout.strip()
    print(f"Instance launched: {instance_id}")
    return instance_id


def wait_for_ssm(instance_id: str, region: str, timeout_s: int = 300) -> None:
    """Polls until the newly launched instance reports Online in AWS SSM."""
    print(f"=== [2/6] Waiting for SSM agent to come online on {instance_id} ===")
    start_t = time.time()
    while time.time() - start_t < timeout_s:
        cmd = [
            "aws",
            "ssm",
            "describe-instance-information",
            "--filters",
            f"Key=InstanceIds,Values={instance_id}",
            "--region",
            region,
            "--query",
            "InstanceInformationList[0].PingStatus",
            "--output",
            "text",
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode == 0 and res.stdout.strip() == "Online":
            print(f"SSM agent is Online ({time.time() - start_t:.1f}s)")
            return
        time.sleep(5)
    raise TimeoutError(f"SSM agent on {instance_id} did not come online within {timeout_s}s")


def extract_artifacts_and_terminate(
    instance_id: str, sg_id: str, bucket: str, region: str, metadata: dict
) -> None:
    """Extracts database and fallback artifacts from instance, asserts 0 SG rules, and initiates termination."""
    print("=== [5/6] Extracting confirmatory run artifacts prior to shutdown ===")
    export_cmd = [
        "python3 -c \"",
        "from scripts.export_remote_fallback import export_bundle;",
        "export_bundle('/opt/alloc/current/data/recorded_fallback_stage6.json');",
        f"import subprocess; subprocess.run(['aws', 's3', 'cp', '/opt/alloc/current/data/recorded_fallback_stage6.json', 's3://{bucket}/artifacts/recorded_fallback_stage6.json', '--region', '{region}'], check=True);",
        f"subprocess.run(['aws', 's3', 'cp', '/opt/alloc/current/alloc.db', 's3://{bucket}/artifacts/alloc_stage6.db', '--region', '{region}'], check=True);",
        "\"",
    ]
    out, err, code = run_ssm(instance_id, [" ".join(export_cmd)], region=region)
    if code != 0:
        print(f"WARNING: SSM export returned error {code}: {err}")

    # Copy down artifacts locally
    local_data_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "data"))
    os.makedirs(local_data_dir, exist_ok=True)
    local_fallback = os.path.join(local_data_dir, "recorded_fallback_stage6.json")
    subprocess.run(
        ["aws", "s3", "cp", f"s3://{bucket}/artifacts/recorded_fallback_stage6.json", local_fallback, "--region", region],
        check=True,
    )
    print(f"Successfully downloaded confirmatory fallback bundle to: {local_fallback}")

    print("=== [6/6] Verifying Security Group zero-inbound rules at termination ===")
    sg_rules_count = verify_security_group_zero_inbound(sg_id, region)
    metadata["termination_sg_inbound_rules"] = sg_rules_count
    metadata["termination_timestamp"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    meta_file = os.path.join(local_data_dir, "stage6_live_metadata.json")
    with open(meta_file, "w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2)
    print(f"Saved run metadata to {meta_file}")

    print(f"Triggering shutdown of ephemeral instance {instance_id}...")
    run_ssm(instance_id, ["sudo shutdown -h now"], region=region)
    print("Instance shutdown command dispatched. Waiting for EC2 termination state...")
    subprocess.run(
        ["aws", "ec2", "wait", "instance-terminated", "--instance-ids", instance_id, "--region", region],
        check=True,
    )
    print("Instance terminated cleanly.")
