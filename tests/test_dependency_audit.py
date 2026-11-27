"""The workflow audits the lock's full inventory and propagates failed verdicts."""

import json
import os
import shutil
import subprocess
import sys
import tomllib
import unittest
from collections.abc import Iterable, Iterator
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import yaml
from packaging.utils import canonicalize_name
from packaging.version import Version
from pdm_launch import child_environment, create_project, launch
from pdm_stand_in import changed, hostile_selectors, outer_stand_in, tree
from pip_audit._cli import audit
from pip_audit._service.interface import Dependency, VulnerabilityID, VulnerabilityResult

ROOT = Path(__file__).resolve().parents[1]
LOCAL_ACCEPTANCE = "-e ./acceptance#egg=nci-sip-acceptance\n"
# The exporter may be real or provide bounded fixtures; the audit always runs its real CLI.
PDM_BOUNDARY = """
pdm() {
    if [[ "$1" == export ]]; then
        case "$EXPORT_MODE" in
            real) command "$PDM_COMMAND" export --project "$PDM_EXPORT_PROJECT" "${@:2}" ;;
            fail) printf 'export failed\\n' >&2; return 23 ;;
            fixture) cp "$EXPORT_INPUT" "${@: -1}" ;;
        esac
    else
        "$AUDIT_PYTHON" "$AUDIT_FIXTURE" "${@:3}"
    fi
}
"""


def audit_block() -> str:
    workflow = yaml.safe_load((ROOT / ".github/workflows/audit.yml").read_text())
    matches = [
        step["run"]
        for step in workflow["jobs"]["pip-audit"]["steps"]
        if step.get("name") == "Audit every locked dependency group"
    ]
    if len(matches) != 1:
        raise AssertionError("Expected exactly one project dependency audit step")
    return matches[0]


def execute_audit(
    contents: str = "", *, mode: str = "fixture"
) -> tuple[subprocess.CompletedProcess[str], str, bool]:
    with TemporaryDirectory(prefix="audit workflow ") as directory:
        root = Path(directory)
        source = root / "export.txt"
        source.write_text(contents)
        requirements = root / "requirements.txt"
        requirements.write_text("stale-package==9.0\n")
        invoked = root / "audit-invoked"
        # The real export reads copies of the lock and project metadata in an owned project that
        # the wrapper selects explicitly; the workflow text itself is unchanged.
        project = create_project(root / "project", pyproject=(ROOT / "pyproject.toml").read_text())
        shutil.copyfile(ROOT / "pdm.lock", project / "pdm.lock")
        environment = {
            **child_environment(root / "home", os.environ),
            "RUNNER_TEMP": str(root),
            "EXPORT_INPUT": str(source),
            "EXPORT_MODE": mode,
            "PDM_COMMAND": shutil.which("pdm") or "pdm",
            "PDM_EXPORT_PROJECT": str(project),
            # The test module is also the offline auditor entry point, and imports the launcher.
            "PYTHONPATH": str(ROOT / "test_support"),
            "AUDIT_PYTHON": sys.executable,
            "AUDIT_FIXTURE": str(Path(__file__).resolve()),
            "AUDIT_INVOKED": str(invoked),
            "PIP_AUDIT_VULNERABILITY_SERVICE": "pypi",
        }
        result = launch(
            [
                "bash",
                "--noprofile",
                "--norc",
                "-eo",
                "pipefail",
                "-c",
                PDM_BOUNDARY + audit_block(),
            ],
            root,
            environment,
            300,
            project,
        )
        return result, requirements.read_text(), invoked.exists()


class OfflineAdvisories:
    """Fixed responses at the advisory-service boundary; never contacts a registry."""

    def query_all(
        self, dependencies: Iterable[Dependency]
    ) -> Iterator[tuple[Dependency, list[VulnerabilityResult]]]:
        for dependency in dependencies:
            vulnerabilities = []
            if dependency.name == "audit-vulnerable":
                vulnerabilities = [
                    VulnerabilityResult(
                        VulnerabilityID("PYSEC-2099-1"),
                        "Offline workflow fixture vulnerability",
                        [Version("2.0")],
                        set(),
                    )
                ]
            yield dependency, vulnerabilities


class DependencyAuditInputTest(unittest.TestCase):
    def locked_inventory(self):
        packages = tomllib.loads((ROOT / "pdm.lock").read_text())["package"]
        local = [package for package in packages if "path" in package]
        self.assertEqual(
            [(p["name"], p["path"], p.get("editable")) for p in local],
            [("nci-sip-acceptance", "./acceptance", True)],
        )
        return {
            (canonicalize_name(package["name"]), package["version"])
            for package in packages
            if "path" not in package
        }

    def test_real_export_ignores_inherited_pdm_selectors(self):
        with TemporaryDirectory() as directory:
            outer = outer_stand_in(Path(directory).resolve())
            before = tree(outer)
            with patch.dict(os.environ, hostile_selectors(outer)):
                os.environ.pop("PDM_IGNORE_ACTIVE_VENV", None)
                result, _requirements, _ = execute_audit(mode="real")
            self.assertEqual(changed(before, tree(outer)), {}, result.stderr)
            self.assertEqual(result.returncode, 0, result.stderr)
            actual = {
                (canonicalize_name(dependency["name"]), dependency["version"])
                for dependency in json.loads(result.stdout)["dependencies"]
            }
            self.assertEqual(actual, self.locked_inventory())

    def test_every_external_locked_version_reaches_the_real_auditor(self):
        expected = self.locked_inventory()

        result, requirements, _ = execute_audit(mode="real")

        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        actual = {
            (canonicalize_name(dependency["name"]), dependency["version"])
            for dependency in report["dependencies"]
        }
        self.assertEqual(actual, expected)
        self.assertEqual(report["fixes"], [])
        self.assertNotIn(";", requirements)
        self.assertNotIn("[", requirements)
        self.assertNotIn("-e ", requirements)

    def test_only_the_known_local_project_is_excluded(self):
        result, requirements, _ = execute_audit(LOCAL_ACCEPTANCE + "audit-safe==1.0\n")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(requirements, "audit-safe==1.0\n")
        self.assertEqual(
            json.loads(result.stdout),
            {"dependencies": [{"name": "audit-safe", "version": "1.0", "vulns": []}], "fixes": []},
        )

    def test_foreign_platform_requirement_is_audited_instead_of_skipped(self):
        result, requirements, _ = execute_audit('audit-safe==1.0; sys_platform == "foreign"\n')

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            {"dependencies": [{"name": "audit-safe", "version": "1.0", "vulns": []}], "fixes": []},
        )
        self.assertEqual(requirements, "audit-safe==1.0\n")

    def test_unexpected_editable_or_url_cannot_be_reported_as_a_clean_audit(self):
        for requirement, error in (
            (
                "-e ./another-project#egg=another-project\n",
                "requirement another-project is not pinned: another-project from ./another-project",
            ),
            (
                "external @ https://example.invalid/external.whl\n",
                "external: URL requirements cannot be pinned to a specific package version",
            ),
        ):
            with self.subTest(requirement=requirement):
                result, requirements, _ = execute_audit(requirement)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertEqual(requirements, requirement)
                self.assertEqual(result.stdout, "")
                self.assertIn("ERROR:pip_audit._cli:" + error, result.stderr)

    def test_unpinned_requirement_fails_at_the_source_boundary(self):
        result, requirements, _ = execute_audit("audit-safe\n")

        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(requirements, "audit-safe\n")
        self.assertEqual(result.stdout, "")
        self.assertIn("requirement audit-safe is not pinned: audit-safe", result.stderr)

    def test_conflicting_locked_versions_are_preserved_and_rejected(self):
        contents = "audit-safe==1.0\naudit-safe==2.0\n"
        result, requirements, _ = execute_audit(contents)

        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(requirements, contents)
        self.assertEqual(result.stdout, "")
        self.assertIn(
            "package audit-safe has duplicate requirements: audit-safe==2.0", result.stderr
        )

    def test_export_failure_never_audits_the_stale_file(self):
        result, requirements, invoked = execute_audit(mode="fail")

        self.assertEqual(result.returncode, 23)
        self.assertEqual(result.stderr, "export failed\n")
        self.assertEqual(result.stdout, "")
        self.assertEqual(requirements, "stale-package==9.0\n")
        self.assertFalse(invoked)

    def test_vulnerability_verdict_fails_the_workflow_with_its_report(self):
        result, requirements, _ = execute_audit("audit-vulnerable==1.0\n")

        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(requirements, "audit-vulnerable==1.0\n")
        self.assertEqual(
            json.loads(result.stdout)["dependencies"],
            [
                {
                    "name": "audit-vulnerable",
                    "version": "1.0",
                    "vulns": [
                        {
                            "id": "PYSEC-2099-1",
                            "fix_versions": ["2.0"],
                            "aliases": [],
                            "description": "Offline workflow fixture vulnerability",
                        }
                    ],
                }
            ],
        )


if __name__ == "__main__":
    marker = Path(os.environ["AUDIT_INVOKED"])
    marker.write_text("invoked\n")
    sys.argv = ["pip-audit", *sys.argv[1:], "--format=json", "--cache-dir", str(marker.parent)]
    with patch("pip_audit._cli.PyPIService.query_all", OfflineAdvisories().query_all):
        audit()
