"""Deploy an exact develop commit, test its image, and roll back unhealthy releases.

Invoked through the restricted SSH entrypoint by GitHub Actions. Runtime .env
and data stay on EC2; no reset/clean or credential output is used.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.request

REPO = Path("/home/ubuntu/IRSRequesterAutomation")
STATE = Path("/home/ubuntu/.local/state/irs-pin-deployments/actions")
PROJECT = "live-prod"
SERVICES = ("api", "frontend")


def run(args: list[str], *, capture: bool = False, cwd: Path = REPO) -> str:
    result = subprocess.run(args, cwd=cwd, check=True, text=True,
                            stdout=subprocess.PIPE if capture else None,
                            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    return result.stdout.strip() if capture else ""


def runtime_database(path: str) -> bool:
    return path.startswith(("LIVE-PROD/data/", "QA/data/")) and path.endswith((".db", ".db-journal", ".db-wal", ".db-shm"))


def check_checkout(commit: str, *, repo: Path = REPO) -> None:
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("A full commit SHA is required.")
    if run(["git", "branch", "--show-current"], capture=True, cwd=repo) != "develop":
        raise ValueError("EC2 checkout must remain on develop.")
    # Preserve existing audit changes, but never discard local source/staged work.
    staged = run(["git", "diff", "--cached", "--name-only"], capture=True, cwd=repo)
    dirty = run(["git", "diff", "--name-only", "HEAD"], capture=True, cwd=repo).splitlines()
    if staged or any(not runtime_database(path) for path in dirty):
        raise ValueError("EC2 has local source/staged changes; refusing to overwrite them.")
    changes = run(["git", "diff", "--name-only", "HEAD", commit], capture=True, cwd=repo).splitlines()
    if any(runtime_database(path) for path in changes):
        raise ValueError("Commit changes tracked runtime databases. Preserve/untrack that data manually before deployment.")
    if any(path.startswith("LIVE-PROD/.env") and path != "LIVE-PROD/.env.example" for path in changes):
        raise ValueError("Runtime credentials must not be deployed from Git.")
    run(["git", "merge-base", "--is-ancestor", "HEAD", commit], cwd=repo)


@contextmanager
def deployment_lock():
    import fcntl
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (STATE / "deploy.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def extract_release(commit: str, release: Path) -> Path:
    data = subprocess.check_output(["git", "archive", commit, "LIVE-PROD"], cwd=REPO)
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        for member in archive.getmembers():
            target = (release / member.name).resolve()
            if not target.is_relative_to(release.resolve()) or not (member.isdir() or member.isfile()):
                raise ValueError("Unsupported path in release archive.")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(member) as source, target.open("wb") as destination:
                    shutil.copyfileobj(source, destination)
    return release / "LIVE-PROD"


def compose(files: list[Path], *args: str, capture: bool = False) -> str:
    command = ["sudo", "-n", "docker", "compose", "--project-name", PROJECT,
               "--project-directory", str(REPO / "LIVE-PROD"),
               "--env-file", str(REPO / "LIVE-PROD/.env")]
    for path in files:
        command.extend(["-f", str(path)])
    return run([*command, *args], capture=capture)


def wait_healthy(*, timeout: int = 180) -> None:
    deadline = time.monotonic() + timeout
    for url in ("http://127.0.0.1:8002/health", "http://127.0.0.1:8520/_stcore/health"):
        while True:
            try:
                with urllib.request.urlopen(url, timeout=3) as response:
                    if response.status == 200:
                        break
            except OSError:
                pass
            if time.monotonic() >= deadline:
                raise RuntimeError("Service health check timed out.")
            time.sleep(3)


def deploy(commit: str) -> int:
    with deployment_lock():
        run(["git", "fetch", "origin", "develop"])
        if run(["git", "rev-parse", "origin/develop"], capture=True) != commit:
            print("A newer develop commit exists; deployment superseded.", flush=True)
            return 0
        check_checkout(commit)
        if not (REPO / "LIVE-PROD/.env").is_file():
            raise ValueError("Server LIVE-PROD/.env is missing.")
        if shutil.disk_usage(STATE).free < 2 * 1024**3:
            raise ValueError("Deployment requires at least 2 GiB of free space.")

        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        release = STATE / f"{stamp}-{commit[:12]}"
        release.mkdir(mode=0o700)
        app = extract_release(commit, release)
        override = release / "build.override.json"
        override.write_text(json.dumps({"services": {
            service: {"build": {"context": str(app), "dockerfile": "Dockerfile"}}
            for service in SERVICES
        }}))
        candidate = [app / "docker-compose.yml", override]
        active_path = STATE / "active.json"
        if active_path.exists():
            previous = [Path(path) for path in json.loads(active_path.read_text())["compose_files"]]
        else:
            saved = release / "previous-compose.yml"
            shutil.copy2(REPO / "LIVE-PROD/docker-compose.yml", saved)
            previous = [saved]
        old_images = {}
        for service in SERVICES:
            container = compose(previous, "ps", "-q", service, capture=True)
            if not container:
                raise ValueError(f"No running {service} container to preserve for rollback.")
            old_images[service] = run(["sudo", "-n", "docker", "inspect", "--format", "{{.Image}}", container], capture=True)
            run(["sudo", "-n", "docker", "tag", old_images[service], f"{PROJECT}-{service}:before-{stamp}"])
        rollback_override = release / "rollback.override.json"
        rollback_override.write_text(json.dumps({"services": {
            service: {"image": image} for service, image in old_images.items()
        }}))
        (release / "deployment.json").write_text(json.dumps({
            "commit": commit, "previous_compose_files": [str(path) for path in previous],
            "previous_images": old_images,
        }, indent=2))

        activated = False
        try:
            print(f"Building {commit[:12]}; current services remain running.", flush=True)
            compose(candidate, "config", "--quiet")
            config = json.loads(compose(candidate, "config", "--format", "json", capture=True))
            # Data must bind to the original server directory, never the release snapshot.
            data_dir = str((REPO / "LIVE-PROD/data").resolve())
            for service in SERVICES:
                mounts = config["services"][service].get("volumes", [])
                if not any(mount.get("source") == data_dir and mount.get("target") == "/app/data" for mount in mounts):
                    raise ValueError("Production data mount differs from the persistent server directory.")
                image = config["services"][service].get("image")
                if image and image != f"{PROJECT}-{service}":
                    raise ValueError("Unexpected application image name.")
            del config  # Expanded environment values stay in memory, never in logs/backups.
            compose(candidate, "build", "api", "frontend")
            run(["sudo", "-n", "docker", "run", "--rm", "--network", "none",
                 "--mount", f"type=bind,src={app / 'tests'},dst=/tmp/irs-pin-tests,readonly",
                 f"{PROJECT}-api", "python", "-m", "unittest", "discover", "-s", "/tmp/irs-pin-tests",
                 "-p", "test_*sync.py", "-v"])

            # Do not activate an old job after a more recent push finishes testing.
            run(["git", "fetch", "origin", "develop"])
            if run(["git", "rev-parse", "origin/develop"], capture=True) != commit:
                print("Build superseded by a newer push; active services unchanged.", flush=True)
                return 0
            wait_healthy(timeout=1200)  # Synchronous batch work can temporarily block /health.
            check_checkout(commit)
            run(["git", "pull", "--ff-only", "origin", "develop"])
            if run(["git", "rev-parse", "HEAD"], capture=True) != commit:
                raise ValueError("Develop advanced during pull; refusing to activate a different commit.")
            activated = True
            compose(candidate, "up", "-d", "--no-build", "--pull", "never",
                    "--timeout", "3600", "--wait", "--wait-timeout", "180", "api", "frontend")
            wait_healthy()
            # Store only source/config paths and revision metadata, never credentials.
            record = {"commit": commit, "compose_files": [str(path) for path in candidate]}
            temporary = STATE / "active.tmp"
            temporary.write_text(json.dumps(record, indent=2))
            temporary.replace(active_path)
            (release / "SUCCESS").write_text(f"Healthy deployment of {commit}\n")
            print(f"Deployed {commit}; API and frontend healthy.", flush=True)
            return 0
        except Exception:
            if activated:
                print("Activation failed; restoring the previous images/configuration.", flush=True)
                compose([*previous, rollback_override], "up", "-d", "--no-build", "--pull", "never",
                        "--timeout", "3600", "--wait", "--wait-timeout", "180", "api", "frontend")
                wait_healthy()
                # Keep a stable copy of the previous effective rollback configuration.
                active_path.write_text(json.dumps({"compose_files": [str(path) for path in [*previous, rollback_override]],
                                                   "commit": "rolled-back"}, indent=2))
            raise
        finally:
            # A failed/superseded build must not leave default tags pointing at an unactivated image.
            if not (release / "SUCCESS").exists():
                for service, image in old_images.items():
                    run(["sudo", "-n", "docker", "tag", image, f"{PROJECT}-{service}"])


if __name__ == "__main__":
    os.umask(0o077)
    try:
        raise SystemExit(deploy(sys.argv[1]))
    except (ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"Deployment failed: {exc}", file=sys.stderr, flush=True)
        raise SystemExit(1)
