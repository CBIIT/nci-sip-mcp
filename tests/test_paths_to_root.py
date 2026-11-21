import json
from unittest.mock import patch

from jsonschema import Draft202012Validator

from fakes import concept
from nci_sip_mcp.evs import EVSClient
from nci_sip_mcp.registry import invoke
from test_evs_client import FakeResponse, http_error
from test_server import ServerFixture


class PathsToRootTest(ServerFixture):
    def setUp(self):
        super().setUp()
        self.seed = concept("C1", active=True)
        self.root = concept(
            "C9", active=False, conceptStatus="Retired_Concept", licenseText="Verbatim licence"
        )
        self.paths = [[self.seed, concept("C2", active=True), self.root], [self.seed, self.root]]
        self.context.evs = EVSClient("https://example.invalid", max_attempts=1)

    def paths_result(self, payload, **arguments):
        replies = [FakeResponse(json.dumps(item).encode()) for item in (self.seed, payload)]
        with patch("nci_sip_mcp.http_client._open", side_effect=replies) as opened:
            result = invoke(
                self.context,
                "get_concept_hierarchy",
                **(
                    {
                        "terminology": "ncit",
                        "release": "26.06e",
                        "code": "C1",
                        "direction": "pathsToRoot",
                    }
                    | arguments
                ),
            )
        return result, opened

    def test_platform_paths_keep_order_and_nodes_keep_status_provenance_and_licence(self):
        result, opened = self.paths_result(self.paths, depth=0, limit=1, cursor="ignored")
        self.assertEqual(result["paths"], [["C1", "C2", "C9"], ["C1", "C9"]])
        self.assertEqual([node["code"] for node in result["nodes"]], ["C2", "C9"])
        root = result["nodes"][1]
        self.assertFalse(root["active"])
        self.assertEqual(root["status"], "Retired_Concept")
        self.assertEqual(root["provenance"]["attribution"], "Verbatim licence")
        self.assertEqual(root["provenance"]["depth"], 2)
        self.assertEqual(root["provenance"]["relationship"], {"kind": "parent", "name": ""})
        self.assertEqual(result["truncation"], {"occurred": False})
        self.assertNotIn("nextCursor", result)
        self.assertEqual(
            opened.call_args.args[0].full_url,
            "https://example.invalid/api/v1/concept/ncit_26.06e/C1/pathsToRoot?include=minimal",
        )
        tool = next(
            tool
            for tool in self.session(lambda client: client.list_tools()).tools
            if tool.name == "get_concept_hierarchy"
        )
        Draft202012Validator(tool.output_schema).validate(result)

    def test_empty_paths_are_attributed_to_the_verified_seed(self):
        for paths in ([], [[self.seed]]):
            with self.subTest(paths=paths):
                result, _ = self.paths_result(paths)
                self.assertEqual(result["nodes"], [])
                self.assertEqual(result["paths"], [["C1"]] if paths else [])
                self.assertEqual(
                    result["provenance"]["upstream"], {"terminology": "ncit", "version": "26.06e"}
                )

    def test_a_wrong_seed_identity_never_starts_the_paths_request(self):
        self.seed = self.seed | {"code": "C3"}
        result, opened = self.paths_result([])
        self.assertEqual(result["error"]["code"], "upstream_unavailable")
        self.assertEqual(opened.call_count, 1)

    def test_malformed_paths_never_become_partial_success(self):
        for paths in (
            {},
            [None],
            [[]],
            [[self.root]],
            [[self.seed, self.seed]],
            [[self.seed, {"code": 3}]],
        ):
            with self.subTest(paths=paths):
                result, _ = self.paths_result(paths)
                self.assertEqual(result["error"]["code"], "upstream_unavailable")
                self.assertNotIn("paths", result)

    def test_every_path_concept_must_belong_to_the_requested_release_and_terminology(self):
        for changed, expected in (
            ({"version": "other"}, "release_mismatch"),
            ({"terminology": "other"}, "upstream_unavailable"),
            ({"active": None}, "upstream_unavailable"),
        ):
            with self.subTest(changed=changed):
                result, _ = self.paths_result([[self.seed, self.root | changed]])
                self.assertEqual(result["error"]["code"], expected)

    def test_unknown_seed_is_not_found_and_no_paths_request_is_made(self):
        with patch(
            "nci_sip_mcp.http_client._open",
            side_effect=http_error(404, b'{"message":"Code not found = C1"}'),
        ) as opened:
            result = invoke(
                self.context,
                "get_concept_hierarchy",
                terminology="ncit",
                release="26.06e",
                code="C1",
                direction="pathsToRoot",
            )
        self.assertEqual(result["error"]["code"], "not_found")
        self.assertEqual(opened.call_count, 1)
