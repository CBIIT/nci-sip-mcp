"""The weekly command reports every family, including structural drift, offline."""

import ast
import copy
import inspect
import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

import yaml
from scripts.ssis_contracts import REGISTER, request, validate

from nci_si_acceptance.craft import craft
from nci_si_mcp.registry import SPECS
from nci_si_mcp.ssis import SSISClient
from test_http_client import Reply, ServerTestCase

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = (
    "crafted/ssis-sparql/graph-identities-bounded.json",
    "recorded/ssis-sparql/data-elements-statuses-c17357.json",
    "recorded/ssis-sparql/data-elements-statuses-c17357-descendants.json",
    "crafted/ssis-public-id/values-c20197.json",
    "crafted/ssis-public-id/values-c17357-descendants.json",
)
IDS = (
    "graph-identities",
    "elements-exact",
    "elements-descendants",
    "values-exact",
    "values-descendants",
)
REQUIRED = (
    ("graph", "date"),
    ("id", "name", "roles"),
    ("id", "name", "roles"),
    ("id", "value", "concept"),
    ("id", "value", "concept"),
)


def evidence(name):
    fixtures = ROOT / "acceptance/fixtures"
    if name.startswith("crafted/ssis-public-id/"):
        return craft(fixtures)[name]
    return json.loads((fixtures / name).read_text())


def bodies():
    return [evidence(name)["response"]["body"] for name in EVIDENCE]


def passed():
    return [{"id": name, "status": "pass"} for name in IDS]


class SSISContractCommandTest(ServerTestCase):
    def test_status_aggregate_drift_fails_but_omitted_and_empty_evidence_passes(self):
        for field in ("workflowStatuses", "registrationStatuses"):
            for term, error in (
                ("missing", None),
                ({"type": "literal", "value": ""}, None),
                ({"type": "literal", "value": "v v%20DRAFT%20"}, None),
                ({"type": "uri", "value": "vRELEASED"}, f"{field}: expected aggregate literal"),
                ({"type": "literal", "value": "RELEASED"}, "upstream_unavailable"),
                ({"type": "literal", "value": "v%FF"}, "upstream_unavailable"),
            ):
                with self.subTest(field=field, term=term):
                    payloads = bodies()
                    row = payloads[1]["results"]["bindings"][0]
                    row.pop(field, None)
                    if term != "missing":
                        row[field] = term
                    result, _ = self.command(payloads)
                    expected = passed()
                    if error:
                        expected[1] = {"id": "elements-exact", "status": "fail", "error": error}
                    self.assertEqual(json.loads(result.stdout), expected)
                    self.assertEqual(result.returncode, int(error is not None))

    def test_partial_role_binding_fails_and_still_reports_all_five(self):
        for value in (None, "valueMeaning|bad|0"):
            with self.subTest(value=value):
                payloads = bodies()
                row = payloads[1]["results"]["bindings"][0]
                if value is None:
                    del row["roles"]
                else:
                    row["roles"]["value"] = value
                result, server = self.command(payloads)
                expected = passed()
                expected[1] = {
                    "id": "elements-exact",
                    "status": "fail",
                    "error": "binding missing required variables"
                    if value is None
                    else "upstream_unavailable",
                }
                self.assertEqual(json.loads(result.stdout), expected)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(len(server.bodies), 5)

    def command(self, payloads):
        server = self.serve(
            *[
                Reply(body=body if isinstance(body, bytes) else json.dumps(body).encode())
                for body in payloads
            ]
        )
        result = subprocess.run(  # noqa: S603 - fixed module and local fixture server
            [sys.executable, "-m", "scripts.ssis_contracts", "--url", server.url],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        return result, server

    def test_bad_success_body_reports_all_families_without_upstream_prose(self):
        # Given a bad first HTTP 200 and four valid responses, when the real CLI runs,
        # then it reports the failure safely and still checks every remaining family.
        for bad in (
            b"<html>private-upstream-prose</html>",
            b"private-upstream-prose: not JSON",
            b'{"apiResponse":{"type":"E","message":"private-upstream-prose"}}',
        ):
            with self.subTest(body=bad):
                payloads = bodies()
                payloads[0] = bad
                result, server = self.command(payloads)
                expected = passed()
                expected[0] = {
                    "id": "graph-identities",
                    "status": "fail",
                    "error": "upstream_unavailable",
                }
                self.assertEqual(result.stdout, json.dumps(expected, indent=2) + "\n")
                self.assertEqual(result.returncode, 1)
                self.assertEqual(len(server.bodies), 5)
                self.assertEqual(result.stderr, "")

    def test_graph_dates_reject_invalid_values_and_accept_runtime_date_forms(self):
        # Given graph metadata, when the CLI checks its date, then runtime-supported
        # ISO and English dates pass while impossible or arbitrary dates fail.
        for date, valid in (
            ("not-a-date", False),
            ("2026-02-30", False),
            ("", False),
            ("2026-10-08", True),
            ("October 08, 2026", True),
        ):
            with self.subTest(date=date):
                payloads = bodies()
                payloads[0]["results"]["bindings"][0]["date"]["value"] = date
                result, server = self.command(payloads)
                expected = passed()
                if not valid:
                    expected[0] = {
                        "id": "graph-identities",
                        "status": "fail",
                        "error": "date: invalid graph date",
                    }
                self.assertEqual(json.loads(result.stdout), expected)
                self.assertEqual(result.returncode, int(not valid), result.stderr)
                self.assertEqual(len(server.bodies), 5)

    def test_missing_head_variable_fails_exact_family_and_still_reports_all_five(self):
        # Given recorded rows intact but SELECT metadata missing name, when the weekly
        # command runs, then it fails that family and still attempts the later families.
        payloads = bodies()
        payloads[1]["head"]["vars"].remove("name")
        result, server = self.command(payloads)
        expected = passed()
        expected[1] = {
            "id": "elements-exact",
            "status": "fail",
            "error": "head.vars must be exactly: id, name, roles, "
            "workflowStatuses, registrationStatuses",
        }
        self.assertEqual(result.stdout, json.dumps(expected, indent=2) + "\n")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(len(server.bodies), 5)

    def test_blank_element_names_fail_both_families_and_still_report_all_five(self):
        # Given blank names in both element families, when the real CLI runs,
        # then each reports name drift and all five queries finish with a nonzero exit.
        for name in ("", " \t\n"):
            with self.subTest(name=name):
                payloads = bodies()
                expected = passed()
                for index in (1, 2):
                    payloads[index]["results"]["bindings"][0]["name"]["value"] = name
                    expected[index] = {
                        "id": IDS[index],
                        "status": "fail",
                        "error": "name: must not be blank",
                    }
                result, server = self.command(payloads)
                self.assertEqual(json.loads(result.stdout), expected)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertEqual(len(server.bodies), 5)

    def test_nonblank_names_and_empty_permissible_value_text_pass(self):
        # Given nonblank names with surrounding whitespace and empty permissible
        # value text, when the CLI runs, then these valid content terms still pass.
        payloads = bodies()
        for index in (1, 2):
            payloads[index]["results"]["bindings"][0]["name"]["value"] = " \tValid name\n"
        for payload in payloads[3:]:
            payload["results"]["bindings"][0]["value"]["value"] = ""
        result, server = self.command(payloads)
        self.assertEqual(json.loads(result.stdout), passed())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(server.bodies), 5)

    def test_crafted_public_id_and_bounded_identity_contracts_pass(self):
        # Given valid responses, when the CLI checks every family, then the wire
        # path and Accept match the real SSIS client's planned request for each.
        result, server = self.command(bodies())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), passed())
        entries = yaml.safe_load(REGISTER.read_text())["queries"]
        planned = [request(entry) for entry in entries]
        self.assertEqual(
            [(path, headers["accept"]) for path, headers in server.seen],
            [(item.path, item.accept) for item in planned],
        )

    def test_both_graph_identities_are_required_but_content_families_may_be_empty(self):
        # Given empty content families and zero, one or both graph identities, when
        # the CLI runs, then only a missing required identity fails the graph family.
        for retained in ((), (0,), (1,), (0, 1)):
            with self.subTest(retained=retained):
                payloads = bodies()
                rows = payloads[0]["results"]["bindings"]
                payloads[0]["results"]["bindings"] = [rows[index] for index in retained]
                for payload in payloads[1:]:
                    payload["results"]["bindings"] = []
                result, server = self.command(payloads)
                expected = passed()
                missing = retained != (0, 1)
                if missing:
                    expected[0] = {
                        "id": "graph-identities",
                        "status": "fail",
                        "error": "graph: required identity missing",
                    }
                self.assertEqual(json.loads(result.stdout), expected)
                self.assertEqual(result.returncode, int(missing), result.stderr)
                self.assertEqual(len(server.bodies), 5)

    def test_invalid_binding_and_third_graph_fail_the_exact_family(self):
        # Given one malformed row, when the CLI runs, then that family's exact
        # failure is reported and every other family still passes.
        for index, key, term, error in (
            (1, None, "x", "binding must be an object"),
            (
                1,
                "unselected",
                {"type": "literal", "value": "extra"},
                "binding has unselected variables",
            ),
            (
                0,
                "graph",
                {"type": "uri", "value": "https://example.test/third-graph"},
                "graph: unexpected identity",
            ),
        ):
            with self.subTest(error=error):
                payloads = bodies()
                rows = payloads[index]["results"]["bindings"]
                if index == 0:
                    rows.append(copy.deepcopy(rows[0]))
                if key is None:
                    rows[-1] = term
                else:
                    rows[-1][key] = term
                result, server = self.command(payloads)
                expected = passed()
                expected[index] = {"id": IDS[index], "status": "fail", "error": error}
                self.assertEqual(json.loads(result.stdout), expected)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertEqual(len(server.bodies), 5)

    def test_empty_and_changed_content_counts_and_releases_pass(self):
        payloads = bodies()
        payloads[0]["results"]["bindings"][0]["version"]["value"] = "future-release"
        payloads[1]["results"]["bindings"] = []
        payloads[2]["results"]["bindings"] = payloads[2]["results"]["bindings"][:1]
        payloads[2]["results"]["bindings"][0]["name"]["value"] = "Changed upstream label"
        result, _ = self.command(payloads)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), passed())

    def test_every_family_rejects_missing_required_bindings_and_bad_terms(self):
        for bad_term in (None, {"type": "bnode", "value": "bad"}):
            with self.subTest(term=bad_term):
                payloads = bodies()
                for payload in payloads:
                    row = payload["results"]["bindings"][0]
                    key = next(iter(row))
                    row[key] = copy.deepcopy(bad_term)
                result, _ = self.command(payloads)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertEqual([row["status"] for row in json.loads(result.stdout)], ["fail"] * 5)

    def test_http_failure_is_reported_and_does_not_stop_later_queries(self):
        server = self.serve(Reply(403), *[Reply(body=json.dumps(b).encode()) for b in bodies()[1:]])
        result = subprocess.run(  # noqa: S603 - fixed CLI and local fixture URL
            [sys.executable, "-m", "scripts.ssis_contracts", "--url", server.url],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        expected = passed()
        expected[0] = {"id": "graph-identities", "status": "fail", "error": "UpstreamRejectedError"}
        self.assertEqual(json.loads(result.stdout), expected)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(len(server.bodies), 5)


def normalized(query):
    query = re.sub(r"ncit:C[1-9][0-9]*", "ncit:CODE", query)
    return re.sub(r"LIMIT [0-9]+", "LIMIT BOUND", " ".join(query.split()))


def query_operations():
    tree = ast.parse(inspect.getsource(SSISClient))
    return {
        node.name
        for node in tree.body[0].body
        if isinstance(node, ast.FunctionDef) and not node.name.startswith("_")
    }


class SSISRegisterTest(unittest.TestCase):
    def test_content_versions_are_unselected_in_every_public_id_family(self):
        # Given a graph-derived content binding, when it claims an item version,
        # then the checker rejects the field that no consumer can attribute.
        entries = yaml.safe_load(REGISTER.read_text())["queries"]
        for entry, original in zip(entries[1:], bodies()[1:], strict=True):
            for version in ("invalid", "1.2.3", " 1", "1 ", "-1", "1."):
                with self.subTest(family=entry["id"], version=version):
                    payload = copy.deepcopy(original)
                    payload["results"]["bindings"][0]["version"] = {
                        "type": "literal",
                        "value": version,
                    }
                    with self.assertRaises(ValueError) as raised:
                        validate(payload, entry)
                    self.assertEqual(str(raised.exception), "binding has unselected variables")

    def test_concepts_require_ncit_namespace_and_code(self):
        # Given a value binding, when its concept URI cannot resolve at the seam,
        # then the checker rejects it even though it is an absolute URI.
        entries = yaml.safe_load(REGISTER.read_text())["queries"]
        for entry, original in zip(entries[3:], bodies()[3:], strict=True):
            for uri in (
                "https://example.test/C1",
                "https://ncicb.nci.nih.gov/xml/owl/EVS/Thesaurus.owl#C1",
                "http://ncicb.nci.nih.gov/xml/owl/EVS/Thesaurus.owl#C0",
                "http://ncicb.nci.nih.gov/xml/owl/EVS/Thesaurus.owl#C01",
                "http://ncicb.nci.nih.gov/xml/owl/EVS/Thesaurus.owl#bad",
                "http://ncicb.nci.nih.gov/xml/owl/EVS/Thesaurus.owl#C1/extra",
            ):
                with self.subTest(family=entry["id"], uri=uri):
                    payload = copy.deepcopy(original)
                    payload["results"]["bindings"][0]["concept"]["value"] = uri
                    with self.assertRaises(ValueError) as raised:
                        validate(payload, entry)
                    self.assertEqual(str(raised.exception), "concept: expected NCIt concept URI")

    def test_nonblank_opaque_graph_versions(self):
        # Given an opaque graph version, when it is checked, then arbitrary
        # release strings pass but empty and whitespace-only strings fail.
        entries = yaml.safe_load(REGISTER.read_text())["queries"]
        for entry, original in zip(entries[:1], bodies()[:1], strict=True):
            versions = ("invalid", "1.2.3", "future-release")
            for version in versions:
                with self.subTest(family=entry["id"], version=version):
                    payload = copy.deepcopy(original)
                    payload["results"]["bindings"][0]["version"] = {
                        "type": "literal",
                        "value": version,
                    }
                    validate(payload, entry)
        for version in ("", "  "):
            with self.subTest(family=IDS[0], version=version):
                payload = bodies()[0]
                payload["results"]["bindings"][0]["version"]["value"] = version
                with self.assertRaises(ValueError) as raised:
                    validate(payload, entries[0])
                self.assertEqual(str(raised.exception), "version: must not be blank")

    def test_every_required_variable_is_required_even_when_other_terms_are_valid(self):
        entries = yaml.safe_load(REGISTER.read_text())["queries"]
        for entry, original, required in zip(entries, bodies(), REQUIRED, strict=True):
            self.assertEqual(entry["required"], list(required), entry["id"])
            for key in required:
                with self.subTest(family=entry["id"], missing=key):
                    payload = copy.deepcopy(original)
                    del payload["results"]["bindings"][0][key]
                    with self.assertRaises(ValueError) as raised:
                        validate(payload, entry)
                    self.assertEqual(str(raised.exception), "binding missing required variables")

    def test_register_maps_content_hops_to_their_spec_requirements(self):
        entries = yaml.safe_load(REGISTER.read_text())["queries"]
        expected = (
            ["X-1", "X-2", "X-3", "get_release_alignment-1"],
            ["find_data_elements_for_concept-1", "ground_value-2"],
            ["find_data_elements_for_concept-1"],
            ["find_data_elements_for_concept-2", "ground_value-2"],
            ["find_data_elements_for_concept-2"],
        )
        self.assertEqual([entry["requirements"] for entry in entries], list(expected))

    def test_register_references_real_spec_requirements_and_tools(self):
        requirements = yaml.safe_load((ROOT / "spec/requirements.yaml").read_text())
        tools = {spec.name for spec in SPECS}
        for entry in yaml.safe_load(REGISTER.read_text())["queries"]:
            with self.subTest(family=entry["id"]):
                self.assertLessEqual(set(entry["requirements"]), requirements.keys())
                self.assertLessEqual(set(entry["consumers"]), tools)

    def test_register_covers_every_query_operation_and_expansion_variant_exactly(self):
        register = yaml.safe_load(REGISTER.read_text())
        operations = query_operations()
        entries = register["queries"]
        self.assertEqual({entry["operation"] for entry in entries}, operations)
        self.assertEqual(tuple(entry["id"] for entry in entries), IDS)
        for operation in operations:
            signature = inspect.signature(getattr(SSISClient, operation))
            expected = [False, True] if "expand_descendants" in signature.parameters else [None]
            actual = [
                e["kwargs"].get("expand_descendants")
                for e in entries
                if e["operation"] == operation
            ]
            self.assertEqual(actual, expected)

    def test_registered_request_arguments_are_exact(self):
        entries = yaml.safe_load(REGISTER.read_text())["queries"]
        self.assertEqual(
            [(entry["operation"], entry["args"], entry["kwargs"]) for entry in entries],
            [
                ("get_graph_identities", [], {}),
                ("find_data_elements", ["C17357"], {"expand_descendants": False, "maximum": 1000}),
                ("find_data_elements", ["C17357"], {"expand_descendants": True, "maximum": 1000}),
                (
                    "find_permissible_values",
                    ["C20197"],
                    {"expand_descendants": False, "maximum": 1000},
                ),
                (
                    "find_permissible_values",
                    ["C17357"],
                    {"expand_descendants": True, "maximum": 1000},
                ),
            ],
        )

    def test_real_client_requests_match_independent_evidence_only_parameters_vary(self):
        register = yaml.safe_load(REGISTER.read_text())
        for entry in register["queries"]:
            with self.subTest(family=entry["id"]):
                fixture = evidence(entry["evidence"])
                captured = request(entry)
                expected = fixture["request"]
                self.assertEqual(
                    register["transport"],
                    {
                        "method": expected["method"],
                        "path": expected["path"],
                        "accept": expected["headers"]["Accept"],
                        "form": "query",
                    },
                )
                self.assertEqual(
                    (captured.path, captured.accept),
                    (expected["path"], expected["headers"]["Accept"]),
                )
                expected_query = expected["form"]["query"]
                if entry["id"] == "values-descendants":
                    # The independent recording has this one graph block on three lines.
                    # Declare the current packet's exact formatting; this grants no transport.
                    expected_query = expected_query.replace(
                        "  GRAPH <http://ncicb.nci.nih.gov/xml/owl/EVS/Thesaurus.rdf> {\n"
                        "    ?concept rdfs:subClassOf* ncit:C17357 .\n  }",
                        "  GRAPH <http://ncicb.nci.nih.gov/xml/owl/EVS/Thesaurus.rdf> "
                        "{ ?concept rdfs:subClassOf* ncit:C17357 . }",
                    )
                self.assertEqual(captured.fields, {"query": expected_query})
                self.assertEqual(
                    captured.fields["query"].split()[-2:], ["LIMIT", str(entry["limit"])]
                )
                if entry["args"]:
                    varied = copy.deepcopy(entry)
                    varied["args"] = ["C4817"]
                    varied["kwargs"]["maximum"] = 2
                    changed = request(varied).fields["query"]
                    self.assertEqual(normalized(changed), normalized(expected["form"]["query"]))
                    self.assertEqual(changed.split()[-2:], ["LIMIT", "3"])

    def test_structural_boundaries_and_semantic_identifiers_are_checked(self):
        entry = yaml.safe_load(REGISTER.read_text())["queries"][1]
        original = bodies()[1]
        cases = [
            (None, "response must be an object"),
            ({"head": original["head"]}, "results.bindings must be a list"),
        ]
        for key, term, error in (
            ("id", {"type": "literal", "value": "007"}, "id: invalid public identifier"),
            ("name", {"type": "literal", "value": 3}, "name: term value must be a string"),
        ):
            payload = copy.deepcopy(original)
            payload["results"]["bindings"][0][key] = term
            cases.append((payload, error))
        for payload, error in cases:
            with self.subTest(error=error), self.assertRaises(ValueError) as raised:
                validate(payload, entry)
            self.assertEqual(str(raised.exception), error)

    def test_query_limit_is_not_silently_accepted(self):
        entry = yaml.safe_load(REGISTER.read_text())["queries"][3]
        original = bodies()[3]
        payload = copy.deepcopy(original)
        payload["results"]["bindings"] = original["results"]["bindings"][:1] * 1002
        with self.assertRaisesRegex(ValueError, "exceed requested LIMIT"):
            validate(payload, entry)

    def test_graph_and_concept_require_absolute_uris(self):
        # Given the correct graph URI with a literal term type, when validated,
        # then it fails the URI contract just like non-URI concept terms.
        entries = yaml.safe_load(REGISTER.read_text())["queries"]
        originals = bodies()
        cases = (
            (
                "graph",
                "literal",
                "http://ncicb.nci.nih.gov/xml/owl/EVS/Thesaurus.rdf",
                "graph: expected absolute URI",
            ),
            (
                "graph",
                "typed-literal",
                "http://ncicb.nci.nih.gov/xml/owl/EVS/Thesaurus.rdf",
                "graph: expected absolute URI",
            ),
            ("concept", "literal", "http://example.test/C1", "concept: expected absolute URI"),
            ("concept", "uri", "relative/C1", "concept: expected absolute URI"),
        )
        for key, kind, value, error in cases:
            with self.subTest(key=key, kind=kind, value=value):
                index = 0 if key == "graph" else 3
                payload = copy.deepcopy(originals[index])
                payload["results"]["bindings"][0][key] = {"type": kind, "value": value}
                with self.assertRaises(ValueError) as raised:
                    validate(payload, entries[index])
                self.assertEqual(str(raised.exception), error)
