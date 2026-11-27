import asyncio
import base64
import hashlib
import json
import logging
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

import yaml
from mcp.types import JSONRPCMessage
from pydantic import TypeAdapter

from fakes import FakeSSIS, captured, catalogue_rows, concept, records
from nci_sip_mcp.audit import audited, compact, emit, hashed, secrets
from nci_sip_mcp.bounds import Budget, RequestBudgetError, budgeted
from nci_sip_mcp.config import Settings, configure_logging
from nci_sip_mcp.context import Context
from nci_sip_mcp.errors import current_correlation_id
from nci_sip_mcp.evs import EVSClient
from nci_sip_mcp.http_client import UpstreamUnavailableError
from nci_sip_mcp.registry import SPECS, invoke
from nci_sip_mcp.server import _audit_tools, _validate_inputs
from nci_sip_mcp.traffic_identity import product_user_agent
from test_evs_client import FakeResponse, http_error
from test_http_client import Reply, ServerTestCase
from test_index import normalized
from test_server import ServerFixture, pinned


def digest(value):
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(encoded.encode()).hexdigest()


class AuditRecordTest(unittest.TestCase):
    def test_nonfinite_numbers_are_strings_in_nested_audit_values(self):
        value = {"numbers": [float("inf"), float("-inf"), float("nan"), 1.5]}
        self.assertEqual(json.loads(compact(value)), {"numbers": ["inf", "-inf", "nan", 1.5]})

    def test_diagnostic_verbosity_does_not_disable_audit_records(self):
        logger = logging.getLogger("nci_sip_mcp.audit")
        self.addCleanup(logger.setLevel, logger.level)
        with captured() as stream, patch("nci_sip_mcp.config.logging.basicConfig"):
            configure_logging("ERROR")
            logging.getLogger("nci_sip_mcp").setLevel(logging.ERROR)
            with audited("lookup", {}, {}, ()) as record:
                record.result = {}

        (result,) = records(stream)
        self.assertEqual((result["level"], result["status"]), ("INFO", "ok"))

    def test_latency_is_reported_in_milliseconds(self):
        with (
            captured() as stream,
            patch("nci_sip_mcp.audit.time.perf_counter", return_value=10.25),
            audited("lookup", {}, {}, ()) as record,
        ):
            record.started = 10
            record.result = {}

        (result,) = records(stream)
        self.assertEqual(result["elapsedMs"], 250)

    def test_every_registered_parameter_has_an_explicit_audit_class(self):
        for spec in SPECS:
            with self.subTest(spec.operation):
                self.assertEqual(set(spec.audit), {p.name for p in spec.parameters})
                self.assertLessEqual(set(spec.audit.values()), {"plain", "hash"})

    def test_known_free_text_and_undeclared_fields_are_hashed(self):
        arguments = {"query": "kinase inhibition", "code": "C1", "new field": "private text"}
        with (
            captured() as stream,
            audited("search", arguments, {"query": "hash", "code": "plain"}, ()) as record,
        ):
            record.result = {"results": []}

        (result,) = records(stream)
        expected = hashlib.sha256(b'"kinase inhibition"').hexdigest()
        self.assertEqual(result["parameters"]["query"], {"sha256": expected})
        self.assertEqual(result["parameters"]["code"], "C1")
        self.assertEqual(result["parameters"][compact(hashed("new field"))], hashed("private text"))
        for text in ("kinase inhibition", "new field", "private text"):
            self.assertNotIn(text, stream.getvalue())

    def test_secrets_are_removed_at_the_boundary_including_nested_metadata(self):
        key, credential = "audit-key-canary", "user:audit-password-canary"
        basic = base64.b64encode(credential.encode()).decode()
        with (
            captured() as stream,
            audited(
                "lookup",
                {"code": key},
                {"code": "plain"},
                secrets(key, credential),
                key,
            ) as record,
        ):
            record.result = {
                "provenance": {"release": {"identifier": credential}},
                "truncation": {"perKind": {key: [basic]}},
            }
            emit(
                logging.getLogger("nci_sip_mcp.test"),
                logging.WARNING,
                "diagnostic",
                details={key: [credential, basic]},
            )
            logging.getLogger("nci_sip_mcp.external").warning("external canary")

        for secret in (key, credential, basic, "audit-password-canary"):
            self.assertNotIn(secret, stream.getvalue())
        (result,) = records(stream)
        self.assertEqual(result["parameters"]["code"], "[redacted]")
        self.assertEqual(result["correlationId"], "[redacted]")
        self.assertEqual(result["release"]["resolved"], [{"identifier": "[redacted]"}])

    def test_external_diagnostics_are_structured_without_messages_or_tracebacks(self):
        with captured() as stream:
            try:
                raise RuntimeError("exception canary")
            except RuntimeError:
                logging.getLogger("nci_sip_mcp.external").exception("caller query canary")

        (result,) = records(stream, "diagnostic")
        self.assertEqual(result["messageHash"], hashed("caller query canary")["sha256"])
        self.assertEqual(result["level"], "ERROR")
        self.assertNotIn("canary", stream.getvalue())

    def test_raw_payload_metadata_is_not_mistaken_for_a_release(self):
        with captured() as stream, audited("lookup", {}, {}, ()) as record:
            record.result = {"raw": {"provenance": None}, "upstream": {"provenance": "opaque"}}

        (result,) = records(stream)
        self.assertEqual(result["release"]["resolved"], [])
        self.assertEqual(result["status"], "ok")


def compact_text(value):
    """The one text form a client receives, written out independently of `compact`."""
    return json.dumps(value, separators=(",", ":"), sort_keys=True, ensure_ascii=False)


class AuditAdapterTest(ServerFixture):
    def test_mcp_runtime_error_is_recorded_before_the_sdk_handles_it(self):
        self.evs.errors["get_concept"] = RuntimeError("runtime canary")
        with captured() as stream:
            result = self.session(
                lambda client: client.call_tool("get_concept", pinned(code="C3262"))
            )

        (record,) = records(stream)
        self.assertTrue(result.is_error)
        self.assertEqual(record["errorType"], "RuntimeError")
        self.assertEqual(record["status"], "error")
        self.assertNotIn("runtime canary", stream.getvalue())

    def test_mcp_emits_one_record_with_the_result_size_release_and_caller_identifier(self):
        self.evs.concepts["C3262"] = dict(self.evs.concepts["C3262"], name="Café")
        with captured() as stream:
            result = self.session(
                lambda client: client.call_tool(
                    "get_concept", pinned(code="C3262"), meta={"correlationId": "caller-audit"}
                )
            )

        (record,) = records(stream)
        content = result.structured_content
        self.assertEqual(record["correlationId"], content["provenance"]["correlationId"])
        self.assertEqual(record["correlationId"], "caller-audit")
        self.assertEqual(record["tool"], "get_concept")
        self.assertEqual(record["target"], {"terminology": "ncit"})
        self.assertEqual(
            record["release"],
            {
                "requested": "26.06e",
                "resolved": [content["provenance"]["release"]],
                "selection": "explicit",
                "selected": {
                    "terminology": "ncit",
                    "channel": "monthly",
                    "version": "26.06e",
                    "date": None,
                },
            },
        )
        self.assertEqual(
            record["resultSize"],
            len(json.dumps(content, ensure_ascii=False, separators=(",", ":")).encode("utf-8")),
        )
        self.assertEqual(
            (record["status"], record["responseCode"], record["outboundRequests"]), ("ok", "ok", 0)
        )
        self.assertGreaterEqual(record["elapsedMs"], 0)
        self.assertIsNotNone(datetime.fromisoformat(record["timestamp"]).tzinfo)
        self.assertNotIn("Café", stream.getvalue())

    def test_schema_rejection_is_one_record_with_generated_correlation_and_no_request(self):
        with captured() as stream:
            result = self.session(lambda client: client.call_tool("get_concept", {"code": "C3262"}))

        (record,) = records(stream)
        self.assertTrue(result.is_error)
        self.assertEqual(record["responseCode"], "invalid_request")
        self.assertEqual(record["status"], "error")
        self.assertEqual(
            record["correlationId"], result.structured_content["error"]["correlationId"]
        )
        self.assertTrue(record["correlationId"])
        self.assertEqual(record["outboundRequests"], 0)
        self.assertEqual(self.evs.calls, [])
        text = result.content[0].text
        self.assertEqual(text, compact_text(result.structured_content))
        self.assertEqual(record["resultSize"], len(text.encode("utf-8")))

    def test_overflowing_json_number_keeps_audit_json_valid(self):
        wire = (
            '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":'
            '{"name":"get_concept_neighborhood","arguments":'
            '{"terminology":"ncit","release":"26.06e","code":"C1","depth":1e999}}}'
        )
        message = TypeAdapter(JSONRPCMessage).validate_json(wire)

        async def run():
            ctx = SimpleNamespace(method=message.method, params=message.params, meta={})

            async def unexpected_call(ctx):
                raise AssertionError("Invalid arguments reached the handler")

            async def validate(ctx):
                return await _validate_inputs("unified")(ctx, unexpected_call)

            return await _audit_tools(self.context, "unified")(ctx, validate)

        def reject_constant(value):
            raise AssertionError(f"Non-JSON numeric constant: {value}")

        with captured() as stream:
            result = asyncio.run(run())
        self.assertEqual(result["structuredContent"]["error"]["code"], "invalid_request")
        entries = [
            json.loads(line, parse_constant=reject_constant)
            for line in stream.getvalue().splitlines()
        ]
        self.assertEqual(sum(row["event"] == "call_completed" for row in entries), 1)

    def test_expected_error_keeps_its_code_but_logs_no_exception_message(self):
        self.evs.errors["get_concept"] = UpstreamUnavailableError("upstream private canary")
        with captured() as stream:
            failed, result = self.call("get_concept", code="C3262")

        (record,) = records(stream)
        self.assertTrue(failed)
        self.assertEqual(record["responseCode"], result["error"]["code"])
        self.assertEqual(record["responseCode"], "upstream_unavailable")
        self.assertNotIn("upstream private canary", stream.getvalue())

    def test_unexpected_errors_propagate_and_do_not_contaminate_the_next_call(self):
        self.evs.errors["get_concept"] = RuntimeError("bug canary")
        with captured() as stream:
            with self.assertRaisesRegex(RuntimeError, "bug canary"):
                invoke(self.context, "get_concept", **pinned(code="C3262"))
            self.evs.errors.clear()
            result = invoke(self.context, "get_concept", **pinned(code="C3262"))

        failure, success = records(stream)
        self.assertEqual(
            (failure["status"], failure["errorType"], failure["resultSize"]),
            ("error", "RuntimeError", None),
        )
        self.assertNotEqual(failure["correlationId"], success["correlationId"])
        self.assertEqual(success["correlationId"], result["provenance"]["correlationId"])
        self.assertIsNone(current_correlation_id())
        self.assertNotIn("bug canary", stream.getvalue())

    def test_result_size_is_the_size_of_the_text_sent_to_the_client(self):
        self.evs.concepts["C3262"] = dict(self.evs.concepts["C3262"], name="Café")
        with captured() as stream:
            result = self.session(
                lambda client: client.call_tool("get_concept", pinned(code="C3262"))
            )

        (record,) = records(stream)
        text = result.content[0].text
        self.assertEqual(json.loads(text), result.structured_content)
        self.assertEqual(text, compact_text(result.structured_content))
        self.assertEqual(record["resultSize"], len(text.encode("utf-8")))

    def test_truncation_is_copied_with_per_kind_counts(self):
        with captured() as stream:
            failed, result = self.call("get_concept_neighborhood", code="C3262", maxNodes=1)

        (record,) = records(stream)
        self.assertFalse(failed)
        self.assertTrue(result["truncation"]["occurred"])
        self.assertIn("perKind", result["truncation"])
        self.assertEqual(record["truncation"], result["truncation"])
        self.assertEqual(len(record["release"]["resolved"]), 1)

    def test_empty_index_search_has_zero_requests_and_hashed_query(self):
        invoke(self.context, "index_codes", ["C3262"])
        with captured() as stream:
            result = invoke(self.context, "search", "absent-query-canary", mode="bm25")

        (record,) = records(stream)
        self.assertEqual(result["hits"], [])
        self.assertEqual(record["outboundRequests"], 0)
        self.assertEqual(record["truncation"], {"occurred": False})
        self.assertEqual(record["release"]["resolved"], [result["provenance"]["release"]])
        self.assertNotIn("absent-query-canary", stream.getvalue())

    def test_release_mismatch_records_the_requested_release_and_error(self):
        invoke(self.context, "index_codes", ["C3262"])
        with captured() as stream:
            result = invoke(
                self.context, "search_concepts", "ncit", "query-canary", "old", mode="semantic"
            )

        (record,) = records(stream)
        self.assertEqual(result["error"]["code"], "release_mismatch")
        self.assertEqual(record["release"]["requested"], "old")
        self.assertEqual(record["responseCode"], "release_mismatch")
        self.assertNotIn("query-canary", stream.getvalue())

    def test_resource_and_concurrent_mcp_calls_each_have_their_own_record(self):
        async def calls(client):
            return await asyncio.gather(
                client.call_tool(
                    "get_concept", pinned(code="C3262"), meta={"correlationId": "one"}
                ),
                client.call_tool(
                    "get_concept", pinned(code="C4741"), meta={"correlationId": "two"}
                ),
            )

        with captured() as stream:
            self.session(calls)
            resource = self.read("ncit://concept/26.06e/C3262")

        found = records(stream)
        self.assertEqual(len(found), 3)
        by_id = {record["correlationId"]: record for record in found}
        self.assertEqual(by_id["one"]["parameters"]["code"], "C3262")
        self.assertEqual(by_id["two"]["parameters"]["code"], "C4741")
        self.assertEqual(by_id[resource["provenance"]["correlationId"]]["tool"], "concept_resource")


class AuditHttpTest(ServerTestCase):
    def test_split_batches_count_every_actual_attempt(self):
        root = concept(
            "C1",
            active=True,
            children=[{"code": "C2", "name": "Two"}, {"code": "C3", "name": "Three"}],
        )
        server = self.serve(
            Reply(body=json.dumps(catalogue_rows("role")).encode()),
            Reply(body=json.dumps(catalogue_rows("association")).encode()),
            Reply(body=json.dumps([root]).encode()),
            Reply(body=b"x" * 2000),
            Reply(body=json.dumps([concept("C2", active=True)]).encode()),
            Reply(body=json.dumps([concept("C3", active=True)]).encode()),
        )
        with tempfile.TemporaryDirectory() as directory:
            context = Context(
                Settings(data_dir=Path(directory)),
                evs=EVSClient(server.url, max_response_bytes=1000, user_agent=product_user_agent()),
            )
            with captured() as stream:
                result = invoke(
                    context,
                    "get_concept_neighborhood",
                    **pinned(code="C1", depth=1, kinds=["child"]),
                )

        (record,) = records(stream)
        self.assertEqual([node["code"] for node in result["nodes"]], ["C1", "C2", "C3"])
        self.assertEqual(record["outboundRequests"], 6)
        self.assertEqual(len(server.seen), 6)
        self.assertTrue(
            all(
                headers["x-correlation-id"] == record["correlationId"] for _, headers in server.seen
            )
        )

    def test_retries_count_actual_requests_preserve_the_observer_and_share_correlation(self):
        server = self.serve(Reply(503), Reply(429, headers={"Retry-After": "0"}))
        client = self.client(server)
        observed = []
        client.on_request = observed.append
        with captured() as stream, audited("lookup", {}, {}, (), "http-correlation") as record:
            record.result = client.get_json("/x")

        (result,) = records(stream)
        self.assertEqual(result["outboundRequests"], len(server.seen))
        self.assertEqual(result["outboundRequests"], 3)
        self.assertEqual(len(observed), 3)
        self.assertEqual(
            {headers["x-correlation-id"] for _, headers in server.seen}, {"http-correlation"}
        )
        self.assertEqual(
            {json.loads(line)["correlationId"] for line in stream.getvalue().splitlines()},
            {"http-correlation"},
        )

    def test_budget_refusal_is_not_an_outbound_attempt(self):
        server = self.serve(Reply(503))
        client = self.client(server)
        with (
            captured() as stream,
            self.assertRaises(RequestBudgetError),
            audited("walk", {}, {}, ()),
            budgeted(Budget(requests=1)),
        ):
            client.get_json("/x")

        (record,) = records(stream)
        self.assertEqual(record["outboundRequests"], 1)
        self.assertEqual(len(server.seen), 1)

    def test_shared_client_counts_are_isolated_across_concurrent_calls(self):
        server = self.serve()
        client = self.client(server)
        barrier = Barrier(2)

        def run(count):
            with audited("walk", {}, {}, (), str(count)) as record:
                barrier.wait(timeout=5)
                for _ in range(count):
                    record.result = client.get_json("/x")

        with captured() as stream, ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(run, [1, 2]))

        self.assertEqual(
            {record["correlationId"]: record["outboundRequests"] for record in records(stream)},
            {"1": 1, "2": 2},
        )
        self.assertEqual(len(server.seen), 3)


ROOT = Path(__file__).resolve().parents[1]
TOOLS = yaml.safe_load((ROOT / "spec/tools.yaml").read_text())


def _free_text_roots(tool: str) -> set[str]:
    # The specification's free-text argument names, without index or path suffixes.
    return {p.split("[")[0].split(".")[0] for p in TOOLS[tool].get("free_text", [])}


# Parameters whose values are free text or opaque (audited as a SHA-256 digest); every other
# parameter of these tools is an identifier, a closed value or a number, audited in the clear.
HASHED = {
    "get_form": {"keyword"},
    "get_permissible_value": set(),
    "get_code_map": {"targetContext", "cursor"},
    "match_data_elements": {"entities", "modelVariant", "filters"},
    "match_value_meanings": {"values"},
    "get_data_element": {"longName", "questionText"},
    "search_data_elements": {"query", "filters", "cursor"},
    "list_contexts": {"cursor"},
    "list_classification_schemes": {"context", "cursor"},
    "resolve_registry_release": set(),
}


class CaDSRAuditClassificationTest(TestCase):
    def test_each_cadsr_parameter_has_the_specification_audit_class(self):
        tools = {spec.name: spec for spec in SPECS if spec.group == "cadsr" and spec.name in TOOLS}
        self.assertEqual(set(tools), set(HASHED))
        for name, spec in tools.items():
            # The specification's free-text arguments are hashed; the table may hash more
            # (identifiers that correlate callers), never less.
            self.assertLessEqual(_free_text_roots(name), HASHED[name], name)
            for parameter in spec.parameters:
                with self.subTest(tool=name, parameter=parameter.name):
                    expected = "hash" if parameter.name in HASHED[name] else "plain"
                    self.assertEqual(spec.audit.get(parameter.name), expected)

    def test_data_element_resource_identifiers_are_plain_audit_fields(self):
        patterns = TOOLS["get_data_element"]["patterns"]
        resources = [
            spec for spec in SPECS if spec.uri and spec.uri.startswith("cadsr://data-element/")
        ]
        self.assertEqual(len(resources), 2)
        for spec in resources:
            with self.subTest(uri=spec.uri):
                self.assertTrue(all(parameter.name in patterns for parameter in spec.parameters))
                self.assertEqual(
                    spec.audit, {parameter.name: "plain" for parameter in spec.parameters}
                )


def digest_record(value):
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return {"sha256": hashlib.sha256(encoded.encode()).hexdigest()}


class RegistryAuditPrivacyTest(ServerFixture):
    def test_search_cursor_is_hashed_even_when_the_option_is_refused(self):
        cursor = "private-cursor-canary"
        with captured() as stream:
            result = invoke(
                self.context, "search_concepts", "ncit", "26.06e", "Neoplasm", cursor=cursor
            )
        self.assertEqual(result["error"]["code"], "invalid_request")
        (record,) = records(stream)
        self.assertEqual(record["parameters"]["cursor"], digest_record(cursor))
        self.assertNotIn(cursor, stream.getvalue())

    def test_legacy_relationship_filter_is_hashed_in_completion_record(self):
        names = ["Disease_Has_Abnormal_Cell"]
        with captured() as stream:
            result = invoke(
                self.context, "traverse", ["C3262"], max_depth=1, relationship_names=names
            )
        self.assertEqual([edge["target_code"] for edge in result["edges"]], ["C4741", "C12922"])
        (record,) = records(stream)
        self.assertEqual(record["parameters"]["relationship_names"], digest_record(names))
        self.assertNotIn(names[0], stream.getvalue())

    def test_invisible_tool_names_and_arguments_are_hashed(self):
        async def rejected(_):
            return {"isError": True}

        request = SimpleNamespace(
            method="tools/call",
            params={"name": "get_concept", "arguments": {"code": "C3262"}},
            meta={"correlationId": "hidden-tool"},
        )
        with captured() as stream:
            result = asyncio.run(_audit_tools(self.context, "cadsr")(request, rejected))
        self.assertEqual(result, {"isError": True})
        (record,) = records(stream)
        self.assertEqual(
            record["tool"], json.dumps(digest_record("get_concept"), separators=(",", ":"))
        )
        self.assertEqual(list(record["parameters"].values()), [digest_record("C3262")])
        self.assertEqual(record["correlationId"], "hidden-tool")


class AuditDiagnosticPrivacyTest(unittest.TestCase):
    def test_request_and_retry_diagnostics_hash_each_original_value(self):
        client = EVSClient(
            "https://example.invalid", retry_backoff_seconds=0, user_agent=product_user_agent()
        ).http
        path = "/private-path-canary"
        reason = f"EVS request failed for {path}: HTTP 503 Service Unavailable"
        with (
            captured() as stream,
            patch(
                "nci_sip_mcp.http_client._open", side_effect=[http_error(503), FakeResponse(b"{}")]
            ),
        ):
            self.assertEqual(client.get_json(path), {})
        attempts = records(stream, "upstream_request")
        self.assertEqual([row["urlHash"] for row in attempts], [digest(client.base_url + path)] * 2)
        (retry,) = records(stream, "upstream_request_retry")
        self.assertEqual(retry["pathHash"], digest(path))
        self.assertEqual(retry["reasonHash"], digest(reason))
        for value in (client.base_url, path, reason):
            self.assertNotIn(value, stream.getvalue())

    def test_password_alone_is_redacted_from_a_diagnostic(self):
        with captured() as stream, audited("lookup", {}, {}, secrets(None, "user:pw123")):
            emit(logging.getLogger("nci_sip_mcp.test"), logging.WARNING, "canary", value="pw123")
        self.assertEqual(records(stream, "canary")[0]["value"], "[redacted]")
        self.assertNotIn("pw123", stream.getvalue())

    def test_hashes_do_not_depend_on_object_key_order(self):
        self.assertEqual(hashed({"a": 1, "b": 2}), hashed({"b": 2, "a": 1}))
        self.assertEqual(hashed({"b": 2, "a": 1}), {"sha256": digest({"a": 1, "b": 2})})

    def test_raw_upstream_release_is_excluded_and_platform_releases_are_deduplicated(self):
        release = {"terminology": "ncit", "identifier": "26.06e"}
        with captured() as stream, audited("lookup", {}, {}, ()) as record:
            record.result = {
                "nodes": [{"provenance": {"release": dict(release)}} for _ in range(3)],
                "raw": {"provenance": {"release": {"identifier": "upstream-canary"}}},
            }
        self.assertEqual(records(stream)[0]["release"]["resolved"], [release])
        self.assertNotIn("upstream-canary", stream.getvalue())

    def test_cancellation_propagates_and_discards_a_previously_set_result(self):
        # Nested contexts let CodeQL see that assertRaises consumes the cancellation.
        with captured() as stream:  # noqa: SIM117
            with self.assertRaises(asyncio.CancelledError):
                with audited("lookup", {}, {}, ()) as record:
                    record.result = {"code": "C1"}
                    raise asyncio.CancelledError()
        (result,) = records(stream)
        self.assertEqual(result["errorType"], "CancelledError")
        self.assertEqual(result["status"], "error")
        self.assertIsNone(result["resultSize"])


class IndexActivationAuditTest(ServerFixture):
    def test_each_index_activation_logs_its_new_release_and_build_identity(self):
        with captured() as stream:
            for version in ("26.06e", "26.07a"):
                self.local_index.upsert_concepts(
                    normalized([concept("C1", version=version)]),
                    None,
                    self.context.embedding_provider,
                )
        first, second = records(stream, "index_activated")
        self.assertEqual((first["release"], second["release"]), ("26.06e", "26.07a"))
        self.assertNotEqual(first["build_id"], second["build_id"])
        self.assertEqual(self.context.index.get_active_manifest().release_version, "26.07a")


@patch("nci_sip_mcp.server.configure_logging")
class CrossDomainAuditPrivacyTest(ServerFixture):
    def setUp(self):
        super().setUp()
        self.context.ssis = FakeSSIS(self.settings)

    def test_all_cross_domain_parameters_keep_their_audit_privacy_class(self, _):
        calls = (
            (
                "find_data_elements_for_concept",
                {
                    "conceptCode": "bad",
                    "terminology": "ncit",
                    "release": "26.06e",
                    "expandDescendants": True,
                    "includePermissibleValues": True,
                    "limit": 17,
                    "cursor": "cursor-canary",
                },
                {"cursor"},
            ),
            (
                "get_concept_for_permissible_value",
                {
                    "permissibleValueId": "456",
                    "dataElementId": "123",
                    "value": "value-canary",
                    "release": "26.06e",
                    "version": "4.0",
                },
                {"value"},
            ),
            (
                "resolve_stored_value",
                {
                    "conceptCode": "bad",
                    "commons": "commons-canary",
                    "dataElementId": "123",
                    "release": "26.06e",
                },
                {"commons"},
            ),
            ("get_release_alignment", {"maxIntervalDays": -1}, set()),
        )
        for name, arguments, private in calls:
            with self.subTest(tool=name), captured() as stream:
                result = invoke(self.context, name, **arguments)
                self.assertEqual(result["error"]["code"], "invalid_request")
                expected = {
                    key: {"sha256": digest(value)} if key in private else value
                    for key, value in arguments.items()
                }
                self.assertEqual(records(stream)[0]["parameters"], expected)
                self.assertTrue(all(arguments[key] not in stream.getvalue() for key in private))
