"""Snapshot lifecycle and field attribution are caller-visible index guarantees."""

import sqlite3
from unittest.mock import patch

from fakes import concept
from nci_sip_mcp.errors import correlated
from nci_sip_mcp.evs import normalize_concept
from nci_sip_mcp.index_adapter import LocalIndexAdapter
from ncit_index import index as index_module
from ncit_index.embeddings import HashingEmbeddingProvider
from ncit_index.errors import (
    IndexBuildError,
    IndexCompatibilityError,
    IndexCursorError,
    IndexStateError,
    IndexStorageError,
)
from ncit_index.index import LocalIndex
from ncit_index.storage import clean_stale_builds, concept_fields, fts_table, name_key
from test_index import RAW_CONCEPTS, IndexTestCase, normalized


class BuildLifecycleTest(IndexTestCase):
    def test_candidate_search_does_not_change_active_build_or_rollback_target(self):
        index = self.build()
        active = index.get_active_manifest()
        candidate = index.build(
            normalized([dict(RAW_CONCEPTS[0], code="C999")]), None, self.provider
        )
        before = index.list_builds()
        hits, manifest = index.search_build(candidate.build_id, "neoplasm", self.provider)
        self.assertEqual([hit.concept.code for hit in hits], ["C999"])
        self.assertEqual(manifest.build_id, candidate.build_id)
        self.assertFalse(manifest.active)
        self.assertEqual(index.get_active_manifest(), active)
        self.assertEqual(index.list_builds(), before)

    def test_candidate_search_snapshot_survives_concurrent_candidate_removal(self):
        index = self.build()
        candidate = index.build(
            normalized([dict(RAW_CONCEPTS[0], code="C999")]), None, self.provider
        )
        other = index.build(normalized([dict(RAW_CONCEPTS[0], code="C888")]), None, self.provider)
        embed = self.provider.embed

        def activate_during_query(texts):
            index.activate(other.build_id)
            return embed(texts)

        with patch.object(self.provider, "embed", side_effect=activate_during_query):
            hits, manifest = index.search_build(candidate.build_id, "neoplasm", self.provider)
        self.assertEqual([hit.concept.code for hit in hits], ["C999"])
        self.assertEqual(manifest.build_id, candidate.build_id)
        self.assertEqual(index.get_active_manifest().build_id, other.build_id)
        with self.assertRaisesRegex(IndexBuildError, "unavailable"):
            index.search_build(candidate.build_id, "neoplasm", self.provider)

    def test_a_sample_whose_activation_check_fails_leaves_no_inactive_snapshot(self):
        index = self.build()
        active = index.get_active_manifest()
        other = index.build(normalized([dict(RAW_CONCEPTS[0], code="C888")]), None, self.provider)
        embed = self.provider.embed

        def activate_during_sample(texts):
            index.activate(other.build_id)
            return embed(texts)

        with (
            patch.object(self.provider, "embed", side_effect=activate_during_sample),
            self.assertRaisesRegex(IndexStateError, "changed during sample indexing"),
        ):
            index.upsert_concepts(
                normalized([dict(RAW_CONCEPTS[0], code="C777")]), None, self.provider
            )

        self.assertEqual(
            {build.build_id for build in index.list_builds()}, {active.build_id, other.build_id}
        )
        self.assertEqual(index.get_active_manifest().build_id, other.build_id)

    def test_candidate_search_requires_completed_compatible_build_without_active_index(self):
        index = LocalIndex(self.path)
        candidate = index.build(normalized(RAW_CONCEPTS), None, self.provider)
        hits, manifest = index.search_build(candidate.build_id, "neoplasm", self.provider)
        self.assertTrue(hits)
        self.assertEqual(manifest.build_id, candidate.build_id)
        self.assertIsNone(index.get_active_manifest())
        with self.assertRaises(IndexCompatibilityError):
            index.search_build(candidate.build_id, "neoplasm", HashingEmbeddingProvider(64))
        with index._connect() as conn:
            conn.execute("UPDATE manifests SET state = 'building'")
        with self.assertRaisesRegex(IndexBuildError, "unavailable"):
            index.search_build(candidate.build_id, "neoplasm", self.provider)

    def test_cursor_expires_before_checking_a_replacement_builds_provider(self):
        index = self.build()
        active = index.get_active_manifest()
        replacement = index.build(
            normalized(RAW_CONCEPTS), None, HashingEmbeddingProvider(dimensions=64)
        )
        index.activate(replacement.build_id)
        with self.assertRaises(IndexCursorError) as raised:
            index.search_page("cancer", self.provider, build_id=active.build_id)
        self.assertEqual(raised.exception.details["cursorRelease"], active.release_version)
        self.assertEqual(raised.exception.details["currentRelease"], replacement.release_version)

    def test_unusable_build_lease_reports_storage_error_and_keeps_active_index(self):
        index = self.build()
        active = index.get_active_manifest()
        with patch("ncit_index.index.uuid4") as identifier:
            identifier.return_value.hex = "f" * 32
            lease_path = self.path / f"build-{'f' * 32}.sqlite3"
            lease_path.mkdir()
            with self.assertRaises(IndexStorageError) as raised:
                index.build(normalized(RAW_CONCEPTS), None, self.provider)
        self.assertIn(str(lease_path), str(raised.exception))
        self.assertEqual(index.get_active_manifest(), active)

    def test_another_cache_writer_succeeds_mid_build_and_cannot_activate_partial_data(self):
        index = self.build()
        embed = self.provider.embed
        calls = []
        second_batch = 2

        def write_during_second_batch(texts):
            calls.append(len(texts))
            if len(calls) == second_batch:
                with index._connect() as conn:
                    building = conn.execute(
                        "SELECT build_id FROM manifests WHERE state = 'building'"
                    ).fetchone()[0]
                    count = conn.execute(
                        "SELECT COUNT(*) FROM concepts WHERE build_id = ?", (building,)
                    ).fetchone()[0]
                self.assertEqual(count, 64)
                with self.assertRaises(IndexBuildError):
                    index.activate(building)
                index.upsert_concepts(
                    normalized([dict(RAW_CONCEPTS[0], code="C999")]),
                    None,
                    HashingEmbeddingProvider(),
                )
            return embed(texts)

        rows = [dict(RAW_CONCEPTS[0], code=f"C{number}") for number in range(65)]
        with patch.object(self.provider, "embed", side_effect=write_during_second_batch):
            built = index.build(normalized(rows), None, self.provider)
        self.assertEqual(built.concept_count, 65)
        self.assertIsNotNone(index.get_concept_snapshot("C999")[1])
        self.assertFalse(built.active)
        self.assertEqual(list(self.path.glob("build-*.sqlite3")), [])

    def test_cache_payload_and_manifest_share_snapshot_during_activation(self):
        index = self.build()
        active = index.get_active_manifest()
        other = index.build(
            normalized([dict(RAW_CONCEPTS[1], version="other")]), None, self.provider
        )
        read = LocalIndex._active_manifest

        def activate_after_manifest(conn):
            manifest = read(conn)
            index.activate(other.build_id)
            return manifest

        with patch.object(LocalIndex, "_active_manifest", staticmethod(activate_after_manifest)):
            manifest, cached = index.get_concept_snapshot("C3262")
        self.assertEqual(manifest, active)
        self.assertEqual(cached.release_version, active.release_version)
        self.assertEqual(index.get_active_manifest().release_version, "other")

    def test_activation_is_idempotent_and_unknown_rebuild_is_rejected(self):
        index = self.build()
        active = index.get_active_manifest()
        self.assertEqual(index.activate(active.build_id), active)
        with self.assertRaises(IndexBuildError):
            index.rebuild("unknown", self.provider)
        self.assertEqual(index.get_active_manifest(), active)

    def test_failed_activation_rolls_back_the_active_flag_and_retention(self):
        index = self.build()
        active = index.get_active_manifest()
        built = index.build(normalized([RAW_CONCEPTS[0]]), None, self.provider)
        with index._connect() as conn:
            conn.execute(
                "CREATE TRIGGER refuse_activation BEFORE UPDATE ON manifests "
                "WHEN NEW.active = 1 BEGIN SELECT RAISE(ABORT, 'blocked'); END"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            index.activate(built.build_id)
        self.assertEqual(index.get_active_manifest(), active)
        self.assertEqual(len(index.list_builds()), 2)

    def test_interrupted_build_stays_hidden_until_next_start_removes_it(self):
        index = self.build()
        active = index.get_active_manifest()
        embed = self.provider.embed
        calls = []
        second_batch = 2

        def interrupt_second_batch(texts):
            calls.append(len(texts))
            if len(calls) == second_batch:
                raise KeyboardInterrupt
            return embed(texts)

        rows = [dict(RAW_CONCEPTS[0], code=f"C{number}") for number in range(65)]
        with (
            patch.object(self.provider, "embed", side_effect=interrupt_second_batch),
            self.assertRaises(KeyboardInterrupt),
        ):
            index.build(normalized(rows), None, self.provider)
        self.assertEqual(index.list_builds(), [active])
        self.assertEqual(self.provider.embed, embed)
        with index._connect() as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM manifests WHERE state = 'building'").fetchone()[
                    0
                ],
                1,
            )
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM concepts").fetchone()[0], 66)
        next_build = index.build(normalized(RAW_CONCEPTS), None, self.provider)
        self.assertEqual(next_build.concept_count, 2)
        with index._connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM concepts").fetchone()[0], 4)
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM manifests WHERE state = 'building'").fetchone()[
                    0
                ],
                0,
            )

    def test_invalid_streams_never_leave_partial_snapshots(self):
        index = self.build()
        active = index.get_active_manifest()
        cases = [
            [RAW_CONCEPTS[0], dict(RAW_CONCEPTS[1], version="other")],
            [dict(RAW_CONCEPTS[0], code="")],
            [RAW_CONCEPTS[0], RAW_CONCEPTS[0]],
            [dict(RAW_CONCEPTS[0], name="", definitions=[], synonyms=[])],
        ]
        for rows in cases:
            with self.subTest(rows=rows), self.assertRaises(IndexBuildError):
                index.build(normalized(iter(rows)), None, self.provider)
        with self.assertRaises(IndexCompatibilityError):
            index.build(
                normalized(RAW_CONCEPTS), None, self.provider, expected_release_version="other"
            )
        self.assertEqual(index.list_builds(), [active])

    def test_embedding_dimension_change_between_batches_rolls_back(self):
        index = self.build()
        embed = self.provider.embed
        calls = []

        def changing(texts):
            calls.append(len(texts))
            vectors = embed(texts)
            return vectors if len(calls) == 1 else [row[:2] for row in vectors]

        rows = [dict(RAW_CONCEPTS[0], code=f"C{number}") for number in range(65)]
        with (
            patch.object(self.provider, "embed", side_effect=changing),
            self.assertRaisesRegex(IndexCompatibilityError, "changed during"),
        ):
            index.build(normalized(rows), None, self.provider)
        self.assertEqual(index.get_active_manifest().concept_count, 2)
        self.assertEqual(len(index.list_builds()), 1)

    def test_inactive_build_cannot_change_active_bm25_statistics(self):
        index = self.build()
        before = index.search("kinase tumor", self.provider, mode="bm25")
        index.build(
            normalized([dict(RAW_CONCEPTS[0], code=f"C{n}") for n in range(50)]),
            None,
            self.provider,
        )
        self.assertEqual(index.search("kinase tumor", self.provider, mode="bm25"), before)

    def test_inactive_same_release_build_does_not_change_readers_and_can_roll_back(self):
        index = self.build()
        old = index.get_active_manifest()
        changed = dict(RAW_CONCEPTS[1], name="Changed Name")

        built = index.build(normalized([changed]), None, self.provider)

        self.assertFalse(built.active)
        self.assertEqual(index.get_active_manifest(), old)
        self.assertEqual(index.get_concept_snapshot("C3262")[1].preferred_name, "Neoplasm")
        index.activate(built.build_id)
        self.assertEqual(index.get_concept_snapshot("C3262")[1].preferred_name, "Changed Name")
        index.activate(old.build_id)
        self.assertEqual(index.get_concept_snapshot("C3262")[1].preferred_name, "Neoplasm")

    def test_third_activation_keeps_only_active_and_previous_build(self):
        index = self.build()
        first = index.get_active_manifest()
        second = index.build(normalized([RAW_CONCEPTS[0]]), None, self.provider)
        index.activate(second.build_id)
        third = index.build(normalized([RAW_CONCEPTS[1]]), None, self.provider)
        index.activate(third.build_id)

        self.assertEqual(
            {item.build_id for item in index.list_builds()}, {second.build_id, third.build_id}
        )
        with self.assertRaises(IndexBuildError):
            index.activate(first.build_id)
        index.activate(second.build_id)
        self.assertEqual(
            index.get_concept_snapshot("C40704")[1].preferred_name, RAW_CONCEPTS[0]["name"]
        )

    def test_a_pending_removal_is_hidden_and_untouched_by_the_next_build(self):
        index = self.build()
        stale = index.build(normalized([RAW_CONCEPTS[0]]), None, self.provider)
        replacement = index.build(normalized([RAW_CONCEPTS[1]]), None, self.provider)
        busy = sqlite3.OperationalError("database is locked")
        busy.sqlite_errorcode = sqlite3.SQLITE_BUSY
        with patch.object(index_module, "delete_build", side_effect=busy):
            index.activate(replacement.build_id)

        index.build(normalized([RAW_CONCEPTS[0]]), None, self.provider)

        self.assertEqual(index.pending_removal(), [stale.build_id])
        self.assertNotIn(stale.build_id, {item.build_id for item in index.list_builds()})
        index.activate(replacement.build_id)
        self.assertEqual(index.pending_removal(), [])

    def test_invalid_build_keeps_active_snapshot(self):
        index = self.build()
        active = index.get_active_manifest()

        with self.assertRaises(IndexBuildError):
            index.build([], None, self.provider)

        self.assertEqual(index.get_active_manifest(), active)
        self.assertEqual(index.get_concept_snapshot("C3262")[1].preferred_name, "Neoplasm")


class FieldSearchTest(IndexTestCase):
    def test_name_wins_field_ties_and_exact_name_beats_an_adversarial_model(self):
        index = LocalIndex(self.path)
        with patch.object(
            self.provider, "embed", side_effect=lambda texts: [[1.0, 0.0] for _ in texts]
        ):
            index.upsert_concepts(normalized(RAW_CONCEPTS), None, self.provider)
            tied = index.search("arbitrary query", self.provider, mode="vector")
            self.assertEqual([hit.matched_on for hit in tied], ["name", "name"])
        with patch.object(self.provider, "embed", return_value=[[0.0, 1.0]]):
            exact = index.search(
                "Receptor Tyrosine Kinase Inhibition", self.provider, limit=1, mode="vector"
            )
        self.assertEqual(exact[0].concept.code, "C40704")
        self.assertEqual((exact[0].matched_on, exact[0].score), ("name", 1.0))

    def test_public_manifest_excludes_operator_state(self):
        index = self.build()
        with correlated():
            public = LocalIndexAdapter(index).get_active_manifest().to_result()
        self.assertEqual(
            set(public),
            {"terminology", "version", "concepts", "embedding", "builtAt", "provenance"},
        )
        self.assertEqual(
            public["embedding"], {"provider": "hashing", "model": "hashing-128", "dimensions": 128}
        )

    def test_identical_text_is_embedded_once_with_name_priority(self):
        calls = []
        embed = self.provider.embed

        def record(texts):
            calls.extend(texts)
            return embed(texts)

        self.provider.embed = record
        raw = dict(
            RAW_CONCEPTS[1],
            synonyms=[{"name": "Neoplasm"}, {"name": "Tumor"}],
            definitions=[{"definition": "Neoplasm"}],
        )
        index = LocalIndex(self.path)
        index.upsert_concepts(normalized([raw]), None, self.provider)

        self.assertEqual(calls, ["Neoplasm", "Tumor"])
        self.assertEqual(index.search("Neoplasm", self.provider)[0].matched_on, "name")

    def test_exact_name_normalizes_case_nfc_and_whitespace(self):
        raw = dict(RAW_CONCEPTS[1], name="Café   Cell", synonyms=[], definitions=[])
        index = self.build([raw, RAW_CONCEPTS[0]])

        for mode in ("vector", "hybrid"):
            with self.subTest(mode=mode):
                hits = index.search("  CAFE\u0301\tCELL ", self.provider, limit=1, mode=mode)
                self.assertEqual(hits[0].concept.code, "C3262")
                self.assertEqual(hits[0].matched_on, "name")

    def test_synonym_and_definition_hits_name_the_matching_field(self):
        index = self.build()

        for query, expected in (("Tumor", "synonym"), ("benign malignant tissue", "definition")):
            with self.subTest(query=query):
                hit = index.search(query, self.provider, mode="bm25")[0]
                self.assertEqual(hit.concept.code, "C3262")
                self.assertEqual(hit.matched_on, expected)


class BuildStorageIntegrityTest(IndexTestCase):
    def test_expired_cursor_names_requested_and_current_releases(self):
        index = self.build([concept("C1")])
        old = index.get_active_manifest()
        index.upsert_concepts(normalized([concept("C2", version="new")]), None, self.provider)
        with self.assertRaises(IndexCursorError) as raised:
            index.search_page("term", self.provider, build_id=old.build_id, requested_release="old")
        self.assertEqual(
            raised.exception.details, {"cursorRelease": "old", "currentRelease": "new"}
        )

    def test_page_ranks_continue_after_offset(self):
        index = self.build([concept(f"C{i}", "Same") for i in range(1, 5)])
        hits, total, _ = index.search_page("Same", self.provider, mode="bm25", offset=2, limit=2)
        self.assertEqual(([hit.rank for hit in hits], total), ([3, 4], 4))

    def test_evaluation_inputs_survive_concurrent_candidate_pruning(self):
        index = self.build([concept("C1")])
        candidate = index.build(normalized([concept("C2")]), None, self.provider)
        replacement = index.build(normalized([concept("C3")]), None, self.provider)
        original = index_module._completed_manifest

        def prune_after_read(conn, build):
            manifest = original(conn, build)
            index.activate(replacement.build_id)
            return manifest

        with patch.object(index_module, "_completed_manifest", side_effect=prune_after_read):
            manifest, missing = index.evaluation_inputs(candidate.build_id, {"C2"})
        self.assertEqual((manifest.build_id, missing), (candidate.build_id, []))
        self.assertNotIn(candidate.build_id, {item.build_id for item in index.list_builds()})

    def test_pruning_removes_vectors_and_fts_storage(self):
        index = self.build([concept("C1")])
        old = index.get_active_manifest().build_id
        index.upsert_concepts(normalized([concept("C2")]), None, self.provider)
        index.upsert_concepts(normalized([concept("C3")]), None, self.provider)
        with index._connect() as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM concept_vectors WHERE build_id = ?", (old,)
                ).fetchone()[0],
                0,
            )
            self.assertIsNone(
                conn.execute(
                    "SELECT name FROM sqlite_master WHERE name = ?", (fts_table(old),)
                ).fetchone()
            )

    def test_duplicate_field_text_keeps_name_before_synonym_before_definition(self):
        raw = concept(
            "C1",
            "Name",
            synonyms=[{"name": "Name"}, {"name": "Alias"}],
            definitions=[{"definition": "Name"}, {"definition": "Alias"}],
        )
        item = normalize_concept(raw, None, "active_cache")
        self.assertEqual(concept_fields(item), [("name", "Name"), ("synonym", "Alias")])

    def test_exact_name_key_collapses_whitespace_and_normalizes_unicode(self):
        self.assertEqual(name_key("  CAFE\u0301\t  cell\n"), "café cell")

    def test_nonbusy_failure_cleaning_a_stale_build_is_not_swallowed(self):
        index = self.build([concept("C1")])
        with index._connect() as conn:
            conn.execute("UPDATE manifests SET active = 0, state = 'building'")
        error = sqlite3.OperationalError("lease I/O failure")
        error.sqlite_errorcode = sqlite3.SQLITE_IOERR
        with (
            index._connect() as conn,
            patch("ncit_index.storage.delete_build", side_effect=error),
            self.assertRaisesRegex(sqlite3.OperationalError, "lease I/O failure"),
        ):
            clean_stale_builds(conn, self.path)


class RankingBoundaryTest(IndexTestCase):
    def test_exact_name_beats_a_better_vector_before_page_selection(self):
        provider = HashingEmbeddingProvider(dimensions=2)
        values = {"Exact": [1.0, 0.0], "Other": [0.0, 1.0]}
        with patch.object(provider, "embed", side_effect=lambda texts: [values[t] for t in texts]):
            index = self.build([concept("C9", "Exact"), concept("C1", "Other")], provider=provider)
        with patch.object(provider, "embed", return_value=[[0.0, 1.0]]):
            hits = index.search("Exact", provider, mode="vector", limit=1)
        self.assertEqual(
            [(hit.concept.code, hit.score, hit.matched_on) for hit in hits], [("C9", 1.0, "name")]
        )

    def test_bm25_exact_casefold_match_is_injected_and_wins_score_ties(self):
        index = self.build(
            [
                concept("C1", "Other", synonyms=[{"name": "STRASSE"}]),
                concept("C9", "Straße"),
            ]
        )
        hits = index.search("STRASSE", self.provider, mode="bm25", limit=1)
        self.assertEqual(
            [(hit.concept.code, hit.score, hit.matched_on) for hit in hits], [("C9", 1.0, "name")]
        )

    def test_bm25_ties_use_code_order_and_continue_after_the_first_page(self):
        index = self.build(
            [concept(code, "Other", synonyms=[{"name": "needle"}]) for code in ("C9", "C1", "C5")]
        )
        # A valid query plan can visit equal matches in insertion order.
        # The public ordering must not depend on the optional lookup index.
        with index._connect() as conn:
            conn.execute("DROP INDEX fields_concept")
        first, total, _ = index.search_page("needle", self.provider, mode="bm25", limit=1)
        second, _, _ = index.search_page("needle", self.provider, mode="bm25", limit=1, offset=1)
        self.assertEqual(total, 3)
        self.assertEqual([first[0].concept.code, second[0].concept.code], ["C1", "C5"])
        self.assertEqual([first[0].score, second[0].score], [1.0, 1.0])

    def test_bm25_equal_fields_prefer_name_over_synonym_and_definition(self):
        index = self.build(
            [
                concept(
                    "C1",
                    "alpha beta",
                    synonyms=[{"name": "beta alpha"}],
                    definitions=[{"definition": "alpha  beta"}],
                )
            ]
        )
        hits = index.search("alpha", self.provider, mode="bm25")
        self.assertEqual(
            [(hit.concept.code, hit.matched_on, hit.score) for hit in hits], [("C1", "name", 1.0)]
        )

    def test_hybrid_lexical_scores_stay_with_their_concepts_after_a_scan_chunk(self):
        rows = [concept(f"C{i:03}", "noise") for i in range(256)] + [concept("C999", "needle")]
        with patch.object(
            self.provider, "embed", side_effect=lambda texts: [[1.0, 0.0] for _ in texts]
        ):
            index = self.build(rows)
            hits = index.search("needle query", self.provider, mode="hybrid", limit=2)
        self.assertEqual(hits[0].concept.code, "C999")
        self.assertEqual(hits[0].score_components["bm25"], 1.0)
        self.assertEqual(hits[1].score_components["bm25"], 0.0)

    def test_vector_mode_never_reports_a_bm25_component_even_for_exact_names(self):
        index = self.build([concept("C1", "Alpha"), concept("C2", "Alpha Beta")])
        hits = index.search("Alpha", self.provider, mode="vector")
        self.assertEqual([hit.concept.code for hit in hits], ["C1", "C2"])
        self.assertEqual([hit.score_components["bm25"] for hit in hits], [0.0, 0.0])

    def test_vector_nonexact_matches_also_have_no_bm25_component(self):
        index = self.build([concept("C1", "Alpha Beta"), concept("C2", "Alpha Gamma")])
        hits = index.search("Alpha", self.provider, mode="vector")
        self.assertEqual({hit.concept.code for hit in hits}, {"C1", "C2"})
        self.assertEqual([hit.score_components["bm25"] for hit in hits], [0.0, 0.0])

    def test_positive_cosines_normalize_the_weakest_match_to_zero(self):
        provider = HashingEmbeddingProvider(dimensions=2)
        values = {"Alpha": [1.0, 0.0], "Beta": [1.0, 1.0], "query": [1.0, 0.0]}
        with patch.object(provider, "embed", side_effect=lambda texts: [values[t] for t in texts]):
            index = self.build([concept("C1", "Alpha"), concept("C2", "Beta")], provider=provider)
            hits = index.search("query", provider, mode="vector")
        self.assertEqual(
            [(hit.concept.code, hit.score) for hit in hits], [("C1", 1.0), ("C2", 0.0)]
        )

    def test_no_matching_retirement_status_has_zero_total_and_no_hits(self):
        index = self.build([concept("C1", "Alpha", active=True)])
        hits, total, manifest = index.search_page(
            "Alpha", self.provider, retired_status="Retired_Concept"
        )
        self.assertEqual((hits, total), ([], 0))
        self.assertEqual(manifest.concept_count, 1)

    def test_a_retirement_filter_pages_exactly_over_a_mix_of_multi_field_concepts(self):
        # Concepts carry several fields each, so the scan's arrays must be sized by the
        # fields of the matching concepts only, while the page stays exact.
        index = self.build(
            [
                concept("C1", "Alpha", synonyms=[{"name": "Alpha one"}], active=False),
                concept("C2", "Alpha two", synonyms=[{"name": "Alpha too"}], active=True),
                concept("C3", "Alpha three", active=False),
                concept("C4", "Alpha four", synonyms=[{"name": "Alpha 4"}], active=True),
            ]
        )
        retired_status = "Retired_Concept"
        with index._connect() as conn:
            conn.execute(
                "UPDATE concepts SET status = ? WHERE code IN ('C1', 'C3')", (retired_status,)
            )
        first, total, _ = index.search_page(
            "Alpha", self.provider, mode="vector", limit=1, retired_status=retired_status
        )
        second, _, _ = index.search_page(
            "Alpha", self.provider, mode="vector", limit=1, offset=1, retired_status=retired_status
        )
        self.assertEqual(total, 2)
        self.assertEqual({hit.concept.code for hit in first + second}, {"C1", "C3"})
