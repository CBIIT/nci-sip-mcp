"""The identifier forms of the server are the `patterns` of spec/tools.yaml, once."""

import re
import unittest
from pathlib import Path

import yaml

from nci_sip_mcp import validation

ROOT = Path(__file__).resolve().parents[1]
FORMS = {
    "terminology": validation.TERMINOLOGY_FORM,
    "release": validation.RELEASE_FORM,
    "code": validation.NCIT_CODE_FORM,
    "conceptCode": validation.NCIT_CODE_FORM,
    "valueSet": validation.NCIT_CODE_FORM,
    "codes": validation.NCIT_CODE_FORM,
    "publicId": validation.REGISTRY_ID_FORM,
    "dataElementId": validation.REGISTRY_ID_FORM,
    "permissibleValueId": validation.REGISTRY_ID_FORM,
    "version": validation.ITEM_VERSION_FORM,
}


def _stated(form):
    return form["ncit"] if isinstance(form, dict) else form


class IdentifierFormTest(unittest.TestCase):
    def test_every_pattern_of_the_specification_is_the_form_of_that_identifier(self):
        tools = yaml.safe_load((ROOT / "spec/tools.yaml").read_text())
        checked = 0
        for tool, spec in tools.items():
            for parameter, form in (spec.get("patterns") or {}).items():
                with self.subTest(tool=tool, parameter=parameter):
                    self.assertEqual(FORMS[parameter], _stated(form))
                    checked += 1
        self.assertGreater(checked, 30)

    def test_forms_refuse_a_trailing_newline_and_zero_prefixes(self):
        for form, bad in (
            (validation.NCIT_CODE_FORM, ["C0", "C3262\n", "c3262", "C"]),
            (validation.REGISTRY_ID_FORM, ["0", "012", "12\n"]),
            (validation.ITEM_VERSION_FORM, ["1.", "1.2.3", ".5"]),
            (validation.RELEASE_FORM, ["-26.09d", "26 09", "26.09d\n"]),
        ):
            for value in bad:
                with self.subTest(form=form, value=value):
                    self.assertIsNone(re.fullmatch(form, value))

    def test_validate_identifier_states_the_form_in_its_refusal(self):
        with self.assertRaisesRegex(Exception, "must match"):
            validation.validate_identifier("C0", validation.NCIT_CODE_FORM, "code")
