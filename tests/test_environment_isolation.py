import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GIT = shutil.which("git")
# Reaches Settings.from_env() without setting anything itself.
AFFECTED = (
    "tests/test_release_selection.py::ReleaseSelectionTest::"
    "test_real_stateless_http_resolves_each_call"
)


class DeveloperEnvironmentTest(unittest.TestCase):
    def test_server_fixtures_do_not_share_mutable_concepts(self):
        from test_server import ServerFixture  # noqa: PLC0415 - only this isolation contract

        first = ServerFixture()
        self.addCleanup(first.doCleanups)
        first.setUp()
        first.evs.concepts["C3262"]["definitions"] = [{"definition": "first fixture only"}]
        second = ServerFixture()
        self.addCleanup(second.doCleanups)
        second.setUp()

        self.assertNotIn("definitions", second.evs.concepts["C3262"])
        self.assertEqual(
            first.evs.concepts["C3262"]["definitions"], [{"definition": "first fixture only"}]
        )

    def test_a_developers_settings_do_not_decide_a_unit_tests_result(self):
        # Given a shell that selects the fixture upstream mode and holds an invalid licence key,
        # as a developer's may; the fixture must remove every such variable, not just one
        environment = {
            **os.environ,
            "NCI_SIP_UPSTREAM_MODE": "fixture",
            "NCI_SIP_EVS_LICENSE_KEY": "k",
        }
        # When one unit test that builds its settings from the environment runs in a child
        run = subprocess.run(  # noqa: S603 - fixed executable and a repository test id
            [sys.executable, "-m", "pytest", AFFECTED, "-q", "-p", "no:cacheprovider", "--no-cov"],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        # Then it passes exactly as it does in a clean shell
        self.assertEqual(run.returncode, 0, run.stdout[-1500:])

    def test_git_fixture_state_cannot_reach_a_decoy_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            decoy = Path(directory) / "decoy"
            decoy.mkdir()
            clean = {
                name: value for name, value in os.environ.items() if not name.startswith("GIT_")
            }
            subprocess.run(  # noqa: S603 - resolved Git in an owned decoy repository.
                [GIT, "init", "--quiet"],
                cwd=decoy,
                env=clean,
                check=True,
            )
            subprocess.run(  # noqa: S603 - resolved Git in an owned decoy repository.
                [
                    GIT,
                    "-c",
                    "user.name=Setup role",
                    "-c",
                    "user.email=setup@example.invalid",
                    "commit",
                    "--allow-empty",
                    "--quiet",
                    "-m",
                    "test: decoy baseline",
                ],
                cwd=decoy,
                env=clean,
                check=True,
            )
            git_dir = decoy / ".git"

            def snapshot():
                refs = subprocess.check_output(  # noqa: S603 - fixed Git read in the decoy.
                    [GIT, "for-each-ref", "--format=%(refname) %(objectname)"],
                    cwd=decoy,
                    env=clean,
                    text=True,
                )
                log = subprocess.check_output(  # noqa: S603 - fixed Git read in the decoy.
                    [GIT, "log", "--format=%H %s"],
                    cwd=decoy,
                    env=clean,
                    text=True,
                )
                hooks = sorted(path.name for path in (git_dir / "hooks").iterdir())
                return (git_dir / "config").read_bytes(), refs, hooks, log

            before = snapshot()
            environment = os.environ | {
                "GIT_DIR": str(git_dir),
                "GIT_INDEX_FILE": str(git_dir / "index"),
                "GIT_WORK_TREE": str(decoy),
                "GIT_COMMON_DIR": str(git_dir),
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "core.bare",
                "GIT_CONFIG_VALUE_0": "true",
            }
            run = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "tests/test_branch_cleanup.py",
                    "-p",
                    "no:cacheprovider",
                    "-q",
                ],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )

            self.assertEqual(run.returncode, 0, run.stdout[-1500:] + run.stderr[-1500:])
            self.assertEqual(snapshot(), before)
            self.assertEqual(
                subprocess.run(  # noqa: S603 - fixed Git config read in the decoy.
                    [GIT, "config", "--local", "--get-regexp", "^user\\."],
                    cwd=decoy,
                    env=clean,
                    capture_output=True,
                    text=True,
                    check=False,
                ).stdout,
                "",
            )
