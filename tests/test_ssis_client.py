"""Shared SI contracts: recorded requests and malformed/empty distinctions, offline.

The bounded identity template adds LIMIT 3 to the original recording;
its response is reused offline, not represented as a new live recording.
"""

import copy
import json
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs

from nci_sip_mcp.config import Settings
from nci_sip_mcp.errors import InputValidationError, PlatformError
from nci_sip_mcp.http_client import UpstreamTimeoutError, UpstreamTooLargeError
from nci_sip_mcp.invocation import call
from nci_sip_mcp.ssis import CADSR_GRAPH, NCIT_GRAPH, SSISClient
from nci_sip_mcp.traffic_identity import product_user_agent
from test_evs_client import FakeResponse
from test_http_client import Reply, ServerTestCase

FIXTURES = Path(__file__).resolve().parents[1] / "acceptance/fixtures/recorded"


def recording(name):
    return json.loads((FIXTURES / name).read_text())


def sparql_reply(rows):
    return Reply(body=json.dumps({"results": {"bindings": rows}}).encode())


def without_roles(rows):
    return [{key: value for key, value in row.items() if key != "roles"} for row in rows]


def plain_rows(rows):
    return [{key: value["value"] for key, value in row.items()} for row in rows]


class SSISClientTest(ServerTestCase):
    def test_distinct_public_id_queries_apply_the_bound_after_the_corrected_keys(self):
        """Given duplicate version assertions, the database must bound public-ID associations."""
        prefixes = """PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX owl: <http://www.w3.org/2002/07/owl#>
PREFIX dc: <http://purl.org/dc/elements/1.1/>
PREFIX mdr: <http://www.iso.org/11179/MDR#>
PREFIX cadsr: <http://cbiit.nci.nih.gov/caDSR#>
PREFIX ncit: <http://ncicb.nci.nih.gov/xml/owl/EVS/Thesaurus.owl#>"""
        elements = recording("ssis-sparql/data-elements-statuses-c17357.json")["request"]["form"][
            "query"
        ]
        elements = (
            elements.removeprefix(prefixes + "\n")
            .replace("VALUES ?concept { ncit:C17357 }", "CONCEPT_CLAUSE")
            .replace("LIMIT 1001", "LIMIT 3")
        )
        values = """SELECT DISTINCT ?id ?value ?concept WHERE {
CONCEPT_CLAUSE
GRAPH <http://cbiit.nci.nih.gov/caDSR> {
VALUES ?role { cadsr:main_concept cadsr:minor_concept }
?node ?role ?concept .
?pv cadsr:has_concept ?node ; mdr:value ?value .
?element mdr:permitted_value ?pv ; cadsr:publicId ?id .
} } ORDER BY ?id ?value ?concept LIMIT 3"""
        for expand, clause in (
            (False, "VALUES ?concept { ncit:C17357 }"),
            (
                True,
                "GRAPH <http://ncicb.nci.nih.gov/xml/owl/EVS/Thesaurus.rdf> "
                "{ ?concept rdfs:subClassOf* ncit:C17357 . }",
            ),
        ):
            for method, template in (
                ("find_data_elements", elements),
                ("find_permissible_values", values),
            ):
                with self.subTest(method=method, expand=expand):
                    server = self.serve(sparql_reply([]))
                    getattr(self.ssis(server), method)(
                        "C17357", expand_descendants=expand, maximum=2
                    )
                    query = parse_qs(server.bodies[0].decode())["query"][0]
                    expected = prefixes + "\n" + template.replace("CONCEPT_CLAUSE", clause)
                    self.assertEqual(query.split(), expected.split())

    def ssis(self, server):
        client = SSISClient(Settings(ssis_sparql_url=server.url), user_agent=product_user_agent())
        client.sparql_http.sleep = lambda _: None
        return client

    def test_graph_identity_preserves_both_dates_and_optional_version_without_release_check(self):
        fixture = recording("ssis-sparql/graph-identities.json")
        server = self.serve(Reply(body=json.dumps(fixture["response"]["body"]).encode()))
        result = self.ssis(server).get_graph_identities()
        self.assertEqual(
            result,
            [
                {"graph": NCIT_GRAPH, "date": "September 28, 2026", "version": "26.09d"},
                {"graph": CADSR_GRAPH, "date": "2026-07-01"},
            ],
        )
        query = parse_qs(server.bodies[0].decode())["query"][0]
        expected = fixture["request"]["form"]["query"] + "\nLIMIT 3"
        self.assertEqual(query.split(), expected.split())
        self.assertEqual(server.seen[0][1]["content-type"], "application/x-www-form-urlencoded")
        self.assertEqual(server.seen[0][1]["accept"], "application/sparql-results+json")

    def test_content_queries_preserve_crafted_public_id_projection_rows(self):
        for name, method, argument, options in (
            ("data-elements-roles-c17357", "find_data_elements", "C17357", {}),
            (
                "data-elements-roles-c17357-descendants",
                "find_data_elements",
                "C17357",
                {"expand_descendants": True},
            ),
            ("values-c20197", "find_permissible_values", "C20197", {}),
            (
                "values-c17357-descendants",
                "find_permissible_values",
                "C17357",
                {"expand_descendants": True},
            ),
        ):
            with self.subTest(name=name):
                fixture = recording(f"ssis-sparql/{name}.json")
                rows = fixture["response"]["body"]["results"]["bindings"]
                projected = [
                    {key: value for key, value in row.items() if key != "version"} for row in rows
                ]
                fixture["response"]["body"]["results"]["bindings"] = projected
                server = self.serve(Reply(body=json.dumps(fixture["response"]["body"]).encode()))
                result = getattr(self.ssis(server), method)(argument, **options)
                rows = fixture["response"]["body"]["results"]["bindings"]
                self.assertEqual(
                    without_roles(result),
                    plain_rows(without_roles(rows)),
                )
                query = parse_qs(server.bodies[0].decode())["query"][0]
                self.assertNotIn("TRANSITIVE", query)

    def test_maximum_plus_one_retains_the_sentinel_and_rejects_an_unbounded_answer(self):
        fixture = recording("ssis-sparql/data-elements-roles-c17357.json")
        rows = fixture["response"]["body"]["results"]["bindings"]
        server = self.serve(sparql_reply(rows[:3]), sparql_reply(rows[:4]))
        client = self.ssis(server)
        self.assertEqual(len(client.find_data_elements("C17357", maximum=2)), 3)
        with self.assertRaises(PlatformError) as raised:
            client.find_data_elements("C17357", maximum=2)
        self.assertEqual(raised.exception.code, "upstream_unavailable")
        for body in server.bodies:
            self.assertTrue(parse_qs(body.decode())["query"][0].rstrip().endswith("LIMIT 3"))

    def test_empty_lists_are_successful_for_every_operation(self):
        for method, arguments, body in (
            ("find_data_elements", ("C17357",), {"results": {"bindings": []}}),
            ("find_permissible_values", ("C17357",), {"results": {"bindings": []}}),
        ):
            with self.subTest(method=method):
                server = self.serve(Reply(body=json.dumps(body).encode()))
                self.assertEqual(getattr(self.ssis(server), method)(*arguments), [])

    def test_missing_or_malformed_bindings_never_mean_empty(self):
        for body in (
            {},
            {"results": {}},
            {"results": None},
            {"results": {"bindings": None}},
            {"results": {"bindings": [{}]}},
            {"results": {"bindings": ["bad"]}},
        ):
            with self.subTest(body=body):
                server = self.serve(Reply(body=json.dumps(body).encode()))
                with self.assertRaises(PlatformError) as raised:
                    self.ssis(server).find_data_elements("C17357")
                self.assertEqual(raised.exception.code, "upstream_unavailable")

    def test_each_selected_variable_is_required_and_malformed_terms_fail(self):
        original = recording("ssis-sparql/data-elements-roles-c17357.json")["response"]["body"][
            "results"
        ]["bindings"][0]
        for key in ("id", "name", "roles"):
            for term in ("missing", None, {}, {"value": "x"}, {"type": "literal", "value": 4}):
                with self.subTest(key=key, term=term):
                    row = copy.deepcopy(original)
                    if term == "missing":
                        del row[key]
                    else:
                        row[key] = term
                    with self.assertRaises(PlatformError) as raised:
                        self.ssis(self.serve(sparql_reply([row]))).find_data_elements("C17357")
                    self.assertEqual(raised.exception.code, "upstream_unavailable")

    def test_invalid_graph_metadata_is_rejected_without_a_fabricated_identity(self):
        original = recording("ssis-sparql/graph-identities.json")["response"]["body"]["results"][
            "bindings"
        ]
        for key, value in (
            ("graph", "https://wrong.example/graph"),
            ("date", "not a date"),
            ("version", ""),
        ):
            with self.subTest(key=key):
                rows = copy.deepcopy(original)
                rows[0][key]["value"] = value
                with self.assertRaises(PlatformError) as raised:
                    self.ssis(self.serve(sparql_reply(rows))).get_graph_identities()
                self.assertEqual(raised.exception.code, "upstream_unavailable")

    def test_partial_duplicate_and_overfull_graph_identities_are_not_silently_used(self):
        original = recording("ssis-sparql/graph-identities.json")["response"]["body"]["results"][
            "bindings"
        ]
        for rows in ([], original[:1], original[:1] * 2, original + original[:1], original * 2):
            with self.subTest(count=len(rows)):
                with self.assertRaises(PlatformError) as raised:
                    self.ssis(self.serve(sparql_reply(rows))).get_graph_identities()
                self.assertEqual(raised.exception.code, "upstream_unavailable")

    def test_both_versions_are_preserved_even_when_different_from_evs(self):
        rows = copy.deepcopy(
            recording("ssis-sparql/graph-identities.json")["response"]["body"]["results"][
                "bindings"
            ]
        )
        rows[0]["version"]["value"] = "26.08e"
        rows[1]["version"] = {"type": "literal", "value": "actual-registry-version"}
        result = self.ssis(self.serve(sparql_reply(rows))).get_graph_identities()
        self.assertEqual([row["version"] for row in result], ["26.08e", "actual-registry-version"])

    def test_html_403_reaches_the_common_error_path_with_one_request(self):
        server = self.serve(Reply(403, body=b"<html>Request blocked</html>"))
        client = self.ssis(server)
        result = call(
            "find_data_elements_for_concept",
            lambda client=client: {"items": client.find_data_elements("C17357")},
        )
        self.assertEqual(result["error"]["code"], "upstream_unavailable")
        self.assertEqual(result["error"]["details"]["status"], 403)
        self.assertNotIn("reason", result["error"]["details"])
        self.assertEqual(len(server.bodies), 1)

    def test_other_http_rejections_keep_the_original_status(self):
        for status in (401, 403):
            with self.subTest(status=status):
                server = self.serve(Reply(status, body=b'{"message":"Access denied"}'))
                client = self.ssis(server)
                result = call(
                    "find_data_elements_for_concept",
                    lambda client=client: {"items": client.find_data_elements("C17357")},
                )
                self.assertEqual(result["error"]["code"], "upstream_unavailable")
                self.assertEqual(result["error"]["details"]["status"], status)
                self.assertNotIn("reason", result["error"]["details"])
                self.assertNotIn("inspection layer", result["error"]["message"])
                self.assertEqual(len(server.bodies), 1)

    def test_ssis_clients_never_send_the_cadsr_credential(self):
        rows = recording("ssis-sparql/graph-identities.json")["response"]["body"]["results"][
            "bindings"
        ]
        server = self.serve(sparql_reply(rows))
        client = SSISClient(
            Settings(
                ssis_sparql_url=server.url,
                cadsr_credential="fixture-only:secret-value",
            ),
            user_agent=product_user_agent(),
        )
        self.assertEqual(
            {row["graph"] for row in client.get_graph_identities()}, {NCIT_GRAPH, CADSR_GRAPH}
        )
        self.assertTrue(all("authorization" not in headers for _, headers in server.seen))


class SSISValidationTest(unittest.TestCase):
    def test_identifiers_rejected_before_any_transport(self):
        client = SSISClient(Settings(), user_agent=product_user_agent())
        with patch("nci_sip_mcp.http_client._open", side_effect=AssertionError("Unexpected HTTP")):
            for method in (
                client.find_data_elements,
                client.find_permissible_values,
            ):
                for value in ("", "C17357>", '2200604"', "C17357}", None, 123):
                    with (
                        self.subTest(method=method.__name__, value=value),
                        self.assertRaises(InputValidationError),
                    ):
                        method(value)


class SSISContractsTest(ServerTestCase):
    def test_role_encoding_and_present_order_errors_fail_the_client_contract(self):
        source = recording("ssis-sparql/data-elements-roles-c17357.json")
        row = source["response"]["body"]["results"]["bindings"][0]
        prefix = (
            "property|http%3A%2F%2Fncicb.nci.nih.gov%2Fxml%2Fowl%2FEVS%2FThesaurus.owl%23C17357|"
        )
        for text in (
            "",
            prefix + "bad",
            prefix + "1.0",
            prefix + "%20",
            prefix + "%FF",
            prefix + "%",
            prefix + "0|injection",
            prefix + "%7C",
            prefix + "%F0%9F%A7%AC",
            "property|C17357|0",
            "property|https%3A%2F%2Fexample.org%2FC17357|0",
        ):
            with self.subTest(text=text):
                row["roles"]["value"] = text
                with self.assertRaises(PlatformError) as raised:
                    self.ssis(self.serve(sparql_reply([row]))).find_data_elements("C17357")
                self.assertEqual(raised.exception.code, "upstream_unavailable")

    def test_normalized_roles_are_stable_across_aggregate_order_and_duplicate_spelling(self):
        source = recording("ssis-sparql/data-elements-roles-c17357.json")
        row = source["response"]["body"]["results"]["bindings"][0]
        token = row["roles"]["value"]
        alternate = token.rsplit("|", 1)[0] + "|%20%2B00%20"
        row["roles"]["value"] = alternate + " " + token
        row["workflowStatuses"] = {"type": "literal", "value": "vZ v%41 vZ"}
        row["registrationStatuses"] = {"type": "literal", "value": "vStandard v"}
        first = self.ssis(self.serve(sparql_reply([row]))).find_data_elements("C17357")
        row["roles"]["value"] = token + " " + alternate
        row["workflowStatuses"]["value"] = "vA vZ"
        row["registrationStatuses"]["value"] = "v vStandard"
        second = self.ssis(self.serve(sparql_reply([row]))).find_data_elements("C17357")
        self.assertEqual(first, second)
        self.assertEqual(first[0]["roles"], token)
        self.assertEqual(first[0]["workflowStatuses"], "vA vZ")
        self.assertEqual(first[0]["registrationStatuses"], "v vStandard")

    def test_same_role_and_concept_preserve_distinct_orders(self):
        source = recording("ssis-sparql/data-elements-roles-c17357.json")
        row = source["response"]["body"]["results"]["bindings"][0]
        prefix = (
            "property|http%3A%2F%2Fncicb.nci.nih.gov%2Fxml%2Fowl%2FEVS%2FThesaurus.owl%23C17357|"
        )
        row["roles"]["value"] = prefix + "2 " + prefix + "0"
        result = self.ssis(self.serve(sparql_reply([row]))).find_data_elements("C17357")
        self.assertEqual(result[0]["roles"], prefix + "0 " + prefix + "2")

    def test_distinct_role_tuples_are_sorted_independently_of_aggregate_order(self):
        source = recording("ssis-sparql/data-elements-roles-c17357.json")
        row = source["response"]["body"]["results"]["bindings"][0]
        concept = "http%3A%2F%2Fncicb.nci.nih.gov%2Fxml%2Fowl%2FEVS%2FThesaurus.owl%23C17357"
        object_class = f"objectClass|{concept}|0"
        value_meaning = f"valueMeaning|{concept}|1"
        for aggregate in (f"{value_meaning} {object_class}", f"{object_class} {value_meaning}"):
            with self.subTest(aggregate=aggregate):
                row["roles"]["value"] = aggregate
                result = self.ssis(self.serve(sparql_reply([row]))).find_data_elements("C17357")
                self.assertEqual(result[0]["roles"], f"{object_class} {value_meaning}")

    def ssis(self, server):
        return SSISClient(Settings(ssis_sparql_url=server.url), user_agent=product_user_agent())

    def test_identity_requires_each_graph_and_date_field(self):
        original = recording("ssis-sparql/graph-identities.json")["response"]["body"]["results"][
            "bindings"
        ]
        for key in ("graph", "date"):
            with self.subTest(key=key):
                rows = copy.deepcopy(original)
                del rows[0][key]
                with self.assertRaises(PlatformError) as raised:
                    self.ssis(self.serve(sparql_reply(rows))).get_graph_identities()
                self.assertEqual(raised.exception.code, "upstream_unavailable")

    def test_identity_preserves_version_spacing_and_date_case(self):
        rows = recording("ssis-sparql/graph-identities.json")["response"]["body"]["results"][
            "bindings"
        ]
        rows[0]["version"]["value"] = " 26.09D "
        rows[0]["date"]["value"] = "sEPTEMBER 28, 2026"
        rows[0]["date"]["type"] = "typed-literal"
        result = self.ssis(self.serve(sparql_reply(rows))).get_graph_identities()
        self.assertEqual(result[0]["version"], " 26.09D ")
        self.assertEqual(result[0]["date"], "sEPTEMBER 28, 2026")

    def test_blank_version_and_blank_node_are_malformed(self):
        original = recording("ssis-sparql/graph-identities.json")["response"]["body"]["results"][
            "bindings"
        ]
        for key, term in (
            ("version", {"type": "literal", "value": " \t "}),
            ("version", {"type": "bnode", "value": "26.09d"}),
        ):
            with self.subTest(term=term):
                rows = copy.deepcopy(original)
                rows[0][key] = term
                with self.assertRaises(PlatformError) as raised:
                    self.ssis(self.serve(sparql_reply(rows))).get_graph_identities()
                self.assertEqual(raised.exception.code, "upstream_unavailable")

    def test_value_operations_accept_sentinel_and_clamp_query_bounds(self):
        for method, argument in (("find_permissible_values", "C17357"),):
            with self.subTest(method=method):
                self.assert_value_bounds(method, argument)

    def assert_value_bounds(self, method, argument):
        row = {
            "id": {"type": "literal", "value": "2200604"},
            "version": {"type": "literal", "value": "4"},
            "value": {"type": "literal", "value": "Male"},
            "concept": {"type": "uri", "value": "http://example.test/C1"},
            "role": {"type": "uri", "value": CADSR_GRAPH + "#main_concept"},
        }
        server = self.serve(sparql_reply([row] * 3), sparql_reply([]), sparql_reply([row] * 4))
        operation = getattr(self.ssis(server), method)
        self.assertEqual(len(operation(argument, maximum=2)), 3)
        self.assertEqual(operation(argument, maximum=1002), [])
        with self.assertRaises(PlatformError) as raised:
            operation(argument, maximum=2)
        self.assertEqual(raised.exception.code, "upstream_unavailable")
        queries = [parse_qs(body.decode())["query"][0] for body in server.bodies]
        self.assertEqual(
            [query.split()[-2:] for query in queries],
            [["LIMIT", "3"], ["LIMIT", "1001"], ["LIMIT", "3"]],
        )
        self.assertTrue(all(f"GRAPH <{CADSR_GRAPH}>" in query for query in queries))
        self.assertEqual([path for path, _ in server.seen], ["/sparql"] * 3)

    def test_find_value_requires_concept(self):
        row = {
            "id": {"type": "literal", "value": "2200604"},
            "version": {"type": "literal", "value": "4"},
            "value": {"type": "literal", "value": "Male"},
        }
        server = self.serve(sparql_reply([row]))
        client = self.ssis(server)
        with self.assertRaises(PlatformError) as raised:
            client.find_permissible_values("C17357")
        self.assertEqual(raised.exception.code, "upstream_unavailable")


class SSISValidationContractsTest(unittest.TestCase):
    def test_identifiers_and_expansion_name_the_bad_parameter_before_http(self):
        client = SSISClient(Settings(), user_agent=product_user_agent())
        cases = [
            (client.find_data_elements, "C", {}, "conceptCode"),
            (client.find_data_elements, "C0", {}, "conceptCode"),
            (client.find_permissible_values, "C0", {}, "conceptCode"),
            (client.find_permissible_values, "17357", {}, "conceptCode"),
            (client.find_data_elements, "C1", {"expand_descendants": 1}, "expandDescendants"),
            (client.find_data_elements, "C1", {"expand_descendants": "true"}, "expandDescendants"),
            (client.find_permissible_values, "C1", {"expand_descendants": 1}, "expandDescendants"),
        ]
        with patch("nci_sip_mcp.http_client._open", side_effect=AssertionError("Unexpected HTTP")):
            for operation, argument, options, parameter in cases:
                with self.subTest(operation=operation.__name__, argument=argument):
                    with self.assertRaises(InputValidationError) as raised:
                        operation(argument, **options)
                    self.assertEqual(raised.exception.details["parameter"], parameter)

    def test_every_value_operation_validates_maximum(self):
        client = SSISClient(Settings(), user_agent=product_user_agent())
        for operation, argument in (
            (client.find_data_elements, "C1"),
            (client.find_permissible_values, "C1"),
        ):
            for maximum in (0, -1, True, 1.5, "2"):
                with self.subTest(operation=operation.__name__, maximum=maximum):
                    with (
                        patch("nci_sip_mcp.http_client._open", side_effect=AssertionError("HTTP")),
                        self.assertRaises(InputValidationError) as raised,
                    ):
                        operation(argument, maximum=maximum)
                    self.assertEqual(raised.exception.details["parameter"], "maximum")

    def test_host_and_timeout_reach_the_transport(self):
        client = SSISClient(
            Settings(
                ssis_sparql_url="https://sparql.example",
                timeout_seconds=17,
            ),
            user_agent=product_user_agent(),
        )
        with patch("nci_sip_mcp.http_client._open") as transport:
            transport.return_value = FakeResponse(b'{"results":{"bindings":[]}}')
            self.assertEqual(client.find_data_elements("C1"), [])
        self.assertEqual(
            [(call.args[0].full_url, call.args[1]) for call in transport.call_args_list],
            [("https://sparql.example/sparql", 17)],
        )

    def test_timeout_and_ten_megabyte_bound_apply_to_the_sparql_surface(self):
        client = SSISClient(Settings(timeout_seconds=17), user_agent=product_user_agent())
        client.sparql_http.sleep = lambda _: None
        with (
            patch("nci_sip_mcp.http_client._open", side_effect=TimeoutError),
            self.assertRaises(UpstreamTimeoutError) as raised,
        ):
            client.find_data_elements("C1")
        self.assertEqual(raised.exception.details["seconds"], 17)
        self.assertEqual(raised.exception.details["attempts"], 3)
        response = FakeResponse(b"", {"Content-Length": str(10 * 1024 * 1024 + 1)})
        with (
            patch("nci_sip_mcp.http_client._open", return_value=response),
            self.assertRaises(UpstreamTooLargeError) as raised,
        ):
            client.find_data_elements("C1")
        self.assertEqual(
            raised.exception.details,
            {"bound": "ssis_response_bytes", "limit": 10485760, "reached": 10485761},
        )


class SSISRetryContractsTest(ServerTestCase):
    def test_the_surface_retries_503_until_the_third_response(self):
        sparql = self.serve(Reply(503), Reply(503), sparql_reply([]))
        client = SSISClient(Settings(ssis_sparql_url=sparql.url), user_agent=product_user_agent())
        client.sparql_http.sleep = lambda _: None
        self.assertEqual(client.find_data_elements("C1"), [])
        self.assertEqual([path for path, _ in sparql.seen], ["/sparql"] * 3)
        self.assertEqual(sparql.bodies, [sparql.bodies[0]] * 3)
