"""Executes remote commands on EC2 via AWS SSM and streams output cleanly."""

import json
import subprocess
import sys


def run_ssm(instance_id: str, commands: list[str], region: str = "ap-south-1") -> tuple[str, str, int]:
    params = json.dumps({"commands": commands})
    cmd = [
        "aws",
        "ssm",
        "send-command",
        "--instance-ids",
        instance_id,
        "--document-name",
        "AWS-RunShellScript",
        "--parameters",
        params,
        "--region",
        region,
        "--query",
        "Command.CommandId",
        "--output",
        "text",
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    if res.returncode != 0:
        return "", res.stderr, res.returncode

    command_id = res.stdout.strip()

    wait_cmd = [
        "aws",
        "ssm",
        "wait",
        "command-executed",
        "--command-id",
        command_id,
        "--instance-id",
        instance_id,
        "--region",
        region,
    ]
    subprocess.run(wait_cmd)

    import os
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}

    get_cmd = [
        "aws",
        "ssm",
        "get-command-invocation",
        "--command-id",
        command_id,
        "--instance-id",
        instance_id,
        "--region",
        region,
        "--output",
        "json",
    ]
    inv_res = subprocess.run(get_cmd, capture_output=True, env=env)
    out_json = inv_res.stdout.decode("utf-8", errors="replace")
    try:
        data = json.loads(out_json)
        stdout = data.get("StandardOutputContent", "")
        stderr = data.get("StandardErrorContent", "")
        code = data.get("ResponseCode", 0)
        return stdout, stderr, code
    except Exception as e:
        return out_json, str(e), 1


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python ssm_run.py <instance-id> <cmd1> [cmd2 ...]")
        sys.exit(1)
    iid = sys.argv[1]
    cmds = sys.argv[2:]
    out, err, code = run_ssm(iid, cmds)
    if out:
        sys.stdout.buffer.write(out.encode("utf-8", errors="replace") + b"\n")
    if err:
        sys.stderr.buffer.write(err.encode("utf-8", errors="replace") + b"\n")
    sys.exit(code)
