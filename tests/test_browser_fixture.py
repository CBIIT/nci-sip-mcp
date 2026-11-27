"""The browser journeys' server fixture runs as its own program, outside pytest's import paths."""

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/browser/test_servers.py"
# A finite, generous bound for start and stop, not a measured duration.
RUN_SECONDS = 60


class BrowserFixtureTest(unittest.TestCase):
    def test_the_fixture_starts_without_pytest_paths_and_stops_at_end_of_input(self):
        # Playwright starts it as `python tests/browser/test_servers.py <site>`
        # (docs/site-assets/browser/journeys.spec.mjs), where pytest's `pythonpath` does not apply.
        environment = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        # The fixture keeps its scratch under the checkout's ignored tmp/, as the journeys run it.
        (ROOT / "tmp").mkdir(exist_ok=True)
        with TemporaryDirectory() as temporary:
            site = Path(temporary)
            (site / "index.html").write_text("<!doctype html><title>fixture</title>\n")
            # The fixture prints its ready record, then serves until its input ends.
            ran = subprocess.run(  # noqa: S603 - this repository's fixture program
                [sys.executable, str(FIXTURE), str(site)],
                cwd=ROOT,
                env=environment,
                input="",
                capture_output=True,
                text=True,
                timeout=RUN_SECONDS,
                check=False,
            )
        self.assertEqual(ran.returncode, 0, f"stdout:\n{ran.stdout}\nstderr:\n{ran.stderr}")
        ready = json.loads(ran.stdout.splitlines()[0])
        self.assertEqual(set(ready), {"docs", "portal", "execution"})
        for name in ("docs", "portal"):
            with self.subTest(server=name):
                self.assertRegex(ready[name], r"^http://127\.0\.0\.1:[0-9]+$")
        scratch = Path(ready["execution"]).parent
        self.assertEqual(scratch.parent, ROOT / "tmp")
        self.assertTrue(scratch.name.startswith("browser-"), scratch)
        self.assertFalse(scratch.exists())
