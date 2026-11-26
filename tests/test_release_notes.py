"""Reviewed release notes reach the real semantic-release flow before a tag exists.

Every scenario runs the entry points in a sacrificial repository whose remote is unreachable:
a local tag can be created, nothing can be published. `gh` is a recording stub.
"""

import contextlib
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from typing import NamedTuple

import jinja2
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
if argv[0] != "api" and argv[:2] != ["release", "create"]:
    sys.exit(99)
if argv[:2] == ["release", "create"]:
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
    print(json.dumps({"tagger": {"date": os.environ.get("FAKE_GH_TAG_DATE") or date},
                      "object": {"type": "commit", "sha": commit}}))
elif "/git/commits/" in path:
    sha = path.rsplit("/", 1)[1]
    date = git("log", "-1", "--format=%cI", sha)
    date = os.environ.get("FAKE_GH_TAG_DATE") or date
    print(json.dumps({"sha": sha, "committer": {"date": date}}))
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
        with tempfile.TemporaryDirectory() as directory, contextlib.chdir(directory):
            fake_package = Path(directory) / "semantic_release" / "__init__.py"
            with (
                unittest.mock.patch.object(
                    release_notes.semantic_release, "__file__", str(fake_package)
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
        self.assertIn("gh release create failed: HTTP 500", result.stderr)

    def run_arranged(self, project, setup):
        """Recover after the named change to the scratch project, if any."""
        if setup == "dated":
            project.env["GIT_COMMITTER_DATE"] = "2020-03-04T12:00:00+00:00"
            project.git("tag", "-f", "-a", "v0.2.0", "-m", "v0.2.0")
            del project.env["GIT_COMMITTER_DATE"]
        elif setup == "advance":
            project.commit("docs: later")
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
