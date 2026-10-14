"""Cross-platform deployment script for Alloc Platform to AWS EC2 host via SSM."""

import os
import subprocess
import sys
import tarfile
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from scripts.ssm_run import run_ssm

BUCKET = "alloc-platform-481374029754-ap-south-1"
IID = "i-011b23accbf304c75"
REGION = "ap-south-1"


def deploy():
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    print("=== [1/4] Packaging repository archive ===")

    with tempfile.NamedTemporaryFile(suffix=".tgz", delete=False) as tmp_f:
        tmp_path = tmp_f.name

    def filter_func(tarinfo):
        name = tarinfo.name
        if any(
            x in name
            for x in [
                ".git",
                ".venv",
                "__pycache__",
                ".pytest_cache",
                "infra/state",
                "alloc.db",
                "bootstrap_admin.txt",
            ]
        ):
            return None
        return tarinfo

    with tarfile.open(tmp_path, "w:gz") as tar:
        tar.add(repo_root, arcname=".", filter=filter_func)

    sz = os.path.getsize(tmp_path)
    print(f"Archive created ({sz} bytes)")

    print(f"=== [2/4] Uploading to s3://{BUCKET}/releases/latest.tgz ===")
    upload_cmd = [
        "aws",
        "s3",
        "cp",
        tmp_path,
        f"s3://{BUCKET}/releases/latest.tgz",
        "--region",
        REGION,
    ]
    subprocess.run(upload_cmd, check=True)
    os.remove(tmp_path)

    print(f"=== [3/4] Triggering remote install via SSM on instance {IID} ===")
    out, err, code = run_ssm(IID, ["/usr/local/bin/alloc-install"], region=REGION)
    print("--- Remote Installation Output ---")
    sys.stdout.buffer.write(out.encode("utf-8", errors="replace") + b"\n")
    if code != 0:
        print("ERROR: Remote installation failed!", file=sys.stderr)
        sys.stderr.buffer.write(err.encode("utf-8", errors="replace") + b"\n")
        sys.exit(code)

    print("=== [4/4] Smoke testing API status endpoint on EC2 ===")
    out_smoke, err_smoke, code_smoke = run_ssm(
        IID,
        ["curl --retry 5 --retry-connrefused --retry-delay 1 -s -f http://127.0.0.1:8000/api/v1/status"],
        region=REGION,
    )
    print("Smoke Test Response:", out_smoke)
    if code_smoke != 0:
        print("ERROR: Smoke test failed!", file=sys.stderr)
        sys.stderr.buffer.write(err_smoke.encode("utf-8", errors="replace") + b"\n")
        sys.exit(code_smoke)

    print("=== Deployment successfully completed! ===")


if __name__ == "__main__":
    deploy()
