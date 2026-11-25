"""Existing builds remain searchable after the vector storage migration."""

import json
import sqlite3
import struct
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from fakes import concept, schema_five
from nci_sip_mcp.evs import normalize_concept
from nci_sip_mcp.models import IndexManifest
from ncit_index.embeddings import HashingEmbeddingProvider, SentenceTransformersProvider
from ncit_index.errors import IndexCompatibilityError, IndexStorageError
from ncit_index.index import LocalIndex
from ncit_index.storage import (
    SCHEMA_VERSION,
    concept_fields,
    create_fts,
    fts_table,
    vector_bytes,
)
from test_index import IndexTestCase, normalized


class Float32MigrationTest(IndexTestCase):
    def old_build(self, conn, build, rows, active):
        manifest = IndexManifest(
            "ncit",
            "26.06e",
            None,
            self.provider.name,
            self.provider.model,
            len(rows),
            "2026-10-05T00:00:00Z",
            str(self.path / "nci_si.sqlite3"),
            self.provider.dimensions,
            active,
            build,
        )
        conn.execute(
            "INSERT INTO manifests VALUES (?, ?, ?, 'complete')",
            (build, json.dumps(manifest.to_dict()), int(active)),
        )
        create_fts(conn, build)
        for raw in rows:
            self.old_concept(conn, build, raw)
        return manifest

    def old_concept(self, conn, build, raw):
        item = normalize_concept(raw, release_date=None, source="active_cache")
        conn.execute(
            "INSERT INTO concepts VALUES (?, ?, ?, ?)",
            (build, item.code, json.dumps(item.to_stored()), item.preferred_name.lower()),
        )
        for kind, text in concept_fields(item):
            vector = self.provider.embed([text])[0]
            field = conn.execute(
                "INSERT INTO fields(build_id, code, kind, text, vector) VALUES (?, ?, ?, ?, ?)",
                (build, item.code, kind, text, json.dumps(vector)),
            )
            conn.execute(
                f"INSERT INTO {fts_table(build)}(rowid, code, kind, text) VALUES (?, ?, ?, ?)",  # noqa: S608
                (field.lastrowid, item.code, kind, text),
            )

    def test_schema_five_keeps_both_builds_status_fts_and_rankings(self):
        rows = [
            concept("C1", "Alpha", active=False, conceptStatus="retired-test"),
            concept("C2", "Beta", active=True, synonyms=[{"name": "Kinase"}]),
        ]
        with closing(sqlite3.connect(self.path / "nci_si.sqlite3")) as conn, conn:
            schema_five(conn)
            active = self.old_build(conn, "a" * 32, rows, True)
            older = self.old_build(conn, "b" * 32, [concept("C3", "Older", active=True)], False)
        index = LocalIndex(self.path)
        self.assertEqual(index.get_active_manifest().to_dict(), active.to_dict())
        self.assertEqual(
            {m.build_id for m in index.list_builds()}, {active.build_id, older.build_id}
        )
        self.assertEqual(index.get_concept_snapshot("C1")[1].raw, rows[0])
        for mode in ("bm25", "vector", "hybrid"):
            with self.subTest(mode=mode):
                hits = index.search("Kinase", self.provider, mode=mode)
                self.assertEqual((hits[0].concept.code, hits[0].matched_on), ("C2", "synonym"))
        retired, total, _ = index.search_page(
            "Kinase", self.provider, retired_status="retired-test"
        )
        self.assertEqual(([h.concept.code for h in retired], total), (["C1"], 1))
        with index._connect() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
            self.assertIsNone(
                conn.execute("SELECT name FROM sqlite_master WHERE name = 'vector_lsh'").fetchone()
            )
        index.activate(older.build_id)
        self.assertEqual(index.search("Older", self.provider)[0].concept.code, "C3")


class VectorIntegrityTest(IndexTestCase):
    def test_cosine_ranking_ignores_unequal_stored_vector_magnitudes(self):
        provider = HashingEmbeddingProvider(dimensions=2)
        values = {"Alpha": [1.0, 0.0], "Beta": [10.0, 10.0], "query": [1.0, 0.0]}
        with patch.object(
            provider, "embed", side_effect=lambda texts: [values[text] for text in texts]
        ):
            index = LocalIndex(self.path)
            index.upsert_concepts(
                normalized([concept("C1", "Alpha"), concept("C2", "Beta")]), None, provider
            )
            hits = index.search("query", provider, mode="vector")
        self.assertEqual([hit.concept.code for hit in hits], ["C1", "C2"])
        self.assertGreater(hits[0].score, hits[1].score)

    def test_unstorable_embeddings_cannot_replace_the_active_build(self):
        index = self.build()
        active = index.get_active_manifest()
        for value in (float("nan"), 1e39):
            with (
                self.subTest(value=value),
                patch.object(
                    self.provider,
                    "embed",
                    side_effect=lambda texts, value=value: [[value] * 128 for _ in texts],
                ),
                self.assertRaises(IndexCompatibilityError),
            ):
                index.upsert_concepts(normalized([concept("C99", "New")]), None, self.provider)
            self.assertEqual(index.get_active_manifest(), active)
            self.assertIsNone(index.get_concept_snapshot("C99")[1])

    def test_vector_page_offset_reaches_the_last_concept(self):
        provider = HashingEmbeddingProvider(dimensions=2)
        with patch.object(provider, "embed", side_effect=lambda texts: [[0.0, 1.0] for _ in texts]):
            index = LocalIndex(self.path)
            index.upsert_concepts(
                normalized([concept(f"C{i:05}", "Other") for i in range(300)]),
                None,
                provider,
            )
        with patch.object(provider, "embed", return_value=[[1.0, 0.0]]):
            hits, total, _ = index.search_page("query", provider, mode="vector", offset=299)
        self.assertEqual(total, 300)
        self.assertEqual([hit.concept.code for hit in hits], ["C00299"])

    def test_malformed_or_nonfinite_vector_blobs_fail_without_a_ranking(self):
        index = self.build()
        with index._connect() as conn:
            row = conn.execute("SELECT code, vector FROM concept_vectors LIMIT 1").fetchone()
        for blob in (row[1][:-1], struct.pack("<f", float("nan")) + row[1][4:]):
            with self.subTest(size=len(blob)):
                with index._connect() as conn:
                    conn.execute(
                        "UPDATE concept_vectors SET vector = ? WHERE code = ?", (blob, row[0])
                    )
                with self.assertRaises(IndexStorageError):
                    index.search("kinase", self.provider)

    def test_nonfinite_query_embedding_is_rejected(self):
        index = self.build()
        with (
            patch.object(self.provider, "embed", return_value=[[float("inf")] * 128]),
            self.assertRaises(IndexCompatibilityError),
        ):
            index.search("kinase", self.provider)

    def test_float64_provider_rounds_consistently_before_ranking(self):
        provider = HashingEmbeddingProvider(dimensions=2)
        values = {"Alpha": [1.0, 2.0 + 1e-8], "Beta": [1.0, 2.0], "query": [1.0, 0.0]}
        with patch.object(
            provider, "embed", side_effect=lambda texts: [values[text] for text in texts]
        ):
            index = LocalIndex(self.path)
            index.upsert_concepts(
                normalized([concept("C1", "Alpha"), concept("C2", "Beta")]), None, provider
            )
            hits = index.search("query", provider, mode="vector")
        self.assertEqual([hit.concept.code for hit in hits], ["C1", "C2"])
        self.assertEqual(hits[0].score, hits[1].score)


class ArrayVectorTest(IndexTestCase):
    """Model vectors reach storage as float32 arrays, byte-identical to the float-list path."""

    def test_sentence_transformer_rows_are_float32_arrays_that_store_as_their_floats(self):
        encoded = np.array([[0.25, -0.5, 1.0], [0.125, 0.0, -2.0]], dtype=np.float32)
        provider = SentenceTransformersProvider.__new__(SentenceTransformersProvider)
        provider._model = SimpleNamespace(encode=lambda texts, **options: encoded)

        rows = provider.embed(["a", "b"])

        self.assertEqual([row.dtype for row in rows], [np.dtype("<f4")] * 2)
        self.assertEqual(vector_bytes(rows[0]), struct.pack("<3f", 0.25, -0.5, 1.0))
        self.assertEqual(vector_bytes(rows[1]), vector_bytes([0.125, 0.0, -2.0]))

    def test_a_float64_array_rounds_to_the_same_bytes_as_a_float_list(self):
        values = [0.1, -2.5, 1e-3 + 1e-12]
        self.assertEqual(vector_bytes(np.array(values)), vector_bytes(values))

    def test_array_values_float32_cannot_store_are_rejected_as_incompatible(self):
        cases = {
            "nan": ([np.nan], "non-finite values"),
            "inf": ([np.inf], "non-finite values"),
            "overflow": ([1e300], "cannot be stored as float32"),
        }
        for label, (values, reason) in cases.items():
            with self.subTest(label), self.assertRaises(IndexCompatibilityError) as raised:
                vector_bytes(np.array(values))
            self.assertIn(reason, str(raised.exception))


class BinaryVectorIntegrityTest(IndexTestCase):
    def test_nonbinary_vector_kind_storage_is_reported_as_database_corruption(self):
        index = self.build([concept("C1", "Alpha")])
        for kinds in ("x", 1):
            with self.subTest(kinds=kinds):
                with index._connect() as conn:
                    conn.execute("UPDATE concept_vectors SET kinds = ?", (kinds,))
                with self.assertRaisesRegex(IndexStorageError, "Stored vector length"):
                    index.search("Alpha", self.provider, mode="vector")

    def test_nonbinary_vectors_are_reported_as_database_corruption(self):
        index = self.build([concept("C1", "Alpha")])
        # Matching the expected byte length isolates the type guard from the size guard.
        for vector in ("x" * (self.provider.dimensions * 4), 1):
            with self.subTest(vector_type=type(vector).__name__):
                with index._connect() as conn:
                    conn.execute("UPDATE concept_vectors SET vector = ?", (vector,))
                with self.assertRaisesRegex(IndexStorageError, "Stored vector length"):
                    index.search("Alpha", self.provider, mode="vector")
