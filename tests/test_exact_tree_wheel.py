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

    def build(self, subdir="."):
        from scripts.exact_tree_wheel import exact_tree_wheel  # noqa: PLC0415

        return exact_tree_wheel(self.root, self.dist, subdir)

    def commit(self):
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

    def built_source(self):
        with zipfile.ZipFile(self.build()) as archive:
            return archive.read("demo_pkg/__init__.py").decode()

    def test_a_repository_without_any_commit_builds_from_its_files(self):
        self.assertEqual(self.built_source(), "VALUE = 'committed'\n")

    def test_an_uncommitted_edit_of_a_tracked_file_is_in_the_wheel(self):
        self.commit()
        (self.root / "src/demo_pkg/__init__.py").write_text("VALUE = 'edited'\n")

        self.assertEqual(self.built_source(), "VALUE = 'edited'\n")

    def test_the_build_leaves_the_repository_index_and_status_untouched(self):
        self.commit()
        package = self.root / "src/demo_pkg"
        (package / "staged.py").write_text("STAGED = 1\n")
        git(self.root, "add", "src/demo_pkg/staged.py")
        (package / "__init__.py").write_text("VALUE = 'modified'\n")
        (package / "fresh.py").write_text("FRESH = 1\n")

        def observed():
            return [
                subprocess.run(  # noqa: S603
                    ["git", "-C", str(self.root), *arguments],  # noqa: S607
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout
                for arguments in (("diff", "--cached", "--name-only"), ("status", "--porcelain"))
            ]

        before = observed()
        wheel = self.build()

        self.assertEqual(before[0], "src/demo_pkg/staged.py\n")
        self.assertEqual(observed(), before)
        with zipfile.ZipFile(wheel) as archive:
            self.assertEqual(archive.read("demo_pkg/fresh.py"), b"FRESH = 1\n")

    def test_a_subdirectory_wheel_has_tracked_files_and_omits_ignored_ones(self):
        package = self.root / "pkgs/inner/src/inner_pkg"
        package.mkdir(parents=True)
        (self.root / "pkgs/inner/pyproject.toml").write_text(
            PYPROJECT.replace("demo-pkg", "inner-pkg"), encoding="utf-8"
        )
        (package / "__init__.py").write_text("INNER = 1\n")
        (package / "secret.py").write_text("SECRET = 1\n")
        (self.root / ".gitignore").write_text("secret.py\n")
        self.commit()

        with zipfile.ZipFile(self.build("pkgs/inner")) as archive:
            names = set(archive.namelist())

        self.assertIn("inner_pkg/__init__.py", names)
        self.assertNotIn("inner_pkg/secret.py", names)

    def test_a_failed_build_reports_pips_error(self):
        (self.root / "pyproject.toml").write_text("not [valid toml", encoding="utf-8")

        with self.assertRaises(subprocess.CalledProcessError) as raised:
            self.build()

        stderr = raised.exception.stderr.decode()
        self.assertIn("TOMLDecodeError", stderr)
        self.assertIn(stderr.strip(), str(raised.exception))

    def test_a_non_empty_dist_dir_is_refused(self):
        self.dist.mkdir()
        (self.dist / "stale-1.0-py3-none-any.whl").write_bytes(b"")

        with self.assertRaisesRegex(ValueError, "dist_dir must be empty"):
            self.build()
