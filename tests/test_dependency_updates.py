"""Dependency selection and update automation cover every committed ecosystem."""

import importlib.util
import io
import re
import subprocess
import tomllib
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import call, patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
LOCK_SCRIPT = ROOT / "scripts" / "dependency_lock.py"


def read_yaml(name):
    return yaml.safe_load((ROOT / name).read_text(encoding="utf-8"))


def tracked(pattern):
    result = subprocess.run(  # noqa: S603 - Git lists this repository's tracked manifests.
        ["git", "ls-files", pattern],  # noqa: S607 - Git is the repository boundary.
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return [ROOT / name for name in result.stdout.splitlines()]


def lock_script():
    if not LOCK_SCRIPT.is_file():
        raise AssertionError("scripts/dependency_lock.py must own the lock command")
    spec = importlib.util.spec_from_file_location("dependency_lock", LOCK_SCRIPT)
    if spec is None or spec.loader is None:
        raise AssertionError("scripts/dependency_lock.py must be loadable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def compose_manifests(root=ROOT):
    patterns = ("**/*compose*.yaml", "**/*compose*.yml")
    ignored = {".git", ".venv", "tmp"}
    return {
        path
        for pattern in patterns
        for path in root.glob(pattern)
        if not ignored.intersection(path.relative_to(root).parts)
    }


def registry_images(path):
    compose = yaml.safe_load(path.read_text(encoding="utf-8"))
    images = {
        service["image"] for service in compose.get("services", {}).values() if "image" in service
    }
    local = {"nci-si-admin:local", "nci-si-docs:local", "nci-sip-mcp:verified"}
    return images - local


class DependencyLockPolicyTest(unittest.TestCase):
    def test_lock_cutoff_tracks_an_independently_changed_shared_minimum_age(self):
        module = lock_script()
        with patch("scripts.dependency_age.MINIMUM_AGE", timedelta(days=7)):
            self.assertEqual(module.command(ROOT), ["pdm", "lock", "--exclude-newer", "7d"])

    def test_lock_command_uses_only_the_relative_five_day_cutoff(self):
        project = tomllib.loads((ROOT / "pyproject.toml").read_text())
        scripts = project["tool"]["pdm"]["scripts"]

        self.assertEqual(scripts["lock"], "python -m scripts.dependency_lock")
        self.assertEqual(
            project["tool"]["pdm"]["options"],
            {
                "install": ["-G", "server", "-G", "index"],
                "sync": ["-G", "server", "-G", "index"],
                "lock": ["-G", ":all"],
            },
        )
        self.assertIsNone(re.search(r"20\d\d-\d\d-\d\d|T\d\d:", scripts["lock"]))
        self.assertEqual(lock_script().read_exceptions(ROOT), {})
        self.assertEqual(lock_script().command(ROOT), ["pdm", "lock", "--exclude-newer", "5d"])

    def test_security_exception_uses_the_latest_distribution_upload_cutoff(self):
        def metadata(url):
            return {
                "urls": [
                    {"upload_time_iso_8601": "2026-10-19T00:00:00Z"},
                    {"upload_time_iso_8601": "2026-10-20T19:01:00Z"},
                ]
            }

        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "pyproject.toml").write_text(
                "[tool.nci-si.dependency-age.exceptions]\n"
                'setuptools = {version = "84.2.1", advisory = "GHSA-xmj3-9gf7-h52w"}\n'
            )
            arguments = lock_script().command(root, metadata)
        self.assertEqual(
            arguments,
            [
                "pdm",
                "lock",
                "--exclude-newer",
                "5d",
                "--exclude-newer-override",
                "setuptools=2026-10-20T19:01:00+00:00",
            ],
        )

    def test_lock_refuses_unverifiable_exception_upload_times(self):
        module = lock_script()
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "pyproject.toml").write_text(
                "[tool.nci-si.dependency-age.exceptions]\n"
                'setuptools = {version = "84.2.1", advisory = "GHSA-xmj3-9gf7-h52w"}\n'
            )
            with self.assertRaisesRegex(
                ValueError,
                "^setuptools upload time could not be read: has no distribution upload time$",
            ):
                module.command(root, lambda _: {"urls": []})

    def test_lock_entry_point_preserves_resolver_failure_status(self):
        module = lock_script()
        with (
            patch.object(module, "command", return_value=["pdm", "lock", "--exclude-newer", "5d"]),
            patch.object(
                module.subprocess, "run", return_value=subprocess.CompletedProcess([], 7)
            ) as resolver,
        ):
            self.assertEqual(module.main(), 7)
        self.assertEqual(
            resolver.call_args,
            call(["pdm", "lock", "--exclude-newer", "5d"], cwd=ROOT, check=False),
        )
        output, errors = io.StringIO(), io.StringIO()
        with (
            patch.object(
                module,
                "command",
                side_effect=ValueError("Dependency age exceptions must be a table"),
            ),
            redirect_stdout(output),
            redirect_stderr(errors),
        ):
            status = module.main()
        self.assertEqual(status, 1)
        self.assertEqual(errors.getvalue(), "Dependency age exceptions must be a table\n")
        self.assertEqual(output.getvalue(), "")


class DependencyAuditWorkflowTest(unittest.TestCase):
    def test_audit_checks_age_then_vulnerabilities(self):
        workflow = read_yaml(".github/workflows/audit.yml")
        self.assertEqual(
            workflow[True],
            {
                "workflow_dispatch": None,
                "pull_request": None,
                "push": {"branches": ["main"]},
                "schedule": [{"cron": "17 6 * * 1"}],
            },
        )
        self.assertEqual(workflow["permissions"], {"contents": "read"})
        self.assertEqual(
            workflow["concurrency"],
            {
                "group": (
                    "audit-${{ github.event_name == 'pull_request' && github.ref || github.sha }}"
                ),
                "cancel-in-progress": "${{ github.event_name == 'pull_request' }}",
            },
        )
        job = workflow["jobs"]["pip-audit"]
        self.assertEqual(
            {key: value for key, value in job.items() if key != "steps"},
            {"name": "pip-audit", "runs-on": "ubuntu-24.04", "timeout-minutes": 10},
        )
        self.assertEqual(
            job["steps"],
            [
                {
                    "uses": "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
                    "with": {"fetch-depth": 0, "persist-credentials": False},
                },
                {"uses": "$/.github/actions/setup"},
                {
                    "name": "Reject dependencies released inside five days",
                    "run": "pdm run python scripts/dependency_age.py",
                },
                {
                    "name": "Audit every locked dependency group",
                    "run": (
                        "pdm export -G :all --without-hashes --no-extras "
                        '-o "$RUNNER_TEMP/all-requirements.txt"\n'
                        "sed -e '\\|^-e \\./acceptance#egg=nci-sip-acceptance$|d' "
                        "-e 's/;.*//' "
                        '"$RUNNER_TEMP/all-requirements.txt" > "$RUNNER_TEMP/requirements.txt"\n'
                        'pdm run pip-audit -r "$RUNNER_TEMP/requirements.txt" '
                        "--no-deps --disable-pip --strict\n"
                    ),
                },
                {
                    "name": "Audit the actual PDM tool environment",
                    "env": {"PDM_TOOL_HOME": "${{ runner.temp }}/pdm-tool"},
                    "run": (
                        'tool_site="$("$PDM_TOOL_HOME/venv/bin/python" -c '
                        '\'import sysconfig; print(sysconfig.get_path("purelib"))\')"\n'
                        'pdm run pip-audit --path "$tool_site" --strict\n'
                    ),
                },
            ],
        )


class DependabotTest(unittest.TestCase):
    def test_new_compose_files_cannot_escape_registry_image_discovery(self):
        with TemporaryDirectory(prefix="tmp") as directory:
            root = Path(directory)
            root = root / "tmp"
            root.mkdir()
            manifest = root / "compose.new.yml"
            manifest.write_text("services:\n  app:\n    image: nci-si-other:latest\n")

            self.assertEqual(compose_manifests(root), {manifest})
            self.assertEqual(registry_images(manifest), {"nci-si-other:latest"})

    def entries(self):
        return read_yaml(".github/dependabot.yml")["updates"]

    def test_every_update_entry_has_the_exact_policy(self):
        expected = {
            ("github-actions", "/"): "actions",
            ("docker", "/"): "docker",
            ("docker", "/container"): "docker",
            ("npm", "/docs/site-assets"): "npm",
        }
        actual = {}
        for entry in self.entries():
            key = (entry["package-ecosystem"], entry["directory"])
            (group,) = entry["groups"]
            self.assertEqual(
                {k: entry[k] for k in ("schedule", "cooldown", "groups", "commit-message")},
                {
                    "schedule": {"interval": "weekly"},
                    "cooldown": {"default-days": 7},
                    "groups": {group: {"patterns": ["*"]}},
                    "commit-message": {"prefix": "ci"},
                },
            )
            actual[key] = group
        self.assertEqual(actual, expected)

    def test_every_dockerfile_directory_is_covered(self):
        entries = {(row["package-ecosystem"], row["directory"]) for row in self.entries()}
        docker_directories = {
            "/" + str(path.parent.relative_to(ROOT)).replace(".", "").strip("/")
            for path in tracked("*Dockerfile*")
        }
        docker_directories = {directory or "/" for directory in docker_directories}

        self.assertEqual(
            docker_directories,
            {directory for kind, directory in entries if kind == "docker"},
        )

    def test_every_npm_lock_directory_is_covered(self):
        entries = {(row["package-ecosystem"], row["directory"]) for row in self.entries()}
        npm_directories = {
            "/" + str(path.parent.relative_to(ROOT)) for path in tracked("*package-lock.json")
        }

        self.assertEqual(
            npm_directories,
            {directory for kind, directory in entries if kind == "npm"},
        )

    def test_every_compose_manifest_is_local_or_covered(self):
        entries = {(row["package-ecosystem"], row["directory"]) for row in self.entries()}
        manifests = compose_manifests()
        self.assertTrue(manifests)
        for path in manifests:
            with self.subTest(path=path.relative_to(ROOT)):
                images = registry_images(path)
                directory = "/" + str(path.parent.relative_to(ROOT))
                self.assertTrue(
                    not images or ("docker-compose", directory) in entries,
                    f"{path.relative_to(ROOT)} registry images: {sorted(images)}",
                )

    def test_python_uses_the_age_checked_pdm_path(self):
        text = (ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8")

        self.assertNotIn("pip", {entry["package-ecosystem"] for entry in self.entries()})
        self.assertIn("Dependabot cannot update pdm.lock", text)
