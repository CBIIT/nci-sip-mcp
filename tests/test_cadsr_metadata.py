"""CDE metadata is passed through the real MCP boundary, never filled from another call."""

import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from fakes import cde_response, cde_row, data_element, form, vm_response, vm_row
from nci_sip_mcp.cadsr import CDE_MATCH, CaDSRClient
from test_evs_client import FakeResponse
from test_server import ServerFixture

FIXTURES = Path(__file__).resolve().parents[1] / "acceptance/fixtures/recorded/cadsr"
SCALARS = {
    "definition": "Text designations that identify sex.  ",
    "shortName": "PRSN_SEX_TX_TP",
    "latestVersionIndicator": "Yes",
    "beginDate": "2010-05-07",
    "endDate": None,
    "origin": "NCISTD: NCI CRF Harmonization and Standards Initiative",
}


def recorded_element():
    return json.loads((FIXTURES / "data-element-2200604.json").read_text())["response"]["body"]


class MetadataFixture(ServerFixture):
    def setUp(self):
        super().setUp()
        self.context.cadsr = CaDSRClient(
            replace(self.settings, cadsr_base_url="https://cadsr.test")
        )

    def answer(self, body, tool="get_data_element", **arguments):
        with patch(
            "nci_sip_mcp.http_client._open",
            side_effect=[FakeResponse(json.dumps(body).encode())],
        ) as opened:
            result = self.session(lambda client: client.call_tool(tool, arguments))
        self.paths = [urlsplit(call.args[0].full_url).path for call in opened.call_args_list]
        return result


class DataElementMetadataTest(MetadataFixture):
    def test_recorded_scalars_keep_whitespace_and_null_without_fetching_detail(self):
        result = self.answer(recorded_element(), publicId="2200604")

        self.assertEqual({key: result.structured_content.get(key) for key in SCALARS}, SCALARS)
        self.assertFalse(result.is_error)
        self.assertNotIn("referenceDocuments", result.structured_content)
        self.assertEqual(self.paths, ["/NCIAPI/1.0/api/DataElement/2200604"])

    def test_reference_documents_include_keeps_every_recorded_row_and_unknown_field(self):
        body = recorded_element()
        documents = body["DataElement"]["ReferenceDocuments"]
        self.assertEqual(len(documents), 10)
        documents.append({"documentType": "Future type", "extra": {"kept": [1, None]}})
        documents.insert(5, {})

        result = self.answer(body, publicId="2200604", include=["referenceDocuments"])

        self.assertEqual(result.structured_content.get("referenceDocuments"), documents)
        self.assertFalse(result.is_error)
        self.assertNotIn("alternateNames", result.structured_content)
        self.assertNotIn("valueDomain", result.structured_content)
        self.assertEqual(self.paths, ["/NCIAPI/1.0/api/DataElement/2200604"])

    def test_absent_and_null_scalars_stay_distinct_without_backfill(self):
        for extras in ({}, dict.fromkeys(SCALARS)):
            with self.subTest(extras=extras):
                result = self.answer({"DataElement": data_element(**extras)}, publicId="123")

                self.assertEqual(
                    {
                        key: result.structured_content[key]
                        for key in SCALARS
                        if key in result.structured_content
                    },
                    extras,
                )
                self.assertFalse(result.is_error)
                self.assertEqual(self.paths, ["/NCIAPI/1.0/api/DataElement/123"])

    def test_malformed_metadata_is_an_upstream_error_not_success(self):
        for key in SCALARS:
            with self.subTest(field=key):
                result = self.answer({"DataElement": data_element(**{key: []})}, publicId="123")

                self.assertTrue(result.is_error)
                self.assertEqual(result.structured_content["error"]["code"], "upstream_unavailable")

    def test_malformed_document_sections_are_not_silently_empty(self):
        for documents in (None, {}, ["not a row"]):
            with self.subTest(documents=documents):
                result = self.answer(
                    {"DataElement": data_element(ReferenceDocuments=documents)},
                    publicId="123",
                    include=["referenceDocuments"],
                )

                self.assertEqual(
                    result.structured_content.get("error", {}).get("code"), "upstream_unavailable"
                )
                self.assertTrue(result.is_error)

    def test_match_metadata_comes_only_from_its_producing_answer(self):
        rows = [cde_row("123", definition="Own definition"), cde_row("456")]
        result = self.answer(
            cde_response(matches=rows), "match_data_elements", entities=[{"name": "Q"}]
        )

        first, second = (match["dataElement"] for match in result.structured_content["matches"])
        self.assertEqual(first["definition"], "Own definition")
        self.assertEqual(set(SCALARS) & second.keys(), set())
        self.assertEqual(len(self.paths), 1)

    def test_search_preserves_recorded_metadata_without_a_detail_fetch(self):
        body = json.loads((FIXTURES / "question-text-sex-of-a-person.json").read_text())[
            "response"
        ]["body"]
        result = self.answer(body, "search_data_elements", query="Sex of a Person")

        item = result.structured_content["results"][0]["dataElement"]
        self.assertEqual({key: item.get(key) for key in SCALARS}, SCALARS)
        self.assertEqual(self.paths, ["/NCIAPI/1.0/api/DataElement/search"])


FORM_SCALARS = ("definition", "type", "beginDate", "endDate")
FORM_ARRAYS = ("instructions", "classificationSchemes")


class FormAndMatchMetadataTest(MetadataFixture):
    def test_recorded_form_metadata_survives_both_module_choices(self):
        body = json.loads((FIXTURES / "form-5406471.json").read_text())["response"]["body"]
        raw = body["form"]
        raw["instructions"].insert(0, {})
        expected = {key: raw[key] for key in (*FORM_SCALARS, *FORM_ARRAYS)}
        for include in (True, False):
            with self.subTest(includeModules=include):
                result = self.answer(body, "get_form", publicId="5406471", includeModules=include)
                content = result.structured_content

                self.assertEqual({key: content.get(key) for key in expected}, expected)
                self.assertFalse(result.is_error)
                self.assertEqual("modules" in content, include)
                self.assertEqual(content["workflowStatus"], "RETIRED ARCHIVED")
                self.assertNotIn("latestVersionIndicator", content)
                self.assertEqual(self.paths, ["/NCIFormAPI.v2_0:NciFormApiRad/Form/5406471"])

    def test_form_scalars_are_supplied_nullable_text_without_backfill(self):
        for extras in ({}, dict.fromkeys(FORM_SCALARS), dict.fromkeys(FORM_SCALARS, "")):
            with self.subTest(extras=extras):
                result = self.answer({"form": form(**extras)}, "get_form", publicId="123")
                content = result.structured_content

                self.assertEqual(
                    {key: content[key] for key in FORM_SCALARS if key in content},
                    extras,
                )
                self.assertFalse(result.is_error)
                self.assertEqual(self.paths, ["/NCIFormAPI.v2_0:NciFormApiRad/Form/123"])

    def test_form_array_absence_and_empty_remain_distinct(self):
        # caDSR sending null is sending none: the list is absent, as when the key is missing.
        empty = {key: [] for key in FORM_ARRAYS}
        for extras, expected in (({}, {}), (dict.fromkeys(FORM_ARRAYS), {}), (empty, empty)):
            with self.subTest(extras=extras):
                result = self.answer(
                    {"form": form(**extras)}, "get_form", publicId="123", includeModules=False
                )

                self.assertEqual(
                    {
                        key: result.structured_content[key]
                        for key in FORM_ARRAYS
                        if key in result.structured_content
                    },
                    expected,
                )
                self.assertFalse(result.is_error)
                self.assertNotIn("modules", result.structured_content)

    def test_form_malformed_metadata_is_refused_even_without_modules(self):
        cases = [(key, []) for key in FORM_SCALARS] + [
            (key, value) for key in FORM_ARRAYS for value in ({}, "rows", ["not an object"])
        ]
        for key, value in cases:
            with self.subTest(field=key, value=value):
                result = self.answer(
                    {"form": form(**{key: value})}, "get_form", publicId="123", includeModules=False
                )

                self.assertTrue(result.is_error)
                self.assertEqual(result.structured_content["error"]["code"], "upstream_unavailable")

    def test_recorded_match_definitions_keep_their_own_values_and_order(self):
        body = json.loads((FIXTURES / "vm-match-male.json").read_text())["response"]["body"]
        rows = body["matchResults"][0]["matches"]
        result = self.answer(body, "match_value_meanings", values=["Male"])
        items = [match["item"] for match in result.structured_content["matches"]]

        self.assertEqual(
            [item.get("matchedDefinition") for item in items],
            [row["matchedDefinition"] for row in rows],
        )
        self.assertFalse(result.is_error)
        self.assertEqual(self.paths, ["/vmMatch/v1/vmMatch"])

    def test_a_match_without_a_definition_omits_it_and_copies_no_other(self):
        # vmMatch item metadata is absent, never null, where the platform gives none.
        rows = [
            vm_row(matchedDefinition="First "),
            vm_row(),
            vm_row(matchedDefinition=None),
            vm_row(matchedDefinition=""),
        ]
        result = self.answer(vm_response(matches=rows), "match_value_meanings", values=["Q"])
        items = [match["item"] for match in result.structured_content["matches"]]

        self.assertEqual(
            [item.get("matchedDefinition", "absent") for item in items],
            ["First ", "absent", "absent", ""],
        )
        self.assertFalse(result.is_error)
        self.assertEqual(self.paths, ["/vmMatch/v1/vmMatch"])

    def test_each_match_carries_only_its_own_licence_text(self):
        rows = [vm_row(licenseText="Licence of the first"), vm_row(itemId="456", version="3")]
        result = self.answer(vm_response(matches=rows), "match_value_meanings", values=["Q"])
        first, second = (
            match["item"]["provenance"] for match in result.structured_content["matches"]
        )

        self.assertEqual(first["attribution"], "Licence of the first")
        self.assertNotIn("attribution", second)
        self.assertEqual(second["upstream"], {"itemId": "456", "version": "3"})

    def test_malformed_match_definition_refuses_the_whole_answer(self):
        for invalid in ([], {}, 42, False):
            with self.subTest(value=invalid):
                rows = [vm_row(matchedDefinition="First"), vm_row(matchedDefinition=invalid)]
                result = self.answer(
                    vm_response(matches=rows), "match_value_meanings", values=["Q"]
                )

                self.assertTrue(result.is_error)
                self.assertEqual(result.structured_content["error"]["code"], "upstream_unavailable")
                self.assertNotIn("matches", result.structured_content)


class Missing:
    """A field caDSR left out of its answer."""

    def __repr__(self):
        return "MISSING"


MISSING = Missing()
NOT_TEXT = (MISSING, None, 7)
NAME_AND_STATUS = ("longName", "workflowStatus", "registrationStatus")
NULLABLE = ("context", "dateCreated", "dateModified")
ELEMENT_PATH = "/NCIAPI/1.0/api/DataElement/123"
FORM_PATH = "/NCIFormAPI.v2_0:NciFormApiRad/Form/123"


def broken(raw, field, value):
    """raw with field left out (MISSING) or set to value."""
    changed = {key: item for key, item in raw.items() if key != field}
    return changed if value is MISSING else changed | {field: value}


def nested_element(record, field, value):
    """A data element whose scheme, scheme item or value meaning has field broken."""
    parts = {
        "scheme": {"publicId": "700", "version": "1", "longName": "Scheme", "context": "TEST"},
        "item": {"publicId": "701", "version": "1", "longName": "Item"},
        "meaning": {"publicId": "801", "version": "1", "longName": "Meaning", "Concepts": []},
    }
    parts[record] = broken(parts[record], field, value)
    scheme = parts["scheme"] | {"ClassificationSchemeItems": [parts["item"]]}
    value_row = {"publicId": "800", "value": "A", "ValueMeaning": parts["meaning"]}
    return data_element(
        ClassificationSchemes=[scheme], ValueDomain={"PermissibleValues": [value_row]}
    )


class RequiredNameAndStatusTest(MetadataFixture):
    """A registry item's name and statuses are never null (spec/records.yaml, #295)."""

    def assert_refused(self, result, section, path):
        self.assertTrue(result.is_error)
        error = result.structured_content["error"]
        self.assertEqual(
            {key: value for key, value in error.items() if key != "correlationId"},
            {
                "code": "upstream_unavailable",
                "message": (
                    f"caDSR returned malformed {section}. Ask the operator to check the API."
                ),
                "details": {"surface": "cadsr"},
            },
        )
        self.assertEqual(self.paths, [path])

    def test_a_data_element_without_its_name_or_status_is_refused(self):
        for field in NAME_AND_STATUS:
            for value in NOT_TEXT:
                with self.subTest(field=field, value=value):
                    raw = broken(data_element(), field, value)
                    result = self.answer({"DataElement": raw}, publicId="123")

                    self.assert_refused(result, f"data element {field}", ELEMENT_PATH)

    def test_a_form_without_its_name_or_status_is_refused(self):
        for field in NAME_AND_STATUS:
            for value in NOT_TEXT:
                for modules in (True, False):
                    with self.subTest(field=field, value=value, includeModules=modules):
                        result = self.answer(
                            {"form": broken(form(), field, value)},
                            "get_form",
                            publicId="123",
                            includeModules=modules,
                        )

                        self.assert_refused(result, f"form {field}", FORM_PATH)

    def test_one_search_row_without_its_name_or_status_fails_the_page(self):
        for field in NAME_AND_STATUS:
            for value in NOT_TEXT:
                with self.subTest(field=field, value=value):
                    rows = [data_element("123"), broken(data_element("456"), field, value)]
                    result = self.answer({"DataElements": rows}, "search_data_elements", query="Q")

                    self.assert_refused(
                        result, f"data element {field}", "/NCIAPI/1.0/api/DataElement/search"
                    )
                    self.assertNotIn("results", result.structured_content)

    def test_one_match_without_its_name_or_status_fails_the_page(self):
        for field in NAME_AND_STATUS:
            for value in NOT_TEXT:
                with self.subTest(field=field, value=value):
                    rows = [cde_row("123"), broken(cde_row("456"), field, value)]
                    result = self.answer(
                        cde_response(matches=rows), "match_data_elements", entities=[{"name": "Q"}]
                    )

                    self.assert_refused(result, f"data element {field}", CDE_MATCH)
                    self.assertNotIn("matches", result.structured_content)

    def test_a_nested_scheme_item_or_value_meaning_without_its_name_is_refused(self):
        cases = (
            ("classificationSchemes", "scheme", "longName", "classification scheme longName"),
            ("classificationSchemes", "scheme", "context", "classification scheme context"),
            ("classificationSchemes", "item", "longName", "classification scheme item longName"),
            ("permissibleValues", "meaning", "longName", "value meaning longName"),
        )
        for section, record, field, malformed in cases:
            for value in NOT_TEXT:
                with self.subTest(record=record, field=field, value=value):
                    raw = nested_element(record, field, value)
                    result = self.answer({"DataElement": raw}, publicId="123", include=[section])

                    self.assert_refused(result, malformed, ELEMENT_PATH)

    def test_context_and_dates_preserve_absence_and_explicit_null(self):
        calls = (
            ("get_data_element", "DataElement", data_element(), {}),
            ("get_form", "form", form(), {"includeModules": False}),
        )
        for tool, key, raw, extra in calls:
            for field in NULLABLE:
                for value in (MISSING, None):
                    with self.subTest(tool=tool, field=field, value=value):
                        result = self.answer(
                            {key: broken(raw, field, value)}, tool, publicId="123", **extra
                        )

                        self.assertFalse(result.is_error)
                        self.assertEqual(
                            {field: result.structured_content[field]}
                            if field in result.structured_content
                            else {},
                            {} if value is MISSING else {field: None},
                        )
                        self.assertEqual(len(self.paths), 1)

    def test_an_empty_name_or_status_is_passed_through_as_sent(self):
        # The record says never null, not never blank; the live contract is stricter.
        calls = (
            ("get_data_element", "DataElement", data_element(), {}),
            ("get_form", "form", form(), {"includeModules": False}),
        )
        for tool, key, raw, extra in calls:
            for field in NAME_AND_STATUS:
                with self.subTest(tool=tool, field=field):
                    result = self.answer({key: raw | {field: ""}}, tool, publicId="123", **extra)

                    self.assertFalse(result.is_error)
                    self.assertEqual(result.structured_content[field], "")
