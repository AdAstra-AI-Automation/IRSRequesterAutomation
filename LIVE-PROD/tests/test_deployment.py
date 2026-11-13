"""Deployment guards use temporary Git repositories; release tests mock Docker."""

from contextlib import nullcontext
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import deploy_ec2 as deploy, github_deploy_entrypoint as entrypoint


class DeploymentGuardTests(unittest.TestCase):
    def setUp(self):
        self.folder = TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.repo = Path(self.folder.name)
        self.git("init", "-b", "develop")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Deployment Test")
        (self.repo / "LIVE-PROD/data").mkdir(parents=True)
        (self.repo / "LIVE-PROD/data/audit.db").write_bytes(b"original audit")
        (self.repo / "LIVE-PROD/app.py").write_text("# original\n")
        self.git("add", ".")
        self.git("commit", "-m", "initial")

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.repo, stderr=subprocess.DEVNULL, text=True).strip()

    def incoming(self, path="LIVE-PROD/app.py", *, remove=False):
        self.git("switch", "-c", "incoming")
        if remove:
            self.git("rm", path)
        else:
            (self.repo / path).write_text("# incoming\n")
            self.git("add", path)
        self.git("commit", "-m", "incoming")
        commit = self.git("rev-parse", "HEAD")
        self.git("switch", "develop")
        return commit

    def test_source_update_allows_untouched_runtime_audit_changes(self):
        commit = self.incoming()
        audit = self.repo / "LIVE-PROD/data/audit.db"
        audit.write_bytes(b"live audit must survive")
        deploy.check_checkout(commit, repo=self.repo)
        self.assertEqual(audit.read_bytes(), b"live audit must survive")

    def test_runtime_database_deletion_is_rejected_before_data_changes(self):
        commit = self.incoming("LIVE-PROD/data/audit.db", remove=True)
        audit = self.repo / "LIVE-PROD/data/audit.db"
        audit.write_bytes(b"live audit")
        with self.assertRaisesRegex(ValueError, "runtime databases"):
            deploy.check_checkout(commit, repo=self.repo)
        self.assertEqual(audit.read_bytes(), b"live audit")

    def test_local_source_and_staged_edits_are_preserved(self):
        commit = self.incoming()
        app = self.repo / "LIVE-PROD/app.py"
        app.write_text("# operator change\n")
        for staged in (False, True):
            if staged:
                self.git("add", "LIVE-PROD/app.py")
            with self.assertRaisesRegex(ValueError, "source/staged changes"):
                deploy.check_checkout(commit, repo=self.repo)
            self.assertEqual(app.read_text(), "# operator change\n")

    def test_wrong_branch_and_invalid_commit_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "full commit SHA"):
            deploy.check_checkout("develop; arbitrary command", repo=self.repo)
        commit = self.incoming()
        self.git("switch", "incoming")
        with self.assertRaisesRegex(ValueError, "remain on develop"):
            deploy.check_checkout(commit, repo=self.repo)

    def test_runtime_credentials_cannot_arrive_in_a_commit(self):
        commit = self.incoming("LIVE-PROD/.env")
        with self.assertRaisesRegex(ValueError, "credentials"):
            deploy.check_checkout(commit, repo=self.repo)

    def test_forced_ssh_entrypoint_rejects_other_commands(self):
        sha = "a" * 40
        self.assertEqual(entrypoint.requested_commit("deploy " + sha), sha)
        for command in ("", "bash", "deploy develop", "deploy " + sha + "; id", "deploy " + sha + "\n"):
            with self.assertRaises(ValueError):
                entrypoint.requested_commit(command)


class ReleaseLifecycleTests(unittest.TestCase):
    def exercise(self, *, fail_activation):
        with TemporaryDirectory() as folder:
            base = Path(folder)
            repo = base / "repo"
            app = repo / "LIVE-PROD"
            app.mkdir(parents=True)
            (app / ".env").write_text("# fake test configuration\n")
            (app / "docker-compose.yml").write_text("services: {}\n")
            state = base / "state"
            state.mkdir()
            released_app = base / "release-source"
            released_app.mkdir()
            (released_app / "docker-compose.yml").write_text("services: {}\n")
            commit = "a" * 40
            commands = []
            compose_calls = []

            def fake_run(args, **kwargs):
                commands.append(args)
                if args[:2] == ["git", "rev-parse"]:
                    return commit
                if "inspect" in args:
                    return "sha256:previous-image"
                return ""

            def fake_compose(files, *args, **kwargs):
                compose_calls.append(([str(path) for path in files], args))
                if args[:2] == ("ps", "-q"):
                    return "test-container"
                if args[:3] == ("config", "--format", "json"):
                    return json.dumps({"services": {
                        service: {"volumes": [{"source": str((app / 'data').resolve()), "target": "/app/data"}]}
                        for service in deploy.SERVICES
                    }})
                return ""

            health = [None, RuntimeError("Unhealthy new image"), None] if fail_activation else [None, None]
            with patch.object(deploy, "REPO", repo), patch.object(deploy, "STATE", state), \
                 patch.object(deploy, "deployment_lock", return_value=nullcontext()), \
                 patch.object(deploy, "check_checkout"), \
                 patch.object(deploy, "extract_release", return_value=released_app), \
                 patch.object(deploy, "run", side_effect=fake_run), \
                 patch.object(deploy, "compose", side_effect=fake_compose), \
                 patch.object(deploy, "wait_healthy", side_effect=health), \
                 patch.object(deploy.shutil, "disk_usage", return_value=SimpleNamespace(free=10 * 1024**3)):
                if fail_activation:
                    with self.assertRaisesRegex(RuntimeError, "Unhealthy"):
                        deploy.deploy(commit)
                else:
                    self.assertEqual(deploy.deploy(commit), 0)
            active = json.loads((state / "active.json").read_text())
            self.assertEqual(active["commit"], "rolled-back" if fail_activation else commit)
            ups = [args for _, args in compose_calls if args[0] == "up"]
            self.assertEqual(len(ups), 2 if fail_activation else 1)
            pull = commands.index(["git", "pull", "--ff-only", "origin", "develop"])
            self.assertTrue(any("unittest" in command for command in commands[:pull]))
            self.assertFalse(any("reset" in command or "clean" in command for command in commands))

    def test_success_records_exact_deployed_commit(self):
        self.exercise(fail_activation=False)

    def test_unhealthy_activation_restores_previous_images_and_config(self):
        self.exercise(fail_activation=True)


if __name__ == "__main__":
    unittest.main()
