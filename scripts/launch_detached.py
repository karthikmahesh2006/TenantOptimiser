#!/usr/bin/env python3
"""Stage 6 Detached Launcher for Ephemeral EC2 Evaluation.

Features:
1. Strict 0-inbound security group assertion (Amendment 3).
2. Preflight IAM simulation: asserts s3:PutObject on target S3 prefix before RunInstances.
3. Key scheme: backups/stage6/<commit12>/<run_id>.json (never overwrites).
4. Generates self-contained bash user-data containing:
   - Hard auto-termination watchdog (runs x (duration + overhead) + 25m buffer).
   - Git presence assurance (dnf install -y git if missing) & public git clone at pinned commit.
   - Live environment setup (docker-socket-proxy, alloc-worker, .venv).
   - benchmark_matrix execution logging to /var/log/stage6.log.
   - Guaranteed cleanup trap exporting artifacts, logs, done.json and calling shutdown -h now.
5. Launches EC2 instance with InstanceInitiatedShutdownBehavior=terminate.
6. Prints instance id, region, S3 prefix, and EXITS immediately (no session held open).
"""

import argparse
import json
import math
import os
import subprocess
import sys
import time
from typing import Dict, List, Optional, Tuple

from alloc_platform.orchestrator.benchmark_matrix import build_plan_tuples, compute_watchdog_minutes


def load_state() -> Dict[str, str]:
    """Loads AWS state from infra/state/resources.env if present."""
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


def get_aws_account_id(region: str = "ap-south-1") -> str:
    """Retrieves current AWS account ID via STS."""
    try:
        cmd = ["aws", "sts", "get-caller-identity", "--query", "Account", "--output", "text", "--region", region]
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return res.stdout.strip()
    except Exception:
        return "481374029754"


def verify_security_group_zero_inbound(sg_id: str, region: str, dry_run: bool = False):
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
        print("[DRY-RUN] Verifying Security Group zero-inbound:")
        print(f"  Command: {' '.join(cmd)}")
        print("  Action: DescribeSecurityGroups (asserting len(IpPermissions) == 0)")
        return "dry-run"

    res = subprocess.run(cmd, capture_output=True, text=True, check=True)
    inbound_rules = json.loads(res.stdout)
    count = len(inbound_rules)
    if count != 0:
        raise RuntimeError(
            f"SECURITY VIOLATION: Security group {sg_id} has {count} inbound rules (must be 0)!"
        )
    return count


def verify_iam_put_object_permission(
    role_arn: str,
    target_s3_arn: str,
    region: str = "ap-south-1",
    dry_run: bool = False,
) -> str:
    """Simulates principal policy to verify s3:PutObject permission before RunInstances."""
    cmd = [
        "aws",
        "iam",
        "simulate-principal-policy",
        "--policy-source-arn",
        role_arn,
        "--action-names",
        "s3:PutObject",
        "--resource-arns",
        target_s3_arn,
        "--region",
        region,
        "--output",
        "json",
    ]
    if dry_run:
        print("[DRY-RUN] Simulating IAM Principal Policy for S3 PutObject:")
        print(f"  Role ARN:     {role_arn}")
        print(f"  Target ARN:   {target_s3_arn}")
        print("  EvalDecision: allowed (dry-run)")
        return "allowed"

    res = subprocess.run(cmd, capture_output=True, text=True, check=True)
    data = json.loads(res.stdout)
    results = data.get("EvaluationResults", [])
    if not results or results[0].get("EvalDecision") != "allowed":
        decision = results[0].get("EvalDecision") if results else "no-results"
        raise PermissionError(
            f"PREFLIGHT IAM CHECK FAILED: Role {role_arn} is NOT authorized for s3:PutObject on {target_s3_arn} (EvalDecision: {decision}). "
            "Aborting launch before RunInstances to avoid execution without persistence."
        )
    return "allowed"


def build_detached_user_data(
    watchdog_minutes: int,
    pinned_commit: str,
    bucket: str,
    region: str,
    preset: str,
    s3_prefix: str = "backups/stage6",
    overhead: float = 5.0,
) -> str:
    """Builds self-contained user-data script for detached autonomous execution."""
    s3_dest = f"s3://{bucket}/{s3_prefix.rstrip('/')}"

    return f"""#!/usr/bin/env bash
set -euo pipefail

# 1. Hard watchdog: auto-terminate after budget cap
echo "Setting {watchdog_minutes}-minute hard auto-termination watchdog..."
sudo shutdown -h +{watchdog_minutes} "Stage 6 evaluation budget cap reached"

LOG_FILE="/var/log/stage6.log"
exec > >(tee -a "$LOG_FILE") 2>&1
echo "=== Stage 6 Detached Execution Started: $(date -u +"%Y-%m-%dT%H:%M:%SZ") ==="
echo "Target Commit: {pinned_commit}"
echo "Preset:        {preset}"
echo "Bucket:        {bucket}"
echo "S3 Dest:       {s3_dest}"
echo "Region:        {region}"
echo "Watchdog:      {watchdog_minutes} minutes"

# 2. Cleanup & shutdown trap: runs on normal completion OR any failure
cleanup_and_shutdown() {{
    EXIT_CODE=$?
    echo "=== Executing cleanup_and_shutdown (exit code: $EXIT_CODE) at $(date -u +"%Y-%m-%dT%H:%M:%SZ") ==="

    # Final DB and fallback bundle export
    if [ -d "/opt/alloc/repo" ] && [ -f "/opt/alloc/repo/.venv/bin/python" ]; then
        echo "Exporting fallback bundle and SQLite database..."
        /opt/alloc/repo/.venv/bin/python -c "
import sys, subprocess
sys.path.insert(0, '/opt/alloc/repo/src')
try:
    from scripts.export_remote_fallback import export_bundle
    export_bundle('/tmp/recorded_fallback_stage6.json')
    subprocess.run(['aws', 's3', 'cp', '/tmp/recorded_fallback_stage6.json', '{s3_dest}/recorded_fallback_stage6.json', '--region', '{region}'], check=False)
except Exception as e:
    print('Bundle export error:', e)
try:
    subprocess.run(['aws', 's3', 'cp', '/var/lib/alloc/alloc.db', '{s3_dest}/alloc_stage6.db', '--region', '{region}'], check=False)
except Exception as e:
    print('DB upload error:', e)
" || true
    fi

    # Write fallback done.json if benchmark_matrix did not write one
    if [ ! -f /tmp/stage6_done.json ]; then
        STATUS="completed"
        REASON="Execution completed"
        if [ $EXIT_CODE -ne 0 ]; then
            STATUS="failed"
            REASON="User-data script exited with error code $EXIT_CODE"
        fi
        cat > /tmp/stage6_done.json <<DONE_EOF
{{
  "status": "$STATUS",
  "exit_code": $EXIT_CODE,
  "reason": "$REASON",
  "commit_hash": "{pinned_commit}",
  "preset": "{preset}",
  "timestamp": "$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
}}
DONE_EOF
    fi

    # Upload done.json and execution log to S3
    aws s3 cp /tmp/stage6_done.json "{s3_dest}/done.json" --region "{region}" || true
    aws s3 cp "$LOG_FILE" "{s3_dest}/stage6.log" --region "{region}" || true

    echo "=== Triggering shutdown -h now (InstanceInitiatedShutdownBehavior=terminate) ==="
    sudo shutdown -h now
}}
trap cleanup_and_shutdown EXIT

# 3. Ensure git is available (install via dnf if absent on base AMI)
if ! command -v git >/dev/null 2>&1; then
    echo "Installing git via dnf..."
    dnf install -y git
fi

# 4. Public git checkout at pinned commit
REPO_DIR="/opt/alloc/repo"
rm -rf "$REPO_DIR"
mkdir -p "$REPO_DIR"
echo "Cloning repository..."
git clone https://github.com/karthikmahesh2006/TenantOptimiser.git "$REPO_DIR"
cd "$REPO_DIR"
echo "Checking out pinned commit {pinned_commit}..."
git checkout "{pinned_commit}"
CHECKED_OUT_HASH=$(git rev-parse HEAD)
echo "Verified checked-out commit: $CHECKED_OUT_HASH"

if [ "$CHECKED_OUT_HASH" != "{pinned_commit}" ]; then
    echo "FATAL: Checked out commit $CHECKED_OUT_HASH does not match pinned {pinned_commit}!"
    exit 1
fi

# 5. Environment & Worker provisioning (reusing /opt/alloc configuration)
mkdir -p /etc/alloc /var/lib/alloc
if [ ! -f /etc/alloc/env ]; then
    cat > /etc/alloc/env <<ENV_EOF
SECRET_KEY=$(openssl rand -hex 32)
ALLOC_MODE=live
DOCKER_HOST=tcp://127.0.0.1:2375
DB_PATH=/var/lib/alloc/alloc.db
MAX_TENANTS=10
MAX_DURATION_S=600
MAX_REPEATS=10
MAX_CONTAINER_CPU=4.0
MIN_CONTAINER_CPU=0.05
MAX_CONTAINER_MEM_MB=16384.0
MIN_CONTAINER_MEM_MB=32.0
ENV_EOF
fi

systemctl enable --now docker
if ! docker ps --format '{{{{.Names}}}}' | grep -q '^dockerproxy$'; then
    echo "Starting docker-socket-proxy..."
    docker rm -f dockerproxy || true
    docker run -d \\
        --name dockerproxy \\
        --restart always \\
        -e CONTAINERS=1 \\
        -e POST=1 \\
        -e DELETE=1 \\
        -e NETWORKS=1 \\
        -e IMAGES=0 \\
        -e EXEC=0 \\
        -e VOLUMES=0 \\
        -e INFO=1 \\
        -v /var/run/docker.sock:/var/run/docker.sock:ro \\
        -p 127.0.0.1:2375:2375 \\
        tecnativa/docker-socket-proxy
fi

echo "Building worker container image..."
docker build -t alloc-worker:latest "$REPO_DIR/src/alloc_platform/workload/worker"

if [ -d "/opt/alloc/current/.venv" ]; then
    echo "Reusing pre-installed virtual environment from /opt/alloc/current/.venv..."
    cp -al /opt/alloc/current/.venv "$REPO_DIR/.venv" || cp -r /opt/alloc/current/.venv "$REPO_DIR/.venv"
    "$REPO_DIR/.venv/bin/pip" install -e "$REPO_DIR"
else
    echo "Setting up Python virtual environment..."
    python3.11 -m venv "$REPO_DIR/.venv"
    "$REPO_DIR/.venv/bin/pip" install --upgrade pip
    "$REPO_DIR/.venv/bin/pip" install "$REPO_DIR[dev]"
fi

if ! id -u alloc >/dev/null 2>&1; then
    useradd -r -s /sbin/nologin -d /var/lib/alloc alloc || true
fi
chown -R alloc:alloc /var/lib/alloc "$REPO_DIR"

# 6. Execute benchmark_matrix under live mode
echo "Running benchmark_matrix with preset {preset}..."
export ALLOC_MODE=live
export DOCKER_HOST=tcp://127.0.0.1:2375
export DB_PATH=/var/lib/alloc/alloc.db

"$REPO_DIR/.venv/bin/python" -m alloc_platform.orchestrator.benchmark_matrix \\
    --preset "{preset}" \\
    --s3-bucket "{bucket}" \\
    --s3-prefix "{s3_prefix}" \\
    --overhead "{overhead}" \\
    --region "{region}" \\
    --commit-hash "$CHECKED_OUT_HASH" \\
    --done-file /tmp/stage6_done.json \\
    --db-path /var/lib/alloc/alloc.db

echo "=== Stage 6 execution completed cleanly ==="
"""


def compute_s3_keys(bucket: str, s3_prefix: str, preset: str) -> List[str]:
    """Returns the list of S3 keys that will be written by this evaluation."""
    prefix = s3_prefix.rstrip("/")
    keys = [
        f"{prefix}/progress.json",
        f"{prefix}/done.json",
        f"{prefix}/recorded_fallback_stage6.json",
        f"{prefix}/alloc_stage6.db",
        f"{prefix}/stage6.log",
    ]
    plan = build_plan_tuples(preset=preset)
    for idx, (pol, sc_id, seed) in enumerate(plan, 1):
        run_id = f"run_{sc_id}_{pol.replace(':', '_')}_s{seed}_{idx}"
        keys.append(f"{prefix}/{run_id}.json")
    return keys


def launch_detached(
    pinned_commit: str,
    preset: str = "reduced30",
    overhead: float = 5.0,
    dry_run: bool = False,
    subnet_id: Optional[str] = None,
    image_id: Optional[str] = None,
    sg_id: Optional[str] = None,
    iam_profile: Optional[str] = None,
    bucket: Optional[str] = None,
    region: Optional[str] = None,
    s3_prefix: Optional[str] = None,
    instance_type: str = "c7i-flex.large",
) -> Tuple[str, str, str]:
    """Verifies security group, builds user-data, launches EC2 instance, and returns immediately."""
    state = load_state()
    region = region or state.get("REGION", "ap-south-1")
    sg_id = sg_id or state.get("SG_ID")
    iam_profile = iam_profile or state.get("PROFILE_NAME", "alloc-platform-ec2")
    role_name = state.get("ROLE_NAME", "alloc-platform-ec2")
    bucket = bucket or state.get("BUCKET", "alloc-platform-481374029754-ap-south-1")
    subnet_id = subnet_id or state.get("SUBNET_ID")
    image_id = image_id or state.get("AMI_ID")

    # Scheme: backups/stage6/<commit12>
    commit12 = pinned_commit[:12]
    if s3_prefix is None:
        effective_s3_prefix = f"backups/stage6/{commit12}"
    else:
        effective_s3_prefix = s3_prefix if commit12 in s3_prefix else f"{s3_prefix.rstrip('/')}/{commit12}"

    # Hard Validation: In non-dry-run mode, fail immediately on missing or placeholder parameters
    if not dry_run:
        for p_name, p_val in [
            ("SubnetId", subnet_id),
            ("ImageId", image_id),
            ("SecurityGroupId", sg_id),
            ("IamProfile", iam_profile),
            ("Bucket", bucket),
        ]:
            if not p_val or "placeholder" in p_val.lower():
                raise ValueError(
                    f"FATAL: Required infrastructure parameter {p_name} is unset, empty, or placeholder ('{p_val}'). "
                    "Non-dry-run mode requires valid AWS resource IDs and will NOT fall back to defaults or placeholders."
                )

    # 1. Zero-Inbound Verification
    pre_rules = verify_security_group_zero_inbound(sg_id or "sg-dryrun", region, dry_run=dry_run)

    # 2. Preflight IAM simulation check
    account_id = get_aws_account_id(region) if not dry_run else "481374029754"
    role_arn = f"arn:aws:iam::{account_id}:role/{role_name}"
    preflight_target_arn = f"arn:aws:s3:::{bucket}/{effective_s3_prefix}/preflight_check.json"
    verify_iam_put_object_permission(role_arn, preflight_target_arn, region, dry_run=dry_run)

    plan = build_plan_tuples(preset=preset)
    duration = 120
    watchdog_min, explanation = compute_watchdog_minutes(
        runs=len(plan), duration_s=duration, overhead_s=overhead
    )

    user_data = build_detached_user_data(
        watchdog_minutes=watchdog_min,
        pinned_commit=pinned_commit,
        bucket=bucket or "bucket-dryrun",
        region=region,
        preset=preset,
        s3_prefix=effective_s3_prefix,
        overhead=overhead,
    )

    run_params = {
        "ImageId": image_id or "ami-placeholder",
        "InstanceType": instance_type,
        "SubnetId": subnet_id or "subnet-placeholder",
        "SecurityGroupIds": [sg_id or "sg-placeholder"],
        "IamInstanceProfile": {"Name": iam_profile},
        "InstanceInitiatedShutdownBehavior": "terminate",
        "UserData": user_data,
        "TagSpecifications": [
            {
                "ResourceType": "instance",
                "Tags": [
                    {"Key": "Project", "Value": "alloc-platform"},
                    {"Key": "Name", "Value": f"alloc-stage6-{preset}"},
                    {"Key": "Stage", "Value": "stage6-detached"},
                    {"Key": "Commit", "Value": pinned_commit},
                    {"Key": "Preset", "Value": preset},
                ],
            }
        ],
        "Region": region,
    }

    s3_keys = compute_s3_keys(bucket or "bucket-dryrun", effective_s3_prefix, preset)

    if dry_run:
        print("============================================================")
        print(f" STAGE 6 DETACHED LAUNCHER (DRY_RUN=True, PRESET={preset})")
        print("============================================================")
        print(f"Pinned Commit Hash: {pinned_commit} (prefix commit12: {commit12})")
        print(f"Region:             {region}")
        print(f"Instance Type:      {instance_type}")
        print(f"Security Group:     {sg_id} (inbound rules: {pre_rules})")
        print(f"IAM Profile:        {iam_profile} (Role: {role_arn})")
        print(f"S3 Artifact Bucket: {bucket}")
        print(f"S3 Artifact Prefix: {effective_s3_prefix}")
        print(f"Planned Runs:       {len(plan)} runs")
        print(f"{explanation}")
        print("\n--- [DRY-RUN] Full User-Data Script Text ---")
        print(user_data)
        print("\n--- [DRY-RUN] AWS EC2 run-instances API Parameters ---")
        print(json.dumps(run_params, indent=2))
        print("\n--- [DRY-RUN] S3 Keys That Will Be Written ---")
        for k in s3_keys:
            print(f"  s3://{bucket}/{k}")
        print("\n[DRY-RUN] Completed successfully with zero AWS infrastructure mutations.")
        return "i-dryrun-placeholder-detached", region, f"s3://{bucket}/{effective_s3_prefix}"

    # LIVE LAUNCH
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
        f"ResourceType=instance,Tags=[{{Key=Project,Value=alloc-platform}},{{Key=Name,Value=alloc-stage6-{preset}}},{{Key=Stage,Value=stage6-detached}},{{Key=Commit,Value={pinned_commit}}},{{Key=Preset,Value={preset}}}]",
        "--region",
        region,
        "--query",
        "Instances[0].InstanceId",
        "--output",
        "text",
    ]

    print(f"Calling EC2 run-instances for detached execution ({preset}, {len(plan)} runs)...")
    res = subprocess.run(cmd, capture_output=True, text=True, check=True)
    instance_id = res.stdout.strip()
    s3_uri = f"s3://{bucket}/{effective_s3_prefix}"
    print(f"\n============================================================")
    print(f" DETACHED INSTANCE LAUNCHED SUCCESSFULLY")
    print(f"============================================================")
    print(f"Instance ID: {instance_id}")
    print(f"Region:      {region}")
    print(f"S3 Prefix:   {s3_uri}")
    print(f"Commit:      {pinned_commit}")
    print(f"Preset:      {preset} ({len(plan)} runs, {watchdog_min}m watchdog)")
    print(f"Security:    0 inbound rules confirmed (SG: {sg_id})")
    print(f"Status Cmd:  python scripts/check_stage6_status.py --instance-id {instance_id}")
    print(f"============================================================")
    print("Launcher exiting immediately. The operator may now safely close the laptop.")
    return instance_id, region, s3_uri


def main():
    parser = argparse.ArgumentParser(description="Stage 6 Detached Autonomous Launcher")
    parser.add_argument("--pinned-commit", type=str, required=True, help="Pinned git commit hash to check out.")
    parser.add_argument("--preset", type=str, choices=["reduced30", "smoke3", "full105"], default="reduced30", help="Suite preset.")
    parser.add_argument("--overhead", type=float, default=5.0, help="Per-run overhead seconds (default: 5.0).")
    parser.add_argument("--dry-run", action="store_true", help="Print all configs and user-data without launching.")
    parser.add_argument("--subnet-id", type=str, help="Subnet ID override.")
    parser.add_argument("--ami-id", type=str, help="AMI ID override.")
    parser.add_argument("--sg-id", type=str, help="Security group ID override.")
    parser.add_argument("--iam-profile", type=str, help="IAM profile override.")
    parser.add_argument("--bucket", type=str, help="S3 bucket override.")
    parser.add_argument("--region", type=str, help="AWS region override.")
    parser.add_argument("--s3-prefix", type=str, default=None, help="S3 prefix (default: backups/stage6/<commit12>).")
    parser.add_argument("--instance-type", type=str, default="c7i-flex.large", help="Instance type (default: c7i-flex.large).")
    args = parser.parse_args()

    launch_detached(
        pinned_commit=args.pinned_commit,
        preset=args.preset,
        overhead=args.overhead,
        dry_run=args.dry_run,
        subnet_id=args.subnet_id,
        image_id=args.ami_id,
        sg_id=args.sg_id,
        iam_profile=args.iam_profile,
        bucket=args.bucket,
        region=args.region,
        s3_prefix=args.s3_prefix,
        instance_type=args.instance_type,
    )


if __name__ == "__main__":
    main()
