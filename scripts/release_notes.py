"""Reviewed release notes: validate fragments, render the complete body, recover a missing release.

A release is described by `release-notes/<slug>.md` fragments added since the last release tag.
The body is the human section followed by semantic-release's own generated change list; the
locked tool renders both, so no commit parser is copied here. The tool stays the publisher:
`prepare` runs before it and fails before any tag exists when a note is invalid or missing.
Functions work in the current directory, which is the repository checkout.
"""

import argparse
import contextlib
import json
import os
import re
import shutil
import string
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path

import semantic_release
from git import Commit, Repo
from scripts import release_ci
from semantic_release.changelog.release_history import ReleaseHistory
from semantic_release.commit_parser import CommitParser, ParseError, ParserOptions
from semantic_release.commit_parser.token import ParseResult
from semantic_release.version.algorithm import tags_and_versions
from semantic_release.version.translator import VersionTranslator

FRAGMENTS = "release-notes"
WORK_DIR = ".release-notes"
CONFIG = f"{WORK_DIR}/semantic-release.json"
TEMPLATE_DIR = f"{WORK_DIR}/template"
SECTIONS = ("Changes for users", "Upgrade", "Security")
GENERATED_TEMPLATE = ".generated_notes.md.j2"
RELEASE_NOTES_TEMPLATE = ".release_notes.md.j2"
# Characters that cannot start or end a Jinja delimiter or an escape; everything else is escaped.
_LITERAL_SAFE = frozenset(string.ascii_letters + string.digits + " .,:;!?()-_/'")
_HEADING = re.compile(r"^#{1,6}(?:\s|$)")
_HEADINGS = re.compile(r"# \S[^\n]*" + "".join(f"\n## {name}" for name in SECTIONS))
_REQUIRED_HEADINGS = ", ".join(["'# <title>'", *(f"'## {name}'" for name in SECTIONS)])
_HTTP_STATUS = re.compile(r"HTTP/\S+ (\d{3})")
_REPOSITORY = re.compile(r"[\w.-]+/[\w.-]+")
_OUTPUT_NOTES = re.compile(r"release_notes<<EOF\n(.*)EOF\n", re.DOTALL)
_PREVIEW_IDENTITY = ("-c", "user.name=release-notes-preview", "-c", "user.email=preview@invalid")


class ReleaseNotesError(Exception):
    """A release-notes rule failed; the message says what to change."""


class _TagWalkParser(CommitParser[ParseResult, ParserOptions]):
    """Reads no commit message: only the release tags of the history are wanted."""

    def parse(self, commit: Commit) -> ParseResult:
        return ParseError(commit, "not parsed")


@dataclass(frozen=True, slots=True)
class Fragment:
    path: str
    title: str
    sections: tuple[str, str, str]


@dataclass(frozen=True, slots=True)
class Plan:
    candidate: str
    tag: str
    due: bool


def run(
    *command: str, env: dict[str, str] | None = None, stdin: str | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        command, capture_output=True, text=True, check=False, env=env, input=stdin, timeout=600
    )


def git(*args: str, stdin: str | None = None) -> str:
    result = run("git", *args, stdin=stdin)
    if result.returncode:
        raise ReleaseNotesError(f"git {args[0]} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _section_marks(path: str, lines: list[str]) -> list[int]:
    """The heading line indexes of a fragment, which must be exactly the required ones."""
    marks = [index for index, line in enumerate(lines) if _HEADING.match(line)]
    headings = "\n".join(lines[index] for index in marks)
    preface = "".join(lines[: next(iter(marks), 0)])
    if not _HEADINGS.fullmatch(headings) or preface.strip():
        raise ReleaseNotesError(f"{path}: the sections must be exactly {_REQUIRED_HEADINGS}")
    return marks


def parse_fragment(path: str, text: str) -> Fragment:
    if "<!--" in text:
        raise ReleaseNotesError(f"{path}: HTML comments are not allowed")
    lines = text.splitlines()
    marks = _section_marks(path, lines)
    ends = [*marks[2:], len(lines)]
    bodies = [
        "\n".join(lines[start + 1 : end]).strip()
        for start, end in zip(marks[1:], ends, strict=True)
    ]
    for name, body in zip(SECTIONS, bodies, strict=True):
        if not body:
            raise ReleaseNotesError(f"{path}: the section '{name}' is empty")
    return Fragment(path, lines[marks[0]][2:].strip(), (bodies[0], bodies[1], bodies[2]))


def render_human_section(fragments: list[Fragment]) -> str:
    parts = ["## Release summary\n\n"]
    for fragment in fragments:
        changes, upgrade, security = fragment.sections
        parts.append(
            f"### {fragment.title}\n\n#### Changes for users\n\n{changes}\n\n"
            f"#### Upgrade\n\n{upgrade}\n\n#### Security\n\n{security}\n\n"
        )
    return "".join(parts)


# The widest code point each Python escape letter can carry, and its hexadecimal width.
_ESCAPES = ((0xFF, "x", 2), (0xFFFF, "u", 4), (0x10FFFF, "U", 8))


def _escape(character: str) -> str:
    code = ord(character)
    letter, width = next((letter, width) for top, letter, width in _ESCAPES if code <= top)
    return f"\\{letter}{code:0{width}x}"


def encode_literal(text: str) -> str:
    """The body of a double-quoted Jinja string that evaluates to exactly `text`."""
    return "".join(
        character if character in _LITERAL_SAFE else _escape(character) for character in text
    )


def release_table() -> dict[str, object]:
    with Path("pyproject.toml").open("rb") as file:
        return dict(tomllib.load(file)["tool"]["semantic_release"])


def tag_format() -> str:
    return str(release_table()["tag_format"])


def _subtable(table: dict[str, object], name: str) -> dict[str, object]:
    value = table.get(name, {})
    if not isinstance(value, dict):
        raise ReleaseNotesError(f"[tool.semantic_release.{name}] must be a table")
    return {str(key): item for key, item in value.items()}


def write_config(repository_url: str | None) -> None:
    table = release_table()
    table["changelog"] = {**_subtable(table, "changelog"), "template_dir": TEMPLATE_DIR}
    if repository_url:
        table["remote"] = {**_subtable(table, "remote"), "url": repository_url}
    Path(WORK_DIR).mkdir(exist_ok=True)
    Path(CONFIG).write_text(json.dumps({"semantic_release": table}), encoding="utf-8")


def write_template(human_section: str) -> None:
    source = Path(semantic_release.__file__).parent / "data/templates/conventional/md"
    if not (source / RELEASE_NOTES_TEMPLATE).is_file() or not (source / ".components").is_dir():
        raise ReleaseNotesError("the packaged semantic-release templates have another layout")
    target = Path(TEMPLATE_DIR)
    shutil.rmtree(target, ignore_errors=True)
    shutil.copytree(source, target)
    (target / RELEASE_NOTES_TEMPLATE).rename(target / GENERATED_TEMPLATE)
    (target / RELEASE_NOTES_TEMPLATE).write_text(
        f'{{{{ "{encode_literal(human_section)}" }}}}<details>\n'
        "<summary>Generated change list</summary>\n\n"
        f'{{% include "{GENERATED_TEMPLATE}" %}}\n\n</details>\n',
        encoding="utf-8",
    )


def tree_fragments() -> list[Fragment]:
    names = git("ls-tree", "-r", "-z", "--name-only", "HEAD", "--", f"{FRAGMENTS}/").split("\0")
    paths = [name for name in names if name]
    for path in paths:
        if not path.endswith(".md"):
            raise ReleaseNotesError(f"{path}: only .md files belong in {FRAGMENTS}/")
    return [parse_fragment(path, git("show", f"HEAD:{path}")) for path in paths]


def last_release_tag() -> str | None:
    translator = VersionTranslator(tag_format=tag_format())
    with Repo(".") as repo:
        history = ReleaseHistory.from_git_history(repo, translator, _TagWalkParser())
    released = list(history.released)
    return released[0].as_tag() if released else None


def existing_versions() -> set[str]:
    translator = VersionTranslator(tag_format=tag_format())
    with Repo(".") as repo:
        return {str(version) for _, version in tags_and_versions(repo.tags, translator)}


def candidate_version() -> str:
    result = run(sys.executable, "-m", "semantic_release", "-c", CONFIG, "version", "--print")
    words = result.stdout.split()
    if result.returncode or not words:
        raise ReleaseNotesError(
            "semantic-release printed no candidate version; is this a release branch?"
        )
    return words[-1]


def pending_paths(base_tag: str | None) -> list[str]:
    base = base_tag or git("hash-object", "-t", "tree", "--stdin", stdin="")
    # Rename detection is pinned: a renamed released note is not added, a pending one is.
    names = git(
        "diff", "--name-only", "-z", "--find-renames=50%", "--diff-filter=A", base, "HEAD",
        "--", f"{FRAGMENTS}/",
    )  # fmt: skip
    return sorted(name for name in names.split("\0") if name)


def prepare(repository_url: str | None = None) -> Plan:
    """Validate every note, decide whether a release is due, stage the template if it is."""
    write_config(repository_url)
    fragments = {fragment.path: fragment for fragment in tree_fragments()}
    candidate = candidate_version()
    plan = Plan(candidate, tag_format().format(version=candidate), False)
    if candidate in existing_versions():
        return plan
    pending = [fragments[path] for path in pending_paths(last_release_tag()) if path in fragments]
    if not pending:
        raise ReleaseNotesError(
            f"no pending release note for {plan.tag}: add {FRAGMENTS}/<slug>.md "
            f"with the sections {', '.join(SECTIONS)}"
        )
    write_template(render_human_section(pending))
    return Plan(candidate, plan.tag, True)


def tool_notes() -> str:
    """The complete body the tool renders for the next release, without any side effect."""
    with tempfile.TemporaryDirectory() as directory:
        output = Path(directory) / "output"
        result = run(
            sys.executable, "-m", "semantic_release", "--noop", "-c", CONFIG, "version",
            "--no-commit", "--no-tag", "--no-push", "--no-changelog", "--skip-build",
            env={**os.environ, "GITHUB_OUTPUT": str(output)},
        )  # fmt: skip
        text = output.read_text(encoding="utf-8") if output.exists() else ""
    match = _OUTPUT_NOTES.search(text)
    if result.returncode or not match:
        raise ReleaseNotesError(
            f"semantic-release rendered no release notes: {result.stderr.strip()}"
        )
    return match.group(1)


def isolated_checkout(source: Path, commits: list[str], tags: list[str]) -> None:
    """Fill the current empty directory with the given commits and tags, and no remote."""
    git("init", "-q", "-b", "main")
    for tag in tags:
        git("fetch", "-q", "--no-tags", str(source), f"+refs/tags/{tag}:refs/tags/{tag}")
    for commit in commits:
        git("fetch", "-q", "--no-tags", str(source), commit)
    git("checkout", "-q", "-B", "main", commits[0])


def preview(base: str, head: str, repository_url: str) -> str:
    source = Path.cwd()
    tags = git("tag", "--list").split()
    with tempfile.TemporaryDirectory() as directory, contextlib.chdir(directory):
        isolated_checkout(source, [base, head], tags)
        merged = run("git", *_PREVIEW_IDENTITY, "merge", "--no-ff", "--no-edit", head)
        if merged.returncode:
            raise ReleaseNotesError(f"merge conflict between {base} and {head}")
        plan = prepare(repository_url)
        due = f"yes (`{plan.tag}`)" if plan.due else "no"
        report = (
            f"## Release notes preview\n\n- base: `{base}`\n- head: `{head}`\n"
            f"- merge: `{git('rev-parse', 'HEAD')}`\n- release due: {due}\n"
        )
        return report + (f"\n{tool_notes()}" if plan.due else "")


def remote_tag_commit(repository: str, tag: str) -> str:
    result = run("gh", "api", f"repos/{repository}/commits/refs/tags/{tag}")
    sha = json.loads(result.stdout).get("sha") if result.returncode == 0 else None
    if not isinstance(sha, str):
        raise ReleaseNotesError(f"could not read the remote tag {tag} in {repository}")
    return sha


def release_exists(repository: str, tag: str) -> bool:
    """The structured HTTP status of the release lookup: 200 exists, 404 absent, else refuse."""
    result = run("gh", "api", "-i", f"repos/{repository}/releases/tags/{tag}")
    status = _HTTP_STATUS.match(result.stdout)
    if status and status[1] in {"200", "404"}:
        return status[1] == "200"
    raise ReleaseNotesError(
        f"could not tell whether {tag} has a release in {repository}; nothing was created"
    )


def frozen_notes(tag: str, commit: str, repository: str) -> str:
    """Re-render the original body from the history as it was at `tag`."""
    source = Path.cwd()
    merged = git("tag", "--merged", commit).split()
    with tempfile.TemporaryDirectory() as directory, contextlib.chdir(directory):
        isolated_checkout(source, [commit], [name for name in merged if name != tag])
        plan = prepare(f"https://github.com/{repository}.git")
        if not plan.due or plan.tag != tag:
            raise ReleaseNotesError(f"the history at {tag} no longer yields that release")
        return tool_notes()


def verified_commit(tag: str, repository: str) -> str:
    """The commit of a local tag that is checked out, green in CI and the same on the remote."""
    found = run("git", "rev-parse", "--verify", "-q", f"refs/tags/{tag}^{{commit}}")
    if found.returncode:
        raise ReleaseNotesError(f"no tag {tag} here; fetch it first")
    commit = found.stdout.strip()
    if commit != git("rev-parse", "HEAD"):
        raise ReleaseNotesError(
            f"{tag} ({commit}) is not the checked-out commit; check the tag out"
        )
    try:
        release_ci.verify_ci(repository, commit)
    except ValueError as error:
        raise ReleaseNotesError(str(error)) from error
    remote = remote_tag_commit(repository, tag)
    if remote != commit:
        raise ReleaseNotesError(
            f"the remote tag {tag} in {repository} is {remote}, not the verified {commit}"
        )
    return commit


def recover(tag: str, repository: str) -> None:
    """Create the GitHub release of an existing tag whose release is missing."""
    if not _REPOSITORY.fullmatch(repository):
        raise ReleaseNotesError(f"{repository!r} is not an owner/name repository")
    commit = verified_commit(tag, repository)
    if release_exists(repository, tag):
        raise ReleaseNotesError(f"{tag} already has a release; notes are never overwritten")
    notes = frozen_notes(tag, commit, repository)
    with tempfile.TemporaryDirectory() as directory:
        file = Path(directory) / "notes.md"
        file.write_text(notes, encoding="utf-8")
        created = run(
            "gh", "release", "create", tag, "--repo", repository, "--verify-tag",
            "--notes-file", str(file),
        )  # fmt: skip
    if created.returncode:
        raise ReleaseNotesError(f"gh release create failed: {created.stderr.strip()}")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("prepare")
    show = commands.add_parser("preview")
    for name in ("--base", "--head", "--repository-url"):
        show.add_argument(name, required=True)
    fix = commands.add_parser("recover")
    fix.add_argument("tag")
    fix.add_argument("--repository", required=True)
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "prepare":
            prepare()
        elif arguments.command == "preview":
            sys.stdout.write(preview(arguments.base, arguments.head, arguments.repository_url))
        else:
            recover(arguments.tag, arguments.repository)
    except ReleaseNotesError as error:
        sys.stderr.write(f"release notes: {error}\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
