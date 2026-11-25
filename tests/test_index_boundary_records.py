"""Caller-visible records crossing the read and local-operator boundaries."""

import logging
from unittest.mock import patch

from fakes import captured, concept, records, release
from nci_sip_mcp.errors import InputValidationError
from nci_sip_mcp.evaluation import GoldQuery, evaluate_build, evaluate_retrieval
from nci_sip_mcp.http_client import UpstreamUnavailableError
from nci_sip_mcp.indexing import full_build, open_index
from nci_sip_mcp.registry import invoke
from test_index import normalized
from test_indexing import fixture_calibration
from test_server import ServerFixture


class IndexBoundaryRecordsTest(ServerFixture):
    def test_search_preserves_name_and_definition_scores(self):
        rows = [
            concept("C1", "Neoplasm"),
            concept("C2", "Other", definitions=[{"definition": "Neoplasm"}]),
        ]
        self.evs.concepts = {row["code"]: row for row in rows}
        invoke(self.context, "index_codes", ["C1", "C2"])
        result = invoke(self.context, "search", query="Neoplasm", mode="bm25")
        self.assertEqual(
            [
                (hit["concept"]["code"], hit["matched_on"], hit["score_components"])
                for hit in result["hits"]
            ],
            [
                ("C1", "name", {"bm25": 1.0, "vector": 0.0}),
                ("C2", "definition", {"bm25": 1.0, "vector": 0.0}),
            ],
        )

    def test_sample_replacement_and_operator_activation_emit_info_records(self):
        with captured(), self.assertLogs("nci_sip_mcp.index", level="INFO") as logs:
            invoke(self.context, "index_codes", ["C3262"])
            first = self.local_index.get_active_manifest()
            self.evs.release = release("26.07d", "2026-07-27")
            self.evs.concepts = {"C3262": concept("C3262", version="26.07d")}
            invoke(self.context, "index_codes", ["C3262"])
            second = self.local_index.get_active_manifest()
            restored = invoke(self.context, "index_activate", build_id=first.build_id)
        self.assertEqual(restored["buildId"], first.build_id)
        self.assertEqual(
            [
                (
                    row.levelno,
                    row.structured["event"],
                    row.structured["build_id"],
                    row.structured["release"],
                )
                for row in logs.records
            ],
            [
                (logging.INFO, "index_activated", first.build_id, "26.06e"),
                (logging.INFO, "index_activated", second.build_id, "26.07d"),
                (logging.INFO, "index_activated", first.build_id, "26.06e"),
            ],
        )

    def assert_cached_date(self):
        self.evs.errors["get_concept"] = UpstreamUnavailableError("offline")
        lookup = invoke(self.context, "lookup", code="C3262")
        search = invoke(self.context, "search", query="Neoplasm", mode="bm25")
        expected = {"terminology": "ncit", "identifier": "26.06e", "date": "2026-06-29"}
        self.assertEqual(lookup["provenance"]["release"], expected)
        self.assertEqual(search["hits"][0]["concept"]["provenance"]["release"], expected)

    def test_sample_cached_lookup_and_search_keep_recorded_release_date(self):
        invoke(self.context, "index_codes", ["C3262"])
        self.assert_cached_date()

    def test_full_build_cached_lookup_and_search_keep_recorded_release_date(self):
        row = concept("C3262", "Neoplasm")
        with patch.object(self.evs, "get_index_page", return_value=(1, [row]), create=True):
            built = full_build(self.context, release())
        evaluate_build(
            self.local_index,
            self.context.embedding_provider,
            fixture_calibration("C3262", "Neoplasm"),
            built.build_id,
        )
        invoke(self.context, "index_activate", build_id=built.build_id)
        self.assert_cached_date()

    def test_embedding_progress_counts_every_sixteenth_batch_including_partial(self):
        for count, reported in ((960, []), (1023, [1023]), (1025, [1024])):
            with self.subTest(count=count), captured() as stream:
                manifest = open_index(self.settings.data_dir).build(
                    normalized(concept(f"C{i}") for i in range(1, count + 1)),
                    None,
                    self.context.embedding_provider,
                )
            progress = [
                {key: row[key] for key in ("build_id", "concepts", "batches")}
                for row in records(stream, "index_embedding_progress")
            ]
            expected = [
                {"build_id": manifest.build_id, "concepts": concepts, "batches": 16}
                for concepts in reported
            ]
            self.assertEqual(progress, expected)

    def test_evaluation_accepts_index_limit_not_cli_limit(self):
        invoke(self.context, "index_codes", ["C3262"])
        index = open_index(self.settings.data_dir)
        queries = [GoldQuery("Neoplasm", ("C3262",))]
        result = evaluate_retrieval(
            index, self.context.embedding_provider, queries, modes=["bm25"], limit=1000
        )
        self.assertEqual(result[0].per_query[0].ranked_codes, ("C3262",))
        with self.assertRaises(InputValidationError) as caught:
            evaluate_retrieval(
                index, self.context.embedding_provider, queries, modes=["bm25"], limit=1001
            )
        message = "Search limit must be between 1 and 1000"
        self.assertEqual(str(caught.exception), message)
        self.assertEqual(caught.exception.details, {"parameter": "limit", "reason": message})
