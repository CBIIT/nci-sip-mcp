import unittest

from nci_sip_mcp.errors import correlated
from nci_sip_mcp.evs import (
    EVSResponseError,
    normalize_concept,
    object_list,
    verify_release,
)


class EVSTest(unittest.TestCase):
    def test_malformed_payload_fields_are_response_errors(self):
        for field in ("properties", "definitions", "synonyms"):
            with self.subTest(field), self.assertRaises(EVSResponseError):
                normalize_concept({"code": "C1", field: ["text"]}, None, "live_evs")
        self.assertEqual(object_list({"children": None}, "children"), [])
        self.assertEqual(object_list({}, "children"), [])

    def test_verify_release_accepts_only_the_requested_release(self):
        verify_release([], "26.06e")
        verify_release([{"version": "26.06e"}, {"version": "26.06e"}], "26.06e")
        for concepts, served in (
            ([{"version": "26.07a"}], ["26.07a"]),
            ([{"version": "26.06e"}, {}], ["unknown"]),
            ([{"version": "26.06e"}, {"version": "26.05d"}], ["26.05d"]),
        ):
            with self.subTest(concepts=concepts), self.assertRaises(EVSResponseError) as raised:
                verify_release(concepts, "26.06e")
            self.assertEqual(
                str(raised.exception),
                "EVS served content of another release than the one requested",
            )
            self.assertEqual(
                raised.exception.details,
                {"requested": "26.06e", "served": served, "source": "evs"},
            )

    def test_normalize_concept_includes_required_provenance(self):
        concept = normalize_concept(
            {
                "code": "C3262",
                "name": "Neoplasm",
                "terminology": "ncit",
                "version": "26.06e",
                "definitions": [{"definition": "A growth.", "type": "DEFINITION", "source": "NCI"}],
                "synonyms": [{"name": "Tumor", "termType": "SY", "source": "NCI"}],
                "properties": [
                    {"type": "Semantic_Type", "value": "Neoplastic Process"},
                    {"type": "Contributing_Source", "value": "GDC"},
                ],
            },
            release_date="2026-06-29",
            source="live_evs",
            retrieved_at="2026-07-09T00:00:00Z",
        )

        self.assertEqual(concept.code, "C3262")
        self.assertEqual(concept.preferred_name, "Neoplasm")
        self.assertEqual(concept.source_vocabulary, "NCI Thesaurus")
        self.assertEqual(concept.release_version, "26.06e")
        self.assertEqual(concept.release_date, "2026-06-29")
        self.assertEqual(concept.source, "live_evs")
        self.assertEqual(concept.evidence["semantic_types"], ["Neoplastic Process"])
        self.assertEqual(concept.evidence["contributing_sources"], ["GDC"])
        with correlated("call-1"):
            self.assertNotIn("raw", concept.to_dict("https://evs.test/concept"))
            self.assertIn("raw", concept.to_dict("https://evs.test/concept", include_raw=True))

    def test_a_concept_of_another_terminology_is_not_labelled_nci_thesaurus(self):
        concept = normalize_concept(
            {"code": "CL1", "terminology": "ncim"}, release_date=None, source="live_evs"
        )

        self.assertEqual((concept.terminology, concept.source_vocabulary), ("ncim", "ncim"))


if __name__ == "__main__":
    unittest.main()
