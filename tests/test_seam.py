"""Cross-domain behavior, with independently supplied offline graph and EVS states."""

import base64
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from fakes import FakeEVS, FakeSSIS, concept, primary_element, release, terminology_row
from nci_sip_mcp.bounds import Budget
from nci_sip_mcp.caching import cache_call
from nci_sip_mcp.cadsr import EXPORT_FOLDER
from nci_sip_mcp.config import Settings
from nci_sip_mcp.context import Context
from nci_sip_mcp.cursor import decode, encode
from nci_sip_mcp.evs import EVSClient
from nci_sip_mcp.registry import invoke
from nci_sip_mcp.release_selection import SessionRelease, session_scope
from nci_sip_mcp.ssis import CADSR_GRAPH, NCIT_GRAPH, SSISClient
from nci_sip_mcp.traffic_identity import product_user_agent
from test_evs_client import FakeResponse

NCIT = "http://ncicb.nci.nih.gov/xml/owl/EVS/Thesaurus.owl#"


def element(identifier, version="1"):
    return {
        "id": str(identifier),
        "version": version,
        "name": f"Element {identifier}",
        "roles": "objectClass|http%3A%2F%2Fncicb.nci.nih.gov%2Fxml%2Fowl%2FEVS%2F"
        "Thesaurus.owl%23C1|0",
    }


def gdc_map(code="C1", version="26.06e"):
    return {
        "mapsetCode": "NCIt_Maps_To_GDC",
        "source": "ncit",
        "target": "GDC",
        "sourceCode": code,
        "sourceTerminologyVersion": version,
        "targetCode": "diagnosis",
        "targetName": f"Stored {code}",
    }


class SeamTest(unittest.TestCase):
    def test_empty_graph_results_keep_the_queried_graphs_origin(self):
        self.ssis.elements = [element(123)]
        full = self.call(conceptCode="C1", release="26.06e")
        self.ssis.elements = []
        empty = self.call(conceptCode="C1", release="26.06e")

        self.assertEqual(empty["dataElements"], [])
        origin = full["dataElements"][0]["provenance"]
        self.assertEqual(empty["provenance"]["upstream"], {"graphs": self.ssis.graphs})
        self.assertEqual(empty["provenance"]["sourceUri"], origin["sourceUri"])

    def test_empty_gdc_results_keep_the_read_mapsets_origin(self):
        self.context.evs = EVSClient("https://evs.test", user_agent=product_user_agent())
        mapset = {"code": "NCIt_Maps_To_GDC", "version": "26.06e"}
        with patch.object(
            self.context.evs,
            "_get_existing",
            side_effect=[self.evs.get_terminologies(), mapset, {"total": 0, "maps": []}],
        ):
            result = self.call(
                "resolve_stored_value", conceptCode="C1", commons="GDC", release="26.06e"
            )

        self.assertEqual(result["storedValues"], [])
        self.assertEqual(result["provenance"]["upstream"], mapset)
        self.assertEqual(
            result["provenance"]["sourceUri"], "https://evs.test/api/v1/mapset/NCIt_Maps_To_GDC"
        )

    def test_invalid_graph_dates_fail_without_returning_joined_content(self):
        for graph in self.ssis.graphs:
            with self.subTest(graph=graph["graph"]):
                original = graph["date"]
                graph["date"] = "not a dataset date"
                try:
                    result = self.call(conceptCode="C1", release="26.06e")
                finally:
                    graph["date"] = original
                self.assertEqual(result["error"]["code"], "upstream_unavailable")
                self.assertEqual(result["error"]["details"]["surface"], "ssis")
                self.assertNotIn("dataElements", result)

    def test_cursor_at_or_beyond_the_end_is_invalid_instead_of_an_empty_success(self):
        self.ssis.elements = [element(123), element(456)]
        arguments = {
            "conceptCode": "C1",
            "terminology": "ncit",
            "release": "26.06e",
            "expandDescendants": False,
            "includePermissibleValues": False,
            "limit": 1,
        }
        first = self.call(**arguments)
        applied = arguments | {"tool": "find_data_elements_for_concept"}
        position = decode(first["nextCursor"], applied, indexed=True)
        for offset in (len(self.ssis.elements), len(self.ssis.elements) + 1):
            with self.subTest(offset=offset):
                token = encode(applied, offset, position.build_id)
                result = self.call(**arguments, cursor=token)
                self.assertEqual(result["error"]["code"], "invalid_request")
                self.assertEqual(result["error"]["details"]["parameter"], "cursor")
                self.assertNotIn("dataElements", result)

    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        settings = Settings(data_dir=Path(directory.name))
        self.ssis = FakeSSIS(settings)
        self.evs = FakeEVS([concept("C1", active=True), concept("C2", active=True)])
        self.context = Context(settings, evs=self.evs, ssis=self.ssis)
        self.addCleanup(patch.stopall)
        patch(
            "nci_sip_mcp.http_client._open", side_effect=AssertionError("Unexpected HTTP")
        ).start()

    def call(self, operation="find_data_elements_for_concept", **arguments):
        with cache_call() as hint:
            result = invoke(self.context, operation, _correlation_id="seam-test", **arguments)
        self.hint = hint
        return result

    def test_graph_uses_preserve_both_states_without_fabricating_registry_release(self):
        self.ssis.elements = [element(123)]
        result = self.call(conceptCode="C1", release="26.06e")
        self.assertEqual(
            result["dataElements"][0]["dataElement"],
            {"publicId": "123", "longName": "Element 123"},
        )
        provenance = result["dataElements"][0]["provenance"]
        self.assertEqual(provenance["registry"], {"registry": "cadsr"})
        self.assertEqual(provenance["release"]["identifier"], "26.06e")
        self.assertEqual(
            {row["graph"]: row["date"] for row in provenance["graphs"]},
            {NCIT_GRAPH: "2026-06-29", CADSR_GRAPH: "2026-06-01"},
        )
        self.assertEqual(provenance["upstream"]["graphs"], self.ssis.graphs)
        self.assertEqual(self.hint, {"ttlMs": 3_600_000, "cacheScope": "public"})
        self.assertNotIn("permissibleValues", result)

    def test_implicit_release_uses_private_zero_cache(self):
        result = self.call(conceptCode="C1")
        self.assertEqual(result["provenance"]["release"]["identifier"], "26.06e")
        self.assertEqual(self.hint, {"ttlMs": 0, "cacheScope": "private"})
        self.assertEqual(result["dataElements"], [])
        self.assertEqual(result["truncation"], {"occurred": False})

    def test_malformed_joined_identities_and_iris_are_errors_not_invented_content(self):
        for key, value in (("id", "0"), ("id", None), ("name", "")):
            with self.subTest(key=key):
                self.ssis.elements = [element(123) | {key: value}]
                result = self.call(conceptCode="C1")
                self.assertEqual(result["error"]["code"], "upstream_unavailable")
        self.ssis.elements = []
        self.ssis.values = [
            {"id": "123", "version": "1", "value": "Male", "concept": "https://other.test/C1"}
        ]
        result = self.call(conceptCode="C1", includePermissibleValues=True)
        self.assertEqual(result["error"]["details"]["surface"], "ssis")

    def test_thousand_element_boundary_keeps_unqueried_values_explicitly_incomplete(self):
        self.ssis.elements = [element(i) for i in range(1, 1001)]
        result = self.call(conceptCode="C1", includePermissibleValues=True, limit=1000)
        self.assertEqual(
            result["truncation"],
            {
                "occurred": True,
                "bound": "results",
                "limit": 1000,
                "reached": 1000,
                "omitted": 0,
                "exact": False,
            },
        )
        self.assertEqual(result["permissibleValues"], [])
        self.assertFalse(any(call[0] == "values" for call in self.ssis.calls))

    def test_graph_mismatch_fails_before_content_query(self):
        self.ssis.graphs[0]["version"] = "26.05d"
        result = self.call(conceptCode="C1", release="26.06e", expandDescendants=True)
        self.assertEqual(result["error"]["code"], "release_mismatch")
        self.assertEqual(
            result["error"]["details"],
            {"requested": "26.06e", "served": ["26.05d"], "source": NCIT_GRAPH},
        )
        self.assertEqual(self.ssis.calls, [("identities",)])

    def test_missing_graph_version_is_release_not_available(self):
        del self.ssis.graphs[0]["version"]
        result = self.call(conceptCode="C1", release="26.06e", expandDescendants=True)
        self.assertEqual(result["error"]["code"], "release_not_available")
        self.assertEqual(result["error"]["details"], {"requested": "26.06e", "source": NCIT_GRAPH})
        self.assertEqual(self.ssis.calls, [("identities",)])

    def test_one_stream_pages_elements_then_values_with_both_arrays_present(self):
        self.ssis.elements = [element(123), element(456)]
        self.ssis.values = [{"id": "123", "version": "1", "value": "Male", "concept": NCIT + "C1"}]
        arguments = {
            "conceptCode": "C1",
            "release": "26.06e",
            "includePermissibleValues": True,
            "limit": 1,
        }
        first = self.call(**arguments)
        second = self.call(**arguments, cursor=first["nextCursor"])
        third = self.call(**arguments, cursor=second["nextCursor"])
        self.assertEqual([first["permissibleValues"], second["permissibleValues"]], [[], []])
        self.assertEqual(third["dataElements"], [])
        self.assertEqual(third["permissibleValues"][0]["conceptCode"], "C1")
        self.assertNotIn("nextCursor", third)
        self.assertTrue(
            all(page["truncation"] == {"occurred": False} for page in (first, second, third))
        )
        self.assertIn(("values", "C1", False, 998), self.ssis.calls)

    def test_data_element_cap_skips_value_query_and_reports_one_cut(self):
        self.ssis.elements = [element(i) for i in range(1, 1002)]
        result = self.call(
            conceptCode="C1", release="26.06e", includePermissibleValues=True, limit=1000
        )
        self.assertEqual(len(result["dataElements"]), 1000)
        self.assertEqual(result["permissibleValues"], [])
        self.assertEqual(
            result["truncation"],
            {
                "occurred": True,
                "bound": "results",
                "limit": 1000,
                "reached": 1000,
                "omitted": 1,
                "exact": False,
            },
        )
        self.assertNotIn("nextCursor", result)
        self.assertFalse(any(call[0] == "values" for call in self.ssis.calls))

    def test_value_pages_cross_the_join_boundary_then_continue_without_duplicates(self):
        self.ssis.elements = [element(123)]
        self.ssis.values = [
            {"id": "123", "version": "1", "value": label, "concept": NCIT + "C1"}
            for label in ("A", "B", "C", "D")
        ]
        arguments = {"conceptCode": "C1", "includePermissibleValues": True, "limit": 2}
        first = self.call(**arguments)
        second = self.call(**arguments, cursor=first["nextCursor"])
        third = self.call(**arguments, cursor=second["nextCursor"])
        pages = (first, second, third)
        self.assertEqual(
            [[row["dataElement"]["publicId"] for row in page["dataElements"]] for page in pages],
            [["123"], [], []],
        )
        self.assertEqual(
            [[row["value"] for row in page["permissibleValues"]] for page in pages],
            [["A"], ["B", "C"], ["D"]],
        )
        self.assertNotIn("nextCursor", third)
        self.assertTrue(all(page["truncation"] == {"occurred": False} for page in pages))

    def test_value_query_uses_only_the_remaining_cap_and_retains_its_sentinel(self):
        self.ssis.elements = [element(i) for i in range(1, 1000)]
        self.ssis.values = [
            {"id": "123", "version": "1", "value": label, "concept": NCIT + "C1"}
            for label in ("A", "B", "C")
        ]
        result = self.call(
            conceptCode="C1", release="26.06e", includePermissibleValues=True, limit=1000
        )
        self.assertIn(("values", "C1", False, 1), self.ssis.calls)
        self.assertEqual(len(result["permissibleValues"]), 1)
        self.assertEqual(result["truncation"]["omitted"], 1)
        self.assertFalse(result["truncation"]["exact"])

    def test_changed_snapshot_expires_cursor(self):
        self.ssis.elements = [element(123), element(456)]
        first = self.call(conceptCode="C1", release="26.06e", limit=1)
        self.ssis.elements[1]["name"] = "Changed"
        result = self.call(conceptCode="C1", release="26.06e", limit=1, cursor=first["nextCursor"])
        self.assertEqual(result["error"]["code"], "cursor_expired")

    def test_permissible_value_identifier_is_explicitly_unsupported(self):
        result = self.call(
            "get_concept_for_permissible_value", permissibleValueId="123", release="26.06e"
        )
        self.assertEqual(result["error"]["code"], "capability_unavailable")
        self.assertEqual(self.ssis.calls, [])

    def test_invalid_selectors_fail_before_any_upstream_request(self):
        cases = [
            ("find_data_elements_for_concept", {"conceptCode": 'C1>"}'}),
            ("find_data_elements_for_concept", {"conceptCode": "C1", "limit": 0}),
            ("find_data_elements_for_concept", {"conceptCode": "C1", "expandDescendants": 1}),
            (
                "find_data_elements_for_concept",
                {"conceptCode": "C1", "includePermissibleValues": 1},
            ),
            ("find_data_elements_for_concept", {"conceptCode": "C1", "terminology": "mdr"}),
            ("find_data_elements_for_concept", {"conceptCode": "C1", "cursor": "bad"}),
            ("get_concept_for_permissible_value", {"dataElementId": '12>"}', "value": "x"}),
            ("get_concept_for_permissible_value", {"value": "x"}),
            ("get_concept_for_permissible_value", {"dataElementId": "12"}),
            (
                "get_concept_for_permissible_value",
                {"dataElementId": "12", "permissibleValueId": "13", "value": "x"},
            ),
            ("resolve_stored_value", {"conceptCode": "C1", "commons": " "}),
            (
                "resolve_stored_value",
                {"conceptCode": "C1", "commons": "PDC", "dataElementId": "bad"},
            ),
            ("get_release_alignment", {"maxIntervalDays": True}),
            ("get_release_alignment", {"maxIntervalDays": -1}),
        ]
        for operation, arguments in cases:
            with self.subTest(operation=operation, arguments=arguments):
                self.assertEqual(
                    self.call(operation, **arguments)["error"]["code"], "invalid_request"
                )
        self.assertEqual(self.ssis.calls, [])
        self.assertEqual(self.evs.calls, [])

    def test_gdc_selector_is_not_silently_ignored(self):
        result = self.call(
            "resolve_stored_value", conceptCode="C1", commons="GDC", dataElementId="123"
        )
        self.assertEqual(result["error"]["code"], "capability_unavailable")
        self.assertEqual(self.evs.calls, [])

    def test_gdc_keeps_only_exact_concept_maps_and_counts_returned_values(self):
        self.context.evs = EVSClient("https://evs.test", user_agent=product_user_agent())
        mapset = {"code": "NCIt_Maps_To_GDC", "version": "26.06e"}
        common = {
            "mapsetCode": mapset["code"],
            "source": "ncit",
            "target": "GDC",
            "sourceTerminologyVersion": "26.06e",
            "targetCode": "diagnosis",
            "licenseText": "Upstream map attribution",
        }
        rows = [
            common | {"sourceCode": code, "targetName": label}
            for code, label in (("C4817", "Ewing tumor"), ("C48177", "processing"))
        ]
        with patch.object(
            self.context.evs,
            "_get_existing",
            side_effect=[self.evs.get_terminologies("ncit"), mapset, {"total": 2, "maps": rows}],
        ):
            result = self.call(
                "resolve_stored_value", conceptCode="C4817", commons="GDC", release="26.06e"
            )
        self.assertEqual(
            [(row["value"], row["field"]) for row in result["storedValues"]],
            [("Ewing tumor", "diagnosis")],
        )
        self.assertEqual(
            result["evidence"],
            {
                "sources": [{"mapset": mapset["code"], "version": "26.06e"}],
                "valueLevelBinding": True,
                "coverage": 1,
            },
        )
        self.assertEqual(result["confidence"], "asserted")
        self.assertEqual(
            result["storedValues"][0]["provenance"]["attribution"], "Upstream map attribution"
        )

    def test_gdc_malformed_licence_text_is_not_silently_discarded(self):
        self.context.evs = EVSClient("https://evs.test", user_agent=product_user_agent())
        mapset = {"code": "NCIt_Maps_To_GDC", "version": "26.06e", "licenseText": {"bad": "shape"}}
        with patch.object(
            self.context.evs, "_get_existing", side_effect=[self.evs.get_terminologies(), mapset]
        ):
            result = self.call(
                "resolve_stored_value", conceptCode="C1", commons="GDC", release="26.06e"
            )
        self.assertEqual(result["error"]["code"], "upstream_unavailable")
        self.assertEqual(result["error"]["details"]["surface"], "evs")

    def test_gdc_reads_later_pages_before_filtering_and_counting_exact_matches(self):
        self.context.evs = EVSClient("https://evs.test", user_agent=product_user_agent())
        pages = [
            {"total": 11, "maps": [gdc_map(f"C1{i}") for i in range(10)]},
            {"total": 11, "maps": [gdc_map()]},
        ]
        with patch.object(
            self.context.evs,
            "_get_existing",
            side_effect=[
                self.evs.get_terminologies(),
                {"code": "NCIt_Maps_To_GDC", "version": "26.06e"},
                *pages,
            ],
        ) as get:
            result = self.call(
                "resolve_stored_value", conceptCode="C1", commons="GDC", release="26.06e"
            )
        self.assertEqual([row["value"] for row in result["storedValues"]], ["Stored C1"])
        self.assertEqual(result["evidence"]["coverage"], 1)
        self.assertEqual([call.args[1]["fromRecord"] for call in get.call_args_list[-2:]], [0, 10])

    def test_gdc_changing_totals_and_over_bound_results_fail_without_partial_success(self):
        first_page = [gdc_map() for _ in range(10)]
        cases = (
            (
                [{"total": 11, "maps": first_page}, {"total": 12, "maps": [gdc_map()] * 2}],
                "upstream_unavailable",
            ),
            ([{"total": 1001, "maps": first_page}], "bound_exceeded"),
        )
        self.context.evs = EVSClient("https://evs.test", user_agent=product_user_agent())
        for pages, expected in cases:
            with (
                self.subTest(expected=expected),
                patch.object(
                    self.context.evs,
                    "_get_existing",
                    side_effect=[
                        self.evs.get_terminologies(),
                        {"code": "NCIt_Maps_To_GDC", "version": "26.06e"},
                        *pages,
                    ],
                ),
            ):
                result = self.call(
                    "resolve_stored_value", conceptCode="C1", commons="GDC", release="26.06e"
                )
                self.assertEqual(result["error"]["code"], expected)
                self.assertNotIn("storedValues", result)

    def test_gdc_individual_maps_must_name_the_requested_release(self):
        self.context.evs = EVSClient("https://evs.test", user_agent=product_user_agent())
        for version in ("26.05d", None):
            with (
                self.subTest(version=version),
                patch.object(
                    self.context.evs,
                    "_get_existing",
                    side_effect=[
                        self.evs.get_terminologies(),
                        {"code": "NCIt_Maps_To_GDC", "version": "26.06e"},
                        {"total": 1, "maps": [gdc_map(version=version)]},
                    ],
                ),
            ):
                result = self.call(
                    "resolve_stored_value", conceptCode="C1", commons="GDC", release="26.06e"
                )
                self.assertEqual(result["error"]["code"], "release_mismatch")
                self.assertEqual(result["error"]["details"]["served"], [version or "unknown"])

    def test_gdc_checks_mapset_release_before_requesting_maps(self):
        self.context.evs = EVSClient("https://evs.test", user_agent=product_user_agent())
        with patch.object(
            self.context.evs,
            "_get_existing",
            side_effect=[
                self.evs.get_terminologies("ncit"),
                {"code": "NCIt_Maps_To_GDC", "version": "26.05d"},
            ],
        ):
            result = self.call(
                "resolve_stored_value", conceptCode="C1", commons="GDC", release="26.06e"
            )
        self.assertEqual(result["error"]["code"], "release_mismatch")
        self.assertEqual(result["error"]["details"]["served"], ["26.05d"])

    def crosswalk(self):
        return [
            {
                "CDE Public ID": identifier,
                "Version": "1",
                "CRDC Name": "sex",
                "Used By": users,
                "permissibleValues": [{"Permissible Value": "Male", "Concept Code": "C9:C1"}],
            }
            for identifier, users in (
                ("123", "Other, Pediatric Cancer"),
                ("456", "Pediatric Cancer Extra"),
            )
        ]

    def test_crosswalk_matches_complete_context_and_colon_separated_binding(self):
        with patch.object(self.context.cadsr, "get_crdc_list", return_value=self.crosswalk()):
            result = self.call(
                "resolve_stored_value",
                conceptCode="C1",
                commons="Pediatric Cancer",
                release="26.06e",
            )
        self.assertEqual(
            [
                (row["value"], row["field"], row["source"]["dataElement"]["publicId"])
                for row in result["storedValues"]
            ],
            [("Male", "sex", "123")],
        )
        self.assertEqual(result["evidence"]["coverage"], 1)
        self.assertTrue(result["evidence"]["valueLevelBinding"])

    def test_crosswalk_selector_and_missing_binding_return_honest_empty_results(self):
        for commons, identifier in (("PDC", None), ("Pediatric Cancer", "456")):
            with (
                self.subTest(commons=commons),
                patch.object(self.context.cadsr, "get_crdc_list", return_value=self.crosswalk()),
            ):
                result = self.call(
                    "resolve_stored_value",
                    conceptCode="C1",
                    commons=commons,
                    dataElementId=identifier,
                    release="26.06e",
                )
                self.assertEqual(result["storedValues"], [])
                self.assertEqual(result["confidence"], "none")
                self.assertEqual(
                    result["evidence"], {"sources": [], "valueLevelBinding": False, "coverage": 0}
                )
                self.assertEqual(result["provenance"]["registry"], {"registry": "cadsr"})

    def test_alignment_warns_only_above_threshold_and_preserves_graph_difference(self):
        interval = 28
        self.ssis.graphs[0]["version"] = "26.05d"
        listing = '<pre><a href="releasedCDEsXML-OD.zip">export</a> 2026-06-05 12:30\n</pre>'
        for threshold in (27, 28, 29):
            with (
                self.subTest(threshold=threshold),
                patch.object(self.context.cadsr.export_http, "get_text", return_value=listing),
            ):
                result = self.call("get_release_alignment", maxIntervalDays=threshold)
                self.assertEqual(result["intervalDays"], 28)
                self.assertEqual("warning" in result, threshold < interval)
                self.assertEqual(self.hint, {"ttlMs": 0, "cacheScope": "public"})
                datasets = {row["name"]: row for row in result["datasets"]}
                self.assertEqual(datasets["ssis_ncit_graph"]["version"], "26.05d")
                self.assertEqual(
                    datasets["ssis_ncit_graph"]["provenance"]["release"]["identifier"], "26.05d"
                )
                self.assertNotIn("version", datasets["cadsr_export"])
                self.assertEqual(datasets["cadsr_export"]["date"], "2026-06-05")

    def test_alignment_reads_current_evs_without_using_or_changing_a_session_pin(self):
        held = release("25.01d", "2025-01-27")
        state = SessionRelease(selected=held)
        listing = '<pre><a href="releasedCDEsXML-OD.zip">export</a> 2026-06-05 12:30\n</pre>'
        with (
            session_scope(state),
            patch.object(self.context.cadsr.export_http, "get_text", return_value=listing),
        ):
            result = self.call("get_release_alignment")
        ncit = next(row for row in result["datasets"] if row["name"] == "ncit")
        self.assertEqual((ncit["version"], ncit["date"]), ("26.06e", "2026-06-29"))
        self.assertEqual(state.selected, held)

    def test_alignment_does_not_select_the_first_implicit_session_content_release(self):
        state = SessionRelease()
        listing = '<pre><a href="releasedCDEsXML-OD.zip">export</a> 2026-06-05 12:30\n</pre>'
        with (
            session_scope(state),
            patch.object(self.context.cadsr.export_http, "get_text", return_value=listing),
        ):
            result = self.call("get_release_alignment")
        self.assertEqual(result["datasets"][0]["version"], "26.06e")
        self.assertIsNone(state.selected)

    def test_alignment_preserves_the_evs_identity_as_upstream_evidence(self):
        listing = '<pre><a href="releasedCDEsXML-OD.zip">export</a> 2026-06-05 12:30\n</pre>'
        with patch.object(self.context.cadsr.export_http, "get_text", return_value=listing):
            result = self.call("get_release_alignment")
        ncit = next(row for row in result["datasets"] if row["name"] == "ncit")
        upstream = ncit["provenance"]["upstream"]
        self.assertEqual(
            (upstream["terminology"], upstream["version"], upstream["date"]),
            ("ncit", "26.06e", "2026-06-29"),
        )

    def test_alignment_unreadable_dates_name_the_surface_that_supplied_them(self):
        listing = '<pre><a href="releasedCDEsXML-OD.zip">export</a> 2026-06-05 12:30\n</pre>'
        self.evs.release = release("26.06e", "not a date")
        with patch.object(self.context.cadsr.export_http, "get_text", return_value=listing):
            result = self.call("get_release_alignment")
        self.assertEqual(result["error"]["code"], "upstream_unavailable")
        self.assertEqual(result["error"]["details"]["surface"], "evs")

        self.evs.release = release("26.06e", "2026-06-29")
        stale = SimpleNamespace(generated_at="not a date", source_distribution=None)
        with (
            patch.object(self.context.cadsr.export_http, "get_text", return_value=listing),
            patch("nci_sip_mcp.seam.export_state", return_value=stale),
        ):
            result = self.call("get_release_alignment")
        self.assertEqual(result["error"]["code"], "upstream_unavailable")
        self.assertEqual(result["error"]["details"]["surface"], "cadsr")

    def test_alignment_missing_ncit_graph_version_never_becomes_a_registry_identity(self):
        del self.ssis.graphs[0]["version"]
        listing = '<pre><a href="releasedCDEsXML-OD.zip">export</a> 2026-06-05 12:30\n</pre>'
        with patch.object(self.context.cadsr.export_http, "get_text", return_value=listing):
            result = self.call("get_release_alignment")
        self.assertEqual(result["error"]["code"], "release_not_available")
        self.assertEqual(result["error"]["details"]["source"], NCIT_GRAPH)


def bindings(rows):
    return {
        "results": {
            "bindings": [
                {key: {"type": "literal", "value": value} for key, value in row.items()}
                for row in rows
            ]
        }
    }


class CrossDomainQueryBoundaryTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        settings = Settings(data_dir=Path(directory.name))
        self.ssis = FakeSSIS(settings)
        self.evs = FakeEVS([concept("C1", active=True), concept("C2", active=True)])
        self.context = Context(settings, evs=self.evs, ssis=self.ssis)
        blocker = patch(
            "nci_sip_mcp.http_client._open", side_effect=AssertionError("Unexpected HTTP")
        )
        blocker.start()
        self.addCleanup(blocker.stop)

    def call(self, operation="find_data_elements_for_concept", **arguments):
        with cache_call() as hint:
            result = invoke(self.context, operation, _correlation_id="seam-query", **arguments)
        self.hint = hint
        return result

    def resolve(self, **arguments):
        with patch.object(self.context.cadsr, "get_data_element", return_value=self.api_element):
            return self.call(
                "get_concept_for_permissible_value",
                **({"dataElementId": "123", "value": "Male", "release": "26.06e"} | arguments),
            )

    def test_descendant_option_reaches_both_queries_and_binds_the_cursor(self):
        client = SSISClient(self.context.settings, user_agent=product_user_agent())
        self.context.ssis = client
        rows = [element(123), element(456)]
        values = [{"id": "123", "version": "1", "value": "Male", "concept": NCIT + "C1"}]
        arguments = {
            "conceptCode": "C1",
            "expandDescendants": True,
            "includePermissibleValues": True,
        }
        with (
            patch.object(client, "get_graph_identities", return_value=self.ssis.graphs),
            patch.object(
                client.sparql_http, "post_form", side_effect=[bindings(rows), bindings(values)]
            ) as post,
        ):
            first = self.call(**arguments, limit=1)
        self.assertEqual(first["dataElements"][0]["dataElement"]["publicId"], "123")
        self.assertEqual(first["permissibleValues"], [])
        for request in post.call_args_list:
            self.assertIn("rdfs:subClassOf* ncit:C1", request.args[1]["query"])
        changed = self.call(
            **(arguments | {"expandDescendants": False}), limit=1, cursor=first["nextCursor"]
        )
        self.assertEqual(changed["error"]["code"], "invalid_request")
        self.assertEqual(changed["error"]["details"]["parameter"], "cursor")

    def test_default_value_flag_skips_the_query_and_exact_cap_is_complete(self):
        self.ssis.elements = [element(i) for i in range(1, 1001)]
        self.ssis.values = [{"id": "1", "version": "1", "value": "M", "concept": NCIT + "C1"}]
        result = self.call(conceptCode="C1", limit=1000)
        self.assertEqual(len(result["dataElements"]), 1000)
        self.assertEqual(result["truncation"], {"occurred": False})
        self.assertNotIn("permissibleValues", result)
        self.assertNotIn("nextCursor", result)
        self.assertEqual(self.ssis.calls, [("identities",), ("elements", "C1", False, 1000)])

    def test_exact_shared_cap_including_values_is_complete(self):
        self.ssis.elements = [element(i) for i in range(1, 1000)]
        self.ssis.values = [{"id": "1", "version": "1", "value": "M", "concept": NCIT + "C1"}]
        result = self.call(conceptCode="C1", includePermissibleValues=True, limit=1000)
        self.assertEqual(len(result["dataElements"]), 999)
        self.assertEqual(result["permissibleValues"][0]["value"], "M")
        self.assertEqual(result["permissibleValues"][0]["conceptTerminology"], "ncit")
        self.assertEqual(result["truncation"], {"occurred": False})
        self.assertNotIn("nextCursor", result)

    def test_default_page_limit_is_one_hundred(self):
        self.ssis.elements = [element(i) for i in range(1, 102)]
        first = self.call(conceptCode="C1")
        second = self.call(conceptCode="C1", cursor=first["nextCursor"])
        self.assertEqual(len(first["dataElements"]), 100)
        self.assertEqual(
            [row["dataElement"]["publicId"] for row in second["dataElements"]], ["101"]
        )
        self.assertEqual(second["truncation"], {"occurred": False})
        self.assertNotIn("nextCursor", second)

    def test_above_maximum_page_limit_clamps_as_required_by_spec(self):
        self.ssis.elements = [element(i) for i in range(1, 1002)]
        result = self.call(conceptCode="C1", limit=1001)
        self.assertEqual(len(result["dataElements"]), 1000)
        self.assertEqual(result["dataElements"][-1]["dataElement"]["publicId"], "1000")
        self.assertEqual(result["truncation"]["limit"], 1000)
        self.assertEqual(result["truncation"]["omitted"], 1)
        self.assertNotIn("nextCursor", result)

    def test_last_page_cannot_leak_the_sentinel_when_its_limit_overshoots(self):
        self.ssis.elements = [element(i) for i in range(1, 1002)]
        first = self.call(conceptCode="C1", limit=200)
        # A valid continuation position can be resumed without being a page-size multiple.
        token = json.loads(base64.urlsafe_b64decode(first["nextCursor"]))
        token["offset"] = 900
        cursor = base64.urlsafe_b64encode(json.dumps(token).encode()).decode()
        final = self.call(conceptCode="C1", limit=200, cursor=cursor)
        self.assertEqual(
            [row["dataElement"]["publicId"] for row in final["dataElements"]],
            [str(i) for i in range(901, 1001)],
        )
        self.assertEqual(final["truncation"]["omitted"], 1)
        self.assertNotIn("nextCursor", final)

    def test_continuations_compare_an_above_maximum_limit_as_applied(self):
        self.ssis.elements = [element(i) for i in range(1, 1002)]
        first = self.call(conceptCode="C1", limit=200)
        token = json.loads(base64.urlsafe_b64decode(first["nextCursor"]))
        # A maximum-sized first page fills the total cap and issues no cursor.
        # Construct a valid continuation to exercise applied-limit comparison itself.
        token["arguments"]["limit"] = 1000
        token["offset"] = 900
        cursor = base64.urlsafe_b64encode(json.dumps(token).encode()).decode()
        for limit in (1000, 1001):
            with self.subTest(limit=limit):
                result = self.call(conceptCode="C1", limit=limit, cursor=cursor)
                self.assertEqual(
                    [row["dataElement"]["publicId"] for row in result["dataElements"]],
                    [str(i) for i in range(901, 1001)],
                )
                self.assertEqual(result["truncation"]["limit"], 1000)
                self.assertNotIn("nextCursor", result)

    def test_changed_value_rows_expire_a_cursor_even_when_elements_are_unchanged(self):
        self.ssis.elements = [element(123)]
        self.ssis.values = [{"id": "123", "version": "1", "value": "Male", "concept": NCIT + "C1"}]
        arguments = {"conceptCode": "C1", "includePermissibleValues": True, "limit": 1}
        first = self.call(**arguments)
        self.ssis.values[0]["value"] = "Changed"
        result = self.call(**arguments, cursor=first["nextCursor"])
        self.assertEqual(result["error"]["code"], "cursor_expired")

    def test_value_only_pages_keep_provenance_on_the_use_not_the_top_level(self):
        self.ssis.values = [{"id": "123", "version": "1", "value": "Male", "concept": NCIT + "C1"}]
        result = self.call(conceptCode="C1", includePermissibleValues=True)
        self.assertEqual(result["dataElements"], [])
        self.assertNotIn("provenance", result)
        self.assertEqual(
            result["permissibleValues"][0]["provenance"]["upstream"]["graphs"], self.ssis.graphs
        )

    def test_pv_preserves_whitespace_in_an_exactly_matching_value(self):
        self.api_element = primary_element(
            publicId="123",
            value=" Male ",
            codes=[
                {"conceptCode": "C1", "evsSource": "NCI_CONCEPT_CODE", "primaryIndicator": "Yes"}
            ],
        )
        result = self.resolve(value=" Male ")
        self.assertEqual(result["code"], "C1")
        self.assertEqual(result["permissibleValue"]["value"], " Male ")

    def test_pv_empty_data_element_has_no_matching_value(self):
        self.api_element = primary_element(publicId="123")
        self.api_element["ValueDomain"]["PermissibleValues"] = []
        result = self.resolve()
        self.assertEqual(result["error"]["code"], "not_found")

    def test_pv_wrong_evs_concept_identity_is_an_evs_failure(self):
        self.api_element = primary_element(
            publicId="123",
            value="Male",
            codes=[
                {"conceptCode": "C1", "evsSource": "NCI_CONCEPT_CODE", "primaryIndicator": "Yes"}
            ],
        )
        with patch.object(self.evs, "get_concept", return_value=concept("C2", active=True)):
            result = self.resolve()
        self.assertEqual(result["error"]["code"], "upstream_unavailable")
        self.assertEqual(result["error"]["details"], {"surface": "evs"})

    def test_pv_explicit_release_keeps_public_cache_hint(self):
        self.api_element = primary_element(
            publicId="123",
            value="Male",
            codes=[
                {"conceptCode": "C1", "evsSource": "NCI_CONCEPT_CODE", "primaryIndicator": "Yes"}
            ],
        )
        result = self.resolve()
        self.assertEqual(result["code"], "C1")
        self.assertEqual(self.hint, {"ttlMs": 3_600_000, "cacheScope": "public"})

    def test_unresolved_pv_explicit_release_keeps_public_cache_hint(self):
        self.api_element = primary_element(
            publicId="123",
            value="Male",
            codes=[
                {
                    "conceptCode": "C0009219",
                    "evsSource": "NCI_META_CUI",
                    "primaryIndicator": "Yes",
                }
            ],
        )
        # The outer producer must declare its policy even without the child's hint.
        with patch("nci_sip_mcp.cadsr_content.select_cache_hint"):
            result = self.resolve()
        self.assertEqual(
            result["conceptReference"],
            {"conceptCode": "C0009219", "terminology": "NCI_META_CUI"},
        )
        self.assertEqual(result["unresolvedReason"], "non_ncit_source")
        self.assertEqual(self.hint, {"ttlMs": 3_600_000, "cacheScope": "public"})

    def test_pv_identifier_rejects_either_other_selector_before_upstream(self):
        for extra in ({"dataElementId": "123"}, {"value": "Male"}):
            with self.subTest(extra=extra):
                result = self.call(
                    "get_concept_for_permissible_value", permissibleValueId="456", **extra
                )
                self.assertEqual(result["error"]["code"], "invalid_request")
                self.assertEqual(result["error"]["details"]["parameter"], "permissibleValueId")
        unsupported = self.call("get_concept_for_permissible_value", permissibleValueId="456")
        self.assertEqual(unsupported["error"]["code"], "capability_unavailable")
        self.assertEqual(unsupported["error"]["details"], {"capability": "OP-C10"})
        self.assertEqual(self.ssis.calls, [])
        self.assertEqual(self.evs.calls, [])

    def test_pv_malformed_version_is_a_structured_upstream_failure(self):
        self.api_element = primary_element(publicId="123", version="1.2.3")
        result = self.resolve()
        self.assertEqual(result["error"]["code"], "upstream_unavailable")
        self.assertEqual(result["error"]["details"], {"surface": "cadsr"})

    def test_ambiguous_candidates_are_listed_in_sorted_order(self):
        self.api_element = primary_element(
            publicId="123",
            value="Male",
            codes=[
                {"conceptCode": code, "evsSource": "NCI_CONCEPT_CODE", "primaryIndicator": "Yes"}
                for code in ("C9", "C2", "C10", "C1")
            ],
        )
        result = self.resolve()
        self.assertEqual(
            result["error"]["details"],
            {"surface": "cadsr", "candidates": ["C1", "C10", "C2", "C9"]},
        )


def crosswalk_row(identifier, bindings):
    return {
        "CDE Public ID": str(identifier),
        "Version": "1",
        "CRDC Name": "diagnosis",
        "Used By": "PDC",
        "permissibleValues": [
            {"Permissible Value": value, "Concept Code": code} for value, code in bindings
        ],
    }


class StoredValueAndAlignmentTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        settings = Settings(data_dir=Path(directory.name))
        self.evs = FakeEVS()
        self.ssis = FakeSSIS(settings)
        self.context = Context(settings, evs=self.evs, ssis=self.ssis)
        opened = patch(
            "nci_sip_mcp.http_client._open", side_effect=AssertionError("Unexpected HTTP")
        )
        opened.start()
        self.addCleanup(opened.stop)

    def stored(self, commons="PDC", **arguments):
        return invoke(
            self.context,
            "resolve_stored_value",
            conceptCode="C1",
            commons=commons,
            release="26.06e",
            **arguments,
        )

    def gdc(self, rows, total=None):
        self.context.evs = EVSClient("https://evs.test", user_agent=product_user_agent())
        count = len(rows) if total is None else total
        pages = [
            {"total": count, "maps": rows[start : start + 10]} for start in range(0, len(rows), 10)
        ]
        with patch.object(
            self.context.evs,
            "_get_existing",
            side_effect=[
                self.evs.get_terminologies(),
                {"code": "NCIt_Maps_To_GDC", "version": "26.06e"},
                *pages,
            ],
        ):
            return self.stored("GDC")

    def alignment(
        self,
        ncit="2026-06-01",
        graph="2026-06-01",
        cadsr="2026-06-01",
        export="2026-06-01",
        **arguments,
    ):
        self.evs.release = release("26.06e", ncit)
        self.ssis.graphs[0]["date"] = graph
        self.ssis.graphs[1]["date"] = cadsr
        listing = f'<pre><a href="releasedCDEsXML-OD.zip">export</a> {export} 12:30\n</pre>'
        with patch.object(self.context.cadsr.export_http, "get_text", return_value=listing) as get:
            result = invoke(self.context, "get_release_alignment", **arguments)
        self.export_path = get.call_args.args[0]
        return result

    def test_crosswalk_exact_colon_tokens_preserve_all_values_and_count_values_not_sources(self):
        rows = [
            crosswalk_row(123, [("first", "C1:C9"), ("second", "C9:C1"), ("prefix", "C10:C11")]),
            crosswalk_row(456, [("third", "C1"), ("fourth", "C1")]),
            crosswalk_row(789, [("unrelated", "C10")]),
        ]
        with patch.object(self.context.cadsr, "get_crdc_list", return_value=rows) as read:
            result = self.stored()
        self.assertEqual(
            [value["value"] for value in result["storedValues"]],
            ["first", "second", "third", "fourth"],
        )
        self.assertEqual(
            result["evidence"],
            {
                "sources": [
                    {"crosswalk": "CRDC", "dataElement": {"publicId": str(i), "version": "1"}}
                    for i in (123, 456, 789)
                ],
                "coverage": 4,
                "valueLevelBinding": True,
            },
        )
        self.assertEqual(result["confidence"], "asserted")
        self.assertNotIn("provenance", result)
        self.assertIsNone(read.call_args.kwargs["registry_release"])
        self.assert_crosswalk_provenance(result["storedValues"])

    def assert_crosswalk_provenance(self, values):
        for value in values:
            provenance = value["provenance"]
            self.assertEqual(
                provenance["release"],
                {"terminology": "ncit", "identifier": "26.06e", "date": "2026-06-29"},
            )
            self.assertEqual(provenance["registry"], {"registry": "cadsr"})
            self.assertEqual(provenance["source"], "cadsr_rest")
            self.assertIn("/DataElements/getCRDCList", provenance["sourceUri"])

    def test_matching_crosswalk_without_matching_values_retains_its_evidence(self):
        for bindings, binding in (([("prefix", "C10:C11")], True), ([], False)):
            with (
                self.subTest(binding=binding),
                patch.object(
                    self.context.cadsr, "get_crdc_list", return_value=[crosswalk_row(123, bindings)]
                ),
            ):
                result = self.stored()
                self.assertEqual(result["storedValues"], [])
                self.assertEqual(result["confidence"], "none")
                self.assertEqual(
                    result["evidence"],
                    {
                        "sources": [
                            {
                                "crosswalk": "CRDC",
                                "dataElement": {"publicId": "123", "version": "1"},
                            }
                        ],
                        "coverage": 0,
                        "valueLevelBinding": binding,
                    },
                )
                self.assertEqual(result["provenance"]["registry"], {"registry": "cadsr"})

    def test_whitespace_only_crosswalk_field_is_malformed(self):
        row = crosswalk_row(123, [("literal", "C1")]) | {"CRDC Name": "  "}
        with patch.object(self.context.cadsr, "get_crdc_list", return_value=[row]):
            result = self.stored()
        self.assertEqual(result["error"]["code"], "upstream_unavailable")
        self.assertEqual(result["error"]["details"]["surface"], "cadsr")

    def test_gdc_rejects_malformed_matching_map_identity_and_literal_types(self):
        changes = [
            ("mapsetCode", "other"),
            ("source", "other"),
            ("target", "other"),
            ("targetCode", 7),
            ("targetName", None),
        ]
        for key, value in changes:
            with self.subTest(key=key):
                result = self.gdc([gdc_map() | {key: value}])
                self.assertEqual(result["error"]["code"], "upstream_unavailable")
                self.assertEqual(result["error"]["details"]["surface"], "evs")
                self.assertNotIn("storedValues", result)

    def test_malformed_nonmatching_source_code_is_not_silently_filtered(self):
        for code in (None, 12, "not-a-code", "C0", "C1 "):
            with self.subTest(code=code):
                bad = gdc_map("C10")
                bad.pop("sourceCode")
                if code is not None:
                    bad["sourceCode"] = code
                result = self.gdc([gdc_map(), bad])
                self.assertEqual(result["error"]["code"], "upstream_unavailable")
                self.assertEqual(result["error"]["details"]["surface"], "evs")

    def test_gdc_exactly_thousand_rows_is_complete_and_overflow_details_are_truthful(self):
        result = self.gdc([gdc_map() for _ in range(1000)])
        self.assertEqual(len(result["storedValues"]), 1000)
        self.assertEqual(result["evidence"]["coverage"], 1000)
        self.assertNotIn("provenance", result)
        result = self.gdc([gdc_map() for _ in range(10)], total=1001)
        self.assertEqual(result["error"]["code"], "bound_exceeded")
        self.assertEqual(
            result["error"]["details"], {"bound": "results", "limit": 1000, "reached": 10}
        )

    def test_gdc_literal_provenance_names_the_mapset_and_retains_its_row(self):
        row = gdc_map() | {"targetName": " literal "}
        result = self.gdc([row])
        value = result["storedValues"][0]
        self.assertEqual(value["value"], " literal ")
        self.assertEqual(value["provenance"]["upstream"], row)
        self.assertEqual(value["provenance"]["source"], "evs_rest")
        self.assertEqual(
            value["provenance"]["sourceUri"], "https://evs.test/api/v1/mapset/NCIt_Maps_To_GDC"
        )

    def test_invalid_stored_concept_is_rejected_before_any_request(self):
        for code in ("C", "0", "C01", 'C1>"}'):
            with self.subTest(code=code):
                result = invoke(
                    self.context, "resolve_stored_value", conceptCode=code, commons="GDC"
                )
                self.assertEqual(result["error"]["code"], "invalid_request")
                self.assertEqual(result["error"]["details"]["parameter"], "conceptCode")
        self.assertEqual(self.evs.calls, [])
        self.assertEqual(self.ssis.calls, [])

    def test_both_state_tools_share_discovery_and_content_request_allowance(self):
        self.context.evs = EVSClient(
            "https://evs.test", max_attempts=1, user_agent=product_user_agent()
        )
        self.context.ssis = SSISClient(self.context.settings, user_agent=product_user_agent())
        for operation, arguments in (
            ("resolve_stored_value", {"conceptCode": "C1", "commons": "GDC"}),
            ("get_release_alignment", {}),
        ):
            with (
                self.subTest(operation=operation),
                patch("nci_sip_mcp.seam.Budget", return_value=Budget(requests=1)),
                patch(
                    "nci_sip_mcp.http_client._open",
                    side_effect=[FakeResponse(json.dumps([terminology_row()]).encode())],
                ) as opened,
            ):
                result = invoke(self.context, operation, **arguments)
                self.assertEqual(result["error"]["code"], "bound_exceeded")
                self.assertEqual(
                    result["error"]["details"], {"bound": "requests", "limit": 1, "reached": 1}
                )
                self.assertEqual(opened.call_count, 1)

    def test_alignment_accepts_zero_and_warns_for_a_single_day_difference(self):
        result = self.alignment(maxIntervalDays=0)
        self.assertEqual(result["intervalDays"], 0)
        self.assertNotIn("warning", result)
        result = self.alignment(export="2026-06-02", maxIntervalDays=0)
        self.assertEqual(result["intervalDays"], 1)
        self.assertIn("1 days", result["warning"])
        self.assertIn("maxIntervalDays 0", result["warning"])

    def test_alignment_default_threshold_is_31_and_warning_names_observed_interval(self):
        result = self.alignment(export="2026-07-02")
        self.assertEqual(result["intervalDays"], 31)
        self.assertNotIn("warning", result)
        result = self.alignment(export="2026-07-03")
        self.assertEqual(result["intervalDays"], 32)
        self.assertIn("32 days", result["warning"])
        self.assertIn("maxIntervalDays 31", result["warning"])

    def test_each_dataset_can_determine_the_largest_interval(self):
        for dataset in ("ncit", "graph", "cadsr", "export"):
            with self.subTest(dataset=dataset):
                result = self.alignment(**{dataset: "2026-05-01"})
                self.assertEqual(result["intervalDays"], 31)
                result = self.alignment(**{dataset: "2026-07-10"})
                self.assertEqual(result["intervalDays"], 39)

    def test_alignment_registry_states_preserve_export_evidence_without_invented_release(self):
        result = self.alignment()
        self.assertEqual(self.export_path, "/CDE/XML/")
        datasets = {row["name"]: row for row in result["datasets"]}
        self.assertEqual(
            set(datasets), {"ncit", "ssis_ncit_graph", "ssis_cadsr_graph", "cadsr_export"}
        )
        for name in ("ssis_cadsr_graph", "cadsr_export"):
            self.assertEqual(datasets[name]["provenance"]["release"], {"registry": "cadsr"})
        export = datasets["cadsr_export"]
        self.assertNotIn("version", export)
        self.assertEqual(
            export["provenance"]["upstream"],
            {"generatedAt": "2026-06-01T12:30", "sourceDistribution": "releasedCDEsXML-OD.zip"},
        )
        self.assertEqual(
            export["provenance"]["sourceUri"], self.context.cadsr.export_http.url(EXPORT_FOLDER)
        )
        self.assertEqual(export["provenance"]["source"], "cadsr_export")
