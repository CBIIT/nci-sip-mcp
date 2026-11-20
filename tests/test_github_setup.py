"""Repository setup creates missing names without replacing values or exposing credentials."""

import json
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from scripts import github_setup as module

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/github_setup.py"
FAKE_GH = """
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
value = sys.stdin.read() if args[1] == "set" else ""
with Path(os.environ["COMMAND_LOG"]).open("a") as log:
    log.write(json.dumps([args, value]) + "\\n")
if os.environ.get("GH_FAIL"):
    sys.stderr.write("PRIVATE-CREDENTIAL-CANARY")
    sys.exit(7)
if args[1] == "list":
    print(json.dumps([{"name": "NCI_SIP_CADSR_CREDENTIAL"}] if args[0] == "secret" else []))
"""


class GitHubSetupTest(unittest.TestCase):
    def invoke(self, fail=False):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "gh"
            executable.write_text(f"#!{sys.executable}\n" + FAKE_GH)
            executable.chmod(0o700)
            log = root / "commands.jsonl"
            environment = os.environ | {
                "PATH": str(root) + os.pathsep + os.environ["PATH"],
                "COMMAND_LOG": str(log),
            }
            if fail:
                environment["GH_FAIL"] = "1"
            result = subprocess.run(  # noqa: S603 - real CLI and owned fake executable.
                [sys.executable, str(SCRIPT)],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            commands = [json.loads(line) for line in log.read_text().splitlines()]
            return result, commands

    def test_only_missing_names_are_created_with_interactive_replacement_instructions(self):
        result, commands = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            commands,
            [
                [["secret", "list", "--repo", "CBIIT/nci-sip-mcp", "--json", "name"], ""],
                [["variable", "list", "--repo", "CBIIT/nci-sip-mcp", "--json", "name"], ""],
                [
                    ["secret", "set", "NCI_SIP_EVS_LICENSE_KEY", "--repo", "CBIIT/nci-sip-mcp"],
                    module.PLACEHOLDER,
                ],
            ],
        )
        self.assertEqual(
            result.stdout,
            "Existing secret: NCI_SIP_CADSR_CREDENTIAL (unchanged; value not inspected)\n"
            "Created secret: NCI_SIP_EVS_LICENSE_KEY (placeholder)\n"
            "Maintainer: run gh secret set NCI_SIP_CADSR_CREDENTIAL --repo CBIIT/nci-sip-mcp "
            "in your own terminal; it prompts for the value.\n"
            "Maintainer: run gh secret set NCI_SIP_EVS_LICENSE_KEY --repo CBIIT/nci-sip-mcp "
            "in your own terminal; it prompts for the value.\n",
        )

    def test_gh_failure_is_nonzero_without_repeating_its_output(self):
        result, commands = self.invoke(fail=True)
        self.assertEqual(result.returncode, 7)
        self.assertEqual(len(commands), 1)
        self.assertEqual(
            result.stderr,
            "GitHub setup failed while listing secret names; "
            "check gh authentication and repository access.\n",
        )
        self.assertNotIn("PRIVATE-CREDENTIAL-CANARY", result.stdout + result.stderr)

    def test_workflow_names_match_inventory_and_documentation(self):
        names = {"secrets": set(), "vars": set()}
        paths = [*ROOT.glob(".github/workflows/*.yml"), *ROOT.glob(".github/actions/**/action.yml")]
        for path in paths:
            text = path.read_text()
            for kind, name in re.findall(r"\b(secrets|vars)\.([A-Za-z_][A-Za-z_0-9]*)", text):
                names[kind].add(name)
            self.assertNotRegex(text, r"\b(?:secrets|vars)\[")
        self.assertEqual(
            names,
            {"secrets": set(module.SECRETS) | {"GITHUB_TOKEN"}, "vars": set(module.VARIABLES)},
        )
        contributing = ROOT.joinpath("CONTRIBUTING.md").read_text()
        for name, purpose in module.SECRETS.items():
            self.assertIn(f"| `{name}` | {purpose} | Acceptance (live) |", contributing)
            self.assertIn(module.instruction(name), contributing)
        self.assertIn(
            "[workflow credentials](CONTRIBUTING.md#workflow-credentials)",
            ROOT.joinpath("README.md").read_text(),
        )
