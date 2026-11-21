import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from fakes import concept
from nci_sip_mcp.errors import InputValidationError
from nci_sip_mcp.evaluation import evaluate_build
from nci_sip_mcp.evaluation_sets import EvaluationSet, GoldQuery, MetricFloor
from nci_sip_mcp.evs import normalize_concept
from nci_sip_mcp.index_adapter import LocalIndexAdapter, index_event, results_truncation
from ncit_index.embeddings import EmbeddingProvider, HashingEmbeddingProvider
from ncit_index.errors import (
    IndexBuildError,
    IndexCapabilityError,
    IndexCompatibilityError,
    IndexStorageError,
    NoActiveIndexError,
)
from ncit_index.index import SCHEMA_VERSION, LocalIndex
from ncit_index.storage import fts_table


def normalized(concepts, date=None):
    return (normalize_concept(raw, release_date=date, source="active_cache") for raw in concepts)


def update_manifest(index, manifest):
    with index._connect() as conn:
        conn.execute(
            "UPDATE manifests SET payload = ? WHERE build_id = ?",
            (json.dumps(manifest.to_dict()), manifest.build_id),
        )


def concept_search_text(concept):
    return concept.preferred_name


RAW_CONCEPTS = [
    {
        "code": "C40704",
        "name": "Receptor Tyrosine Kinase Inhibition",
        "terminology": "ncit",
        "version": "26.06e",
        "definitions": [{"definition": "Inhibition of receptor tyrosine kinase activity."}],
        "synonyms": [{"name": "RTK Inhibition", "source": "NCI"}],
        "properties": [{"type": "Semantic_Type", "value": "Molecular Function"}],
    },
    {
        "code": "C3262",
        "name": "Neoplasm",
        "terminology": "ncit",
        "version": "26.06e",
        "definitions": [{"definition": "A benign or malignant tissue growth."}],
        "synonyms": [{"name": "Tumor", "source": "NCI"}],
        "properties": [{"type": "Semantic_Type", "value": "Neoplastic Process"}],
    },
]
WORDS = [
    "alpha",
    "bravo",
    "carbon",
    "delta",
    "ember",
    "fjord",
    "garnet",
    "harbor",
    "indigo",
    "jasper",
]


def synthetic_concepts(count):
    """Concepts whose names are distinct three-word combinations."""

    return [
        concept(
            f"C{number}",
            " ".join(WORDS[(number // step) % 10] + str(step) for step in (1, 10, 100)),
        )
        for number in range(count)
    ]


class RenamedProvider(HashingEmbeddingProvider):
    def __init__(self, name="hashing", model="hashing-128", dimensions=128):
        super().__init__(dimensions)
        self.name = name
        self.model = model


class IndexTestCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name)
        self.provider = HashingEmbeddingProvider()

    def build(self, concepts=RAW_CONCEPTS, date="2026-06-29", provider=None, **options):
        index = LocalIndex(self.path, on_event=index_event)
        index.upsert_concepts(
            normalized(concepts, date), date, provider or self.provider, **options
        )
        return index

    def counts(self, index):
        with index._connect() as conn:
            active = index.get_active_manifest().build_id
            return tuple(
                conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE build_id = ?",  # noqa: S608
                    (active,),
                ).fetchone()[0]
                for table in ("concepts", "fields", "manifests")
            )


class UpsertTest(IndexTestCase):
    def test_upsert_activates_the_release_and_caches_concepts_with_provenance(self):
        index = LocalIndex(self.path)
        manifest = index.upsert_concepts(
            normalized(RAW_CONCEPTS, "2026-06-29"), "2026-06-29", self.provider
        )

        self.assertEqual(manifest.release_version, "26.06e")
        self.assertEqual(manifest.concept_count, 2)
        self.assertEqual(manifest.embedding_dimensions, 128)
        self.assertEqual(index.get_active_manifest(), manifest)
        self.assertEqual(self.counts(index), (2, 6, 1))

        cached = index.get_concept_snapshot("C3262")[1]
        self.assertEqual(cached.source, "active_cache")
        self.assertEqual(cached.release_version, "26.06e")
        self.assertEqual(cached.release_date, "2026-06-29")
        self.assertEqual(cached.to_stored()["raw"], RAW_CONCEPTS[1])
        self.assertIsNone(index.get_concept_snapshot("C999")[1])

    def test_cached_concept_keeps_the_time_it_was_fetched(self):
        index = self.build()

        first = index.get_concept_snapshot("C3262")[1].retrieved_at
        second = index.get_concept_snapshot("C3262")[1].retrieved_at

        self.assertEqual(first, second)
        self.assertEqual(index.search("tumor", self.provider)[0].concept.retrieved_at, first)

    def test_reindexing_a_code_replaces_its_rows(self):
        index = self.build()

        manifest = index.upsert_concepts(
            normalized(
                [dict(RAW_CONCEPTS[1], name="Renamed Growth", synonyms=[], definitions=[])],
                "2026-06-29",
            ),
            "2026-06-29",
            self.provider,
        )

        self.assertEqual(manifest.concept_count, 2)
        self.assertEqual(self.counts(index), (2, 4, 1))
        self.assertEqual(index.get_concept_snapshot("C3262")[1].preferred_name, "Renamed Growth")
        for mode in ("bm25", "vector"):
            hits = index.search("renamed", self.provider, mode=mode)
            self.assertEqual(hits[0].concept.code, "C3262", mode)
            self.assertEqual(hits[0].concept.preferred_name, "Renamed Growth")
        self.assertGreater(hits[0].score, hits[1].score)
        self.assertEqual(index.search("tumor", self.provider, mode="bm25"), [])

    def test_sample_does_not_overwrite_an_activation_during_embedding(self):
        index = self.build()
        other = index.build(
            normalized([dict(RAW_CONCEPTS[1], version="other")]), None, self.provider
        )
        embed = self.provider.embed

        def activate_during_embedding(texts):
            index.activate(other.build_id)
            return embed(texts)

        with (
            patch.object(self.provider, "embed", side_effect=activate_during_embedding),
            self.assertRaisesRegex(IndexBuildError, "active index changed"),
        ):
            index.upsert_concepts(normalized([RAW_CONCEPTS[0]]), None, self.provider)
        self.assertEqual(index.get_active_manifest().build_id, other.build_id)
        self.assertEqual(index.get_concept_snapshot("C3262")[1].release_version, "other")

    def test_indexing_another_release_replaces_the_previous_one(self):
        index = self.build()

        with self.assertLogs("nci_sip_mcp.index", level="INFO"):
            manifest = index.upsert_concepts(
                normalized([dict(RAW_CONCEPTS[1], version="26.07d")], "2026-07-27"),
                "2026-07-27",
                self.provider,
            )

        self.assertEqual(manifest.release_version, "26.07d")
        self.assertEqual(manifest.concept_count, 1)
        self.assertEqual(self.counts(index), (1, 3, 1))
        self.assertIsNone(index.get_concept_snapshot("C40704")[1])
        self.assertEqual(index.get_concept_snapshot("C3262")[1].release_version, "26.07d")
        self.assertEqual(index.search("kinase", self.provider, mode="bm25"), [])

    def test_release_mismatch_is_rejected_and_keeps_the_previous_release(self):
        index = self.build()
        before = index.get_active_manifest()

        with self.assertRaises(IndexCompatibilityError):
            index.upsert_concepts(
                normalized([dict(RAW_CONCEPTS[0], version="26.07a")], "2026-07-06"),
                "2026-07-06",
                self.provider,
                expected_release_version="26.07d",
            )

        self.assertEqual(index.get_active_manifest(), before)
        self.assertEqual(self.counts(index), (2, 6, 1))

    def test_unusable_batches_are_rejected(self):
        index = LocalIndex(self.path)
        batches = {
            "empty": [],
            "no version": [dict(RAW_CONCEPTS[0], version="")],
            "no code": [dict(RAW_CONCEPTS[0], code="")],
            "mixed versions": [RAW_CONCEPTS[0], dict(RAW_CONCEPTS[1], version="26.07a")],
        }
        for label, batch in batches.items():
            with self.subTest(label), self.assertRaises(IndexBuildError):
                index.upsert_concepts(normalized(batch, "2026-06-29"), "2026-06-29", self.provider)
        self.assertIsNone(index.get_active_manifest())

    def test_a_code_repeated_in_one_batch_is_indexed_once(self):
        index = LocalIndex(self.path)

        manifest = index.upsert_concepts(
            normalized([RAW_CONCEPTS[1], dict(RAW_CONCEPTS[1], name="Later Name")], "2026-06-29"),
            "2026-06-29",
            self.provider,
        )

        self.assertEqual(manifest.concept_count, 1)
        self.assertEqual(self.counts(index), (1, 3, 1))
        self.assertEqual(index.get_concept_snapshot("C3262")[1].preferred_name, "Later Name")

    def test_count_and_search_ignore_rows_of_an_inactive_release(self):
        index = self.build()
        index.build(
            normalized([concept("C9999", "Tumor Marker", version="26.99z")]), None, self.provider
        )
        self.assertEqual(index.get_active_manifest().concept_count, 2)
        self.assertIsNone(index.get_concept_snapshot("C9999")[1])
        for mode in ("bm25", "vector", "hybrid"):
            hits = index.search("tumor marker", self.provider, mode=mode)
            self.assertNotIn("C9999", [hit.concept.code for hit in hits])
            self.assertEqual({hit.concept.release_version for hit in hits}, {"26.06e"})

    def test_failed_write_rolls_back_every_table_and_keeps_the_previous_release(self):
        index = self.build()
        before = index.get_active_manifest()
        with index._connect() as conn:
            conn.execute(
                """
                CREATE TRIGGER fail_manifest BEFORE INSERT ON manifests
                BEGIN SELECT RAISE(ABORT, 'disk full'); END
                """
            )

        with self.assertRaises(sqlite3.IntegrityError):
            index.upsert_concepts(
                normalized([dict(RAW_CONCEPTS[0], version="26.07d")], "2026-07-27"),
                "2026-07-27",
                self.provider,
            )

        self.assertEqual(index.get_active_manifest(), before)
        self.assertEqual(self.counts(index), (2, 6, 1))
        self.assertEqual(index.get_concept_snapshot("C40704")[1].release_version, "26.06e")

    def test_a_different_embedding_space_cannot_join_the_active_release(self):
        providers = {
            "provider": RenamedProvider(name="other"),
            "model": RenamedProvider(model="hashing-v2"),
            "dimensions": RenamedProvider(dimensions=64),
        }
        for label, provider in providers.items():
            with self.subTest(label):
                index = self.build()
                before = index.get_active_manifest()
                with self.assertRaises(IndexCompatibilityError) as raised:
                    index.upsert_concepts(
                        normalized([RAW_CONCEPTS[0]], "2026-06-29"), "2026-06-29", provider
                    )
                self.assertIn("nci_si.sqlite3", str(raised.exception))
                self.assertEqual(index.get_active_manifest(), before)
                (self.path / "nci_si.sqlite3").unlink()

    def test_dimension_change_is_rejected_when_the_manifest_predates_dimensions(self):
        index = self.build()
        with index._connect() as conn:
            payload = json.loads(conn.execute("SELECT payload FROM manifests").fetchone()[0])
            del payload["embedding_dimensions"]
            conn.execute("UPDATE manifests SET payload = ?", (json.dumps(payload),))

        with self.assertRaises(IndexCompatibilityError):
            index.upsert_concepts(
                normalized([RAW_CONCEPTS[0]], "2026-06-29"),
                "2026-06-29",
                RenamedProvider(dimensions=64),
            )

    def test_provider_returning_wrong_vectors_is_rejected(self):
        class ShortProvider(HashingEmbeddingProvider):
            def embed(self, texts):
                return super().embed(texts)[:-1]

        class RaggedProvider(HashingEmbeddingProvider):
            def embed(self, texts):
                vectors = super().embed(texts)
                return [vectors[0][:-1], *vectors[1:]]

        for provider in (ShortProvider(), RaggedProvider()):
            with self.subTest(type(provider).__name__), self.assertRaises(IndexCompatibilityError):
                LocalIndex(self.path).upsert_concepts(
                    normalized(RAW_CONCEPTS, "2026-06-29"), "2026-06-29", provider
                )


class StorageTest(IndexTestCase):
    def test_new_files_use_large_pages_and_existing_files_keep_their_layout(self):
        index = self.build()
        with index._connect() as conn:
            self.assertEqual(conn.execute("PRAGMA page_size").fetchone()[0], 65536)
            conn.execute("PRAGMA journal_mode=DELETE")
            conn.execute("PRAGMA page_size=4096")
            conn.execute("VACUUM")
            conn.execute("PRAGMA journal_mode=WAL")
        reopened = LocalIndex(self.path)
        with reopened._connect() as conn:
            self.assertEqual(conn.execute("PRAGMA page_size").fetchone()[0], 4096)
        self.assertEqual(reopened.get_concept_snapshot("C3262")[1].preferred_name, "Neoplasm")
        self.assertEqual(reopened.search("Neoplasm", self.provider)[0].concept.code, "C3262")

    def test_unusable_database_is_a_storage_error_naming_the_file(self):
        index = self.build()
        index.db_path.write_bytes(b"not a database" * 100)

        for call in (index.get_active_manifest, lambda: LocalIndex(self.path)):
            with self.assertRaises(IndexStorageError) as raised:
                call()
            self.assertIn(str(index.db_path), str(raised.exception))

    def test_storage_failures_are_recognised_by_primary_code_whatever_the_sub_case(self):
        index = self.build()
        # An extended code carries its primary code in the low byte; the rest are primary codes.
        for code in (
            sqlite3.SQLITE_IOERR_READ,
            sqlite3.SQLITE_BUSY,
            sqlite3.SQLITE_LOCKED,
            sqlite3.SQLITE_CORRUPT,
            sqlite3.SQLITE_READONLY,
            sqlite3.SQLITE_FULL,
        ):
            with self.subTest(code=code):
                failure = sqlite3.OperationalError("storage trouble")
                failure.sqlite_errorcode = code
                with (
                    patch("ncit_index.index.sqlite3.connect", side_effect=failure),
                    self.assertRaises(IndexStorageError) as raised,
                ):
                    index.get_active_manifest()
                self.assertIn(str(index.db_path), str(raised.exception))

    def test_sql_usage_error_propagates_as_a_bug_not_a_storage_failure(self):
        index = self.build()

        with (
            self.assertRaises(sqlite3.OperationalError) as raised,
            index._connect() as conn,
        ):
            conn.execute("SELECT nope FROM concepts")

        self.assertNotIsInstance(raised.exception, IndexStorageError)
        self.assertIn("no such column: nope", str(raised.exception))

    def test_database_that_cannot_be_opened_is_a_storage_error_naming_the_file(self):
        (self.path / "nci_si.sqlite3").mkdir()

        with self.assertRaises(IndexStorageError) as raised:
            LocalIndex(self.path)

        self.assertIn(str(self.path / "nci_si.sqlite3"), str(raised.exception))

    def test_search_reads_one_snapshot_while_the_release_is_replaced(self):
        index = self.build()
        read_manifest = LocalIndex._active_manifest
        replaced = []

        def replace_release_then_read(conn):
            manifest = read_manifest(conn)
            if not replaced:
                replaced.append(True)
                LocalIndex(self.path).upsert_concepts(
                    normalized([concept("C9999", "Tumor Marker", version="26.07d")], "2026-07-27"),
                    "2026-07-27",
                    self.provider,
                )
            return manifest

        with patch.object(LocalIndex, "_active_manifest", staticmethod(replace_release_then_read)):
            hits = index.search("tumor", self.provider, mode="hybrid")

        self.assertEqual({hit.concept.release_version for hit in hits}, {"26.06e"})
        self.assertEqual(index.get_active_manifest().release_version, "26.07d")

    def test_connections_are_closed_after_use(self):
        index = self.build()

        with index._connect() as conn:
            conn.execute("SELECT 1")

        with self.assertRaises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")

    def test_reopening_preserves_the_index(self):
        index = self.build()
        manifest = index.get_active_manifest()

        reopened = LocalIndex(self.path)

        self.assertEqual(reopened.get_active_manifest(), manifest)
        self.assertEqual(self.counts(reopened), (2, 6, 1))
        self.assertEqual(
            reopened.search("tumor", self.provider, mode="bm25")[0].concept.code, "C3262"
        )

    def test_opening_a_current_database_does_not_need_the_write_lock(self):
        self.build()
        writer = sqlite3.connect(str(self.path / "nci_si.sqlite3"), timeout=0)
        self.addCleanup(writer.close)
        writer.execute("BEGIN IMMEDIATE")

        connect = sqlite3.connect
        with patch("ncit_index.index.sqlite3.connect", lambda path: connect(path, timeout=0)):
            manifest = LocalIndex(self.path).get_active_manifest()

        self.assertEqual(manifest.release_version, "26.06e")

    def test_newer_schema_is_refused(self):
        index = self.build()
        with index._connect() as conn:
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")

        with self.assertRaises(IndexCompatibilityError):
            LocalIndex(self.path)

    def test_manifest_with_unknown_keys_still_loads(self):
        index = self.build()
        with index._connect() as conn:
            payload = json.loads(conn.execute("SELECT payload FROM manifests").fetchone()[0])
            payload["added_by_a_later_version"] = 1
            conn.execute("UPDATE manifests SET payload = ?", (json.dumps(payload),))

        self.assertEqual(index.get_active_manifest().release_version, "26.06e")


class MigrationTest(IndexTestCase):
    def test_legacy_migration_preserves_explicit_embedding_dimensions(self):
        self.legacy_database([("26.06e", RAW_CONCEPTS[0])])
        with closing(sqlite3.connect(str(self.path / "nci_si.sqlite3"))) as conn, conn:
            payload = json.loads(conn.execute("SELECT payload FROM manifests").fetchone()[0])
            # Migration retains the recorded identity; only a rebuild reconciles old vectors.
            payload["embedding_dimensions"] = 768
            conn.execute("UPDATE manifests SET payload = ?", (json.dumps(payload),))
        index = LocalIndex(self.path)
        manifest = index.get_active_manifest()
        self.assertEqual(manifest.embedding_dimensions, 768)
        self.assertTrue(manifest.needs_rebuild)
        self.assertEqual(index.get_concept_snapshot("C40704")[1].raw, RAW_CONCEPTS[0])

    def test_a_second_opener_preserves_the_migration_completed_before_it_gets_the_lock(self):
        self.legacy_database([("26.06e", RAW_CONCEPTS[0])])
        connect, directory, completed = sqlite3.connect, self.path, []

        class RacingConnection(sqlite3.Connection):
            def execute(self, statement, *parameters):
                if statement == "BEGIN IMMEDIATE":
                    with patch("ncit_index.index.sqlite3.connect", connect):
                        completed.append(LocalIndex(directory).get_active_manifest())
                return super().execute(statement, *parameters)

        with patch(
            "ncit_index.index.sqlite3.connect",
            lambda path: connect(path, factory=RacingConnection),
        ):
            index = LocalIndex(directory)

        self.assertEqual(index.get_active_manifest(), completed[0])
        self.assertEqual(index.get_concept_snapshot("C40704")[1].raw, RAW_CONCEPTS[0])
        self.assertEqual(len(index.list_builds()), 1)

    def legacy_database(self, releases):
        """Write a database in the layout of the first prototype (schema 0)."""

        db_path = self.path / "nci_si.sqlite3"
        vectors = {}
        with sqlite3.connect(str(db_path)) as conn:
            conn.execute(
                "CREATE TABLE manifests "
                "(release_version TEXT PRIMARY KEY, payload TEXT, active INTEGER)"
            )
            conn.execute(
                """
                CREATE TABLE concepts (
                    release_version TEXT, code TEXT, payload TEXT, search_text TEXT, vector TEXT,
                    PRIMARY KEY (release_version, code)
                )
                """
            )
            for release_version, raw in releases:
                item = normalize_concept(
                    dict(raw, version=release_version), release_date=None, source="active_cache"
                )
                search_text = concept_search_text(item)
                vectors[release_version] = self.provider.embed([search_text])[0]
                manifest = {
                    "terminology": "ncit",
                    "release_version": release_version,
                    "release_date": None,
                    "embedding_provider": self.provider.name,
                    "embedding_model": self.provider.model,
                    "concept_count": 1,
                    "built_at": "2026-07-10T00:00:00Z",
                    "index_path": str(db_path),
                    "active": True,
                }
                conn.execute(
                    "INSERT INTO manifests VALUES (?, ?, 1)",
                    (release_version, json.dumps(manifest)),
                )
                conn.execute(
                    "INSERT INTO concepts VALUES (?, ?, ?, ?, ?)",
                    (
                        release_version,
                        item.code,
                        json.dumps(item.to_stored()),
                        search_text,
                        json.dumps(vectors[release_version]),
                    ),
                )
        conn.close()
        return vectors

    def test_legacy_database_preserves_payloads_and_requires_explicit_rebuild(self):
        self.legacy_database([("26.06e", RAW_CONCEPTS[0])])
        index = LocalIndex(self.path)
        manifest = index.get_active_manifest()
        self.assertTrue(manifest.needs_rebuild)
        self.assertEqual(index.get_concept_snapshot("C40704")[1].raw, RAW_CONCEPTS[0])
        for mode in ("bm25", "vector", "hybrid"):
            with self.subTest(mode=mode), self.assertRaises(IndexCapabilityError) as raised:
                index.search("kinase", self.provider, mode=mode)
            self.assertIn("index-rebuild", str(raised.exception))
        rebuilt = index.rebuild(manifest.build_id, self.provider)
        self.assertEqual(index.get_active_manifest(), manifest)
        with self.assertRaisesRegex(IndexBuildError, "unclassified"):
            index.activate(rebuilt.build_id)
        dataset = EvaluationSet(
            "test-only-legacy-rebuild",
            (GoldQuery(RAW_CONCEPTS[0]["name"], ("C40704",)),),
            MetricFloor(1, 1),
            MetricFloor(1, 1),
            self.provider.name,
            self.provider.model,
            128,
            "26.06e",
            True,
        )
        evaluate_build(index, self.provider, dataset, rebuilt.build_id)
        index.activate(rebuilt.build_id)
        hit = index.search("kinase", self.provider)[0]
        self.assertEqual(hit.concept.code, "C40704")
        self.assertIn(hit.matched_on, ("name", "definition"))

    def test_several_legacy_active_manifests_keep_data_with_one_active(self):
        self.legacy_database([("26.05d", RAW_CONCEPTS[0]), ("26.06e", RAW_CONCEPTS[1])])
        index = LocalIndex(self.path)
        self.assertEqual(index.get_active_manifest().release_version, "26.06e")
        builds = index.list_builds()
        self.assertEqual({item.release_version for item in builds}, {"26.05d", "26.06e"})
        older = next(item for item in builds if item.release_version == "26.05d")
        index.activate(older.build_id)
        self.assertEqual(index.get_concept_snapshot("C40704")[1].release_version, "26.05d")
        self.assertIsNone(index.get_concept_snapshot("C3262")[1])


class SearchTest(IndexTestCase):
    def test_hybrid_search_ranks_and_reports_score_components(self):
        index = self.build()

        hits = index.search("kinase inhibition", self.provider, limit=2, mode="hybrid")

        self.assertEqual([hit.concept.code for hit in hits], ["C40704", "C3262"])
        self.assertEqual([hit.rank for hit in hits], [1, 2])
        self.assertEqual(hits[0].score_components, {"bm25": 1.0, "vector": 1.0})
        self.assertEqual(hits[0].concept.source, "active_cache")

    def test_a_limit_reports_the_scored_concepts_it_left_out_exactly_in_a_small_index(self):
        index = self.build(synthetic_concepts(30))

        hits, total, _ = LocalIndexAdapter(index).search_page(
            "alpha1", self.provider, limit=10, mode="hybrid"
        )
        truncation = results_truncation(total, 10)

        self.assertEqual(len(hits), 10)
        self.assertEqual(
            truncation.to_dict(),
            {
                "occurred": True,
                "bound": "results",
                "limit": 10,
                "reached": 10,
                "omitted": 20,
                "exact": True,
            },
        )
        _, total, _ = LocalIndexAdapter(index).search_page(
            "alpha1", self.provider, limit=30, mode="hybrid"
        )
        everything = results_truncation(total, 30)
        self.assertEqual(everything.to_dict(), {"occurred": False})

    def test_concept_without_a_matching_term_has_a_zero_bm25_component(self):
        index = self.build()

        hits = index.search("kinase inhibition", self.provider, mode="hybrid")

        self.assertEqual([hit.concept.code for hit in hits], ["C40704", "C3262"])
        self.assertEqual(hits[1].score_components, {"bm25": 0.0, "vector": 0.0})
        self.assertEqual((hits[0].score, hits[1].score), (1.0, 0.0))

    def test_hybrid_score_weighs_bm25_and_vector(self):
        index = self.build(synthetic_concepts(30))

        hits = index.search("alpha1 bravo10 alpha100", self.provider, limit=30, mode="hybrid")

        self.assertEqual(len(hits), 30)
        for hit in hits:
            components = hit.score_components
            self.assertAlmostEqual(
                hit.score, 0.55 * components["bm25"] + 0.45 * components["vector"]
            )
        self.assertTrue(
            any(h.score_components["bm25"] != h.score_components["vector"] for h in hits)
        )
        self.assertEqual(
            [hit.score for hit in hits], sorted((hit.score for hit in hits), reverse=True)
        )

    def test_search_without_an_index_is_refused(self):
        with self.assertRaises(NoActiveIndexError):
            LocalIndex(self.path).search("tumor", self.provider)

    def test_search_validates_its_arguments(self):
        index = self.build()
        for arguments in ((" ", 10, "hybrid"), ("tumor", 0, "hybrid"), ("tumor", 10, "fuzzy")):
            with self.subTest(arguments=arguments), self.assertRaises(InputValidationError):
                LocalIndexAdapter(index).search_page(
                    arguments[0], self.provider, limit=arguments[1], mode=arguments[2]
                )

    def test_search_rejects_a_runtime_from_another_embedding_space(self):
        index = self.build()
        providers = {
            "provider": RenamedProvider(name="other"),
            "model": RenamedProvider(model="hashing-v2"),
            "dimensions": RenamedProvider(dimensions=64),
        }
        for label, provider in providers.items():
            with self.subTest(label), self.assertRaises(IndexCompatibilityError):
                index.search("tumor", provider, mode="vector")

    def test_bm25_search_also_refuses_another_embedding_space(self):
        index = self.build()

        with self.assertRaises(IndexCompatibilityError):
            index.search("tumor", RenamedProvider(model="hashing-v2"), mode="bm25")

    def test_query_vector_count_is_checked(self):
        class TwoVectors(RenamedProvider):
            def embed(self, texts):
                return super().embed(texts) * 2

        index = self.build()

        with self.assertRaises(IndexCompatibilityError):
            index.search("tumor", TwoVectors(), mode="vector")

    def test_stored_vectors_of_another_width_are_detected_on_a_legacy_manifest(self):
        index = self.build()
        with index._connect() as conn:
            payload = json.loads(conn.execute("SELECT payload FROM manifests").fetchone()[0])
            del payload["embedding_dimensions"]
            conn.execute("UPDATE manifests SET payload = ?", (json.dumps(payload),))

        with self.assertRaises(IndexStorageError):
            index.search("tumor", RenamedProvider(dimensions=64), mode="vector")

    def test_bm25_mode_does_not_compute_embeddings(self):
        class NoEmbedProvider(EmbeddingProvider):
            name = "hashing"
            model = "hashing-128"

            def embed(self, texts):
                raise AssertionError("BM25-only search must not embed the query")

        index = self.build()

        hits = index.search("tumor", NoEmbedProvider(), mode="bm25")

        self.assertEqual(hits[0].concept.code, "C3262")
        self.assertEqual(hits[0].score_components["vector"], 0.0)

    def test_bm25_matches_names_with_diacritics(self):
        index = self.build([concept("C26883", "Sjögren Syndrome"), concept("C3262", "Neoplasm")])

        for query in ("Sjögren", "sjogren", "SJÖGREN syndrome"):
            with self.subTest(query):
                hits = index.search(query, self.provider, mode="bm25")
                self.assertEqual(hits[0].concept.code, "C26883")

    def test_fts_syntax_in_a_query_is_inert(self):
        index = self.build()

        for query in ('tumor" OR (', "NEAR(a b)", "C3262*", "non-small", "tumor AND NOT growth"):
            with self.subTest(query):
                index.search(query, self.provider, mode="bm25")
        self.assertEqual(index.search("!!!", self.provider, mode="bm25"), [])

    def test_vector_search_finds_preferred_names_across_the_index(self):
        concepts = synthetic_concepts(200)
        index = self.build(concepts)

        for raw in concepts[::10]:
            hits = index.search(raw["name"], self.provider, limit=5, mode="vector")
            self.assertEqual(len(hits), 5)
            self.assertEqual(hits[0].concept.code, raw["code"], raw["name"])

    def test_hybrid_search_fills_the_limit_from_the_whole_small_index(self):
        index = self.build(synthetic_concepts(30))

        hits = index.search("alpha1 alpha10 alpha100", self.provider, limit=20, mode="hybrid")

        self.assertEqual(len(hits), 20)
        self.assertEqual(hits[0].concept.code, "C0")

    def test_large_release_gives_bm25_candidates_a_vector_score(self):
        concepts = synthetic_concepts(60)
        index = self.build(concepts)

        own_text = concept_search_text(
            normalize_concept(concepts[7], release_date=None, source="active_cache")
        )
        vector_hits = index.search(own_text, self.provider, limit=60, mode="vector")
        hybrid_hits = index.search("garnet1", self.provider, limit=60, mode="hybrid")

        self.assertEqual(vector_hits[0].concept.code, "C7")
        bm25_matches = {f"C{number}" for number in range(6, 60, 10)}
        by_code = {hit.concept.code: hit for hit in hybrid_hits}
        self.assertTrue(bm25_matches <= set(by_code))
        # A BM25 candidate is given a vector score too, not only its BM25 score.
        self.assertTrue(any(by_code[code].score_components["vector"] > 0 for code in bm25_matches))

    def test_search_only_returns_the_active_release(self):
        index = self.build()
        index.upsert_concepts(
            normalized([dict(RAW_CONCEPTS[1], version="26.07d")], "2026-07-27"),
            "2026-07-27",
            self.provider,
        )

        for mode in ("bm25", "vector", "hybrid"):
            with self.subTest(mode):
                hits = index.search("kinase tumor", self.provider, mode=mode)
                self.assertEqual({hit.concept.release_version for hit in hits}, {"26.07d"})

    def test_bm25_scores_of_the_top_hits_do_not_depend_on_a_small_limit(self):
        # Thirty concepts match "kinase", each with its own document length and so its own score.
        concepts = [
            concept(f"C{number}", "kinase " + " ".join(f"filler{n}" for n in range(number)))
            for number in range(1, 31)
        ]
        index = self.build(concepts)

        every = index.search("kinase", self.provider, limit=30, mode="bm25")
        few = index.search("kinase", self.provider, limit=2, mode="bm25")

        self.assertEqual(len({hit.score for hit in every}), 30)
        self.assertEqual(
            [(hit.concept.code, hit.score) for hit in few],
            [(hit.concept.code, hit.score) for hit in every[:2]],
        )


class ManifestIntegrityTest(IndexTestCase):
    def test_sample_needing_rebuild_cannot_be_updated(self):
        index = self.build([concept("C1")])
        active = replace(index.get_active_manifest(), needs_rebuild=True)
        update_manifest(index, active)
        with self.assertRaisesRegex(IndexCapabilityError, "index-rebuild"):
            index.upsert_concepts(normalized([concept("C2")]), None, self.provider)
        self.assertEqual(index.get_active_manifest(), active)
        self.assertIsNone(index.get_concept_snapshot("C2")[1])

    def test_missing_dimensions_are_derived_for_sample_compatibility(self):
        index = self.build([concept("C1", "Primary name", synonyms=[{"name": "Distinct alias"}])])
        active = replace(index.get_active_manifest(), embedding_dimensions=None)
        update_manifest(index, active)
        updated = index.upsert_concepts(normalized([concept("C2")]), None, self.provider)
        self.assertEqual(updated.embedding_dimensions, self.provider.dimensions)
        self.assertIsNotNone(index.get_concept_snapshot("C2")[1])

    def test_declared_dimensions_are_not_replaced_by_stored_vector_width(self):
        index = self.build([concept("C1")])
        active = replace(index.get_active_manifest(), embedding_dimensions=7)
        update_manifest(index, active)
        with self.assertRaises(IndexCompatibilityError):
            index.upsert_concepts(normalized([concept("C2")]), None, self.provider)
        self.assertEqual(index.get_active_manifest(), active)

    def test_build_identifier_cannot_form_an_arbitrary_sql_identifier(self):
        for value in ("x" * 32, "a" * 31, "a" * 32 + "; DROP TABLE concepts"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "identifier"):
                fts_table(value)

    def test_building_manifest_cannot_be_active(self):
        index = self.build([concept("C1")])
        with index._connect() as conn, self.assertRaises(sqlite3.IntegrityError):
            conn.execute("UPDATE manifests SET state = 'building' WHERE active = 1")
        self.assertIsNotNone(index.get_active_manifest())

    def test_only_one_manifest_can_be_active(self):
        index = self.build([concept("C1")])
        candidate = index.build(normalized([concept("C2")]), None, self.provider)
        with index._connect() as conn, self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE manifests SET active = 1 WHERE build_id = ?", (candidate.build_id,)
            )
        self.assertEqual(sum(item.active for item in index.list_builds()), 1)


class LegacyMigrationIntegrityTest(IndexTestCase):
    legacy_database = MigrationTest.legacy_database

    def test_empty_legacy_manifest_without_dimensions_still_blocks_sample_updates(self):
        self.legacy_database([("26.06e", concept("C1"))])
        with closing(sqlite3.connect(self.path / "nci_si.sqlite3")) as conn, conn:
            conn.execute("DELETE FROM concepts")
            conn.execute("UPDATE manifests SET payload = json_set(payload, '$.concept_count', 0)")
        index = LocalIndex(self.path)
        active = index.get_active_manifest()
        self.assertIsNone(active.embedding_dimensions)
        with self.assertRaises(IndexBuildError) as raised:
            index.upsert_concepts(normalized([concept("C2")]), None, self.provider)
        self.assertEqual(index.list_builds(), [active])
        self.assertIsNone(index.get_concept_snapshot("C2")[1])
        self.assertIn("separate data directory", str(raised.exception))

    def test_legacy_migration_derives_dimensions_and_retains_status(self):
        self.legacy_database([("26.06e", concept("C1", conceptStatus="Retired_Concept"))])
        index = LocalIndex(self.path)
        self.assertEqual(index.get_active_manifest().embedding_dimensions, self.provider.dimensions)
        with index._connect() as conn:
            self.assertEqual(
                conn.execute("SELECT status FROM concepts").fetchone()[0], "Retired_Concept"
            )

    def test_orphan_legacy_concept_aborts_migration_without_losing_it(self):
        self.legacy_database([("26.06e", concept("C1"))])
        with closing(sqlite3.connect(self.path / "nci_si.sqlite3")) as conn, conn:
            conn.execute("UPDATE concepts SET release_version = 'orphan'")
        with self.assertRaisesRegex(IndexCompatibilityError, "without a manifest"):
            LocalIndex(self.path)
        with closing(sqlite3.connect(self.path / "nci_si.sqlite3")) as conn:
            self.assertEqual(
                conn.execute("SELECT release_version FROM concepts").fetchone()[0], "orphan"
            )


if __name__ == "__main__":
    unittest.main()
