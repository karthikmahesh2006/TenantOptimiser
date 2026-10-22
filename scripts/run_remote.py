"""Send and execute a python script on EC2 via SSM."""

import base64
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from scripts.ssm_run import run_ssm


def run_remote(local_script: str, instance_id: str = "i-011b23accbf304c75"):
    with open(local_script, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("ascii")

    remote_path = f"/tmp/{os.path.basename(local_script)}"
    commands = [
        f"echo {b64} | base64 -d > {remote_path}",
        f"DB_PATH=/var/lib/alloc/alloc.db /opt/alloc/current/.venv/bin/python {remote_path}",
        f"rm -f {remote_path}",
    ]
    stdout, stderr, rc = run_ssm(instance_id, commands)
    print("=== REMOTE OUTPUT ===")
    print(stdout)
    if stderr:
        print("=== REMOTE STDERR ===")
        print(stderr)
    return rc


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python scripts/run_remote.py <path_to_script>")
        sys.exit(1)
    rc = run_remote(sys.argv[1])
    sys.exit(rc)
