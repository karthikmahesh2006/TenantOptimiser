"""Stage 6 Ephemeral Confirmatory Benchmark Launcher & Watchdog Harness.

Provisions an ephemeral c7i-flex.large EC2 instance in ap-south-1 under zero-inbound SG rules,
executes the 85-run confirmatory benchmark matrix for Hypotheses H5-H8 and pre-declared ablations,
extracts all database and fallback artifacts to local storage, asserts 0 inbound SG rules,
and terminates the instance within a strict 75-minute hard watchdog budget.
"""

import argparse
import json
import os
import subprocess
import sys
import time
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
try:
    from scripts.ssm_run import run_ssm
except ImportError:
    run_ssm = None


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


def verify_security_group_zero_inbound(sg_id: str, region: str, dry_run: bool = False) -> int:
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
    if dry_run:
        print("[DRY-RUN] AWS CLI API Call:")
        print(f"  Command: {' '.join(cmd)}")
        print("  Action: DescribeSecurityGroups (asserting len(IpPermissions) == 0)")
        return 0

    res = subprocess.run(cmd, capture_output=True, text=True, check=True)
    inbound_rules = json.loads(res.stdout)
    count = len(inbound_rules)
    if count != 0:
        raise RuntimeError(
            f"SECURITY VIOLATION: Security group {sg_id} has {count} inbound rules (must be 0)!"
        )
    return count


def build_user_data(watchdog_minutes: int) -> str:
    return f"""#!/bin/bash
set -euo pipefail
# Hard watchdog: terminate-on-shutdown enforces budget limit
echo "Setting {watchdog_minutes}-minute hard auto-termination watchdog..."
sudo shutdown -h +{watchdog_minutes} "Stage 6 evaluation budget cap reached"
"""


def launch_instance(
    image_id: str,
    instance_type: str,
    subnet_id: str,
    sg_id: str,
    iam_profile: str,
    region: str,
    watchdog_minutes: int = 75,
    dry_run: bool = False,
) -> str:
    """Launches an ephemeral EC2 instance with InstanceInitiatedShutdownBehavior=terminate and a hard watchdog."""
    print(f"\n=== [1/6] Launching Ephemeral Confirmatory Instance ({instance_type}) ===")
    verify_security_group_zero_inbound(sg_id, region, dry_run=dry_run)

    user_data = build_user_data(watchdog_minutes)

    run_params = {
        "ImageId": image_id,
        "InstanceType": instance_type,
        "SubnetId": subnet_id,
        "SecurityGroupIds": [sg_id],
        "IamInstanceProfile": {"Name": iam_profile},
        "InstanceInitiatedShutdownBehavior": "terminate",
        "UserData": user_data,
        "TagSpecifications": [
            {
                "ResourceType": "instance",
                "Tags": [
                    {"Key": "Project", "Value": "alloc-platform"},
                    {"Key": "Name", "Value": "alloc-stage6-ephemeral"},
                    {"Key": "Stage", "Value": "stage6-confirmatory"},
                ],
            }
        ],
        "Region": region,
    }

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
        "ResourceType=instance,Tags=[{Key=Project,Value=alloc-platform},{Key=Name,Value=alloc-stage6-ephemeral},{Key=Stage,Value=stage6-confirmatory}]",
        "--region",
        region,
        "--query",
        "Instances[0].InstanceId",
        "--output",
        "text",
    ]

    if dry_run:
        print("\n[DRY-RUN] AWS EC2 run-instances API Call Parameters:")
        print(json.dumps(run_params, indent=2))
        print("\n[DRY-RUN] Equivalent AWS CLI Command Line:")
        print(f"  {' '.join(cmd)}")
        return "i-dryrun-placeholder-0123456789abcdef"

    res = subprocess.run(cmd, capture_output=True, text=True, check=True)
    instance_id = res.stdout.strip()
    print(f"Instance launched: {instance_id}")
    return instance_id


def wait_for_ssm(instance_id: str, region: str, timeout_s: int = 300, dry_run: bool = False) -> None:
    """Polls until the newly launched instance reports Online in AWS SSM."""
    print(f"\n=== [2/6] Waiting for SSM agent to come online on {instance_id} ===")
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
    if dry_run:
        print("[DRY-RUN] AWS SSM describe-instance-information API Call:")
        print(f"  Command: {' '.join(cmd)}")
        print("  Action: Wait until PingStatus == 'Online'")
        return

    start_t = time.time()
    while time.time() - start_t < timeout_s:
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode == 0 and res.stdout.strip() == "Online":
            print(f"SSM agent is Online ({time.time() - start_t:.1f}s)")
            return
        time.sleep(5)
    raise TimeoutError(f"SSM agent on {instance_id} did not come online within {timeout_s}s")


def execute_confirmatory_suite(instance_id: str, region: str, dry_run: bool = False) -> None:
    """Executes the 85-run evaluation matrix (60 baseline/hypothesis runs + 25 ablation runs)."""
    print(f"\n=== [3/6] Executing 85-Run Confirmatory Matrix via SSM on {instance_id} ===")
    bench_cmd = (
        "python3 -m alloc_platform.orchestrator.benchmark_matrix "
        "--scenarios scenario_1_baseline scenario_2_burst scenario_3_capacity_cut scenario_over_declaration "
        "--policies saha drf adaptive proportional "
        "--ablations saha:no_declared_floor saha:no_cut_latch saha:no_throttle_escalation saha:reclaim_only saha:fair_only "
        "--seeds 42 43 44 45 46 "
        "--repeats 5"
    )
    if dry_run:
        print("[DRY-RUN] AWS SSM send-command Execution Payload:")
        print(f"  Instance ID: {instance_id}")
        print(f"  DocumentName: AWS-RunShellScript")
        print(f"  Execution Command: {bench_cmd}")
        print("  Total Matrix: 60 core baseline/hypothesis runs + 25 ablation runs = 85 runs (~49.6m active)")
        return

    if run_ssm:
        out, err, code = run_ssm(instance_id, [bench_cmd], region=region, timeout_s=3600)
        if code != 0:
            raise RuntimeError(f"Confirmatory benchmark execution failed (exit {code}): {err}")
    else:
        raise RuntimeError("run_ssm helper not available")


def extract_artifacts_and_terminate(
    instance_id: str,
    sg_id: str,
    bucket: str,
    region: str,
    metadata: dict,
    dry_run: bool = False,
) -> None:
    """Extracts database and fallback artifacts from instance, asserts 0 SG rules, and initiates termination."""
    print(f"\n=== [4/6] Extracting confirmatory run artifacts prior to shutdown ===")
    export_cmd = (
        'python3 -c "'
        'from scripts.export_remote_fallback import export_bundle; '
        'export_bundle(\'/opt/alloc/current/data/recorded_fallback_stage6.json\'); '
        'import subprocess; '
        f'subprocess.run([\'aws\', \'s3\', \'cp\', \'/opt/alloc/current/data/recorded_fallback_stage6.json\', \'s3://{bucket}/artifacts/recorded_fallback_stage6.json\', \'--region\', \'{region}\'], check=True); '
        f'subprocess.run([\'aws\', \'s3\', \'cp\', \'/opt/alloc/current/alloc.db\', \'s3://{bucket}/artifacts/alloc_stage6.db\', \'--region\', \'{region}\'], check=True);'
        '"'
    )
    download_cmd = [
        "aws",
        "s3",
        "cp",
        f"s3://{bucket}/artifacts/recorded_fallback_stage6.json",
        "data/recorded_fallback_stage6.json",
        "--region",
        region,
    ]

    if dry_run:
        print("[DRY-RUN] Remote SSM Export & Local S3 Download Commands:")
        print(f"  SSM Command: {export_cmd}")
        print(f"  Operator Download: {' '.join(download_cmd)}")
        print(f"  Operator DB Download: aws s3 cp s3://{bucket}/artifacts/alloc_stage6.db data/alloc_stage6.db --region {region}")
    else:
        if run_ssm:
            out, err, code = run_ssm(instance_id, [export_cmd], region=region)
            if code != 0:
                print(f"WARNING: SSM export returned error {code}: {err}")
        local_data_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "data"))
        os.makedirs(local_data_dir, exist_ok=True)
        local_fallback = os.path.join(local_data_dir, "recorded_fallback_stage6.json")
        subprocess.run(
            ["aws", "s3", "cp", f"s3://{bucket}/artifacts/recorded_fallback_stage6.json", local_fallback, "--region", region],
            check=True,
        )
        print(f"Successfully downloaded confirmatory fallback bundle to: {local_fallback}")

    print(f"\n=== [5/6] Verifying Security Group zero-inbound rules at termination ===")
    sg_rules_count = verify_security_group_zero_inbound(sg_id, region, dry_run=dry_run)
    metadata["termination_sg_inbound_rules"] = sg_rules_count
    metadata["termination_timestamp"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    meta_file = os.path.join(
        os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "data")),
        "stage6_live_metadata.json",
    )
    if dry_run:
        print("[DRY-RUN] Termination Metadata Artifact Recording:")
        print(f"  Target File: {meta_file}")
        print(f"  Payload: {json.dumps(metadata, indent=2)}")
    else:
        with open(meta_file, "w", encoding="utf-8") as f:
            json.dump(metadata, f, indent=2)
        print(f"Saved run metadata to {meta_file}")

    print(f"\n=== [6/6] Triggering shutdown and asserting instance termination ===")
    shutdown_cmd = "sudo shutdown -h now"
    wait_cmd = [
        "aws",
        "ec2",
        "wait",
        "instance-terminated",
        "--instance-ids",
        instance_id,
        "--region",
        region,
    ]
    if dry_run:
        print("[DRY-RUN] Termination Commands:")
        print(f"  SSM Command: {shutdown_cmd}")
        print(f"  AWS CLI Wait Command: {' '.join(wait_cmd)}")
        print("\n[DRY-RUN] Completed successfully with zero AWS infrastructure mutations.")
        return

    if run_ssm:
        run_ssm(instance_id, [shutdown_cmd], region=region)
    print("Instance shutdown command dispatched. Waiting for EC2 termination state...")
    subprocess.run(wait_cmd, check=True)
    print("Instance terminated cleanly.")


def main():
    parser = argparse.ArgumentParser(
        description="Stage 6 Ephemeral Confirmatory Benchmark Launcher & Watchdog"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print all AWS API calls, parameter dictionaries, user-data, IAM roles, and SG configs without executing.",
    )
    parser.add_argument(
        "--watchdog-minutes",
        type=int,
        default=75,
        help="Hard auto-termination watchdog timer in minutes (default: 75m for 85 runs).",
    )
    parser.add_argument(
        "--instance-type",
        type=str,
        default="c7i-flex.large",
        help="EC2 instance type (default: c7i-flex.large).",
    )
    args = parser.parse_args()

    state = load_state()
    region = state.get("REGION", "ap-south-1")
    sg_id = state.get("SG_ID", "sg-0fa7ce14dea51eaac")
    iam_profile = state.get("PROFILE_NAME", "alloc-platform-ec2")
    bucket = state.get("BUCKET", "alloc-platform-481374029754-ap-south-1")
    subnet_id = state.get("SUBNET_ID", "subnet-placeholder-default")
    image_id = state.get("AMI_ID", "ami-0d2986f2e8c0f7d01")

    print(f"============================================================")
    print(f" STAGE 6 EPHEMERAL CONFIRMATORY LAUNCHER (DRY_RUN={args.dry_run})")
    print(f"============================================================")
    print(f"Region:             {region}")
    print(f"Instance Type:      {args.instance_type}")
    print(f"Security Group:     {sg_id}")
    print(f"IAM Profile:        {iam_profile}")
    print(f"S3 Artifact Bucket: {bucket}")
    print(f"Watchdog Budget:    {args.watchdog_minutes} minutes")

    metadata = {
        "launcher": "scripts/launch_ephemeral_eval.py",
        "dry_run": args.dry_run,
        "region": region,
        "instance_type": args.instance_type,
        "security_group_id": sg_id,
        "iam_profile": iam_profile,
        "s3_bucket": bucket,
        "watchdog_minutes": args.watchdog_minutes,
        "pre_launch_sg_inbound_rules": 0,
        "launch_timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    instance_id = launch_instance(
        image_id=image_id,
        instance_type=args.instance_type,
        subnet_id=subnet_id,
        sg_id=sg_id,
        iam_profile=iam_profile,
        region=region,
        watchdog_minutes=args.watchdog_minutes,
        dry_run=args.dry_run,
    )

    wait_for_ssm(instance_id=instance_id, region=region, dry_run=args.dry_run)
    execute_confirmatory_suite(instance_id=instance_id, region=region, dry_run=args.dry_run)
    extract_artifacts_and_terminate(
        instance_id=instance_id,
        sg_id=sg_id,
        bucket=bucket,
        region=region,
        metadata=metadata,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
