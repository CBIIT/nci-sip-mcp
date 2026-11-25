import io
import json
import logging
import sys
import tempfile
import types
from contextlib import contextmanager, redirect_stderr
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from fakes import concept
from nci_sip_mcp import container_entry
from nci_sip_mcp.audit import JsonFormatter
from ncit_index.embeddings import HashingEmbeddingProvider, SentenceTransformersProvider
from ncit_index.errors import NoActiveIndexError
from test_index import normalized, update_manifest
from test_server import ServerFixture


class ContainerTest(ServerFixture):
    def setUp(self):
        super().setUp()
        self.settings = replace(self.settings, http_require_index=True, transport="streamable-http")

    def index(self):
        self.local_index.upsert_concepts(
            normalized([concept("C1")]), None, HashingEmbeddingProvider()
        )

    def test_missing_required_index_is_rejected_without_creating_one(self):
        self.local_index.db_path.unlink()
        with self.assertRaises(NoActiveIndexError):
            container_entry.prepared_index(self.settings)
        self.assertFalse(self.local_index.db_path.exists())

    def test_empty_or_incompatible_index_is_rejected(self):
        with self.assertRaises(NoActiveIndexError):
            container_entry.prepared_index(self.settings)
        self.index()
        self.assertEqual(
            container_entry.prepared_index(self.settings).get_active_manifest().concept_count, 1
        )

    def test_an_index_built_for_another_model_stops_startup_naming_the_index(self):
        logging.disable(logging.NOTSET)
        self.index()
        wrong = replace(
            self.settings, embedding_provider="sentence-transformers", embedding_model="other"
        )
        with (
            patch.object(container_entry.Settings, "from_env", return_value=wrong),
            patch.object(container_entry, "configure_logging"),
            # The image has no cache for the other model: the index check must come first.
            patch.object(
                container_entry, "SentenceTransformersProvider", side_effect=OSError("not cached")
            ),
            patch.object(container_entry, "run_http") as serve,
            self.assertLogs("nci_sip_mcp.container_entry", level="ERROR") as logs,
        ):
            self.assertEqual(container_entry.main(), 1)
        serve.assert_not_called()
        records = [json.loads(JsonFormatter().format(record)) for record in logs.records]
        self.assertEqual(
            [(r["asset"], r["errorType"]) for r in records], [("index", "IndexCompatibilityError")]
        )

    def test_startup_reports_one_safe_asset_failure(self):
        logging.disable(logging.NOTSET)
        with (
            patch.object(container_entry.Settings, "from_env", return_value=self.settings),
            patch.object(container_entry, "configure_logging"),
            patch.object(container_entry, "prepared_index", side_effect=OSError("sensitive-token")),
            self.assertLogs("nci_sip_mcp.container_entry", level="ERROR") as logs,
        ):
            self.assertEqual(container_entry.main(), 1)
        records = [json.loads(JsonFormatter().format(record)) for record in logs.records]
        self.assertEqual(
            [(r["event"], r["asset"], r["errorType"]) for r in records],
            [("container_startup_failed", "index", "OSError")],
        )
        self.assertNotIn("sensitive-token", json.dumps(records))

    def test_http_serve_keeps_the_active_build(self):
        self.index()
        before = self.context.index.get_active_manifest()
        seen = []

        def serve(settings, context):
            seen.append((settings.transport, context.index.get_active_manifest()))

        with (
            patch.object(container_entry.Settings, "from_env", return_value=self.settings),
            patch.object(container_entry, "run_http", side_effect=serve),
        ):
            self.assertEqual(container_entry.main(), 0)
        self.assertEqual(seen, [("streamable-http", before)])
        self.assertEqual(self.context.index.get_active_manifest(), before)

    def test_absent_local_model_fails_before_importing_the_embedding_library(self):
        absent = str(self.settings.data_dir / "absent-model")
        with (
            patch.dict(sys.modules, {"sentence_transformers": None}),
            self.assertRaisesRegex(FileNotFoundError, "external model"),
        ):
            SentenceTransformersProvider(absent, local_files_only=True)

    def test_cached_model_loads_offline_without_changing_manifest_identity(self):
        # A Hub cache folder is a commit snapshot; the revision is read from its name.
        cached = str(self.settings.data_dir / "snapshots" / ("a" * 40))
        Path(cached).mkdir(parents=True)
        (Path(cached) / "config.json").write_text("{}")  # a directory model is hashed, not empty

        class LocalModel:
            def __init__(self, name, *, local_files_only=False):
                if (name, local_files_only) != (cached, True):
                    raise OSError("Only the local cache is available")

            def encode(self, texts, *, normalize_embeddings, show_progress_bar):
                if show_progress_bar:
                    raise ValueError("Progress output is disabled")
                return [[len(text), int(normalize_embeddings)] for text in texts]

        def snapshot(name, *, local_files_only=False, cache_dir=None):
            if (name, local_files_only) != ("org/model", True):
                raise OSError("Network is unavailable")
            return cached

        modules = {
            "sentence_transformers": types.SimpleNamespace(SentenceTransformer=LocalModel),
            "huggingface_hub": types.SimpleNamespace(snapshot_download=snapshot),
        }
        with patch.dict(sys.modules, modules):
            provider = SentenceTransformersProvider("org/model", local_files_only=True)
            local = SentenceTransformersProvider(cached, local_files_only=True)
        self.assertEqual(
            (provider.model, [row.tolist() for row in provider.embed(["abc"])]),
            ("org/model", [[3.0, 1.0]]),
        )
        self.assertEqual([row.tolist() for row in local.embed(["ab"])], [[2.0, 1.0]])

    def test_bad_model_setting_names_the_variable_without_its_value(self):
        logging.disable(logging.NOTSET)
        error = ValueError("NCI_SIP_EMBEDDING_MODEL cannot be sensitive-token")
        with (
            patch.object(container_entry.Settings, "from_env", side_effect=error),
            patch.object(container_entry, "configure_logging"),
            self.assertLogs("nci_sip_mcp.container_entry", level="ERROR") as logs,
        ):
            self.assertEqual(container_entry.main(), 1)
        records = [json.loads(JsonFormatter().format(record)) for record in logs.records]
        self.assertEqual([r["parameter"] for r in records], ["NCI_SIP_EMBEDDING_MODEL"])
        self.assertNotIn("sensitive-token", json.dumps(records))

    def test_fatal_startup_diagnostic_survives_critical_verbosity(self):
        logging.disable(logging.NOTSET)
        settings = replace(self.settings, log_level="CRITICAL")
        output = io.StringIO()
        with (
            patch.object(logging.getLogger(), "handlers", []),
            redirect_stderr(output),
            patch.object(container_entry.Settings, "from_env", return_value=settings),
        ):
            self.assertEqual(container_entry.main(), 1)
        records = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(
            [(r["event"], r["asset"]) for r in records], [("container_startup_failed", "index")]
        )


class OfflineModel:
    def __init__(self, _name, *, local_files_only=False):
        if not local_files_only:
            raise OSError("Network loading is forbidden")

    def encode(self, texts, **_options):
        return [[len(text), 1] for text in texts]


@contextmanager
def fake_sentence_transformers(model):
    """Replace only the sentence_transformers entry of sys.modules, restoring only that entry.

    `patch.dict(sys.modules, ...)` restores the whole mapping, which evicts any module first
    imported inside it (NumPy, imported by `embed`); a later import of NumPy then fails.
    """
    saved = sys.modules.get("sentence_transformers")
    sys.modules["sentence_transformers"] = types.SimpleNamespace(SentenceTransformer=model)
    try:
        yield
    finally:
        if saved is None:
            del sys.modules["sentence_transformers"]
        else:
            sys.modules["sentence_transformers"] = saved


class ContainerOfflineTest(ServerFixture):
    def model_directory(self):
        """An external model directory of its own: the data directory holds a live database."""
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name)
        (path / "config.json").write_text("{}")
        (path / "weights.bin").write_bytes(b"w1")
        return path

    def test_legacy_active_index_fails_startup_until_operator_rebuilds_it(self):
        settings = replace(self.settings, transport="streamable-http", http_require_index=True)
        self.local_index.upsert_concepts(
            normalized([concept("C1")]), None, HashingEmbeddingProvider()
        )
        legacy = replace(self.context.index.get_active_manifest(), needs_rebuild=True)
        update_manifest(self.local_index, legacy)
        logging.disable(logging.NOTSET)
        with (
            patch.object(container_entry.Settings, "from_env", return_value=settings),
            patch.object(container_entry, "configure_logging"),
            patch.object(
                container_entry, "run_http", side_effect=AssertionError("Cannot serve legacy index")
            ),
            self.assertLogs("nci_sip_mcp.container_entry", level="ERROR") as captured,
        ):
            self.assertEqual(container_entry.main(), 1)
        self.assertEqual(captured.records[0].structured["asset"], "index")
        self.assertEqual(captured.records[0].structured["errorType"], "PlatformError")
        self.assertEqual(self.context.index.get_active_manifest(), legacy)

    def test_startup_serves_an_external_model_with_network_loading_disabled(self):
        settings = replace(
            self.settings,
            transport="streamable-http",
            http_require_index=True,
            embedding_provider="sentence-transformers",
            embedding_model=str(self.model_directory()),
        )
        served = []

        def serve(_settings, context):
            served.append([row.tolist() for row in context.embedding_provider.embed(["abc"])])

        with fake_sentence_transformers(OfflineModel):
            provider = SentenceTransformersProvider(settings.embedding_model, local_files_only=True)
            self.local_index.upsert_concepts(normalized([concept("C1")]), None, provider)
            before = self.context.index.get_active_manifest()
            with (
                patch.object(container_entry.Settings, "from_env", return_value=settings),
                patch.object(container_entry, "run_http", serve),
            ):
                self.assertEqual(container_entry.main(), 0)
        self.assertEqual(served, [[[3.0, 1.0]]])
        self.assertEqual(self.context.index.get_active_manifest(), before)

    def test_startup_refuses_a_model_directory_that_changed_since_the_index_was_built(self):
        model = self.model_directory()
        settings = replace(
            self.settings,
            transport="streamable-http",
            http_require_index=True,
            embedding_provider="sentence-transformers",
            embedding_model=str(model),
        )
        logging.disable(logging.NOTSET)
        with fake_sentence_transformers(OfflineModel):
            provider = SentenceTransformersProvider(str(model), local_files_only=True)
        self.local_index.upsert_concepts(normalized([concept("C1")]), None, provider)
        served = []
        with (
            fake_sentence_transformers(OfflineModel),
            patch.object(container_entry.Settings, "from_env", return_value=settings),
            patch.object(container_entry, "configure_logging"),
            patch.object(container_entry, "run_http", lambda *_: served.append("served")),
        ):
            self.assertEqual((container_entry.main(), served), (0, ["served"]))
            (model / "weights.bin").write_bytes(b"w2")
            with (
                self.assertLogs("nci_sip_mcp.index", level="INFO") as logs,
                self.assertLogs("nci_sip_mcp.container_entry", level="ERROR") as startup,
            ):
                code = container_entry.main()
        mismatch = [
            r.structured for r in logs.records if r.structured["event"].endswith("_mismatch")
        ]
        self.assertEqual((code, served), (1, ["served"]))
        self.assertEqual(
            [
                (r.structured["event"], r.structured["asset"], r.structured["errorType"])
                for r in startup.records
            ],
            [("container_startup_failed", "index", "IndexModelRevisionError")],
        )
        self.assertEqual(len(mismatch), 1)
        stored, runtime = mismatch[0]["stored_revision"], mismatch[0]["runtime_revision"]
        self.assertTrue(stored.startswith("sha256:") and runtime.startswith("sha256:"))
        self.assertNotEqual(stored, runtime)
