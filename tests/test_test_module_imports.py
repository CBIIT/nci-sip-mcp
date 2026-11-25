"""Aggregate-ratchet syntactic imports of `test_*` modules and aliases under root `tests`."""

import ast
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CROSS_IMPORT_TARGETS = 179


def is_test_module(name: str) -> bool:
    return name.rsplit(".", maxsplit=1)[-1].startswith("test_")


def is_tests_package(name: str | None) -> bool:
    return name == "tests" or (name is not None and name.startswith("tests."))


def import_targets(node: ast.AST) -> int:
    if isinstance(node, ast.Import):
        return sum(is_test_module(alias.name) for alias in node.names)
    return import_from_targets(node) if isinstance(node, ast.ImportFrom) else 0


def import_from_targets(node: ast.ImportFrom) -> int:
    if node.module is not None and is_test_module(node.module):
        return 1
    if node.level > 0:
        return sum(is_test_module(alias.name) for alias in node.names)
    if is_tests_package(node.module):
        return sum(is_test_module(alias.name) for alias in node.names)
    return 0


def cross_import_targets(source: str) -> int:
    return sum(import_targets(node) for node in ast.walk(ast.parse(source)))


def tracked_test_sources() -> list[Path]:
    result = subprocess.run(
        ["/usr/bin/git", "ls-files", "-z", "tests"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return [ROOT / name for name in result.stdout.decode().split("\0") if name.endswith(".py")]


class TestModuleImportRatchet(unittest.TestCase):
    def test_scanner_counts_direct_qualified_multiline_and_relative_targets(self):
        source = """
import test_one, package.test_two
from test_three import (
    helper,
)
from package.test_four import helper
from . import test_five, helper
from tests import test_six, helper
from tests.package import test_seven, helper
from helper import test_value
"""

        self.assertEqual(cross_import_targets(source), 7)

    def test_tracked_test_files_keep_the_aggregate_cross_import_target_count(self):
        found = sum(
            cross_import_targets(path.read_text(encoding="utf-8"))
            for path in tracked_test_sources()
        )

        self.assertEqual(
            found,
            CROSS_IMPORT_TARGETS,
            "Move a touched shared helper to tests/fakes.py; lower the ratchet after a reduction",
        )
