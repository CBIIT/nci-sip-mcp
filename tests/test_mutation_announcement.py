"""Piped CLI evidence is readable before the scheduler is allowed to launch."""

import json
import os
import selectors
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.processes import stop

from fakes import project
from test_mutation_gate import ROOT

CHILD = """
import sys
from unittest.mock import patch
from scripts.validation import mutation_gate as gate
from scripts.validation.mutation_run import CoverageEvidence
launch = gate.run_scheduler
def held(*args):
    print('HELD', flush=True)
    assert sys.stdin.readline() == 'release\\n'
    return launch(*args)
mapping = CoverageEvidence(
    __import__('pathlib').Path('.'),
    {'unit': {'src/example.py': {2: ['tests/test_example.py::test_value|run']}}},
    (),
)
with patch.object(gate, 'resolve_coverage', return_value=mapping), \\
     patch.object(gate, 'run_scheduler', held):
    raise SystemExit(gate.main(sys.argv[1:]))
"""


def receive(stream, ending=b"\n"):
    data = b""
    with selectors.DefaultSelector() as selector:
        selector.register(stream, selectors.EVENT_READ)
        while not data.endswith(ending):
            if not selector.select(timeout=30) or not (chunk := os.read(stream.fileno(), 65536)):
                raise AssertionError("CLI did not publish announcement/handshake before release")
            data += chunk
        return data


class MutationAnnouncementTest(unittest.TestCase):
    def test_piped_announcement_names_existing_directory_before_scheduler_release(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            project(root, "def answer():\n    return True\n")
            env = os.environ.copy()
            env.pop("PYTHONUNBUFFERED", None)
            command = [sys.executable, "-c", CHILD, "--root", str(root)]
            command += ["--paths", "src/example.py"]
            with subprocess.Popen(  # noqa: S603 - fixed CLI, owned tiny project
                command,
                cwd=ROOT,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            ) as process:
                try:
                    self.assertEqual(receive(process.stdout, b"HELD\n").splitlines()[-1], b"HELD")
                    announcement = json.loads(receive(process.stderr))
                    work = Path(announcement["directory"])
                    self.assertEqual(announcement, {"outcome": "EVIDENCE", "directory": str(work)})
                    self.assertEqual(work.parent, root / "tmp/mutation-runs")
                    self.assertTrue(work.is_dir())
                    self.assertFalse((work / "mutations.json").exists())
                    stdout, stderr = process.communicate(input=b"release\n", timeout=90)
                    self.assertEqual(process.returncode, 0, (stdout, stderr))
                    manifest = json.loads((work / "mutations.json").read_text())
                    self.assertEqual(set(manifest["sources"]), {"src/example.py"})
                    rows = [json.loads(line) for line in stdout.splitlines()]
                    self.assertEqual(len(rows), len(manifest["mutants"]))
                    self.assertEqual({row["outcome"] for row in rows}, {"KILLED"})
                    for index, candidate in enumerate(manifest["mutants"]):
                        result = json.loads((work / f"result-{index}.json").read_text())
                        self.assertEqual(result["site"], candidate["site"])
                        self.assertTrue(result["restored"])
                        self.assertEqual(result["node"], "tests/test_example.py::test_value")
                finally:
                    stop(process)
