import io
import json
import subprocess
import tempfile
import unittest
from contextlib import chdir, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from scripts import image_publish


class PublicationService:
    digest = "ghcr.io/new-org/project@sha256:" + "a" * 64

    def __init__(self, *, public=True, fail=None):
        self.public, self.fail = public, fail
        self.image = None
        self.local_images = {
            "nci-sip-mcp:verified": self.digest,
            "nci-sip-mcp:unverified": "ghcr.io/new-org/project@sha256:" + "b" * 64,
        }
        self.assets = {}
        self.notes = "Release description\n"

    def command(self, *args):
        operation = args[2] if args[0] == "gh" else args[1]
        if operation == self.fail:
            raise subprocess.CalledProcessError(1, operation)
        commands = {
            "tag": self.run_tag,
            "push": self.run_push,
            "image": self.run_image,
            "--config": self.run___config,
            "upload": self.run_upload,
            "view": self.run_view,
            "edit": self.run_edit,
        }
        return commands[operation](*args)

    def run_tag(self, *args):
        self.local_images[args[3]] = self.local_images[args[2]]
        return ""

    def run_push(self, *args):
        self.image = args[2]
        return ""

    def run_image(self, *args):
        return json.dumps([self.local_images[args[3]]])

    def run___config(self, *args):
        # A mutable tag can be public while the digest we are releasing is unavailable.
        public_refs = {self.image: True, self.digest: self.public}
        if (
            self.image != "ghcr.io/new-org/project:v0.15.0"
            or not public_refs.get(args[-1], False)
            or any(Path(args[2]).iterdir())
        ):
            raise subprocess.CalledProcessError(1, "anonymous manifest check")
        return ""

    def run_upload(self, *args):
        self.assets = {Path(path).name: Path(path).read_text() for path in args[4:-1]}
        return ""

    def run_view(self, *args):
        return json.dumps({"body": self.notes})

    def run_edit(self, *args):
        self.notes = Path(args[-1]).read_text()
        return ""


class ImagePublicationTest(unittest.TestCase):
    def test_missing_publication_program_reports_the_missing_executable(self):
        with (
            patch.object(image_publish.shutil, "which", return_value=None),
            self.assertRaises(FileNotFoundError),
        ):
            image_publish.run("nci-si-missing-publication-command")

    def test_missing_docker_refuses_publication_before_login(self):
        with (
            patch.object(image_publish.shutil, "which", return_value=None),
            patch.dict(
                "os.environ",
                {
                    "PATH": "",
                    "GITHUB_REPOSITORY": "role/repo",
                    "GITHUB_ACTOR": "role",
                    "GH_TOKEN": "not-logged",
                },
            ),
            patch("sys.argv", ["image_publish", "v1.0.0"]),
            self.assertRaises(FileNotFoundError),
        ):
            image_publish.main()

    def evidence(self):
        evidence = Path("tmp/image")
        evidence.mkdir(parents=True)
        (evidence / "scan.json").write_text('{"Results": []}')
        (evidence / "sbom.cdx.json").write_text('{"bomFormat": "CycloneDX"}')
        return evidence

    def publish(self, *, public, fail=None):
        self.service = PublicationService(public=public, fail=fail)
        env = {
            "GITHUB_REPOSITORY": "New-Org/Project",
            "GITHUB_ACTOR": "release-bot",
            "GH_TOKEN": "test-token",
            "GITHUB_OUTPUT": str(Path("outputs").resolve()),
        }
        self.output = io.StringIO()
        with (
            patch.dict("os.environ", env),
            patch("sys.argv", ["image_publish.py", "v0.15.0"]),
            patch.object(image_publish, "run", side_effect=self.service.command),
            patch.object(image_publish.subprocess, "run"),
            redirect_stdout(self.output),
        ):
            image_publish.main()
        return self.service.digest, self.output.getvalue()

    def test_transferred_repository_digest_and_reports_are_named_in_notes(self):
        with tempfile.TemporaryDirectory() as directory, chdir(directory):
            evidence = self.evidence()
            digest, output = self.publish(public=True)
            notes = self.service.notes
            self.assertEqual(self.service.image, "ghcr.io/new-org/project:v0.15.0")
            self.assertEqual(
                self.service.assets,
                {
                    "scan.json": '{"Results": []}',
                    "sbom.cdx.json": '{"bomFormat": "CycloneDX"}',
                    "image-digest.txt": digest + "\n",
                },
            )
            self.assertTrue(notes.startswith("Release description"))
            self.assertIn(digest, notes)
            self.assertIn("sbom.cdx.json", notes)
            self.assertIn("scan.json", notes)
            self.assertEqual((evidence / "image-digest.txt").read_text(), digest + "\n")
            self.assertIn("verified public image", output)
            self.assertNotIn("test-token", output + notes)
            self.assertEqual(
                Path("outputs").read_text(),
                "subject-name=ghcr.io/new-org/project\nsubject-digest=sha256:" + "a" * 64 + "\n",
            )

    def test_private_package_cannot_be_reported_as_public(self):
        with tempfile.TemporaryDirectory() as directory, chdir(directory):
            self.evidence()
            with self.assertRaises(subprocess.CalledProcessError):
                self.publish(public=False)
            self.assertFalse(Path("tmp/image/release-notes.md").exists())
            self.assertFalse(Path("outputs").exists())

    def test_publication_failures_never_announce_success(self):
        for operation in ("push", "upload", "edit"):
            with (
                self.subTest(operation=operation),
                tempfile.TemporaryDirectory() as directory,
                chdir(directory),
            ):
                self.evidence()
                with self.assertRaises(subprocess.CalledProcessError):
                    self.publish(public=True, fail=operation)
                self.assertNotIn("verified public image", self.output.getvalue())
                self.assertEqual(self.service.notes, "Release description\n")

    def test_invalid_tag_fails_before_publication(self):
        with (
            patch("sys.argv", ["image_publish.py", "--bad-tag"]),
            self.assertRaisesRegex(ValueError, "release tag"),
        ):
            image_publish.main()
