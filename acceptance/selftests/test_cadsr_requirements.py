"""The caDSR test of X-15 decides what to expect from the recording the server reached, so a
server that sends Accept and fails for another reason fails it, and so does one that leaves
Accept out and still succeeds (compliant_server.py)."""

import shutil

import pytest

from conftest import SUITE

pytest_plugins = ["pytester"]

X_15 = "test_a_server_that_leaves_out_accept_gets_html_and_never_parses_it"
RECORDED = "recorded/cadsr/data-element-2200604"


@pytest.fixture
def cadsr(compliant, monkeypatch):
    """The compliant server of the unified profile, asking caDSR for a data element, with
    the two recordings that answer that request."""

    monkeypatch.setenv("NCI_SIP_ACCEPTANCE_PROFILE", "unified")
    monkeypatch.setenv("COMPLIANT_SERVER_PROFILE", "unified")
    monkeypatch.setenv("COMPLIANT_SERVER_CADSR", "1")
    for suffix in ("", "-html"):
        target = compliant.path / "fixtures" / f"{RECORDED}{suffix}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(SUITE.parent / "fixtures" / f"{RECORDED}{suffix}.json", target)
    shutil.copy(SUITE / "test_cadsr.py", compliant.path / "tests" / "test_cadsr.py")


@pytest.mark.parametrize(
    ("defect", "outcome"),
    [
        ("", "passed"),
        ("cadsr-no-accept", "passed"),
        ("cadsr-unrelated-failure", "failed"),
        ("cadsr-html-accepted", "failed"),
    ],
    ids=["compliant", "accept-left-out-and-refused", "accept-sent-and-fails", "html-accepted"],
)
def test_the_accept_test_follows_the_recording_the_server_reached(
    cadsr, outcomes, monkeypatch, defect, outcome
):
    monkeypatch.setenv("COMPLIANT_SERVER_DEFECT", defect)

    found = outcomes("tests/test_cadsr.py", "-k", X_15)

    assert found == {X_15: outcome}
