"""A stand-in for the suite's project and environment, with hostile selectors aimed at it.

Test-only support shared by the unit tests and the acceptance self-tests: a launch that leaks an
inherited selector uses the stand-in, which records its use as a change to its own tree.
"""

import hashlib
import os
import sys

from pdm_launch import create_project

SUBJECT = '[project]\nname = "isolation-subject"\nversion = "0"\nrequires-python = ">=3.14"\n'
SHOW = "import os, sys; print(os.environ['PDM_PROJECT_ROOT'], sys.prefix)"
STUB = '#!/bin/sh\ntouch "{marker}"\nexec "{real}" "$@"\n'


def outer_stand_in(base):
    """A sacrificial stand-in for the suite's project and environment; any use of it is recorded."""
    outer = base / "outer"
    marker = outer / "used"
    (outer / "bin").mkdir(parents=True)
    (outer / ".venv/bin").mkdir(parents=True)
    (outer / "home").mkdir()
    stub = outer / "bin/python"
    stub.write_text(STUB.format(marker=marker, real=sys.executable))
    stub.chmod(0o755)
    (outer / ".venv/bin/python").symlink_to(stub)
    (outer / ".venv/pyvenv.cfg").write_text("home = " + str(outer / "bin") + "\n")
    (outer / "home/config.toml").write_text("check_update = false\n")
    create_project(outer, interpreter=stub, pyproject=SUBJECT)
    return outer


def hostile_selectors(outer):
    return {
        "PDM_PROJECT": str(outer),
        "PDM_PYTHON": str(outer / "bin/python"),
        "VIRTUAL_ENV": str(outer / ".venv"),
        "HOME": str(outer / "home"),
        "XDG_CACHE_HOME": str(outer / "home/cache"),
        "XDG_CONFIG_HOME": str(outer / "home/config"),
        "PDM_CONFIG_FILE": str(outer / "home/config.toml"),
    }


def tree(root):
    found = {}
    for path in sorted(root.rglob("*")):
        relative = str(path.relative_to(root))
        if path.is_symlink():
            found[relative] = f"-> {os.readlink(path)}"
        elif path.is_file():
            found[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        else:
            found[relative] = "dir"
    return found


def changed(before, after):
    return {
        name: "removed" if name not in after else "created" if name not in before else "modified"
        for name in sorted(before.keys() | after.keys())
        if before.get(name) != after.get(name)
    }
