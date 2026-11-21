"""Reviewed policy validation rejects raw ambiguity without exposing the rejected input."""

import pytest
import yaml

from conftest import REVIEWED_ORIGINS
from nci_sip_acceptance import environments


def policy_with_origin(monkeypatch, tmp_path, origin, enabled=True):
    document = yaml.safe_load(environments.POLICY.read_bytes())
    document["allowed"]["evs"] = origin
    document["environments"]["evs-only"]["evs"] = origin if enabled else None
    path = tmp_path / "policy.yaml"
    path.write_text(yaml.safe_dump(document))
    monkeypatch.setattr(environments, "POLICY", path)
    monkeypatch.setenv(environments.VARIABLE, "evs-only")


@pytest.mark.parametrize(
    "origin",
    [
        "https://api-evsrest.nci.nih.gov:invalid",
        "https://api-evsrest.nci.nih.gov:0",
        "https://api-evsrest.nci.nih.gov\t",
        "https://api-evsrest.nci.nih.gov?",
        "https://api-evsrest.nci.nih.gov#",
        " https://api-evsrest.nci.nih.gov",
        "https://api-evsrest.nci.nih.gov/space path",
        "https://api-evsrest.nci.nih.gov\n",
        "https://api-evsrest.nci.nih.gov/\x80",
        "https://api-evsrest.nci.nih.gov/a\x00b",
        "ftp://api-evsrest.nci.nih.gov/path",
        "https:///missing-host",
    ],
)
def test_raw_ambiguous_origin_is_rejected_without_echo(monkeypatch, tmp_path, origin):
    policy_with_origin(monkeypatch, tmp_path, origin)
    with pytest.raises(ValueError, match=r"\Ainvalid reviewed environment origin\Z") as error:
        environments.selection("live")
    assert origin not in str(error.value)


@pytest.mark.parametrize(
    ("origin", "enabled"),
    [
        ("https://api-evsrest.nci.nih.gov:8443/api/v1", True),
        ("https://api-evsrest.nci.nih.gov/a%20b", True),
        ("https://api-evsrest.nci.nih.gov", False),
    ],
)
def test_selected_origins_preserve_exact_values_and_valid_narrowing(
    monkeypatch, tmp_path, origin, enabled
):
    policy_with_origin(monkeypatch, tmp_path, origin, enabled)
    selected = environments.selection("live")
    assert selected.origins == REVIEWED_ORIGINS | {"evs": origin if enabled else None}


POLICY_EDITS = {
    "missing": ("  ssis-sparql: null", ""),
    "extra": ("allowed:", "allowed:\n  private-marker: null"),
    "duplicate": ("allowed:", "allowed:\n  evs: https://private-marker.invalid"),
    "permission": ("    cadsr: null", "    cadsr: https://private-marker.invalid"),
    "unknown": ("environments:", "private-marker:\nenvironments:"),
    "mapping": ("environments:\n  evs-only:", "environments:\n  - evs-only:"),
    "key": ("  evs: ", "  !!bool evs: "),
    "node": ("    cadsr: null", "    cadsr: !!null {}"),
    "scalar": ("  evs: https:", "  evs: !!int https:"),
    "syntax": ("allowed:", "allowed: ["),
    "unreadable": None,
    "complex-key": ("allowed:", "allowed:\n  ? !!str [private-marker]\n  : null"),
}


@pytest.mark.parametrize(("case", "edit"), POLICY_EDITS.items(), ids=POLICY_EDITS)
def test_policy_structure_and_permission_are_closed(monkeypatch, tmp_path, case, edit):
    raw = environments.POLICY.read_text()
    path = tmp_path / "policy.yaml"
    if edit is not None:
        old, new = edit
        assert old in raw
        path.write_text(raw.replace(old, new, 1))
    monkeypatch.setattr(environments, "POLICY", path)
    monkeypatch.setenv(environments.VARIABLE, "evs-only")
    with pytest.raises(ValueError) as error:
        environments.selection("live")
    assert "private-marker" not in str(error.value)
    if case == "complex-key":
        assert str(error.value) == "invalid reviewed environment policy key"


def test_shipped_policy_is_exactly_the_reviewed_evs_only_permission():
    assert yaml.safe_load(environments.POLICY.read_bytes()) == {
        "allowed": REVIEWED_ORIGINS,
        "environments": {"evs-only": REVIEWED_ORIGINS},
    }
