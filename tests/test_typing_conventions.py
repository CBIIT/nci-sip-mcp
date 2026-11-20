"""Tracked source escape counts are exact: reductions lower the ratchet immediately."""

import ast
import io
import subprocess
import tokenize
import unittest
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIMITS = {
    "src/nci_sip_mcp": (478, 11, 1, 0),
    "src/ncit_index": (10, 0, 2, 0),
    "scripts": (196, 14, 0, 0),
    "acceptance/src": (177, 0, 0, 0),
}
KINDS = ("dict_any", "cast", "pyright_ignore", "type_ignore")


def escapes(source):
    tree = ast.parse(source)
    counts = Counter()
    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript):
            counts["dict_any"] += ast.unparse(node) in {
                "dict[str, Any]",
                "typing.Dict[str, Any]",
                "Dict[str, Any]",
            }
        if isinstance(node, ast.Call):
            counts["cast"] += ast.unparse(node.func) in {"cast", "typing.cast"}
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT:
            counts["pyright_ignore"] += "pyright: ignore" in token.string
            counts["type_ignore"] += "type: ignore" in token.string
    return tuple(counts[kind] for kind in KINDS)


class TypingConventionsTest(unittest.TestCase):
    def test_each_package_matches_its_exact_recorded_escape_counts(self):
        names = subprocess.check_output(
            ["git", "ls-files", "-z", "*.py"],  # noqa: S607
            cwd=ROOT,
            text=True,
        ).split("\0")
        for package, expected in LIMITS.items():
            measured = Counter()
            for name in names:
                if name.startswith(package + "/"):
                    measured.update(
                        dict(zip(KINDS, escapes((ROOT / name).read_text()), strict=True))
                    )
            for kind, limit in zip(KINDS, expected, strict=True):
                with self.subTest(package=package, kind=kind):
                    self.assertEqual(
                        measured[kind],
                        limit,
                        "When usage falls, lower the recorded value; "
                        "when usage rises, remove the escape.",
                    )

    def test_aliases_and_cast_targets_count_but_strings_and_unrelated_names_do_not(self):
        self.assertEqual(
            escapes(
                "Alias = dict[str, Any]\ntype Other = typing.Dict[str, Any]\n"
                "value = cast(dict[str, Any], raw)\nother = typing.cast(int, raw)\n"
                "forecast(raw)\ntext = 'cast(dict[str, Any], x) # type: ignore'\n"
                "# pyright: ignore[reportUnknown]\n# type: ignore[arg-type]\n"
            ),
            (3, 2, 1, 1),
        )
