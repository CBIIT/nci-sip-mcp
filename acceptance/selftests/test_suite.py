"""The rules of a run: what a live run skips, which scenarios a test selects, and which
requests lacked a fixture."""

import tomllib
from pathlib import Path

import pytest
import yaml

from nci_sip_acceptance.suite import (
    CANNOT_UNPREPARE,
    FIXTURES,
    LIVE_CAPABLE,
    NEEDS_STATE_HOOK,
    NOT_DECLARED_PREPARED,
    NOT_PREPARED,
    PREPARED,
    SKIP,
    UnmatchedUpstream,
    index_set,
    main,
    order_for_state_changes,
    scenarios_of,
    select_registry_checks,
    skip_fixture_only,
    skip_remote_own_servers,
    skip_unprepared,
    unmatched_requests,
)


class Item:
    """The part of a collected test that the rules read and write."""

    def __init__(self, *markers):
        self.markers = [getattr(marker, "mark", marker) for marker in markers]
        self.user_properties = []

    def iter_markers(self, name):
        return (marker for marker in self.markers if marker.name == name)

    def get_closest_marker(self, name):
        return next(self.iter_markers(name), None)

    def add_marker(self, marker):
        self.markers.append(marker.mark)


def test_a_live_run_skips_every_test_not_marked_live_capable_and_every_scenario():
    live = Item(getattr(pytest.mark, LIVE_CAPABLE))
    fixture_only = Item()
    scenario = Item(getattr(pytest.mark, LIVE_CAPABLE), pytest.mark.scenario("release/unknown"))

    skip_fixture_only([live, fixture_only, scenario])

    assert live.get_closest_marker("skip") is None
    assert fixture_only.get_closest_marker("skip").kwargs == {"reason": "fixture mode only"}
    assert scenario.get_closest_marker("skip").kwargs == {"reason": "fixture mode only"}
    # The report reads why from the property, never from the reason (#265).
    assert live.user_properties == []
    assert fixture_only.user_properties == [
        (SKIP, "not_live"),
        ("not_live_reason", "not_live_capable"),
    ]
    assert scenario.user_properties == [(SKIP, "not_live"), ("not_live_reason", "scenario")]


@pytest.mark.parametrize("required", [False, True])
def test_registry_selection_precedes_live_selection_without_suspending_other_checks(required):
    registry = Item(pytest.mark.registry_release, pytest.mark.scenario("cadsr/published"))
    provenance, refusal, alignment = (Item(pytest.mark.live_capable) for _ in range(3))
    items = [registry, provenance, refusal, alignment]

    active = select_registry_checks(items, required)
    skip_fixture_only(active)

    assert active == (items if required else [provenance, refusal, alignment])
    assert registry.user_properties == (
        [(SKIP, "not_live"), ("not_live_reason", "scenario")] if required else []
    )
    assert registry.get_closest_marker("skip").kwargs == {
        "reason": "fixture mode only"
        if required
        else (
            "registry-release requirement suspended pending sponsor decision "
            "(NCI_SIP_ACCEPTANCE_REGISTRY_RELEASE=optional)"
        )
    }
    assert [item.markers for item in (provenance, refusal, alignment)] == [
        [pytest.mark.live_capable.mark]
    ] * 3


def test_scenario_reason_precedes_missing_live_capability_but_empty_marker_is_not_a_scenario():
    scenario, empty = Item(pytest.mark.scenario("fault")), Item(pytest.mark.scenario())
    skip_fixture_only([scenario, empty])
    assert dict(scenario.user_properties)["not_live_reason"] == "scenario"
    assert dict(empty.user_properties)["not_live_reason"] == "not_live_capable"


def test_a_test_selects_the_scenarios_of_all_its_markers():
    item = Item(pytest.mark.scenario("a/one", "a/two"), pytest.mark.scenario("b/three"))

    assert scenarios_of(item) == ("a/one", "a/two", "b/three")
    assert scenarios_of(Item()) == ()


def test_a_failure_for_want_of_fixtures_names_the_requests_and_when():
    failure = UnmatchedUpstream(["GET evs /a {}", "GET evs /b {}"], " while the server started")

    assert failure.requests == ["GET evs /a {}", "GET evs /b {}"]
    assert str(failure).splitlines() == [
        "upstream requests without a fixture while the server started:",
        "GET evs /a {}",
        "GET evs /b {}",
    ]


def test_requests_without_a_fixture_are_named_one_per_line():
    log = [
        {
            "method": "GET",
            "surface": "evs",
            "path": "/api/v1/version",
            "params": {},
            "fixture": "v.json",
        },
        {
            "method": "GET",
            "surface": "evs",
            "path": "/api/v1/x",
            "params": {"a": ["1"]},
            "fixture": None,
        },
    ]

    assert unmatched_requests(log) == ["GET evs /api/v1/x {'a': ['1']}"]


def test_without_a_prepare_command_a_test_that_needs_it_is_skipped_as_not_run():
    needs = Item(getattr(pytest.mark, PREPARED))
    other = Item()

    skip_unprepared([needs, other])

    assert needs.get_closest_marker("skip").kwargs == {"reason": NOT_PREPARED}
    assert other.get_closest_marker("skip") is None


def test_the_index_set_is_every_concept_recorded_with_its_summary_in_the_manifest_s_order():
    manifest = {
        "record": {
            "concepts": {
                "full": ["C4817", "C17049"],
                "minimal": ["C2991"],
                "parents,summary": ["C3262"],
                "children": ["C9118"],
            }
        }
    }

    assert index_set(manifest) == ["C4817", "C17049", "C3262"]


def test_a_remote_server_not_declared_prepared_skips_the_tests_that_need_the_index():
    needs = Item(getattr(pytest.mark, PREPARED))

    skip_unprepared([needs], NOT_DECLARED_PREPARED)

    assert needs.get_closest_marker("skip").kwargs == {"reason": NOT_DECLARED_PREPARED}


def test_a_remote_server_without_a_state_hook_skips_the_tests_needing_a_server_of_its_own():
    scenario = Item(pytest.mark.scenario("release/unknown"))
    own = Item(pytest.mark.own_server)
    session = Item(pytest.mark.mcp_session)
    ordinary = Item()

    skip_remote_own_servers([scenario, own, session, ordinary], has_hook=False)

    assert scenario.get_closest_marker("skip").kwargs == {"reason": NEEDS_STATE_HOOK}
    assert own.get_closest_marker("skip").kwargs == {"reason": NEEDS_STATE_HOOK}
    assert session.get_closest_marker("skip").kwargs == {"reason": NEEDS_STATE_HOOK}
    assert ordinary.get_closest_marker("skip") is None


def test_a_state_hook_leaves_those_tests_to_run_but_not_one_without_the_index():
    scenario = Item(pytest.mark.scenario("release/unknown"))
    unprepared = Item(pytest.mark.unprepared, pytest.mark.own_server)

    skip_remote_own_servers([scenario, unprepared], has_hook=True)

    assert scenario.get_closest_marker("skip") is None
    assert unprepared.get_closest_marker("skip").kwargs == {"reason": CANNOT_UNPREPARE}


def test_a_run_with_a_state_hook_keeps_the_server_as_the_operator_left_it_first():
    ordinary = Item()
    own = Item(pytest.mark.own_server)
    one = Item(pytest.mark.scenario("a/one"))
    other_one = Item(pytest.mark.scenario("a/one"))
    two = Item(pytest.mark.scenario("a/two"))
    items = [two, one, own, ordinary, other_one]

    order_for_state_changes(items)

    assert items == [ordinary, own, one, other_one, two]


def test_the_index_codes_command_prints_the_index_set_of_the_manifest_one_code_per_line(capsys):
    manifest = yaml.safe_load((FIXTURES / "manifest.yaml").read_text(encoding="utf-8"))

    main()

    printed = capsys.readouterr().out
    assert printed.splitlines() == index_set(manifest)
    assert len(printed.splitlines()) > 1


def test_the_index_codes_command_is_a_script_of_the_project():
    project = tomllib.loads(Path(__file__).parents[2].joinpath("pyproject.toml").read_text())

    assert project["tool"]["pdm"]["scripts"]["acceptance-index-codes"] == (
        "python -m nci_sip_acceptance.suite"
    )
