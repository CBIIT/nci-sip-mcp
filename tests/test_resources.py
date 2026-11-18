"""Release-pinned MCP resources, including historical metadata and active-only indexing."""

import json
from dataclasses import replace
from unittest.mock import patch

from mcp.shared.exceptions import MCPError

from fakes import concept, terminology_row
from nci_sip_mcp.registry import invoke
from test_index import normalized
from test_server import ServerFixture


@patch("nci_sip_mcp.server.configure_logging")
class ResourceTest(ServerFixture):
    def error(self, uri):
        with self.assertRaises(MCPError) as raised:
            self.read(uri)
        return json.loads(str(raised.exception))["error"]

    def test_templates_are_exposed_only_in_their_profiles(self, _):
        evs = {
            "ncit://concept/{release}/{code}",
            "ncit://release/{version}",
            "ncit://index/manifest/{release}",
        }
        cadsr = {"cadsr://data-element/{publicId}", "cadsr://data-element/{publicId}/{version}"}
        expected = {"evs": evs, "cadsr": cadsr, "unified": evs | cadsr}
        for profile in ("evs", "unified", "cadsr"):
            with self.subTest(profile=profile):
                self.settings = replace(self.settings, profile=profile)
                listed = self.session(lambda client: client.list_resource_templates())
                self.assertEqual(
                    {row.uri_template for row in listed.resource_templates},
                    expected[profile],
                )
                resources = self.session(lambda client: client.list_resources()).resources
                self.assertEqual(
                    {str(row.uri) for row in resources},
                    set()
                    if profile == "evs"
                    else {"cadsr://registry/release", "cadsr://crosswalk/crdc"},
                )
        with self.assertRaises(MCPError):
            self.read("ncit://concept/26.06e/C3262")

    def test_unmatched_legacy_unpinned_and_moving_uris_never_return_content(self, _):
        invoke(self.context, "index_codes", ["C3262"])
        for uri in (
            "nci-si://concept/ncit/C3262",
            "nci-si://release/ncit/26.06e",
            "nci-si://index/ncit/26.06e/manifest",
            "ncit://concept/C3262",
            "ncit://release/current",
            "ncit://release/latest",
            "ncit://index/manifest/active",
        ):
            with self.subTest(uri=uri), self.assertRaises(MCPError):
                self.read(uri)

    def test_concept_read_pins_requested_release_and_returns_every_section(self, _):
        self.evs.concepts["C3262"] = concept(
            "C3262",
            "Neoplasm",
            version="25.12e",
            active=True,
            synonyms=[{"name": "Tumor"}],
            definitions=[{"definition": "A tissue growth."}],
            properties=[{"code": "P106", "type": "Semantic_Type", "value": "Neoplastic Process"}],
        )
        result = self.read("ncit://concept/25.12e/C3262")
        self.assertEqual(result["name"], "Neoplasm")
        self.assertEqual(result["synonyms"], [{"name": "Tumor"}])
        self.assertEqual(result["definitions"], [{"definition": "A tissue growth."}])
        self.assertEqual(result["properties"], self.evs.concepts["C3262"]["properties"])
        self.assertEqual(result["semanticType"], ["Neoplastic Process"])
        self.assertEqual(result["provenance"]["release"]["identifier"], "25.12e")
        self.assertIn("ncit_25.12e/C3262", result["provenance"]["sourceUri"])
        self.assertEqual([call[0] for call in self.evs.calls], ["get_concept"])
        self.assertEqual(self.error("ncit://concept/26.06e/C3262")["code"], "release_mismatch")

    def test_historical_release_uses_its_row_and_other_served_versions(self, _):
        self.evs.rows = [
            terminology_row(),
            terminology_row("25.12e", "2025-12-29", latest=False, weekly="true"),
            terminology_row("99.99", latest=True) | {"terminology": "other"},
        ]
        result = self.read("ncit://release/25.12e")
        self.assertEqual(
            {key: value for key, value in result.items() if key != "provenance"},
            {
                "terminology": "ncit",
                "version": "25.12e",
                "channel": "weekly",
                "date": "2025-12-29",
                "alternatives": ["26.06e"],
            },
        )
        self.assertEqual(result["provenance"]["release"]["identifier"], "25.12e")
        self.assertEqual(result["provenance"]["release"]["date"], "2025-12-29")
        self.assertEqual(self.evs.calls, [("get_terminologies", None, (False, None))])

    def test_dual_tagged_release_prefers_configured_channel(self, _):
        self.evs.rows = [terminology_row(monthly="true", weekly="true")]
        for channel in ("monthly", "weekly"):
            with self.subTest(channel=channel):
                self.context.settings = replace(self.settings, release_channel=channel)
                result = self.read("ncit://release/26.06e")
                self.assertEqual(result["channel"], channel)
                self.assertEqual(result["version"], "26.06e")

    def test_missing_unrecognized_and_ambiguous_release_metadata_are_errors(self, _):
        for tags in (None, {}, {"monthly": "false"}, {"other": "true"}):
            with self.subTest(tags=tags):
                self.evs.rows = [terminology_row(latest=False) | {"tags": tags}]
                error = self.error("ncit://release/26.06e")
                self.assertEqual(error["code"], "release_not_available")
                self.assertEqual(error["details"], {"requested": "26.06e", "source": "evs"})
        self.evs.rows = [terminology_row(), terminology_row()]
        self.assertEqual(self.error("ncit://release/26.06e")["code"], "release_not_available")

    def test_inactive_matching_index_is_not_served(self, _):
        absent = self.error("ncit://index/manifest/26.06e")
        self.assertEqual(absent["code"], "capability_unavailable")
        self.assertIn("index-build", absent["message"])
        invoke(self.context, "index_codes", ["C3262"])
        candidate = self.local_index.build(
            normalized([concept("C3262", version="25.12e")]),
            None,
            self.context.embedding_provider,
        )
        mismatch = self.error("ncit://index/manifest/25.12e")
        self.assertEqual(mismatch["code"], "release_mismatch")
        self.assertEqual(
            mismatch["details"],
            {"requested": "25.12e", "served": ["26.06e"], "source": "index"},
        )
        self.local_index.activate(candidate.build_id)
        result = self.read("ncit://index/manifest/25.12e")
        self.assertEqual(result["version"], "25.12e")
        self.assertEqual(result["provenance"]["source"], "evs_index")
        self.assertEqual(result["provenance"]["servedBy"], "index")


class IndexResourceCapabilityTest(ServerFixture):
    def test_absent_index_resource_names_its_capability(self):
        result = invoke(self.context, "index_resource", "26.06e")
        self.assertEqual(result["error"]["code"], "capability_unavailable")
        self.assertEqual(result["error"]["details"], {"capability": "NCIt index"})
