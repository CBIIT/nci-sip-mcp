"""The fixture companion installs only its reviewed Linux runtime, with PDM wheel hashes."""

import unittest

from scripts.companion_lock import render


def package(name, *, groups=None, marker=None, version="1.0", hashes=None):
    return {
        "name": name,
        "version": version,
        "groups": groups or ["server"],
        "marker": marker,
        "files": [
            {"file": f"{name}.whl", "hash": value} for value in (hashes or ["sha256:" + "a" * 64])
        ],
    }


class CompanionLockTest(unittest.TestCase):
    def test_runtime_groups_exclude_embedding_and_build_tools_and_other_platforms(self):
        lock = {
            "package": [
                package("mcp"),
                package("numpy", groups=["index", "embeddings"]),
                package("pytest", groups=["test"]),
                package("pyyaml", groups=["acceptance"]),
                package("torch", groups=["embeddings"]),
                package("ruff", groups=["lint"]),
                package("zensical", groups=["docs"]),
                package("pywin32", marker='sys_platform == "win32"'),
                {"name": "nci-sip-acceptance", "groups": ["acceptance"], "editable": True},
            ]
        }
        text = render(lock)
        for name in ("mcp", "numpy", "pytest", "pyyaml"):
            self.assertIn(name + "==1.0", text)
        for name in ("torch", "ruff", "zensical", "pywin32", "nci-sip-acceptance"):
            self.assertNotIn(name + "==", text)
        self.assertIn("--hash=sha256:" + "a" * 64, text)

    def test_duplicate_extra_rows_merge_wheel_hashes_without_duplicate_requirements(self):
        rows = [package("coverage"), package("coverage", hashes=["sha256:" + "b" * 64])]
        text = render({"package": rows})
        self.assertEqual(text.count("coverage==1.0"), 1)
        self.assertIn("--hash=sha256:" + "a" * 64, text)
        self.assertIn("--hash=sha256:" + "b" * 64, text)

    def test_missing_bad_hashes_and_conflicting_versions_do_not_make_an_unlocked_image(self):
        for rows in (
            [package("mcp") | {"files": []}],
            [package("mcp", hashes=["sha256:bad"])],
            [package("mcp"), package("mcp", version="2.0")],
        ):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                render({"package": rows})
