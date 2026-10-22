#!/usr/bin/env python
"""Helper script to start AWS SSM port forwarding to EC2 on port 8000 without shell quoting issues."""

import json
import os
import shutil
import subprocess
import sys


def main():
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    state_file = os.path.join(repo_root, "infra", "state", "resources.env")
    state = {}
    if os.path.exists(state_file):
        with open(state_file, "r") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    parts = line.split("=", 1)
                    state[parts[0].strip()] = parts[1].strip()

    iid = os.getenv("IID", state.get("IID", "i-011b23accbf304c75"))
    region = os.getenv("REGION", state.get("REGION", "ap-south-1"))
    port = "8000"

    print("==================================================================")
    print(" AWS Systems Manager (SSM) Port-Forwarding Tunnel")
    print(" Target EC2: {0} | Region: {1} | Port: {2} -> {2}".format(iid, region, port))
    print("==================================================================")

    # 1. Check if session-manager-plugin is installed
    plugin_found = False
    # Check PATH
    for path_dir in os.environ.get("PATH", "").split(os.pathsep):
        candidate = os.path.join(path_dir, "session-manager-plugin.exe")
        if os.path.exists(candidate):
            plugin_found = True
            break
    if not plugin_found:
        default_inst = r"C:\Program Files\Amazon\SessionManagerPlugin\bin\session-manager-plugin.exe"
        if os.path.exists(default_inst):
            plugin_found = True

    if not plugin_found:
        print("\n[!] Prerequisite missing: AWS 'Session Manager Plugin' is required.")
        print("    AWS CLI requires this plugin to open interactive port forwarding tunnels.")
        installer_path = os.path.expandvars(
            r"%LOCALAPPDATA%\Temp\WinGet\Amazon.SessionManagerPlugin.1.2.896.0\SessionManagerPluginSetup.exe"
        )
        if os.path.exists(installer_path):
            print("\n[*] Found cached installer on your PC: {0}".format(installer_path))
            print("[*] Launching installer GUI now... Please click 'Install' on the popup window.")
            subprocess.run(["powershell", "-Command", "Start-Process '{0}'".format(installer_path)])
        else:
            print("\n[*] Please download and run the official 1-click Windows installer from AWS:")
            print("    https://s3.amazonaws.com/session-manager-downloads/plugin/latest/windows/SessionManagerPluginSetup.exe")
        print("\nAfter installing, re-run this script: .venv\\Scripts\\python scripts/start_tunnel.py")
        sys.exit(1)

    params = json.dumps({"portNumber": [port], "localPortNumber": [port]})
    cmd = [
        "aws",
        "ssm",
        "start-session",
        "--target",
        iid,
        "--document-name",
        "AWS-StartPortForwardingSession",
        "--parameters",
        params,
        "--region",
        region,
    ]

    print("\nStarting encrypted SSM port forwarding tunnel...")
    print("Executing: {0}\n".format(" ".join(cmd)))
    print(">>> SUCCESS: Once connected, access the web UI at: http://localhost:8000 <<<")
    print(">>> Press Ctrl+C at any time in this window to stop the tunnel. <<<\n")

    try:
        subprocess.call(cmd)
    except KeyboardInterrupt:
        print("\nTunnel stopped.")


if __name__ == "__main__":
    main()
