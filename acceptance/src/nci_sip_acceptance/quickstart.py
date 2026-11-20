"""QUICKSTART's table of tools, generated from the specification's tool data.

    pdm run quickstart-tools [--check]

The table lists each tool with its group and the one-line summary of `spec/tools.yaml`, so the
guide cannot drift from the specification. It is written between two markers of QUICKSTART.md.
`--check` writes nothing and exits with 1 when the guide is not current; the acceptance
self-tests run it.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from nci_sip_acceptance.document import GROUPS, cell
from nci_sip_acceptance.spec import SPEC, TOOLS

QUICKSTART = SPEC.parent / "QUICKSTART.md"
BEGIN, END = "<!-- tool-summaries:begin -->", "<!-- tool-summaries:end -->"


def row(name: str, tool: dict[str, Any]) -> str:
    return f"| `{name}` | {GROUPS[tool['group']]} | {cell(tool['summary'])} |"


def render() -> str:
    """The table: one row per tool, in the order of the specification."""

    lines = [BEGIN, "| Tool | Group | What it does |", "|---|---|---|"]
    lines += [row(name, tool) for name, tool in TOOLS.items()]
    return "\n".join([*lines, END])


def current(document: str, table: str) -> str:
    """The document with the text between the markers replaced by `table`."""

    before, found, rest = document.partition(BEGIN)
    _, end, after = rest.partition(END)
    if not (found and end):
        raise SystemExit(f"the guide lacks the markers {BEGIN} and {END}")
    return before + table + after


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write QUICKSTART's table of tools.")
    parser.add_argument("--check", action="store_true", help="write nothing; fail if not current")
    parser.add_argument("--quickstart", type=Path, default=QUICKSTART, help="the guide to write")
    options = parser.parse_args(arguments)
    document = options.quickstart.read_text(encoding="utf-8")
    written = current(document, render())
    if written == document:
        return 0
    if options.check:
        sys.stderr.write(f"{options.quickstart} is not current; run pdm run quickstart-tools\n")
        return 1
    options.quickstart.write_text(written, encoding="utf-8")
    sys.stdout.write(f"Wrote the table of tools to {options.quickstart}.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
