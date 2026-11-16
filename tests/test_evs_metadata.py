"""What EVS says of a terminology and of a concept's place in its hierarchy reaches the caller."""

from dataclasses import replace

from fakes import concept, terminology_row
from test_server import ServerFixture

NCIT_METADATA = {"retiredStatusValue": "Retired_Concept", "uiLabel": "NCI Thesaurus"}
LICENSED_METADATA = {"retiredStatusValue": "true", "licenseText": "Use requires a licence. "}


def listing(*rows):
    return [dict(terminology_row(), **row) for row in rows]


class TerminologyMetadataTest(ServerFixture):
    def test_tags_retired_status_value_and_licence_text_pass_through_unchanged(self):
        self.evs.rows = listing(
            {"tags": {"monthly": "true", "weekly": "true"}, "metadata": NCIT_METADATA},
            {"terminology": "icd10", "tags": None, "metadata": LICENSED_METADATA},
        )

        failed, result = self.call("list_terminologies")

        self.assertFalse(failed)
        ncit, icd10 = result["terminologies"]
        self.assertEqual(ncit.get("tags"), {"monthly": "true", "weekly": "true"})
        self.assertEqual(ncit.get("retiredStatusValue"), "Retired_Concept")
        self.assertNotIn("attribution", ncit["provenance"])
        self.assertNotIn("tags", icd10)
        self.assertEqual(icd10["retiredStatusValue"], "true")
        self.assertEqual(icd10["provenance"]["attribution"], "Use requires a licence. ")

    def test_a_terminology_without_that_metadata_gets_none_of_it(self):
        # Missing, null and empty all mean EVS sent none; an empty tag mapping is still a mapping.
        ncit = {"metadata": {"uiLabel": "NCI Thesaurus", "licenseText": ""}}
        for other in (
            {"tags": None},
            {"tags": None, "metadata": None},
            {"tags": None, "metadata": {"retiredStatusValue": None}},
        ):
            with self.subTest(other=other):
                self.evs.rows = listing(ncit, {"terminology": "icd10", **other})

                failed, result = self.call("list_terminologies")

                self.assertFalse(failed)
                for item in result["terminologies"]:
                    self.assertNotIn("retiredStatusValue", item)
                    self.assertNotIn("attribution", item["provenance"])
                self.assertNotIn("tags", result["terminologies"][1])

    def test_the_retired_status_value_is_kept_verbatim(self):
        for value in ("", " Retired_Concept "):
            with self.subTest(value=value):
                self.evs.rows = listing({"metadata": {"retiredStatusValue": value}})

                failed, result = self.call("list_terminologies")

                self.assertFalse(failed)
                self.assertEqual(result["terminologies"][0].get("retiredStatusValue"), value)

    def test_an_empty_tag_mapping_is_kept(self):
        self.evs.rows = listing({}, {"terminology": "icd10", "tags": {}})

        failed, result = self.call("list_terminologies")

        self.assertFalse(failed)
        self.assertEqual(result["terminologies"][1].get("tags"), {})

    def test_truthy_non_mapping_current_ncit_tags_make_listing_unavailable(self):
        malformed = terminology_row()
        malformed["tags"] = ["monthly"]
        rows = (
            [malformed],
            [malformed, terminology_row("26.07d", monthly="true")],
        )
        for listed in rows:
            with self.subTest(row_count=len(listed)):
                self.evs.rows = listed

                result = self.session(lambda client: client.call_tool("list_terminologies", {}))
                structured = result.structured_content
                error = structured.get("error", {}) if isinstance(structured, dict) else {}

                self.assertTrue(result.is_error)
                self.assertEqual(error.get("code"), "release_not_available")
                self.assertEqual(error.get("details"), {"requested": "ncit", "source": "evs"})
                self.assertNotIn("terminologies", structured or {})

    def test_malformed_tags_on_a_non_current_ncit_release_do_not_fail_listing(self):
        malformed = terminology_row("26.05e", latest=False)
        malformed["tags"] = ["monthly"]
        self.evs.rows = [malformed, terminology_row()]

        failed, result = self.call("list_terminologies")

        self.assertFalse(failed)
        self.assertEqual([item["release"] for item in result["terminologies"]], ["26.06e"])

    def test_missing_null_and_falsy_current_ncit_tags_keep_no_channel_release_behavior(self):
        cases = (
            ("absent", None, False),
            ("null", None, True),
            ("empty mapping", {}, True),
            ("empty list", [], True),
            ("empty string", "", True),
            ("zero", 0, True),
            ("false", False, True),
        )
        for name, tags, present in cases:
            with self.subTest(tags=name):
                row = terminology_row()
                if present:
                    row["tags"] = tags
                else:
                    row.pop("tags")
                self.evs.rows = [row]

                failed, result = self.call("list_terminologies")

                self.assertTrue(failed)
                self.assertEqual(result["error"]["code"], "release_not_available")
                self.assertEqual(result["error"]["details"], {"requested": "ncit", "source": "evs"})

    def test_valid_ncit_tags_still_select_the_configured_channel(self):
        self.evs.rows = listing(
            {"version": "26.09c", "tags": {"monthly": "false", "weekly": "true"}},
            {"version": "26.09d", "tags": {"monthly": "true", "weekly": "false"}},
        )
        for channel, expected in (("monthly", "26.09d"), ("weekly", "26.09c")):
            with self.subTest(channel=channel):
                settings = replace(self.settings, release_channel=channel)
                self.settings = settings
                self.context.settings = settings

                failed, result = self.call("list_terminologies")

                self.assertFalse(failed)
                self.assertEqual([item["release"] for item in result["terminologies"]], [expected])

    def test_malformed_terminology_metadata_fails_the_listing(self):
        # NCIt's tags are read by release selection first; another terminology's are not.
        cases = (
            {"terminology": "icd10", "tags": {"monthly": True}},
            {"terminology": "icd10", "tags": ["monthly"]},
            {"metadata": {"retiredStatusValue": 1}},
            {"metadata": {"retiredStatusValue": 0}},
            {"metadata": {"retiredStatusValue": False}},
            {"metadata": {"retiredStatusValue": []}},
            {"metadata": {"licenseText": ["text"]}},
            {"metadata": "none"},
            {"metadata": []},
            {"metadata": ""},
            {"metadata": False},
        )
        for fields in cases:
            with self.subTest(fields=fields):
                self.evs.rows = listing({}, fields) if "terminology" in fields else listing(fields)

                failed, result = self.call("list_terminologies")

                self.assertTrue(failed)
                self.assertEqual(result["error"]["code"], "upstream_unavailable")
                self.assertNotIn("terminologies", result)


class ConceptLeafTest(ServerFixture):
    def test_the_leaf_flag_passes_through_where_evs_gives_it(self):
        for code, fields, expected in (
            ("C1", {"leaf": True}, True),
            ("C2", {"leaf": False}, False),
            ("C3", {}, "absent"),
            ("C4", {"leaf": None}, "absent"),
        ):
            with self.subTest(fields=fields):
                self.evs.concepts[code] = concept(code, active=True, **fields)

                failed, result = self.call("get_concept", code=code)

                self.assertFalse(failed)
                self.assertEqual(result.get("leaf", "absent"), expected)

    def test_a_leaf_flag_that_is_not_a_boolean_is_refused(self):
        for value in ("true", 1):
            with self.subTest(value=value):
                self.evs.concepts["C1"] = concept("C1", active=True, leaf=value)

                failed, result = self.call("get_concept", code="C1")

                self.assertTrue(failed)
                self.assertEqual(result["error"]["code"], "upstream_unavailable")
