"""Reviewed release notes reach the real semantic-release flow before a tag exists.

Every scenario runs the entry points in a sacrificial repository whose remote is unreachable:
a local tag can be created, nothing can be published. `gh` is a recording stub.
"""

import contextlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from typing import NamedTuple

import jinja2
import yaml
from scripts import release_notes

ROOT = Path(__file__).parent.parent
PYPROJECT = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
RELEASE_TABLE = PYPROJECT[
    PYPROJECT.index("[tool.semantic_release]") : PYPROJECT.index("[tool.pdm.scripts]")
]
CONFIG = ".release-notes/semantic-release.json"
UNREACHABLE = "file:///nonexistent/nci-sip-mcp-scratch-remote.git"
REPOSITORY_URL = "https://github.com/example/scratch.git"
GENERATED = "<details>\n<summary>Generated change list</summary>\n\n"
FAKE_GH = """#!PYTHON
import json, os, subprocess, sys
KNOWN = "example/scratch"
argv = sys.argv[1:]
mode = os.environ.get("FAKE_GH_REMOTE", "")
canned = {"notjson": "<html>", "list": "[]", "objectstr": '{"object": "x"}', "empty": "{}",
          "nosha": '{"object": {"type": "commit"}}'}


def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


path = next((a for a in argv if a.startswith("repos/")), "")
if path:
    repo = "/".join(path.split("/")[1:3])
elif "--repo" in argv:
    repo = argv[argv.index("--repo") + 1]
else:
    repo = os.environ.get("GH_REPO", KNOWN)
record = {"argv": argv, "repo": repo}
if argv[:2] == ["release", "create"]:
    record["notes"] = open(argv[argv.index("--notes-file") + 1], encoding="utf-8").read()
with open(os.environ["FAKE_GH_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps(record) + "\\n")
RELEASE_COMMANDS = [["release", s] for s in ("create", "view", "download", "edit")]
if argv[0] != "api" and argv[:2] not in RELEASE_COMMANDS:
    sys.exit(99)
if argv[:2] == ["release", "view"]:
    print(os.environ["FAKE_GH_VIEW"])
elif argv[:2] == ["release", "download"]:
    if os.environ.get("FAKE_GH_DOWNLOAD") == "fail":
        sys.stderr.write("HTTP 502")
        sys.exit(1)
    target = os.path.join(argv[argv.index("--dir") + 1], argv[argv.index("--pattern") + 1])
    with open(target, "w", encoding="utf-8") as asset:
        asset.write(os.environ["FAKE_GH_DIGEST"])
elif argv[:2] == ["release", "edit"]:
    if os.environ.get("FAKE_GH_EDIT") == "fail":
        sys.stderr.write("HTTP 502")
        sys.exit(1)
elif argv[:2] == ["release", "create"]:
    if repo != KNOWN or os.environ.get("FAKE_GH_CREATE") == "fail":
        sys.stderr.write("HTTP 500")
        sys.exit(1)
elif "/actions/workflows/" in path:
    sha = [p for p in path.split("?")[1].split("&") if p.startswith("head_sha=")][0][9:]
    run = {"id": 7, "head_sha": sha, "head_branch": "main", "event": "push",
           "path": ".github/workflows/ci.yml", "conclusion": os.environ["FAKE_GH_CI"]}
    print(json.dumps({"workflow_runs": [run] if repo == KNOWN else []}))
elif "/git/ref/tags/" in path:
    tag = path.rsplit("/", 1)[1]
    if mode == "ghfail":
        sys.stderr.write("HTTP 500")
        sys.exit(1)
    if mode in canned:
        print(canned[mode])
        sys.exit(0)
    local = git("rev-parse", f"refs/tags/{tag}")
    if os.environ.get("FAKE_GH_TAG_KIND") == "lightweight":
        obj = {"type": "commit", "sha": os.environ.get("FAKE_GH_TAG_SHA") or local}
    else:
        obj = {"type": "tag", "sha": local}
    print(json.dumps({"object": obj}))
elif "/git/tags/" in path:
    sha = path.rsplit("/", 1)[1]
    rows = git("for-each-ref", "--format=%(objectname) %(taggerdate:iso-strict)", "refs/tags")
    date = {r.partition(" ")[0]: r.partition(" ")[2] for r in rows.splitlines()}[sha]
    commit = os.environ.get("FAKE_GH_TAG_SHA") or git("rev-parse", sha + "^{commit}")
    payload = {"tagger": {"date": os.environ.get("FAKE_GH_TAG_DATE") or date},
               "object": {"type": "commit", "sha": commit}}
    if mode == "notagger":
        del payload["tagger"]
    print(json.dumps(payload))
elif "/git/commits/" in path:
    sha = path.rsplit("/", 1)[1]
    date = git("log", "-1", "--format=%cI", sha)
    date = os.environ.get("FAKE_GH_TAG_DATE") or date
    payload = {"sha": sha, "committer": {"date": date}}
    if mode == "nocommitdate":
        del payload["committer"]
    print(json.dumps(payload))
elif "/releases/tags/" in path:
    state = os.environ["FAKE_GH_RELEASE"]
    head = "HTTP/2.0 {}\\r\\nContent-Type: application/json\\r\\n"
    statuses = {"exists": "200 OK", "absent": "404 Not Found", "error": "401 Unauthorized",
                "server": "500 Internal Server Error"}
    if state in statuses:
        print(head.format(statuses[state]) + "\\r\\n{}")
        if state == "absent":
            sys.stderr.write("gh: Not Found (HTTP 404)")
        sys.exit(0 if state == "exists" else 1)
    if state == "nostatus":
        print("Content-Type: application/json\\r\\n\\r\\n{}")
        sys.exit(1)
    if state == "partial":
        sys.stdout.write("HTTP/2.0 404 Not Found\\r\\nContent-Ty")
        sys.stderr.write("unexpected EOF")
        sys.exit(1)
    if state == "stale":
        print(head.format("404 Not Found") + "\\r\\n{}")
        sys.stderr.write("To get started with GitHub CLI, please run: gh auth login")
        sys.exit(4)
    sys.stderr.write("release not found" if state == "sentence" else "dial tcp: lookup failed")
    sys.exit(1)
else:
    sys.exit(98)
""".replace("PYTHON", sys.executable)


def fragment(
    title,
    changes="Users can search faster.",
    upgrade="No action required.",
    security="No security fixes in this release.",
):
    return (
        f"# {title}\n\n## Changes for users\n\n{changes}\n\n## Upgrade\n\n{upgrade}\n\n"
        f"## Security\n\n{security}\n"
    )


def section(
    title,
    changes="Users can search faster.",
    upgrade="No action required.",
    security="No security fixes in this release.",
):
    return (
        f"### {title}\n\n#### Changes for users\n\n{changes}\n\n#### Upgrade\n\n{upgrade}\n\n"
        f"#### Security\n\n{security}\n\n"
    )


class Outcome(NamedTuple):
    prepared: subprocess.CompletedProcess
    released: subprocess.CompletedProcess | None

    @property
    def codes(self):
        return self.prepared.returncode, getattr(self.released, "returncode", None)


class Scratch:
    """A throwaway git project configured with this repository's release rules."""

    def __init__(self, test, *, origin=True):
        self.directory = tempfile.TemporaryDirectory()
        test.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / "project"
        self.root.mkdir()
        bin_dir = Path(self.directory.name) / "bin"
        bin_dir.mkdir()
        gh = bin_dir / "gh"
        gh.write_text(FAKE_GH, encoding="utf-8")
        gh.chmod(493)
        self.gh_log = Path(self.directory.name) / "gh.log"
        self.output = Path(self.directory.name) / "github-output"
        self.env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("GH_", "GITHUB_", "GIT_"))
        }
        self.env.update(
            PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            PYTHONPATH=str(ROOT),
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_CONFIG_SYSTEM=os.devnull,
            GIT_AUTHOR_NAME="Scratch",
            GIT_AUTHOR_EMAIL="scratch@example.invalid",
            GIT_COMMITTER_NAME="Scratch",
            GIT_COMMITTER_EMAIL="scratch@example.invalid",
            FAKE_GH_LOG=str(self.gh_log),
            FAKE_GH_CI="success",
            FAKE_GH_RELEASE="absent",
            GITHUB_OUTPUT=str(self.output),
        )
        self.git("init", "-q", "-b", "main")
        if origin:
            self.git("remote", "add", "origin", REPOSITORY_URL)
            self.git("remote", "set-url", "--push", "origin", UNREACHABLE)
        (self.root / ".gitignore").write_text(".release-notes/\n", encoding="utf-8")
        self.write(
            "pyproject.toml", '[project]\nname = "scratch"\nversion = "0"\n\n' + RELEASE_TABLE
        )

    def run(self, *command):
        return subprocess.run(  # noqa: S603 - fixed argv in a scratch repository
            command,
            cwd=self.root,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )

    def git(self, *args):
        result = self.run("git", *args)
        if result.returncode:
            raise AssertionError(result.stderr)
        return result.stdout.strip()

    def write(self, name, text):
        target = self.root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def commit(self, message, files=None):
        for name, text in (files or {}).items():
            self.write(name, text)
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", message)
        return self.git("rev-parse", "HEAD")

    def tags(self):
        return self.git("tag", "--list").split()

    def notes_tool(self, *args):
        return self.run(sys.executable, "-m", "scripts.release_notes", *args)

    def release(self):
        """Prepare as the workflow does, then run the tool as far as a local tag."""
        prepared = self.notes_tool("prepare")
        if prepared.returncode:
            return Outcome(prepared, None)
        released = self.run(
            sys.executable,
            "-m",
            "semantic_release",
            "-c",
            CONFIG,
            "version",
            "--no-commit",
            "--tag",
            "--no-push",
            "--no-changelog",
            "--skip-build",
        )
        return Outcome(prepared, released)

    def notes(self):
        text = self.output.read_text(encoding="utf-8")
        return re.search("release_notes<<EOF\\n(.*)EOF\\n", text, re.DOTALL).group(1)

    def gh_calls(self):
        if not self.gh_log.exists():
            return []
        return [json.loads(line) for line in self.gh_log.read_text("utf-8").splitlines()]

    def history(self):
        """v0.1.0 holds `old`; a feat since then adds `new`. An unrelated tag is newer."""
        self.commit("chore: start", files={"release-notes/old.md": fragment("Old note")})
        self.git("tag", "v0.1.0")
        return self.commit(
            "feat: add the thing", files={"release-notes/new.md": fragment("New note")}
        )


class ReleaseNotesFlowTest(unittest.TestCase):
    def test_a_valid_note_reaches_the_local_tag_with_the_human_section_before_the_generated_list(
        self,
    ):
        project = Scratch(self)
        project.history()
        outcome = project.release()
        self.assertEqual(outcome.codes, (0, 0), outcome.prepared.stderr)
        self.assertIn("v0.2.0", project.tags())
        notes = project.notes()
        self.assertTrue(
            notes.startswith("## Release summary\n\n" + section("New note") + GENERATED), notes
        )
        self.assertIn("### Features", notes)
        self.assertIn("- Add the thing", notes)
        self.assertTrue(notes.rstrip().endswith("</details>"), notes)
        self.assertNotIn("Old note", notes)
        self.assertEqual(project.gh_calls(), [])

    def test_an_invalid_or_missing_note_leaves_the_tag_untouched(self):
        for name, files, message in (
            ("no note", {}, "no pending release note"),
            (
                "text before the title",
                {"release-notes/new.md": "Heads up.\n\n" + fragment("New note")},
                "release-notes/new.md: the sections must be exactly",
            ),
            (
                "empty heading",
                {"release-notes/new.md": fragment("New note", upgrade="  ")},
                "release-notes/new.md: the section 'Upgrade' is empty",
            ),
            (
                "missing heading",
                {"release-notes/new.md": "# New note\n\n## Changes for users\n\nx\n"},
                "release-notes/new.md: the sections must be exactly",
            ),
            (
                "container marker",
                {
                    "release-notes/new.md": fragment(
                        "New note", changes="a\n<!-- container-image -->\nb"
                    )
                },
                "release-notes/new.md: HTML comments are not allowed",
            ),
        ):
            with self.subTest(name):
                project = Scratch(self)
                project.commit("chore: start", files={"release-notes/old.md": fragment("Old note")})
                project.git("tag", "v0.1.0")
                project.commit("feat: add the thing", files=files)
                outcome = project.release()
                self.assertEqual(outcome.codes, (1, None))
                self.assertIn(message, outcome.prepared.stderr)
                self.assertEqual(project.tags(), ["v0.1.0"])

    def test_every_fragment_in_the_tree_is_checked_even_when_no_release_is_due(self):
        project = Scratch(self)
        project.commit("chore: start", files={"release-notes/old.md": fragment("Old note")})
        project.git("tag", "v0.1.0")
        project.commit("docs: say more", files={"release-notes/bad.md": "# Bad\n"})
        prepared = project.notes_tool("prepare")
        self.assertEqual(prepared.returncode, 1)
        self.assertIn("release-notes/bad.md", prepared.stderr)

    def test_no_release_due_needs_no_note_and_makes_no_tag(self):
        project = Scratch(self)
        project.commit("chore: start")
        project.git("tag", "v0.1.0")
        project.commit("docs: explain")
        outcome = project.release()
        self.assertEqual(outcome.codes, (0, 0), outcome.prepared.stderr)
        self.assertEqual(project.tags(), ["v0.1.0"])

    def test_a_candidate_already_released_on_an_unrelated_tag_is_not_due(self):
        project = Scratch(self)
        project.commit("chore: start")
        project.git("tag", "v0.1.0")
        project.commit("feat: add the thing")
        project.git("switch", "-q", "--detach", "v0.1.0")
        project.commit("chore: elsewhere")
        project.git("tag", "v0.2.0")
        project.git("switch", "-q", "main")
        outcome = project.release()
        self.assertEqual(outcome.codes, (0, 0), outcome.prepared.stderr)
        self.assertEqual(project.tags(), ["v0.1.0", "v0.2.0"])

    def test_an_unrelated_newer_tag_does_not_move_the_range_of_pending_notes(self):
        project = Scratch(self)
        project.commit("chore: start", files={"release-notes/old.md": fragment("Old note")})
        project.git("tag", "v0.1.0")
        project.commit("feat: add the thing", files={"release-notes/new.md": fragment("New note")})
        project.git("switch", "-q", "--detach", "v0.1.0")
        project.commit(
            "chore: elsewhere", files={"release-notes/elsewhere.md": fragment("Elsewhere")}
        )
        project.git("tag", "v9.0.0")
        project.git("switch", "-q", "main")
        outcome = project.release()
        self.assertEqual(outcome.codes, (0, 0), outcome.prepared.stderr)
        self.assertIn("v0.2.0", project.tags())
        self.assertIn(section("New note"), project.notes())
        self.assertNotIn("Elsewhere", project.notes())

    def test_pending_notes_follow_path_order_edit_and_rename_rules(self):
        project = Scratch(self)
        project.commit("chore: start", files={"release-notes/released.md": fragment("Released")})
        project.git("tag", "v0.1.0")
        project.commit(
            "chore: rename a released note",
            files={"release-notes/renamed.md": fragment("Released")},
        )
        project.git("rm", "-q", "release-notes/released.md")
        project.git("commit", "-q", "-m", "chore: remove the old path")
        project.commit("docs: b", files={"release-notes/b.md": fragment("Beta")})
        project.commit("docs: a", files={"release-notes/a_pending.md": fragment("Alpha")})
        project.git("mv", "release-notes/a_pending.md", "release-notes/c.md")
        project.git("commit", "-q", "-m", "docs: rename a pending note")
        project.commit(
            "docs: edit", files={"release-notes/b.md": fragment("Beta", changes="Edited.")}
        )
        project.commit("docs: dropped", files={"release-notes/dropped.md": fragment("Dropped")})
        project.git("rm", "-q", "release-notes/dropped.md")
        project.git("commit", "-q", "-m", "docs: delete a pending note")
        project.commit("fix: repair it")
        outcome = project.release()
        self.assertEqual(outcome.codes, (0, 0), outcome.prepared.stderr)
        self.assertTrue(
            project.notes().startswith(
                "## Release summary\n\n"
                + section("Beta", changes="Edited.")
                + section("Alpha")
                + GENERATED
            ),
            project.notes(),
        )

    def test_the_first_release_carries_the_note_and_the_tools_first_release_text(self):
        project = Scratch(self)
        project.commit("feat: first", files={"release-notes/new.md": fragment("First")})
        outcome = project.release()
        self.assertEqual(outcome.codes, (0, 0), outcome.prepared.stderr)
        self.assertEqual(project.tags(), ["v0.1.0"])
        self.assertTrue(
            project.notes().startswith("## Release summary\n\n" + section("First") + GENERATED)
        )
        self.assertIn("Initial Release", project.notes())

    def test_prose_is_literal_data_never_template_source(self):
        prose = "\n".join(
            [
                '{{ 7*7 }} {% if x %}a{% endif %} {# c #} {% endraw %} {{ "q" }}',
                "\"double\" 'single' back\\slash \\x41 \\n \\u00e9 tab\there",
                "Ünïcödé ✓ 😀 100% # not a heading {x}",
                "line two",
            ]
        )
        project = Scratch(self)
        project.commit("chore: start")
        project.git("tag", "v0.1.0")
        project.commit(
            "feat: add the thing", files={"release-notes/new.md": fragment("Odd", changes=prose)}
        )
        outcome = project.release()
        self.assertEqual(outcome.codes, (0, 0), outcome.prepared.stderr)
        self.assertTrue(
            project.notes().startswith("## Release summary\n\n" + section("Odd", changes=prose))
        )

    def test_a_failing_render_of_the_generated_body_leaves_no_local_tag(self):
        project = Scratch(self)
        project.history()
        self.assertEqual(project.notes_tool("prepare").returncode, 0)
        generated = project.root / ".release-notes/template/.generated_notes.md.j2"
        generated.write_text('{% include "missing.md.j2" %}', encoding="utf-8")

        released = project.run(
            sys.executable, "-m", "semantic_release", "-c", CONFIG, "version",
            "--no-commit", "--tag", "--no-push", "--no-changelog", "--skip-build",
        )  # fmt: skip

        self.assertNotEqual(released.returncode, 0)
        self.assertEqual(project.tags(), ["v0.1.0"])

    def test_the_generated_template_tree_is_not_tracked_or_staged(self):
        project = Scratch(self)
        project.history()
        outcome = project.release()
        self.assertEqual(outcome.codes, (0, 0), outcome.prepared.stderr)
        self.assertTrue((project.root / ".release-notes/template").is_dir())
        self.assertEqual(project.git("status", "--porcelain"), "")
        self.assertEqual(project.git("ls-files", ".release-notes"), "")


TAG_ONLY_COMMAND = [
    "pdm", "run", "semantic-release", "-c", CONFIG, "-v", "version", "--no-commit", "--tag",
    "--push", "--no-changelog", "--no-vcs-release", "--skip-build",
]  # fmt: skip


def workflow_version_command():
    """The release job's exact semantic-release argv, as the workflow writes it."""
    workflow = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text("utf-8"))
    [step] = [s for s in workflow["jobs"]["release"]["steps"] if s.get("id") == "release"]
    return shlex.split(step["run"])


class WorkflowVersionCommandTest(unittest.TestCase):
    def test_the_workflow_command_pushes_the_tag_and_reports_it_without_a_github_release(self):
        """Real semantic-release tags and pushes to a local bare remote, with no token.

        The complete tag-only command is asserted before launch: a command that still asks
        the tool to create the GitHub release would otherwise reach the GitHub API directly,
        which the gh stub cannot see.
        """
        project = Scratch(self)
        bare = Path(project.directory.name) / "remote.git"
        project.git("init", "-q", "--bare", str(bare))
        project.git("config", f"url.{bare.as_uri()}.insteadOf", REPOSITORY_URL)
        project.history()
        self.assertEqual(project.notes_tool("prepare").returncode, 0)
        project.output.unlink(missing_ok=True)
        command = workflow_version_command()
        self.assertEqual(command, TAG_ONLY_COMMAND)
        released = project.run(sys.executable, "-m", "semantic_release", *command[3:])
        self.assertEqual(released.returncode, 0, released.stderr)
        outputs = dict(
            line.split("=", 1)
            for line in project.output.read_text("utf-8").splitlines()
            if re.fullmatch(r"(released|tag|version|is_prerelease)=.*", line)
        )
        self.assertEqual(
            outputs,
            {"released": "true", "tag": "v0.2.0", "version": "0.2.0", "is_prerelease": "false"},
        )
        pushed = project.git("--git-dir", str(bare), "rev-parse", "refs/tags/v0.2.0^{commit}")
        self.assertEqual(pushed, project.git("rev-parse", "HEAD"))
        self.assertEqual(project.gh_calls(), [])


class InstalledGhFlagsTest(unittest.TestCase):
    def test_the_installed_gh_parses_every_release_flag_the_tools_send(self):
        """Help output of the real CLI, which makes no API call; mocks cannot prove this."""
        gh = shutil.which("gh")
        self.assertIsNotNone(gh, "the GitHub CLI is part of the release tool contract")
        flags = {
            "create": ("--repo", "--verify-tag", "--draft", "--notes-file"),
            "view": ("--repo", "--json"),
            "download": ("--repo", "--pattern", "--dir"),
            "edit": ("--repo", "--draft", "--verify-tag"),
        }
        for command, expected in flags.items():
            with self.subTest(command=command):
                shown = subprocess.run(  # noqa: S603 - the installed CLI prints help; no API call
                    [str(gh), "release", command, "--help"],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=30,
                )
                self.assertEqual(shown.returncode, 0, shown.stderr)
                listed = set(re.findall(r"--[a-z][a-z-]*", shown.stdout))
                self.assertEqual(set(expected) - listed, set())


class RepeatedPrepareTest(unittest.TestCase):
    def test_a_second_prepare_before_any_tag_renders_the_edited_pending_note(self):
        project = Scratch(self)
        project.history()
        first = project.notes_tool("prepare")
        self.assertEqual(first.returncode, 0, first.stderr)
        project.commit(
            "docs: edit the pending note",
            files={"release-notes/new.md": fragment("New note", changes="Edited.")},
        )

        second = project.notes_tool("prepare")
        self.assertEqual(second.returncode, 0, second.stderr)
        outcome = project.release()

        self.assertEqual(outcome.codes, (0, 0), outcome.prepared.stderr)
        self.assertEqual(project.tags(), ["v0.1.0", "v0.2.0"])
        self.assertTrue(
            project.notes().startswith(
                "## Release summary\n\n" + section("New note", changes="Edited.") + GENERATED
            )
        )


class CliUsageTest(unittest.TestCase):
    def test_a_missing_argument_is_a_usage_error_that_does_nothing(self):
        for argv, missing in (
            ((), "command"),
            (("preview", "--head", "h", "--repository-url", "u"), "--base"),
            (("preview", "--base", "b", "--repository-url", "u"), "--head"),
            (("preview", "--base", "b", "--head", "h"), "--repository-url"),
            (("recover", "v0.2.0"), "--repository"),
        ):
            with self.subTest(argv=argv):
                project = Scratch(self)
                project.history()

                result = project.notes_tool(*argv)

                self.assertEqual(result.returncode, 2)
                self.assertIn(f"the following arguments are required: {missing}", result.stderr)
                self.assertFalse((project.root / ".release-notes").exists())
                self.assertEqual(project.gh_calls(), [])


class ConfigTableTest(unittest.TestCase):
    def malformed(self, project, name, value):
        text = (project.root / "pyproject.toml").read_text(encoding="utf-8")
        header = "[tool.semantic_release]\n"
        project.write("pyproject.toml", text.replace(header, f"{header}{name} = {value}\n", 1))

    def test_a_config_table_of_the_wrong_type_is_refused_by_name(self):
        project = Scratch(self)
        self.malformed(project, "changelog", '"x"')
        project.history()

        result = project.notes_tool("prepare")

        self.assertEqual(result.returncode, 1)
        self.assertEqual(
            result.stderr, "release notes: [tool.semantic_release.changelog] must be a table\n"
        )

    def test_a_remote_of_the_wrong_type_is_refused_by_the_preview(self):
        project = Scratch(self, origin=False)
        self.malformed(project, "remote", "1")
        project.commit("chore: start")
        project.git("tag", "v0.1.0")
        base = project.git("rev-parse", "HEAD")
        project.git("switch", "-q", "-c", "feature")
        head = project.commit("feat: add", files={"release-notes/new.md": fragment("New")})

        result = project.notes_tool(
            "preview", "--base", base, "--head", head, "--repository-url", REPOSITORY_URL
        )

        self.assertEqual(result.returncode, 1)
        self.assertEqual(
            result.stderr, "release notes: [tool.semantic_release.remote] must be a table\n"
        )


class ToolBoundaryTest(unittest.TestCase):
    """What the tool's exit status and output files mean; the real child runs in the flow tests."""

    BLOCK = "released=true\nrelease_notes<<EOF\nBody text\nEOF\n"

    def render(self, returncode, output, stderr=""):
        def fake(*command, env=None, stdin=None):
            if output is not None:
                Path(env["GITHUB_OUTPUT"]).write_text(output, encoding="utf-8")
            return subprocess.CompletedProcess(command, returncode, "", stderr)

        with unittest.mock.patch.object(release_notes, "run", fake):
            return release_notes.tool_notes()

    def candidate(self, returncode, stdout):
        def fake(*command, env=None, stdin=None):
            return subprocess.CompletedProcess(command, returncode, stdout, "")

        with unittest.mock.patch.object(release_notes, "run", fake):
            return release_notes.candidate_version()

    def test_the_body_is_the_block_of_a_successful_run(self):
        self.assertEqual(self.render(0, self.BLOCK), "Body text\n")

    def test_a_failed_run_is_refused_even_with_a_plausible_block(self):
        with self.assertRaises(release_notes.ReleaseNotesError) as raised:
            self.render(1, self.BLOCK, stderr="boom")

        self.assertEqual(str(raised.exception), "semantic-release rendered no release notes: boom")

    def test_a_successful_run_without_a_block_is_refused(self):
        for name, output in (("no file", None), ("no block", "released=true\n")):
            with self.subTest(name):
                with self.assertRaises(release_notes.ReleaseNotesError) as raised:
                    self.render(0, output)

                self.assertEqual(
                    str(raised.exception), "semantic-release rendered no release notes: "
                )

    def test_the_candidate_is_the_last_word_of_a_successful_print(self):
        self.assertEqual(self.candidate(0, "noise\n0.2.0\n"), "0.2.0")

    def test_a_failed_print_is_refused_even_with_a_version_on_stdout(self):
        with self.assertRaises(release_notes.ReleaseNotesError) as raised:
            self.candidate(1, "0.2.0\n")

        self.assertEqual(
            str(raised.exception),
            "semantic-release printed no candidate version; is this a release branch?",
        )


class FragmentFilesTest(unittest.TestCase):
    def test_a_branch_the_tool_does_not_release_from_is_reported_not_treated_as_no_release(self):
        project = Scratch(self)
        project.history()
        project.git("switch", "-q", "-c", "feature")

        result = project.notes_tool("prepare")

        self.assertEqual(result.returncode, 1)
        self.assertIn("printed no candidate version; is this a release branch?", result.stderr)

    def test_only_markdown_files_belong_in_the_fragment_directory(self):
        project = Scratch(self)
        project.commit("chore: start", files={"release-notes/draft.txt": "x"})

        result = project.notes_tool("prepare")

        self.assertEqual(result.returncode, 1)
        self.assertIn(
            "release-notes/draft.txt: only .md files belong in release-notes/", result.stderr
        )


class LiteralEncoderTest(unittest.TestCase):
    """The encoder's output, read back by Jinja, is exactly the text, byte for byte."""

    def read_back(self, text):
        literal = release_notes.encode_literal(text)
        environment = jinja2.Environment(autoescape=False)  # noqa: S701 - plain text, as the tool renders
        return environment.from_string(f'{{{{ "{literal}" }}}}').render()

    def test_every_awkward_text_round_trips_unchanged(self):
        for text in (
            "",
            "plain words, with: punctuation (and) 'single' quotes.",
            '"double" quotes and a \\ backslash and \\x41 \\n \\u00e9 \\" sequences',
            "{{ 7*7 }} {% if x %}a{% endif %} {# c #} {% endraw %} {%- x -%} {{- y -}}",
            "line one\nline two\r\nline three\ttab\x00nul\x7f",
            "Ünïcödé ✓ 日本語 \u2028\u2029 😀 \U0001f9ea",
            "100% # heading-looking {x} } {",
            "\ud800 lone surrogate",
        ):
            with self.subTest(text=text):
                self.assertEqual(self.read_back(text), text)

    def test_no_delimiter_character_appears_in_the_encoded_literal(self):
        encoded = release_notes.encode_literal('{{ }} {% %} {# #} \\ " \n')

        self.assertEqual([character for character in encoded if character in '{}%#"\n'], [])
        self.assertEqual(encoded.count("\\"), 15)


class PackagedTemplateTest(unittest.TestCase):
    def test_a_missing_packaged_template_fails_instead_of_rendering_without_it(self):
        for name, parts in (
            ("nothing", ()),
            ("components only", (".components/",)),
            ("template file only", (".release_notes.md.j2",)),
        ):
            with (
                self.subTest(name),
                tempfile.TemporaryDirectory() as directory,
                contextlib.chdir(directory),
            ):
                package = Path(directory) / "semantic_release"
                templates = package / "data/templates/conventional/md"
                for part in parts:
                    if part.endswith("/"):
                        (templates / part).mkdir(parents=True)
                    else:
                        templates.mkdir(parents=True, exist_ok=True)
                        (templates / part).write_text("x", encoding="utf-8")
                with (
                    unittest.mock.patch.object(
                        release_notes.semantic_release, "__file__", str(package / "__init__.py")
                    ),
                    self.assertRaisesRegex(release_notes.ReleaseNotesError, "another layout"),
                ):
                    release_notes.write_template("## Release summary\n\n")

                self.assertFalse((Path(directory) / ".release-notes").exists())


class RecoveryTest(unittest.TestCase):
    def release_and_notes(self, project):
        project.history()
        outcome = project.release()
        self.assertEqual(outcome.codes, (0, 0), outcome.prepared.stderr)
        notes = project.notes()
        project.gh_log.unlink(missing_ok=True)
        return notes

    def recover(self, project, tag="v0.2.0"):
        return project.notes_tool("recover", tag, "--repository", "example/scratch")

    def test_a_missing_release_is_created_with_the_complete_original_body_for_the_verified_tag(
        self,
    ):
        project = Scratch(self)
        original = self.release_and_notes(project)
        head = project.git("rev-parse", "HEAD")
        tag_object = project.git("rev-parse", "refs/tags/v0.2.0")
        result = self.recover(project)
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = project.gh_calls()
        route = (
            "repos/example/scratch/actions/workflows/ci.yml/runs"
            f"?head_sha={head}&branch=main&event=push&per_page=100"
        )
        self.assertEqual(
            [call["argv"][:-1] if call["argv"][0] == "release" else call["argv"] for call in calls],
            [
                ["api", route],
                ["api", "repos/example/scratch/git/ref/tags/v0.2.0"],
                ["api", f"repos/example/scratch/git/tags/{tag_object}"],
                ["api", "-i", "repos/example/scratch/releases/tags/v0.2.0"],
                [
                    "release",
                    "create",
                    "v0.2.0",
                    "--repo",
                    "example/scratch",
                    "--verify-tag",
                    "--draft",
                    "--notes-file",
                ],
            ],
        )
        self.assertEqual(calls[4]["notes"], original)
        self.assertEqual(project.tags(), ["v0.1.0", "v0.2.0"])

    def test_a_lightweight_tag_is_checked_by_its_commit_and_date(self):
        project = Scratch(self)
        project.history()
        project.git("tag", "v0.2.0")
        project.env["FAKE_GH_TAG_KIND"] = "lightweight"

        result = self.recover(project)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            [call["argv"][-1] for call in project.gh_calls()[1:3]],
            [
                "repos/example/scratch/git/ref/tags/v0.2.0",
                f"repos/example/scratch/git/commits/{project.git('rev-parse', 'HEAD')}",
            ],
        )

    def test_recovery_needs_no_inherited_pythonpath_in_a_self_contained_checkout(self):
        project = Scratch(self)
        for name in ("release_notes.py", "release_ci.py"):
            project.write(f"scripts/{name}", (ROOT / "scripts" / name).read_text("utf-8"))
        del project.env["PYTHONPATH"]
        original = self.release_and_notes(project)

        result = self.recover(project)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(project.gh_calls()[-1]["notes"], original)

    def test_the_selected_repository_wins_over_the_environment_and_the_checkout(self):
        project = Scratch(self)
        project.history()
        project.git("tag", "-a", "v0.2.0", "-m", "v0.2.0")
        project.env["GH_REPO"] = "other/fork"

        result = self.recover(project)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual({call["repo"] for call in project.gh_calls()}, {"example/scratch"})

    def test_the_original_tag_date_is_kept_in_the_recovered_body(self):
        project = Scratch(self)
        project.history()
        project.env["GIT_COMMITTER_DATE"] = "2020-03-04T12:00:00+00:00"
        project.git("tag", "-a", "v0.2.0", "-m", "v0.2.0")
        del project.env["GIT_COMMITTER_DATE"]

        result = self.recover(project)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("## v0.2.0 (2020-03-04)", project.gh_calls()[-1]["notes"])

    def test_the_range_is_frozen_at_the_target_with_an_unrelated_newer_tag(self):
        project = Scratch(self)
        original = self.release_and_notes(project)
        project.git("switch", "-q", "--detach", "v0.1.0")
        project.commit("chore: elsewhere", files={"release-notes/other.md": fragment("Other")})
        project.git("tag", "v9.0.0")
        project.git("switch", "-q", "--detach", "v0.2.0")
        result = self.recover(project)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(project.gh_calls()[-1]["notes"], original)

    def test_the_first_release_is_recovered_with_its_original_body(self):
        project = Scratch(self)
        project.commit("feat: first", files={"release-notes/new.md": fragment("First")})
        outcome = project.release()
        self.assertEqual(outcome.codes, (0, 0), outcome.prepared.stderr)
        original = project.notes()
        result = self.recover(project, "v0.1.0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(project.gh_calls()[-1]["notes"], original)

    def test_a_tag_the_history_does_not_yield_is_refused(self):
        project = Scratch(self)
        project.history()
        project.git("tag", "-a", "v0.3.0", "-m", "v0.3.0")

        result = self.recover(project, "v0.3.0")

        self.assertEqual(result.returncode, 1)
        self.assertIn("the history at v0.3.0 no longer yields that release", result.stderr)
        self.assertNotIn("create", [call["argv"][1] for call in project.gh_calls()])

    def test_a_failed_creation_is_reported(self):
        project = Scratch(self)
        project.history()
        project.git("tag", "-a", "v0.2.0", "-m", "v0.2.0")
        project.env["FAKE_GH_CREATE"] = "fail"

        result = self.recover(project)

        self.assertEqual(result.returncode, 1)
        self.assertEqual(
            result.stderr,
            "release notes: gh release create failed: HTTP 500; the draft may still exist: "
            "check `gh release view v0.2.0` before running recover again\n",
        )

    def run_arranged(self, project, setup):
        """Recover after the named change to the scratch project, if any."""
        if setup == "dated":
            project.env["GIT_COMMITTER_DATE"] = "2020-03-04T12:00:00+00:00"
            project.git("tag", "-f", "-a", "v0.2.0", "-m", "v0.2.0")
            del project.env["GIT_COMMITTER_DATE"]
        elif setup == "advance":
            project.commit("docs: later")
        elif setup == "lightweight":
            project.git("tag", "-d", "v0.2.0")
            project.git("tag", "v0.2.0")
        arguments = {
            "fork": ("v0.2.0", "other/fork"),
            "malformed": ("v0.2.0", "a/b?x=1"),
            "other-tag": ("v0.9.9", "example/scratch"),
        }.get(setup, ("v0.2.0", "example/scratch"))
        return project.notes_tool("recover", arguments[0], "--repository", arguments[1])

    def test_recovery_refuses_without_creating_anything_when_authority_is_unproven(self):
        for name, env, setup, message in (
            ("release exists", {"FAKE_GH_RELEASE": "exists"}, None, "already has a release"),
            ("lookup fails", {"FAKE_GH_RELEASE": "error"}, None, "could not tell whether"),
            ("lookup server error", {"FAKE_GH_RELEASE": "server"}, None, "could not tell whether"),
            ("lookup transport", {"FAKE_GH_RELEASE": "transport"}, None, "could not tell whether"),
            (
                "lookup partial 404 header",
                {"FAKE_GH_RELEASE": "partial"},
                None,
                "could not tell whether",
            ),
            (
                "lookup stale 404 header after a failure",
                {"FAKE_GH_RELEASE": "stale"},
                None,
                "could not tell whether",
            ),
            (
                "remote tag date differs",
                {"FAKE_GH_TAG_DATE": "2021-01-01T00:00:00Z"},
                "dated",
                "tag date",
            ),
            (
                "lookup without a status line",
                {"FAKE_GH_RELEASE": "nostatus"},
                None,
                "could not tell whether",
            ),
            (
                "remote answer: gh fails",
                {"FAKE_GH_REMOTE": "ghfail"},
                None,
                "could not read git/ref/tags/v0.2.0 of example/scratch",
            ),
            (
                "remote answer: not json",
                {"FAKE_GH_REMOTE": "notjson"},
                None,
                "could not read git/ref/tags/v0.2.0 of example/scratch",
            ),
            (
                "remote answer: a list",
                {"FAKE_GH_REMOTE": "list"},
                None,
                "could not read git/ref/tags/v0.2.0 of example/scratch",
            ),
            (
                "remote answer: object is text",
                {"FAKE_GH_REMOTE": "objectstr"},
                None,
                "the repository's answer lacks object.type",
            ),
            (
                "remote answer: empty object",
                {"FAKE_GH_REMOTE": "empty"},
                None,
                "the repository's answer lacks object.type",
            ),
            (
                "remote answer: no sha",
                {"FAKE_GH_REMOTE": "nosha"},
                None,
                "the repository's answer lacks object.sha",
            ),
            (
                "remote answer: annotated tag without a date",
                {"FAKE_GH_REMOTE": "notagger"},
                None,
                "the repository's answer lacks tagger.date",
            ),
            (
                "remote answer: lightweight tag without a date",
                {"FAKE_GH_REMOTE": "nocommitdate", "FAKE_GH_TAG_KIND": "lightweight"},
                "lightweight",
                "the repository's answer lacks committer.date",
            ),
            (
                "lookup sentence only",
                {"FAKE_GH_RELEASE": "sentence"},
                None,
                "could not tell whether",
            ),
            (
                "remote tag differs",
                {"FAKE_GH_TAG_SHA": "0" * 40},
                None,
                "the remote tag v0.2.0 in example/scratch is",
            ),
            ("malformed repository", {}, "malformed", "is not an owner/name repository"),
            ("other repository", {}, "fork", "No CI run for the main commit"),
            ("ci not green", {"FAKE_GH_CI": "failure"}, None, "Newest CI run has not completed"),
            ("tag is not head", {}, "advance", "is not the checked-out commit"),
            ("no such tag", {}, "other-tag", "no tag v0.9.9"),
        ):
            with self.subTest(name):
                project = Scratch(self)
                project.history()
                project.git("tag", "-a", "v0.2.0", "-m", "v0.2.0")
                project.env.update(env)
                result = self.run_arranged(project, setup)
                self.assertEqual(result.returncode, 1)
                self.assertIn(message, result.stderr)
                self.assertNotIn(
                    ["release", "create"], [call["argv"][:2] for call in project.gh_calls()]
                )
                self.assertEqual(project.tags(), ["v0.1.0", "v0.2.0"])


DIGEST = "ghcr.io/example/scratch@sha256:" + "c" * 64
MARKER = f"Container: `{DIGEST}` (tag `v0.2.0`)."
ASSETS = [{"name": n} for n in ("scan.json", "sbom.cdx.json", "image-digest.txt")]


class PublishTest(unittest.TestCase):
    """The assembled draft is published last, bound to its tag commit and digest bytes."""

    def draft(self, **view):
        project = Scratch(self)
        project.history()
        outcome = project.release()
        self.assertEqual(outcome.codes, (0, 0), outcome.prepared.stderr)
        record = {"isDraft": True, "assets": ASSETS, "body": f"Notes\n{MARKER}\n"} | view
        project.env.update(FAKE_GH_VIEW=json.dumps(record), FAKE_GH_DIGEST=DIGEST + "\n")
        project.gh_log.unlink(missing_ok=True)
        return project

    def publish(self, project, commit=None):
        return project.notes_tool(
            "publish",
            "v0.2.0",
            "--repository",
            "example/scratch",
            "--commit",
            commit or project.git("rev-parse", "HEAD"),
            "--digest",
            DIGEST,
        )

    def release_calls(self, project):
        return [c["argv"] for c in project.gh_calls() if c["argv"][0] == "release"]

    def test_a_bound_draft_is_published_with_one_final_edit(self):
        project = self.draft()
        result = self.publish(project)
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.release_calls(project)
        self.assertEqual(
            calls[0],
            [
                "release",
                "view",
                "v0.2.0",
                "--repo",
                "example/scratch",
                "--json",
                "isDraft,assets,body",
            ],
        )
        self.assertEqual(
            calls[1][:7],
            [
                "release",
                "download",
                "v0.2.0",
                "--repo",
                "example/scratch",
                "--pattern",
                "image-digest.txt",
            ],
        )
        self.assertEqual(
            calls[-1],
            [
                "release",
                "edit",
                "v0.2.0",
                "--repo",
                "example/scratch",
                "--draft=false",
                "--verify-tag",
            ],
        )
        self.assertEqual(len(calls), 3)

    def test_publication_is_refused_unless_every_binding_holds(self):
        cases = {
            "already published": ({"isDraft": False}, {}, "is not a draft"),
            "missing asset": ({"assets": ASSETS[1:]}, {}, "lacks the asset scan.json"),
            "no container line": ({"body": "Notes\n"}, {}, "does not name the container"),
            "other digest bytes": ({}, {"FAKE_GH_DIGEST": DIGEST[:-1] + "d\n"}, "image-digest"),
            "wrong record": ({"isDraft": "yes"}, {}, "release record"),
        }
        for name, (view, env, message) in cases.items():
            with self.subTest(name):
                project = self.draft(**view)
                project.env.update(env)
                result = self.publish(project)
                self.assertEqual(result.returncode, 1)
                self.assertIn(message, result.stderr)
                self.assertNotIn("edit", [c[1] for c in self.release_calls(project)])

    def test_a_failed_digest_download_is_refused_before_any_edit(self):
        project = self.draft()
        project.env["FAKE_GH_DOWNLOAD"] = "fail"
        result = self.publish(project)
        self.assertEqual(
            (result.returncode, result.stderr),
            (1, "release notes: could not download image-digest.txt of v0.2.0\n"),
        )
        self.assertEqual([c[1] for c in self.release_calls(project)], ["view", "download"])

    def test_a_failed_final_edit_is_reported_as_possibly_published_and_not_retried(self):
        project = self.draft()
        project.env["FAKE_GH_EDIT"] = "fail"
        result = self.publish(project)
        self.assertEqual(
            (result.returncode, result.stderr),
            (
                1,
                "release notes: gh release edit failed: HTTP 502; v0.2.0 may still have been "
                "published: check `gh release view v0.2.0` against the tag commit and digest\n",
            ),
        )
        self.assertEqual([c[1] for c in self.release_calls(project)], ["view", "download", "edit"])

    def test_a_tag_on_another_commit_is_refused(self):
        project = self.draft()
        result = self.publish(project, commit="0" * 40)
        self.assertEqual(result.returncode, 1)
        self.assertIn("is not the image source", result.stderr)
        self.assertNotIn("edit", [c[1] for c in self.release_calls(project)])


class PreviewTest(unittest.TestCase):
    def feature(self, project):
        project.commit("chore: start", files={"release-notes/old.md": fragment("Old note")})
        project.git("tag", "v0.1.0")
        base = project.commit("docs: main moves", files={"unrelated.txt": "x"})
        project.git("switch", "-q", "-c", "feature", "v0.1.0")
        head = project.commit(
            "feat: add the thing", files={"release-notes/new.md": fragment("New note")}
        )
        project.git("switch", "-q", "--detach", head)
        return (base, head)

    def preview(self, project, base, head):
        return project.notes_tool(
            "preview", "--base", base, "--head", head, "--repository-url", REPOSITORY_URL
        )

    def test_the_preview_reports_the_merge_and_the_body_without_touching_the_checkout(self):
        project = Scratch(self, origin=False)
        base, head = self.feature(project)
        before = (project.git("rev-parse", "HEAD"), project.tags(), project.git("branch", "--list"))
        result = self.preview(project, base, head)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(
            result.stdout,
            rf"\A## Release notes preview\n\n- base: `{base}`\n- head: `{head}`\n"
            r"- merge: `[0-9a-f]{40}`\n- release due: yes \(`v0\.2\.0`\)\n\n"
            r"## Release summary\n\n",
        )
        self.assertIn(section("New note"), result.stdout)
        self.assertIn(GENERATED, result.stdout)
        self.assertEqual(
            (project.git("rev-parse", "HEAD"), project.tags(), project.git("branch", "--list")),
            before,
        )
        self.assertEqual(project.git("status", "--porcelain"), "")
        self.assertEqual(project.gh_calls(), [])

    def test_a_preview_with_no_release_due_says_so_and_renders_no_notes(self):
        project = Scratch(self, origin=False)
        project.commit("chore: start")
        project.git("tag", "v0.1.0")
        base = project.git("rev-parse", "HEAD")
        project.git("switch", "-q", "-c", "feature")
        head = project.commit("docs: explain")

        result = self.preview(project, base, head)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(
            result.stdout,
            rf"\A## Release notes preview\n\n- base: `{base}`\n- head: `{head}`\n"
            r"- merge: `[0-9a-f]{40}`\n- release due: no\n\Z",
        )

    def test_an_unknown_base_fails_on_the_fetch_and_changes_nothing(self):
        project = Scratch(self, origin=False)
        _, head = self.feature(project)
        before = (
            project.git("rev-parse", "HEAD"),
            project.tags(),
            project.git("status", "--porcelain"),
        )

        result = self.preview(project, "1" * 40, head)

        self.assertEqual(result.returncode, 1)
        self.assertTrue(
            result.stderr.startswith("release notes: git fetch failed: "), result.stderr
        )
        self.assertEqual(
            (
                project.git("rev-parse", "HEAD"),
                project.tags(),
                project.git("status", "--porcelain"),
            ),
            before,
        )

    def test_a_merge_conflict_fails_with_both_commits_and_the_git_diagnostic(self):
        project = Scratch(self, origin=False)
        project.commit("chore: start", files={"shared.txt": "one\n"})
        project.git("tag", "v0.1.0")
        base = project.commit("docs: main edit", files={"shared.txt": "main\n"})
        project.git("switch", "-q", "-c", "feature", "v0.1.0")
        head = project.commit("feat: feature edit", files={"shared.txt": "feature\n"})
        result = self.preview(project, base, head)
        self.assertEqual(result.returncode, 1)
        self.assertIn(f"merge failed between {base} and {head} (exit 1)", result.stderr)
        self.assertIn("CONFLICT (content)", result.stderr)

    def test_a_merge_that_fails_without_a_conflict_is_reported_as_failed_with_git_diagnostic(self):
        project = Scratch(self, origin=False)
        project.commit("chore: start")
        project.git("tag", "v0.1.0")
        base = project.git("rev-parse", "HEAD")
        project.git("switch", "-q", "--orphan", "unrelated")
        head = project.commit("feat: unrelated history", files={"other.txt": "x"})

        result = self.preview(project, base, head)

        self.assertEqual(result.returncode, 1)
        self.assertIn(f"merge failed between {base} and {head} (exit 128)", result.stderr)
        self.assertIn("refusing to merge unrelated histories", result.stderr)
        self.assertNotIn("conflict", result.stderr.lower())

    def test_an_invalid_note_fails_the_preview(self):
        project = Scratch(self, origin=False)
        project.commit("chore: start")
        project.git("tag", "v0.1.0")
        base = project.git("rev-parse", "HEAD")
        project.git("switch", "-q", "-c", "feature")
        head = project.commit("feat: add", files={"release-notes/new.md": "# only a title\n"})
        result = self.preview(project, base, head)
        self.assertEqual(result.returncode, 1)
        self.assertIn("release-notes/new.md", result.stderr)


if __name__ == "__main__":
    unittest.main()
