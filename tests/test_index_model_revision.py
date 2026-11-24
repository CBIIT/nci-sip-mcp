"""An index records the model revision it was embedded with and refuses a different one."""

import io
import json
import sqlite3
import sys
import tempfile
import types
import unittest
from contextlib import closing, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from nci_sip_mcp.cli import main
from nci_sip_mcp.index_adapter import LocalIndexAdapter, index_event
from nci_sip_mcp.registry import invoke
from ncit_index.embeddings import (
    EmbeddingProvider,
    HashingEmbeddingProvider,
    SentenceTransformersProvider,
)
from ncit_index.errors import IndexCompatibilityError
from ncit_index.index import LocalIndex
from test_audit import captured
from test_server import ServerFixture

COMMIT = "a" * 40
NEXT_STEP = (
    "Serve the model files this build was embedded with, or run `index-rebuild` "
    "with the serving model and activate its evaluated build."
)


def provider(revision):
    """A test double: the hashing provider reporting the given model revision."""
    double = HashingEmbeddingProvider()
    double.revision = revision
    return double


class ModelRevisionTest(ServerFixture):
    def index_with(self, revision):
        self.context.embedding_provider = provider(revision)
        invoke(self.context, "index_codes", ["C3262"])
        return self.local_index.get_active_manifest()

    def revision_records(self, logs):
        """The revision events with their own fields (not timestamp, level or correlation)."""
        own = {"build_id", "stored_revision", "runtime_revision"}
        return [
            (row.structured["event"], {k: v for k, v in row.structured.items() if k in own})
            for row in logs.records
            if row.structured["event"].startswith("index_model_revision_")
        ]

    def search(self):
        return invoke(self.context, "search", query="Neoplasm", mode="bm25")

    def test_a_different_runtime_revision_refuses_search_and_names_both(self):
        build = self.index_with("r1")
        self.assertEqual(build.to_dict().get("embedding_revision", "absent"), "r1")
        with captured(), self.assertLogs("nci_sip_mcp.index", level="INFO") as logs:
            self.assertIn("hits", self.search())
            self.context.embedding_provider = provider("r2")
            first, second = self.search(), self.search()
        for answer in (first, second):
            self.assertEqual(answer["error"]["code"], "internal_error")
            self.assertEqual(
                answer["error"]["message"],
                "Index model revision r1 does not match the serving model revision r2. "
                + NEXT_STEP,
            )
        # Once per build in this process, however often it is asked.
        self.assertEqual(
            self.revision_records(logs),
            [
                (
                    "index_model_revision_mismatch",
                    {
                        "build_id": build.build_id,
                        "stored_revision": "r1",
                        "runtime_revision": "r2",
                    },
                )
            ],
        )

    def test_an_unknown_runtime_revision_cannot_be_compared_with_a_known_one(self):
        build = self.index_with("r1")
        self.context.embedding_provider = provider(None)
        with captured(), self.assertLogs("nci_sip_mcp.index", level="INFO") as logs:
            answer = self.search()
        self.assertEqual(
            answer["error"]["message"],
            "Index model revision r1 cannot be compared: the serving model revision cannot be "
            "determined. " + NEXT_STEP,
        )
        self.assertEqual(
            self.revision_records(logs),
            [
                (
                    "index_model_revision_mismatch",
                    {"build_id": build.build_id, "stored_revision": "r1", "runtime_revision": None},
                )
            ],
        )

    def test_evaluation_of_a_build_applies_the_same_check(self):
        build = self.index_with("r1")
        with self.assertRaises(IndexCompatibilityError) as refused:
            self.local_index.search_build(build.build_id, "Neoplasm", provider("r2"))
        self.assertEqual(type(refused.exception).__name__, "IndexModelRevisionError")

    def test_the_evaluate_command_refuses_a_build_under_another_model_revision(self):
        build = self.index_with("r1")
        self.context.embedding_provider = provider("r2")
        stdout = io.StringIO()
        with (
            patch("nci_sip_mcp.cli.configure_logging"),
            patch("nci_sip_mcp.cli.Context", return_value=self.context),
            patch.object(sys, "argv", ["nci-sip-mcp", "evaluate", "--build-id", build.build_id]),
            redirect_stdout(stdout),
        ):
            code = main()
        answer = json.loads(stdout.getvalue())
        self.assertEqual(
            (code, answer["error"]["code"], answer["error"]["message"]),
            (
                1,
                "internal_error",
                "Index model revision r1 does not match the serving model revision r2. "
                + NEXT_STEP,
            ),
        )

    def test_a_legacy_build_without_a_revision_is_served_and_noted_once_never_backfilled(self):
        build = self.index_with("r1")
        with closing(sqlite3.connect(self.local_index.db_path)) as conn, conn:
            payload = json.loads(conn.execute("SELECT payload FROM manifests").fetchone()[0])
            payload.pop("embedding_revision", None)
            conn.execute("UPDATE manifests SET payload = ?", (json.dumps(payload),))
        for runtime, expected in (("r1", "r1"), (None, None)):
            with self.subTest(runtime=runtime):
                self.context.embedding_provider = provider(runtime)
                # A new reader per case: the once-per-build memory belongs to one process.
                reader = LocalIndex(self.settings.data_dir, on_event=index_event)
                self.context.index = LocalIndexAdapter(reader)
                with captured(), self.assertLogs("nci_sip_mcp.index", level="INFO") as logs:
                    self.assertIn("hits", self.search())
                    self.assertIn("hits", self.search())
                self.assertEqual(
                    self.revision_records(logs),
                    [
                        (
                            "index_model_revision_unknown",
                            {"build_id": build.build_id, "runtime_revision": expected},
                        )
                    ],
                )
        with closing(sqlite3.connect(self.local_index.db_path)) as conn:
            stored = json.loads(conn.execute("SELECT payload FROM manifests").fetchone()[0])
        self.assertNotIn("embedding_revision", stored)

    def test_index_builds_lists_the_stored_revision(self):
        self.index_with("r1")
        self.assertEqual(
            [
                b.get("embedding_revision", "absent")
                for b in invoke(self.context, "index_builds")["builds"]
            ],
            ["r1"],
        )


class RevisionSourceTest(unittest.TestCase):
    """The revision comes from the local cache layout only, never from a download."""

    def revision(self, name, **options):
        provider = SentenceTransformersProvider(name, **options)
        return getattr(provider, "revision", "absent")

    def test_a_hub_model_reports_the_commit_of_its_cached_snapshot(self):
        path = f"/cache/models--org--model/snapshots/{COMMIT}"
        for options in ({"local_files_only": True}, {}):
            with (
                self.subTest(**options),
                patch.dict(sys.modules, self.fake_modules(path)),
            ):
                self.assertEqual(self.revision("org/model", **options), COMMIT)

    def test_a_cache_folder_that_is_not_a_commit_is_refused(self):
        modules = self.fake_modules("/cache/models--org--model/snapshots/main")
        with (
            patch.dict(sys.modules, modules),
            self.assertRaisesRegex(IndexCompatibilityError, "commit"),
        ):
            SentenceTransformersProvider("org/model", local_files_only=True)

    def test_a_directory_model_is_hashed_not_mistaken_for_a_hub_commit_by_its_path(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory) / "snapshots" / COMMIT
            snapshot.mkdir(parents=True)
            (snapshot / "config.json").write_text("{}")
            with patch.dict(sys.modules, self.fake_modules(str(snapshot))):
                revision = self.revision(str(snapshot), local_files_only=True)
        self.assertTrue(str(revision).startswith("sha256:"), revision)

    def test_hashing_and_custom_providers_report_what_they_declare(self):
        class Custom(EmbeddingProvider):
            name, model = "custom", "custom"

            def embed(self, texts):
                return [[1.0] for _ in texts]

        self.assertEqual(
            (
                getattr(HashingEmbeddingProvider(), "revision", "absent"),
                getattr(Custom(), "revision", "absent"),
            ),
            ("hashing-v1", None),
        )

    @staticmethod
    def fake_modules(snapshot_path):
        class Model:
            def __init__(self, name, *, local_files_only=False):
                self.name = name

            def encode(self, texts, **_):
                return [[1.0, 0.0] for _ in texts]

        def snapshot(name, *, local_files_only=False):
            if not local_files_only:
                raise OSError("a download is not allowed")
            return snapshot_path

        return {
            "sentence_transformers": types.SimpleNamespace(SentenceTransformer=Model),
            "huggingface_hub": types.SimpleNamespace(snapshot_download=snapshot),
        }
