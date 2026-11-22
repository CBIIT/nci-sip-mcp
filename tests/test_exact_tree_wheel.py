"""Smoke wheels contain the working tree's bytes, committed or not."""

import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path

PYPROJECT = (
    '[build-system]\nrequires = ["pdm-backend>=2.4"]\nbuild-backend = "pdm.backend"\n'
    '[project]\nname = "demo-pkg"\nversion = "1.0"\n'
    "[tool.pdm]\ndistribution = true\n"
)


def git(root, *arguments):
    subprocess.run(["git", "-C", str(root), *arguments], check=True, capture_output=True)  # noqa: S603, S607


class ExactTreeWheelTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "repo"
        (self.root / "src/demo_pkg").mkdir(parents=True)
        (self.root / "pyproject.toml").write_text(PYPROJECT, encoding="utf-8")
        (self.root / "src/demo_pkg/__init__.py").write_text("VALUE = 'committed'\n")
        self.dist = Path(temporary.name) / "dist"
        git(self.root, "init", "-q")

    def built_source(self):
        from scripts.exact_tree_wheel import exact_tree_wheel  # noqa: PLC0415

        wheel = exact_tree_wheel(self.root, self.dist)
        with zipfile.ZipFile(wheel) as archive:
            return archive.read("demo_pkg/__init__.py").decode()

    def test_a_repository_without_any_commit_builds_from_its_files(self):
        self.assertEqual(self.built_source(), "VALUE = 'committed'\n")

    def test_an_uncommitted_edit_of_a_tracked_file_is_in_the_wheel(self):
        git(self.root, "add", "-A")
        git(
            self.root,
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.invalid",
            "commit",
            "-q",
            "-m",
            "base",
        )
        (self.root / "src/demo_pkg/__init__.py").write_text("VALUE = 'edited'\n")

        self.assertEqual(self.built_source(), "VALUE = 'edited'\n")
