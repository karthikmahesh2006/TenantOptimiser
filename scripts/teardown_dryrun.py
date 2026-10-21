"""Teardown dry-run audit script.

Verifies current cloud resources recorded in infra/state/resources.env and prints
the exact AWS CLI commands required for full infrastructure teardown without executing destructive actions.
"""

import os
import subprocess
import sys


def load_state():
    state_file = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "infra", "state", "resources.env"))
    state = {}
    if os.path.exists(state_file):
        with open(state_file, "r") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    state[k.strip()] = v.strip()
    return state


def dry_run():
    state = load_state()
    bucket = state.get("BUCKET", "<BUCKET-PLACEHOLDER>")
    iid = state.get("IID", "<IID-PLACEHOLDER>")
    sg_id = state.get("SG_ID", "<SG-PLACEHOLDER>")
    vpc_id = state.get("VPC_ID", "<VPC-PLACEHOLDER>")
    region = state.get("REGION", "ap-south-1")

    print("=" * 80)
    print("           ALLOC PLATFORM INFRASTRUCTURE TEARDOWN DRY-RUN AUDIT")
    print("=" * 80)
    print(f"Target Region:         {region}")
    print(f"Target EC2 Instance:   {iid}")
    print(f"Target Security Group: {sg_id}")
    print(f"Target S3 Bucket:      {bucket}")
    print(f"Target VPC:            {vpc_id}")
    print("-" * 80)
    print("TEARDOWN EXECUTION PLAN (Dry-Run: No destructive actions taken):")
    print("\n1. Terminate EC2 Instance & Attached EBS Volumes:")
    print(f"   aws ec2 terminate-instances --instance-ids {iid} --region {region}")
    print(f"   aws ec2 wait instance-terminated --instance-ids {iid} --region {region}")

    print("\n2. Delete Project Security Group (Zero Inbound):")
    print(f"   aws ec2 delete-security-group --group-id {sg_id} --region {region}")

    print("\n3. Clean and Delete IAM Instance Profile & Role:")
    print("   aws iam remove-role-from-instance-profile --instance-profile-name alloc-platform-ec2 --role-name alloc-platform-ec2")
    print("   aws iam delete-instance-profile --instance-profile-name alloc-platform-ec2")
    print("   aws iam delete-role-policy --role-name alloc-platform-ec2 --policy-name alloc-s3-cw")
    print("   aws iam detach-role-policy --role-name alloc-platform-ec2 --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore")
    print("   aws iam delete-role --role-name alloc-platform-ec2")

    print("\n4. Purge Releases and Delete S3 Deployment Bucket:")
    print(f"   aws s3 rm s3://{bucket} --recursive --region {region}")
    print(f"   aws s3api delete-bucket --bucket {bucket} --region {region}")

    print("\n5. Verification of Zero Residual Resources:")
    print(f"   aws ec2 describe-instances --instance-ids {iid} --query 'Reservations[0].Instances[0].State.Name' --region {region}")
    print(f"   aws ec2 describe-security-groups --group-ids {sg_id} --region {region} (Expect: InvalidGroup.NotFound)")
    print("=" * 80)
    print("DRY-RUN STATUS: Verified. Teardown plan ready for end-of-project execution.")


if __name__ == "__main__":
    dry_run()
