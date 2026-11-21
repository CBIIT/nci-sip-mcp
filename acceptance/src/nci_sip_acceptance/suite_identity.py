"""Which suite a report comes from, and whether that suite is an approved release.

A report states the suite version, the fixture-set version and a digest over the suite's own
files. The report is an acceptance report only when the digest is one of those listed in
`acceptance/approved.yaml`; otherwise it reads MODIFIED. Changes to `spec/` and the suite need
written approval from the furnished tag (spec/acceptance.md), and this is how an unapproved
change shows: the digest moves and no approved entry carries it.

The digest covers the tests, the fixtures with their manifest, the register of request forms,
the harness and the pytest configuration with the dependency pins (`acceptance/pyproject.toml`),
and the specification data (`spec/`). It leaves out the self-tests, caches, editor and system
litter, the README and change log, the approval record itself, and the server under test, which
lives elsewhere: one approved suite attests any server.
"""

from __future__ import annotations

import hashlib
import json
from importlib.metadata import version
from pathlib import Path
from typing import Any

import yaml

REPOSITORY = Path(__file__).parents[3]
APPROVED = REPOSITORY / "acceptance" / "approved.yaml"
# The suite's own files, relative to the repository root. This is the one definition.
SUITE_PATHS = (
    "acceptance/tests",
    "acceptance/fixtures",
    "acceptance/request-forms",
    "acceptance/src",
    "acceptance/pyproject.toml",
    "spec",
)
MANIFEST = "acceptance/fixtures/manifest.yaml"
RECORDED = "acceptance/fixtures/recorded"
# What a checkout or a run leaves among the suite's files without being part of it.
CACHE_DIRECTORIES = frozenset({"__pycache__", ".pytest_cache"})
IGNORED_NAMES = frozenset({".DS_Store", "Thumbs.db"})
# Compiled, swap and backup files; coverage data; Emacs locks; macOS resource forks.
IGNORED_SUFFIXES = (".pyc", ".swp", ".swo", "~")
IGNORED_PREFIXES = (".coverage", ".#", "._")
SHORT_DIGEST = 12


def _is_part(path: Path) -> bool:
    return not (
        CACHE_DIRECTORIES.intersection(path.parts)
        or path.name.endswith(IGNORED_SUFFIXES)
        or path.name.startswith(IGNORED_PREFIXES)
        or path.name in IGNORED_NAMES
    )


class SuiteError(Exception):
    """The suite's files cannot be digested as they stand."""


def _link(path: Path) -> SuiteError:
    return SuiteError(f"{path} is a symbolic link; the suite's files must be real files")


def _below(root: Path, base: str) -> list[Path]:
    top = root / base
    if top.is_symlink():
        raise _link(top)
    if not top.exists():
        raise SuiteError(f"{root} is not the root of the suite: {base} is missing")
    return [top] if top.is_file() else list(top.rglob("*"))


def suite_files(root: Path = REPOSITORY) -> list[Path]:
    """The suite's files below `root`, in the order of their relative POSIX paths.

    A wrong or empty root, or a symbolic link among the files, is an error: a digest of a
    partial set, or of what a link leads to outside the tree, would attest nothing."""

    files = []
    for base in SUITE_PATHS:
        for path in _below(root, base):
            if not _is_part(path.relative_to(root)):
                continue
            if path.is_symlink():
                raise _link(path)
            if path.is_file():
                files.append(path)
    if root / MANIFEST not in files:
        raise SuiteError(f"{root} is not the root of the suite: {MANIFEST} is missing")
    return sorted(files, key=lambda path: path.relative_to(root).as_posix())


def _content(path: Path) -> bytes:
    """A text file with its line ends as LF, so that a checkout with CRLF digests the same;
    a binary file (one holding a NUL byte) as it is."""

    data = path.read_bytes()
    return data if b"\0" in data else data.replace(b"\r\n", b"\n")


def suite_digest(root: Path = REPOSITORY) -> str:
    """SHA-256 over every suite file's path and content, each preceded by its length, so that
    no two sets of files can make the same stream."""

    digest = hashlib.sha256()
    for path in suite_files(root):
        name = path.relative_to(root).as_posix().encode()
        content = _content(path)
        digest.update(b"%d:%s%d:" % (len(name), name, len(content)))
        digest.update(content)
    return digest.hexdigest()


def fixture_set_version(root: Path = REPOSITORY) -> str:
    """The pinned release and the date of the latest recording, as "ncit_26.09d, recorded
    2026-10-03"."""

    manifest = yaml.safe_load((root / MANIFEST).read_text(encoding="utf-8"))
    dates = [
        json.loads(path.read_text(encoding="utf-8")).get("recorded_on")
        for path in (root / RECORDED).rglob("*.json")
    ]
    latest = max((date for date in dates if date), default=None)
    return f"{manifest['evs']['release']}, " + (f"recorded {latest}" if latest else "no recording")


def identity(root: Path = REPOSITORY) -> dict[str, str]:
    """What a report states of its suite."""

    digest = suite_digest(root)
    return {
        "version": version("nci-sip-acceptance"),
        "fixture_set": fixture_set_version(root),
        "digest": digest,
    }


def approved_digests(path: Path = APPROVED) -> set[str]:
    """The digests of the approved releases of the suite."""

    problem = SuiteError(
        "acceptance/approved.yaml must be a list of entries, each with a digest "
        f"(the file is {path}; an empty list approves nothing)"
    )
    try:
        entries = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise problem from error
    if not isinstance(entries, list) or not all(
        isinstance(entry, dict) and isinstance(entry.get("digest"), str) for entry in entries
    ):
        raise problem
    return {entry["digest"] for entry in entries}


def heading(suite: dict[str, Any], approved: set[str]) -> list[str]:
    """The report's opening lines: its title, and the identity of the suite."""

    if suite["digest"] in approved:
        title = ["# ACCEPTANCE REPORT"]
    else:
        short = suite["digest"][:SHORT_DIGEST]
        title = ["# MODIFIED", "", f"Not an approved release of the suite (digest {short}…)."]
    return [
        *title,
        "",
        f"Suite version: {suite['version']}.",
        f"Fixture set: {suite['fixture_set']}.",
        f"Suite digest: {suite['digest']}.",
        "",
    ]
