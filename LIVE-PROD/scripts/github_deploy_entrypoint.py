#!/usr/bin/python3
"""Forced SSH command: accept only the current develop commit, never a shell.

Install root-owned at /usr/local/sbin/irs-pin-github-deploy. The dedicated
authorized_keys entry must use restrict,command="/usr/local/sbin/irs-pin-github-deploy".
This bootstrap stays outside the checkout so subsequent deploys can update it.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import tempfile

REPO = Path("/home/ubuntu/IRSRequesterAutomation")
DEPLOY_SCRIPT = "LIVE-PROD/scripts/deploy_ec2.py"


def requested_commit(command: str) -> str:
    match = re.fullmatch(r"deploy ([0-9a-f]{40})", command)
    if not match:
        raise ValueError("Only 'deploy <40-character commit SHA>' is permitted.")
    return match[1]


def main() -> int:
    commit = requested_commit(os.environ.get("SSH_ORIGINAL_COMMAND", ""))
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    subprocess.run(["git", "fetch", "origin", "develop"], cwd=REPO, env=env, check=True)
    current = subprocess.check_output(["git", "rev-parse", "origin/develop"], cwd=REPO, text=True).strip()
    if commit != current:
        print("A newer develop commit exists; this deployment was superseded.", flush=True)
        return 0
    script = subprocess.check_output(["git", "show", f"{commit}:{DEPLOY_SCRIPT}"], cwd=REPO)
    # Execute the helper from the verified GitHub revision, not an editable working file.
    with tempfile.TemporaryDirectory(prefix="irs-pin-deploy-") as folder:
        path = Path(folder) / "deploy_ec2.py"
        path.write_bytes(script)
        return subprocess.run(["python3", str(path), commit], cwd=REPO, env=env).returncode


if __name__ == "__main__":
    raise SystemExit(main())
