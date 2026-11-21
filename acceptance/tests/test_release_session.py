"""X-22: a moving upstream alias never changes the content state of an MCP session."""

import pytest

from nci_sip_acceptance.craft import OTHER_RELEASE, RELEASE
from nci_sip_acceptance.results import error_code, release_of

ARGUMENTS = {"terminology": "ncit", "code": "C90000001"}


@pytest.mark.requirement("X-22")
@pytest.mark.mcp_session
@pytest.mark.tool("get_concept")
@pytest.mark.scenario("release/moving-session")
def test_a_session_keeps_its_first_implicit_release_across_an_explicit_override(tools):
    first = tools.call("get_concept", ARGUMENTS)
    second = tools.call("get_concept", ARGUMENTS | {"release": None})
    explicit = tools.call("get_concept", ARGUMENTS | {"release": OTHER_RELEASE})
    last = tools.call("get_concept", ARGUMENTS)
    results = (first, second, explicit, last)
    assert all(not result.is_error for result in results), [r.content for r in results]
    assert [release_of(r.content) for r in results] == [
        ("ncit", RELEASE),
        ("ncit", RELEASE),
        ("ncit", OTHER_RELEASE),
        ("ncit", RELEASE),
    ]


@pytest.mark.requirement("X-22")
@pytest.mark.mcp_session
@pytest.mark.tool("get_concept")
@pytest.mark.scenario("release/withdrawn-session")
def test_a_withdrawn_session_release_fails_without_switching_to_the_new_release(tools):
    first = tools.call("get_concept", ARGUMENTS)
    assert not first.is_error, first.content
    assert release_of(first.content) == ("ncit", RELEASE)
    second = tools.call("get_concept", ARGUMENTS)
    assert error_code(second) == "release_not_available", second.content
    assert second.content["error"]["details"]["requested"] == RELEASE
    assert "start a new session or name a release" in second.content["error"]["message"].lower()
