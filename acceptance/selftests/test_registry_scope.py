"""The suspension list is exact across the complete acceptance suite."""

import pytest
from test_registry_selection import REASON, REGISTRY_CHECKS

from conftest import SUITE

pytest_plugins = ["pytester"]


@pytest.mark.parametrize("selection", ["fixture", "live", "remote"])
def test_optional_suspends_exactly_the_approved_cases_in_the_complete_suite(
    pytester, monkeypatch, selection
):
    monkeypatch.setenv("NCI_SIP_ACCEPTANCE_MODE", "live" if selection == "live" else "fixture")
    if selection == "live":
        # Collection-only simulation; no launcher or managed-policy claim.
        monkeypatch.setattr("nci_sip_acceptance.client.selection", lambda mode: None)
    monkeypatch.setenv("NCI_SIP_ACCEPTANCE_REGISTRY_RELEASE", "optional")
    if selection == "remote":
        monkeypatch.setenv("NCI_SIP_ACCEPTANCE_URL", "http://server.example/mcp")
        monkeypatch.delenv("NCI_SIP_ACCEPTANCE_SERVER", raising=False)
    else:
        monkeypatch.setenv("NCI_SIP_ACCEPTANCE_PREPARE", "true")
    items, _ = pytester.inline_genitems(str(SUITE), "-p", "no:cacheprovider")

    skipped = {
        f"{item.path.name}::{item.name}": item.get_closest_marker("skip").kwargs
        for item in items
        if item.get_closest_marker("registry_release") is not None
    }
    assert skipped == {f"test_cadsr.py::{name}": {"reason": REASON} for name in REGISTRY_CHECKS}
    if selection == "fixture":
        assert {item.name for item in items if item.get_closest_marker("skip")} == REGISTRY_CHECKS
