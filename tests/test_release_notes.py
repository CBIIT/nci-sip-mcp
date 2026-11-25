"""Reviewed release notes reach the real semantic-release flow before a tag exists.

Every scenario runs the entry points in a sacrificial repository whose remote is unreachable:
a local tag can be created, nothing can be published. `gh` is a recording stub.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import NamedTuple

ROOT = Path(__file__).parent.parent
PYPROJECT = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
RELEASE_TABLE = PYPROJECT[
    PYPROJECT.index("[tool.semantic_release]") : PYPROJECT.index("[tool.pdm.scripts]")
]
CONFIG = ".release-notes/semantic-release.json"
UNREACHABLE = "file:///nonexistent/nci-sip-mcp-scratch-remote.git"
REPOSITORY_URL = "https://github.com/example/scratch.git"
GENERATED = "<details>\n<summary>Generated change list</summary>\n\n"
FAKE_GH = f"""#!{sys.executable}
import json, os, sys
argv = sys.argv[1:]
record = {{"argv": argv}}
if argv[:2] == ["release", "create"]:
    record["notes"] = open(argv[argv.index("--notes-file") + 1], encoding="utf-8").read()
with open(os.environ["FAKE_GH_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps(record) + "\\n")
if argv[0] == "api":
    sha = [p for p in argv[1].split("?")[1].split("&") if p.startswith("head_sha=")][0][9:]
    run = {{"id": 7, "head_sha": sha, "head_branch": "main", "event": "push",
           "path": ".github/workflows/ci.yml", "conclusion": os.environ["FAKE_GH_CI"]}}
    print(json.dumps({{"workflow_runs": [run]}}))
elif argv[:2] == ["release", "view"]:
    state = os.environ["FAKE_GH_RELEASE"]
    if state == "exists":
        print('{{"url": "https://example.test/release"}}')
    else:
        sys.stderr.write("release not found" if state == "absent" else "HTTP 401: Bad credentials")
        sys.exit(1)
elif argv[:2] == ["release", "create"]:
    pass
else:
    sys.exit(99)
"""


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

    def test_the_generated_template_tree_is_not_tracked_or_staged(self):
        project = Scratch(self)
        project.history()
        outcome = project.release()
        self.assertEqual(outcome.codes, (0, 0), outcome.prepared.stderr)
        self.assertTrue((project.root / ".release-notes/template").is_dir())
        self.assertEqual(project.git("status", "--porcelain"), "")
        self.assertEqual(project.git("ls-files", ".release-notes"), "")


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
        result = self.recover(project)
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = project.gh_calls()
        route = (
            "repos/example/scratch/actions/workflows/ci.yml/runs"
            f"?head_sha={head}&branch=main&event=push&per_page=100"
        )
        self.assertEqual(
            [call["argv"][:2] for call in calls],
            [["api", route], ["release", "view"], ["release", "create"]],
        )
        self.assertEqual(calls[1]["argv"], ["release", "view", "v0.2.0", "--json", "url"])
        create = calls[2]
        self.assertEqual(
            create["argv"][:5], ["release", "create", "v0.2.0", "--verify-tag", "--notes-file"]
        )
        self.assertEqual(len(create["argv"]), 6)
        self.assertEqual(create["notes"], original)
        self.assertEqual(project.tags(), ["v0.1.0", "v0.2.0"])

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

    def test_recovery_refuses_without_creating_anything_when_authority_is_unproven(self):
        for name, env, setup, message in (
            ("release exists", {"FAKE_GH_RELEASE": "exists"}, None, "already has a release"),
            ("lookup fails", {"FAKE_GH_RELEASE": "error"}, None, "could not tell whether"),
            ("ci not green", {"FAKE_GH_CI": "failure"}, None, "Newest CI run has not completed"),
            ("tag is not head", {}, "advance", "is not the checked-out commit"),
            ("no such tag", {}, "other-tag", "no tag v0.9.9"),
        ):
            with self.subTest(name):
                project = Scratch(self)
                project.history()
                project.git("tag", "-a", "v0.2.0", "-m", "v0.2.0")
                if setup == "advance":
                    project.commit("docs: later")
                project.env.update(env)
                result = self.recover(project, "v0.9.9" if setup == "other-tag" else "v0.2.0")
                self.assertEqual(result.returncode, 1)
                self.assertIn(message, result.stderr)
                self.assertNotIn(
                    ["release", "create"], [call["argv"][:2] for call in project.gh_calls()]
                )
                self.assertEqual(project.tags(), ["v0.1.0", "v0.2.0"])


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

    def test_a_merge_conflict_fails_and_names_both_commits(self):
        project = Scratch(self, origin=False)
        project.commit("chore: start", files={"shared.txt": "one\n"})
        project.git("tag", "v0.1.0")
        base = project.commit("docs: main edit", files={"shared.txt": "main\n"})
        project.git("switch", "-q", "-c", "feature", "v0.1.0")
        head = project.commit("feat: feature edit", files={"shared.txt": "feature\n"})
        result = self.preview(project, base, head)
        self.assertEqual(result.returncode, 1)
        self.assertIn(f"merge conflict between {base} and {head}", result.stderr)

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
