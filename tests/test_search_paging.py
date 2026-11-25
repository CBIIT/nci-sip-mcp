import base64
import json
from dataclasses import replace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from fakes import concept, release, terminology_row
from nci_sip_mcp.evs import EVSClient, EVSReleaseNotFoundError
from nci_sip_mcp.registry import invoke
from nci_sip_mcp.traffic_identity import product_user_agent
from ncit_index.embeddings import HashingEmbeddingProvider
from ncit_index.scoring import rank_page
from test_evs_client import FakeResponse
from test_index import normalized
from test_release_cursor_validation import padded_token
from test_server import ServerFixture, pinned


class IndexedSearchTest(ServerFixture):
    def setUp(self):
        super().setUp()
        self.local_index.upsert_concepts(
            normalized([concept(f"C{i}", "Kinase", active=True) for i in range(1, 6)]),
            None,
            self.context.embedding_provider,
        )

    def search(self, **arguments):
        return invoke(
            self.context,
            "search_concepts",
            **(
                {
                    "terminology": "ncit",
                    "release": "26.06e",
                    "query": "Kinase",
                    "mode": "semantic",
                    "limit": 2,
                }
                | arguments
            ),
        )

    def test_both_indexed_modes_page_all_matches_with_stable_ties(self):
        for mode in ("semantic", "hybrid"):
            with self.subTest(mode=mode):
                first = self.search(mode=mode)
                second = self.search(mode=mode, cursor=first["nextCursor"])
                last = self.search(mode=mode, cursor=second["nextCursor"])
                codes = [
                    hit["concept"]["code"]
                    for page in (first, second, last)
                    for hit in page["results"]
                ]
                self.assertEqual(codes, [f"C{i}" for i in range(1, 6)])
                self.assertEqual([page["totalKnown"] for page in (first, second, last)], [5, 5, 5])
                self.assertNotIn("nextCursor", last)
                self.assertNotIn("truncation", first)

    def test_a_same_release_replacement_expires_the_cursor(self):
        first = self.search()
        self.local_index.upsert_concepts(
            normalized([concept("C6", "Kinase", active=True)]),
            None,
            self.context.embedding_provider,
        )
        result = self.search(cursor=first["nextCursor"])
        self.assertEqual(result["error"]["code"], "cursor_expired")
        self.assertEqual(
            result["error"]["details"],
            {
                "cursorRelease": "26.06e",
                "currentRelease": "26.06e",
            },
        )
        self.assertEqual(
            result["error"]["message"],
            "The active index build changed. Restart the search against the active build.",
        )

    def test_retirement_filters_before_count_and_page_selection(self):
        self.evs.rows = [
            terminology_row()
            | {
                "metadata": {
                    "retiredStatusValue": "retired-test",
                    "conceptStatuses": ["retired-test"],
                }
            }
        ]
        self.local_index.upsert_concepts(
            normalized(
                [
                    concept(f"C{i}", "Kinase", active=False, conceptStatus="retired-test")
                    for i in (2, 4, 6)
                ]
            ),
            None,
            self.context.embedding_provider,
        )
        first = self.search(retired="only")
        last = self.search(retired="only", cursor=first["nextCursor"])
        self.assertEqual([hit["concept"]["code"] for hit in first["results"]], ["C2", "C4"])
        self.assertEqual([hit["concept"]["code"] for hit in last["results"]], ["C6"])
        self.assertEqual(first["totalKnown"], 3)
        self.assertNotIn("nextCursor", last)

    def test_missing_numpy_is_a_capability_error_not_a_partial_ranking(self):
        with patch.dict("sys.modules", {"numpy": None}):
            result = self.search()
        self.assertEqual(result["error"]["code"], "capability_unavailable")
        self.assertEqual(
            result["error"]["message"],
            "Exact indexed search needs NumPy. Install the 'index' extra and retry.",
        )
        self.assertEqual(
            result["error"]["details"], {"capability": "semantic/hybrid index scoring"}
        )
        self.assertNotIn("results", result)

    def test_an_indexed_cursor_requires_its_internal_build_identity(self):
        first = self.search()
        payload = json.loads(base64.urlsafe_b64decode(first["nextCursor"]))
        for build in (None, "bad", 12):
            with self.subTest(build=build):
                token = base64.urlsafe_b64encode(
                    json.dumps(payload | {"build": build}).encode()
                ).decode()
                self.assertEqual(self.search(cursor=token)["error"]["code"], "invalid_request")

    def test_page_count_and_identity_share_a_snapshot_during_activation(self):
        replacement = self.local_index.build(
            normalized([concept("C9", "Kinase", active=True)]),
            None,
            self.context.embedding_provider,
        )

        def activate_then_rank(*args, **kwargs):
            self.local_index.activate(replacement.build_id)
            return rank_page(*args, **kwargs)

        with patch("ncit_index.index.rank_page", side_effect=activate_then_rank):
            first = self.search()
        self.assertEqual(first["totalKnown"], 5)
        self.assertEqual([hit["concept"]["code"] for hit in first["results"]], ["C1", "C2"])
        continued = self.search(cursor=first["nextCursor"])
        self.assertEqual(continued["error"]["code"], "cursor_expired")


class LiveSearchTest(ServerFixture):
    def test_duplicate_search_identities_fail_instead_of_concealing_missing_matches(self):
        row = concept("C1", active=True)
        for mode in ("lexical", "typeahead"):
            with self.subTest(mode=mode), self.replies([{"total": 2, "concepts": [row, row]}]):
                result = self.search(mode=mode)
                self.assertEqual(result["error"]["code"], "upstream_unavailable")
                self.assertNotIn("results", result)

    def setUp(self):
        super().setUp()
        self.context.evs = EVSClient(
            "https://evs.invalid", max_attempts=1, user_agent=product_user_agent()
        )

    def search(self, **arguments):
        return invoke(
            self.context,
            "search_concepts",
            **({"terminology": "ncit", "release": "26.06e", "query": "kinase"} | arguments),
        )

    def replies(self, bodies):
        return patch(
            "nci_sip_mcp.http_client._open",
            side_effect=[FakeResponse(json.dumps(body).encode()) for body in bodies],
        )

    def test_lexical_pages_preserve_order_highlights_and_stop_at_the_last_page(self):
        first = concept(
            "C2",
            active=True,
            highlight="<em>kinase</em> & text",
            synonyms=[{"name": "Protein kinase", "source": "NCI"}],
        )
        second = concept("C1", active=False, conceptStatus="Retired_Concept")
        with self.replies(
            [{"total": 2, "concepts": [first]}, {"total": 2, "concepts": [second]}]
        ) as opened:
            page = self.search(limit=1)
            last = self.search(limit=1, cursor=page["nextCursor"])
        self.assertEqual([p["results"][0]["concept"]["code"] for p in (page, last)], ["C2", "C1"])
        self.assertEqual(page["results"][0]["matchedOn"], first["highlight"])
        self.assertTrue(
            {"synonyms", "definitions", "properties", "semanticType"}.isdisjoint(
                page["results"][0]["concept"]
            )
        )
        self.assertNotIn("score", page["results"][0])
        self.assertNotIn("matchedOn", last["results"][0])
        self.assertNotIn("nextCursor", last)
        self.assertNotIn("truncation", page)
        self.assertEqual(page["totalKnown"], 2)
        provenance = page["results"][0]["concept"]["provenance"]
        self.assertEqual(provenance["upstream"], {"terminology": "ncit", "version": "26.06e"})
        self.assertEqual(
            provenance["sourceUri"], "https://evs.invalid/api/v1/concept/ncit_26.06e/C2"
        )
        self.assertIn("fromRecord=1", opened.call_args_list[1].args[0].full_url)

    def test_typeahead_omits_score_and_highlight_and_encodes_the_whole_query(self):
        query = "a & b/+?é"
        row = concept("C1", active=True, highlight="unexpected")
        with self.replies([{"total": 1, "concepts": [row]}]) as opened:
            result = self.search(query=query, mode="typeahead")
        self.assertEqual(set(result["results"][0]), {"concept"})
        provenance = result["results"][0]["concept"]["provenance"]
        self.assertEqual(provenance["upstream"], {"terminology": "ncit", "version": "26.06e"})
        self.assertEqual(
            provenance["sourceUri"], "https://evs.invalid/api/v1/concept/ncit_26.06e/C1"
        )
        params = parse_qs(urlsplit(opened.call_args.args[0].full_url).query)
        self.assertEqual(params["term"], [query])
        self.assertEqual(params["type"], ["startsWith"])

    def test_empty_matches_have_live_provenance(self):
        with self.replies([{"total": 0}]):
            result = self.search()
        self.assertEqual(result["results"], [])
        self.assertEqual(result["totalKnown"], 0)
        self.assertEqual(result["provenance"]["release"]["identifier"], "26.06e")
        self.assertEqual(result["provenance"]["servedBy"], "live")
        self.assertNotIn("upstream", result["provenance"])

    def test_explicit_defaults_continue_a_cursor_issued_with_defaults_omitted(self):
        rows = [concept(f"C{i}", active=True) for i in range(1, 12)]
        with self.replies(
            [{"total": 11, "concepts": rows[:10]}, {"total": 11, "concepts": rows[10:]}]
        ):
            first = self.search()
            last = self.search(
                mode="lexical", retired="include", limit=10, cursor=first["nextCursor"]
            )
        self.assertEqual(last["results"][0]["concept"]["code"], "C11")
        self.assertNotIn("nextCursor", last)

    def test_changed_arguments_are_invalid_before_an_upstream_request(self):
        with self.replies([{"total": 2, "concepts": [concept("C1", active=True)]}]):
            first = self.search(limit=1)
        for change in (
            {"query": "other"},
            {"mode": "typeahead"},
            {"retired": "only"},
            {"limit": 2},
        ):
            with self.subTest(change=change), self.replies([]):
                result = self.search(**({"limit": 1, "cursor": first["nextCursor"]} | change))
                self.assertEqual(result["error"]["code"], "invalid_request")

    def test_deeply_nested_and_invalid_cursors_are_invalid_before_network(self):
        nested = base64.urlsafe_b64encode(("[" * 1500 + "0" + "]" * 1500).encode()).decode()
        for cursor in (nested, "", "bad", "e30=", "x" * 9000):
            with self.subTest(cursor=cursor[:12]), self.replies([]):
                self.assertEqual(self.search(cursor=cursor)["error"]["code"], "invalid_request")

    def test_retired_only_uses_the_status_named_by_the_pinned_listing(self):
        metadata = release().to_dict() | {
            "metadata": {"retiredStatusValue": "retired-test", "conceptStatuses": ["retired-test"]}
        }
        row = concept("C1", active=False, conceptStatus="retired-test")
        with self.replies([[metadata], {"total": 1, "concepts": [row]}]) as opened:
            result = self.search(retired="only")
        self.assertEqual(result["results"][0]["concept"]["status"], "retired-test")
        self.assertIn("conceptStatus=retired-test", opened.call_args_list[1].args[0].full_url)

    def test_unselectable_retirement_is_invalid_without_a_search_request(self):
        metadata = release().to_dict() | {
            "metadata": {"retiredStatusValue": "true", "conceptStatuses": ["DEFAULT"]}
        }
        with self.replies([[metadata]]):
            result = self.search(retired="only")
        self.assertEqual(result["error"]["code"], "invalid_request")

    def test_unusable_pages_report_upstream_unavailable_without_results(self):
        row = concept("C1", active=True)
        pages = [
            {"total": True, "concepts": [row]},
            {"total": -1, "concepts": []},
            {"total": 1, "concepts": []},
            {"total": 0, "concepts": [row]},
            {"total": 1, "concepts": [None]},
        ]
        for page in pages:
            with self.subTest(page=page), self.replies([page]):
                result = self.search()
                self.assertEqual(result["error"]["code"], "upstream_unavailable")
                self.assertNotIn("results", result)

    def test_wrong_release_search_page_reports_the_conflict_without_results(self):
        row = concept("C1", active=True, version="26.01a")
        with self.replies([{"total": 1, "concepts": [row]}]):
            result = self.search()
        self.assertEqual(result["error"]["code"], "release_mismatch")
        self.assertEqual(
            result["error"]["details"],
            {"requested": "26.06e", "served": ["26.01a"], "source": "evs"},
        )
        self.assertNotIn("results", result)

    def test_withdrawn_cursor_discovers_current_only_after_the_pinned_failure(self):
        with self.replies([{"total": 2, "concepts": [concept("C1", active=True)]}]):
            first = self.search(limit=1)
        with patch.object(
            self.context.evs, "search_concepts", side_effect=EVSReleaseNotFoundError("withdrawn")
        ):
            with self.replies([[terminology_row(version="new")]]):
                result = self.search(limit=1, cursor=first["nextCursor"])
            with self.replies([]):
                initial = self.search()
        self.assertEqual(result["error"]["code"], "cursor_expired")
        self.assertEqual(
            result["error"]["details"], {"cursorRelease": "26.06e", "currentRelease": "new"}
        )
        self.assertEqual(initial["error"]["code"], "release_not_available")

    def test_invalid_highlight_or_missing_code_cannot_be_a_successful_match(self):
        row = concept("C1", active=True)
        for change in ({"highlight": None}, {"code": ""}):
            with (
                self.subTest(change=change),
                self.replies([{"total": 1, "concepts": [row | change]}]),
            ):
                result = self.search()
                self.assertEqual(result["error"]["code"], "upstream_unavailable")
                self.assertNotIn("results", result)

    def test_retired_only_rejects_malformed_or_ambiguous_metadata(self):
        row = terminology_row()
        listings = [
            [row, row],
            [row | {"metadata": "bad"}],
            [row | {"metadata": {"conceptStatuses": "retired-test"}}],
        ]
        for listing in listings:
            with self.subTest(listing=listing), self.replies([listing]):
                self.assertEqual(
                    self.search(retired="only")["error"]["code"], "upstream_unavailable"
                )

    def test_retired_only_cannot_pass_through_an_active_match(self):
        metadata = terminology_row() | {
            "metadata": {
                "retiredStatusValue": "retired-test",
                "conceptStatuses": ["retired-test"],
            }
        }
        row = concept("C1", active=True, conceptStatus="retired-test")
        with self.replies([[metadata], {"total": 1, "concepts": [row]}]):
            result = self.search(retired="only")
        self.assertEqual(result["error"]["code"], "upstream_unavailable")


def reposition(value, offset):
    payload = json.loads(base64.urlsafe_b64decode(value))
    return padded_token(payload | {"offset": offset})


class SearchContinuationTest(ServerFixture):
    def setUp(self):
        super().setUp()
        self.evs.concepts = {
            "C1": concept("C1", "One", active=True, children=[{"code": "C2"}]),
            "C2": concept("C2", "Two", active=True),
        }

    def call(self, tool, **arguments):
        return invoke(
            self.context,
            tool,
            **({"terminology": "ncit", "release": "26.06e"} | arguments),
        )

    def search(self, **arguments):
        return self.call("search_concepts", **({"query": "One", "limit": 1} | arguments))

    def live_rows(self, total, rows):
        return patch.object(self.evs, "search_concepts", return_value=(total, rows), create=True)

    def retirement_metadata(self):
        return terminology_row() | {
            "metadata": {
                "retiredStatusValue": "Retired_Concept",
                "conceptStatuses": ["Retired_Concept"],
            }
        }

    def test_search_cursor_binds_release_and_terminology(self):
        with self.live_rows(2, [self.evs.concepts["C1"]]):
            first = self.search()
            for changed in ({"release": "other"}, {"terminology": "other"}):
                with self.subTest(changed=changed):
                    result = self.search(cursor=first["nextCursor"], **changed)
                    self.assertEqual(result["error"]["code"], "invalid_request")
                    self.assertEqual(result["error"]["details"]["parameter"], "cursor")

    def test_search_cursor_at_or_past_total_is_invalid(self):
        with self.live_rows(2, [self.evs.concepts["C1"]]):
            first = self.search()
        with self.live_rows(2, []):
            for offset in (2, 3):
                with self.subTest(offset=offset):
                    result = self.search(cursor=reposition(first["nextCursor"], offset))
                    self.assertEqual(result["error"]["code"], "invalid_request")
                    self.assertNotIn("results", result)

    def test_indexed_result_preserves_matching_field_without_top_level_provenance(self):
        self.local_index.upsert_concepts(
            normalized(self.evs.concepts.values()), None, self.context.embedding_provider
        )
        hits, total, manifest = self.context.index.search_page(
            "One", self.context.embedding_provider, 1, "vector", requested_release="26.06e"
        )
        for field in ("synonym", "definition"):
            with (
                self.subTest(field=field),
                patch.object(
                    self.context.index,
                    "search_page",
                    return_value=([replace(hits[0], matched_on=field)], total, manifest),
                ),
            ):
                result = self.search(mode="semantic")
                self.assertEqual(result["results"][0]["matchedOn"], field)
                self.assertNotIn("provenance", result)

    def test_retired_only_rejects_inactive_hit_with_different_status(self):
        self.evs.rows = [self.retirement_metadata()]
        raw = concept("C1", active=False, conceptStatus="Obsolete")
        with self.live_rows(1, [raw]):
            result = self.search(retired="only")
        self.assertEqual(result["error"]["code"], "upstream_unavailable")
        self.assertNotIn("results", result)

    def test_retirement_metadata_selects_both_release_and_terminology(self):
        selected = self.retirement_metadata()
        self.evs.rows = [
            selected | {"version": "other"},
            selected | {"terminology": "other"},
            selected,
        ]
        raw = concept("C1", active=False, conceptStatus="Retired_Concept")
        with self.live_rows(1, [raw]):
            result = self.search(retired="only")
        self.assertEqual(result["results"][0]["concept"]["status"], "Retired_Concept")

    def test_absent_retirement_release_is_release_not_available(self):
        self.evs.rows = [self.retirement_metadata() | {"version": "other"}]
        result = self.search(retired="only")
        self.assertEqual(result["error"]["code"], "release_not_available")
        self.assertNotIn("results", result)

    def test_whitespace_query_is_invalid(self):
        with self.live_rows(0, []):
            result = self.search(query=" \t\n ")
        self.assertEqual(result["error"]["code"], "invalid_request")
        self.assertEqual(result["error"]["details"]["parameter"], "query")


class IndexedSearchBoundaryTest(ServerFixture):
    def content(self, operation, **arguments):
        return invoke(self.context, operation, **pinned(**arguments))

    def index_concepts(self, concepts):
        self.local_index.upsert_concepts(
            normalized(concepts),
            None,
            self.context.embedding_provider,
            expected_release_version="26.06e",
        )

    def test_search_returns_all_150_hits_for_large_requested_limits(self):
        self.index_concepts([concept(f"C{i}", "Kinase", active=True) for i in range(1, 151)])
        for limit in (500, 5000):
            with self.subTest(limit=limit):
                result = self.content(
                    "search_concepts", query="Kinase", mode="semantic", limit=limit
                )
                self.assertEqual(len(result["results"]), 150)
                self.assertNotIn("nextCursor", result)

    def test_semantic_and_hybrid_search_return_different_rankings_through_mcp(self):
        # Semantic similarity favors the synonym-free enzyme label; lexical evidence
        # favors the kinase label. Neither preferred name is an exact query match.
        provider = HashingEmbeddingProvider(dimensions=2)
        self.context.embedding_provider = provider
        vectors = {
            "Kinase inhibitor": [0.0, 1.0],
            "Enzyme modulator": [1.0, 0.0],
            "Kinase": [1.0, 0.0],
        }
        with patch.object(provider, "embed", side_effect=lambda texts: [vectors[t] for t in texts]):
            self.index_concepts(
                [
                    concept("C1", "Kinase inhibitor", active=True),
                    concept("C2", "Enzyme modulator", active=True),
                ]
            )
            for mode, expected in (("semantic", ["C2", "C1"]), ("hybrid", ["C1", "C2"])):
                with self.subTest(mode=mode):
                    is_error, result = self.call("search_concepts", query="Kinase", mode=mode)
                    self.assertFalse(is_error)
                    hits = result["results"]
                    self.assertEqual([hit["concept"]["code"] for hit in hits], expected)
                    self.assertGreater(hits[0]["score"], hits[1]["score"])
                    for hit in hits:
                        self.assertEqual(
                            {"sourceUri", "upstream"} & hit["concept"]["provenance"].keys(),
                            set(),
                        )

    def test_invalid_mode_cursor_and_unselectable_retirement_are_refused_with_a_usable_index(self):
        self.index_concepts([concept("C1", "Kinase", active=True)])
        options = ({"mode": "bm25"}, {"cursor": "cursor"}, {"retired": "only"})
        for option in options:
            with self.subTest(option=option):
                result = self.content(
                    "search_concepts", **({"query": "Kinase", "mode": "semantic"} | option)
                )
                self.assertIn("error", result)
                self.assertEqual(result["error"]["code"], "invalid_request")
