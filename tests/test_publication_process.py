"""Exercise the credential and failure boundaries through real child processes."""

import io
import json
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from scripts import image_publish


class PublicationProcessTest(unittest.TestCase):
    def test_login_uses_stdin_and_a_failed_command_stops_publication(self):
        for accepted in (False, True):
            with self.subTest(accepted=accepted), TemporaryDirectory() as directory:
                root = Path(directory)
                executable = root / "docker"
                journal = root / "commands.jsonl"
                executable.write_text(
                    f"#!{sys.executable}\n"
                    "import json, os, sys\n"
                    "from pathlib import Path\n"
                    "operation = sys.argv[1]\n"
                    "if operation == 'release':\n"
                    "    raise SystemExit(print(json.dumps({'isDraft': True})))\n"
                    "record = {'operation': operation, 'arguments': sys.argv[2:]}\n"
                    "if operation == 'login':\n"
                    "    token = sys.stdin.read()\n"
                    "    record['authenticated'] = token == os.environ['EXPECTED_TOKEN']\n"
                    "with Path(os.environ['JOURNAL']).open('a') as stream:\n"
                    "    stream.write(json.dumps(record) + '\\n')\n"
                    "if operation == 'login':\n"
                    "    raise SystemExit(0 if os.environ['ACCEPT_LOGIN'] == '1' else 7)\n"
                    "if operation == 'push':\n"
                    "    raise SystemExit(3)\n"
                )
                executable.chmod(0o700)
                output = io.StringIO()
                environment = {
                    "GITHUB_REPOSITORY": "Example/Project",
                    "GITHUB_ACTOR": "release-bot",
                    "GH_TOKEN": "fixture-token-for-stdin",
                    "EXPECTED_TOKEN": "fixture-token-for-stdin",
                    "ACCEPT_LOGIN": str(int(accepted)),
                    "JOURNAL": str(journal),
                }
                with (
                    patch.dict(os.environ, environment),
                    patch.object(image_publish.shutil, "which", return_value=str(executable)),
                    patch("sys.argv", ["image_publish", "v0.15.1"]),
                    redirect_stdout(output),
                    self.assertRaises(subprocess.CalledProcessError) as failure,
                ):
                    image_publish.main()
                records = [json.loads(line) for line in journal.read_text().splitlines()]
                self.assertEqual(failure.exception.returncode, 3 if accepted else 7)
                self.assertEqual(
                    [record["operation"] for record in records],
                    ["login", "tag", "push"] if accepted else ["login"],
                )
                self.assertTrue(records[0]["authenticated"])
                self.assertIn("--password-stdin", records[0]["arguments"])
                self.assertNotIn(environment["GH_TOKEN"], journal.read_text() + output.getvalue())
                self.assertNotIn("Published", output.getvalue())
