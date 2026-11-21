"""Live journeys accept changed releases/content, but reject broken contracts over MCP."""

import json
import shlex
import shutil
import sys
from copy import deepcopy
from pathlib import Path

import pytest

from conftest import STAND_IN, SUITE, synthetic_live

pytest_plugins = ["pytester"]

FAMILIES = {
    "discovery": ("pin", "test_pin"),
    "include": ("evs", "test_an_include_value_returns_its_section_and_no_other"),
    "identity": (
        "evs",
        "test_a_concept_carries_its_identity_and_the_status_the_platform_publishes",
    ),
    "paths": ("evs", "test_paths_to_root_are_the_platform_s_paths_in_its_order"),
    "maps": ("evs", "test_the_mappings_are_the_concept_s_maps_unchanged_in_order"),
    "relationships": (
        "evs",
        "test_every_relationship_of_the_catalogue_is_listed_by_code_name_and_kind",
    ),
    "element": (
        "cadsr",
        "test_a_data_element_is_its_own_fields_alone_with_its_version_and_statuses",
    ),
    "sections": ("cadsr", "test_each_include_returns_its_section_as_the_platform_gives_it"),
    "cross-domain": (
        "cross_domain",
        "test_the_data_elements_are_the_concept_s_and_with_expansion_its_descendants_too",
    ),
}
PIN = {"terminology": "ncit", "release": "99.test"}
ORIGIN = {
    "release": {"terminology": "ncit", "identifier": "99.test"},
    "source": "evs_rest",
    "servedBy": "live",
    "retrievedAt": "2026-10-10T12:00:00Z",
}
REGISTRY = ORIGIN | {"release": {"registry": "cadsr"}, "source": "cadsr_rest"}
CONCEPT = {
    "code": "C4817",
    "terminology": "ncit",
    "name": "Changed name",
    "active": True,
    "status": "Changed status",
    "provenance": ORIGIN,
}
ELEMENT = {
    "publicId": "2200604",
    "version": "99",
    "longName": "Changed element",
    "context": "New",
    "workflowStatus": "RELEASED",
    "registrationStatus": "Application",
    "dateCreated": "2026-01-01",
    "dateModified": "2026-10-10",
    "definition": "Changed definition",
    "shortName": "Changed short name",
    "latestVersionIndicator": "Yes",
    "beginDate": "2026-01-01",
    "endDate": None,
    "origin": "Changed origin",
    "provenance": REGISTRY,
}
EVS_SECTIONS = {
    "synonyms": [{"name": "New synonym"}],
    "definitions": [{"definition": "New text"}],
    "properties": [{"code": "P106", "value": "New type"}],
    "semanticType": ["New type"],
}
CDE_SECTIONS = {
    "permissibleValues": [
        {
            "publicId": "998",
            "value": "New",
            "valueMeaning": {
                "publicId": "999",
                "version": "2",
                "longName": "New meaning",
                "concepts": [],
            },
            "provenance": REGISTRY,
        }
    ],
    "valueDomain": {"publicId": "997", "version": "2", "type": "Enumerated"},
    "conceptAssociations": [{"conceptCode": "C1", "longName": "New root", "role": "objectClass"}],
    "alternateNames": [{"name": "New alternate", "type": "Synonym"}],
    "classificationSchemes": [
        {
            "publicId": "996",
            "version": "2",
            "longName": "New scheme",
            "context": "New",
            "items": [],
            "provenance": REGISTRY,
        }
    ],
    "referenceDocuments": [{"documentType": "Future type", "unknown": ["unchanged"]}],
}


def reply(tool, arguments, result, family=None):
    return {
        "tool": tool,
        "arguments": arguments,
        "result": deepcopy(result),
        "family": family,
    }


def replies():
    rows = [
        reply(
            "resolve_release",
            {"terminology": "ncit", "channel": "monthly"},
            {"terminology": "ncit", "channel": "monthly", "version": "99.test"},
            "discovery",
        ),
        reply(
            "resolve_registry_release",
            {},
            {
                "published": False,
                "generatedAt": "2026-10-10",
                "sourceDistribution": "releasedCDEsXML-OD.zip",
            },
        ),
    ]
    rows.append(reply("get_concept", PIN | {"code": "C4817"}, CONCEPT, "identity"))
    rows += [
        reply(
            "get_concept",
            PIN | {"code": "C4817", "include": [section]},
            CONCEPT | {section: value},
            "include",
        )
        for section, value in EVS_SECTIONS.items()
    ]
    root = CONCEPT | {"code": "C1", "name": "New root"}
    paths = {"paths": [["C4817", "C1"]], "nodes": [root]}
    rows.append(
        reply(
            "get_concept_hierarchy",
            PIN | {"code": "C4817", "direction": "pathsToRoot"},
            paths,
            "paths",
        )
    )
    mapping = {
        "targetCode": "1234",
        "targetTerminology": "New target",
        "targetName": "New mapping",
        "type": "Related To",
        "provenance": ORIGIN,
    }
    rows.append(
        reply(
            "get_concept_mappings",
            PIN | {"code": "C4817"},
            {"mappings": [mapping]},
            "maps",
        )
    )
    relation = {
        "code": "R42",
        "name": "New relation",
        "kind": "role",
        "polarity": "positive",
        "terminology": "ncit",
        "provenance": ORIGIN,
    }
    rows.append(
        reply(
            "list_relationships",
            PIN,
            {"relationships": [relation]},
            "relationships",
        )
    )
    return rows + cde_replies() + cross_replies()


def cde_replies():
    rows = [
        reply(
            "get_data_element",
            {"publicId": "2200604"},
            ELEMENT | {"provenance": REGISTRY | {"upstream": {"version": "99"}}},
            "element",
        ),
        reply(
            "get_data_element",
            {"publicId": "2200604", "version": "1"},
            ELEMENT | {"version": "1", "provenance": REGISTRY | {"upstream": {"version": "1"}}},
            "element",
        ),
        reply(
            "get_data_element",
            {"publicId": "2200604", "version": "4.0"},
            ELEMENT | {"version": "4", "provenance": REGISTRY | {"upstream": {"version": "4"}}},
            "element",
        ),
    ]
    return rows + [
        reply(
            "get_data_element",
            {"publicId": "2200604", "include": [section]},
            ELEMENT | {section: deepcopy(value)},
            "sections",
        )
        for section, value in CDE_SECTIONS.items()
    ]


def cross_replies():
    origin = ORIGIN | {
        "source": "ssis_sparql",
        "registry": {"published": False, "generatedAt": "2026-10-10"},
    }
    item = {
        "dataElement": {"publicId": "882", "longName": "New CDE"},
        "provenance": origin,
    }
    return [
        reply(
            "find_data_elements_for_concept",
            PIN | {"conceptCode": "C17357", "expandDescendants": expand},
            {"dataElements": [item]},
            "cross-domain",
        )
        for expand in (False, True)
    ]


@pytest.fixture
def live_suite(compliant, monkeypatch):
    synthetic_live(compliant)
    for module in ("evs", "cadsr", "cross_domain"):
        shutil.copy(SUITE / f"test_{module}.py", compliant.path / "tests")
    # A real fixture consumer isolates discovery from later content checks.
    (compliant.path / "tests/test_pin.py").write_text(
        "import pytest\n@pytest.mark.live_capable\n"
        "@pytest.mark.tool('resolve_release')\n"
        "def test_pin(content_pin):\n    assert isinstance(content_pin, dict)\n"
    )
    # Original cases still read these before the live-mode change; retain real old evidence.
    for relative in (
        "evs/concepts/C4817.json",
        "evs/paths-to-root.json",
        "evs/roles.json",
        "evs/associations.json",
        "cadsr/data-element-2200604.json",
        "cadsr/data-element-2200604-version-1.json",
        "ssis-sparql/data-elements-c17357.json",
        "ssis-sparql/data-elements-c17357-descendants.json",
    ):
        destination = compliant.path / "fixtures/recorded" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(SUITE.parent / "fixtures/recorded" / relative, destination)
    monkeypatch.setenv("NCI_SIP_ACCEPTANCE_MODE", "live")
    monkeypatch.delenv("NCI_SIP_ACCEPTANCE_PREPARE", raising=False)
    server = Path(__file__).with_name("live_content_server.py")
    monkeypatch.setenv(
        "NCI_SIP_ACCEPTANCE_SERVER",
        shlex.join([sys.executable, str(server), str(compliant.path / "replies.json")]),
    )
    return compliant


def run_family(suite, family, replacement=None, corruption=None):
    responses = replies()
    selected = [row for row in responses if row["family"] == family]
    if corruption is not None:
        index, path, value = corruption
        corrupt(selected[index]["result"], path, value)
    for row in selected:
        if replacement is not None:
            row["result"] = replacement
    (suite.path / "replies.json").write_text(json.dumps(responses))
    module, function = FAMILIES[family]
    result = suite.runpytest_subprocess(
        f"tests/test_{module}.py::{function}",
        "-p",
        "no:cacheprovider",
        "-p",
        STAND_IN,
        "--report=run.json",
        "--disable-warnings",
    )
    tests = json.loads((suite.path / "run.json").read_text())["tests"]
    return result, {node: row["outcome"] for node, row in tests.items()}


@pytest.mark.parametrize("family", FAMILIES)
def test_live_checks_accept_changed_release_and_content(live_suite, family):
    result, outcomes = run_family(live_suite, family)
    assert outcomes and all(
        value == ("not_live" if "[retired]" in node else "passed")
        for node, value in outcomes.items()
    )
    assert result.ret == 0


MISSING = object()


@pytest.mark.parametrize("field", ["context", "dateCreated", "dateModified"])
def test_live_element_accepts_null_only_for_nullable_scalar_fields(live_suite, field):
    result, outcomes = run_family(live_suite, "element", corruption=(0, field, None))

    assert set(outcomes.values()) == {"passed"}
    assert result.ret == 0


def test_live_nullable_text_can_be_empty_without_becoming_an_identity(live_suite):
    result, outcomes = run_family(live_suite, "element", corruption=(0, "dateModified", ""))

    assert set(outcomes.values()) == {"passed"}
    assert result.ret == 0


def test_live_optional_metadata_can_be_omitted(live_suite):
    result, outcomes = run_family(live_suite, "element", corruption=(0, "context", MISSING))

    assert set(outcomes.values()) == {"passed"}
    assert result.ret == 0


def corrupt(content, path, value):
    keys = [int(key) if key.isdigit() else key for key in path.split(".")]
    for key in keys[:-1]:
        content = content[key]
    if value is MISSING:
        del content[keys[-1]]
    else:
        content[keys[-1]] = value


# Each row changes one property of one otherwise valid reply, not a bundle of defects.
CORRUPTIONS = (
    [
        ("discovery", 0, "terminology", "other"),
        ("discovery", 0, "version", ""),
        ("identity", 0, "code", "C999"),
        ("identity", 0, "terminology", "other"),
        ("identity", 0, "name", ""),
        ("identity", 0, "active", "true"),
        ("identity", 0, "status", ""),
        ("element", 0, "publicId", "9999999"),
        ("element", 0, "version", []),
        ("element", 1, "version", "99"),
        ("element", 0, "longName", MISSING),
        ("element", 0, "extra", True),
        ("element", 0, "dateModified", 5),
        ("element", 0, "context", ["x"]),
        ("element", 0, "longName", ""),
        ("element", 0, "workflowStatus", ""),
        ("element", 0, "registrationStatus", ""),
        ("sections", 0, "publicId", "9999999"),
        ("sections", 1, "valueDomain", []),
        ("sections", 0, "valueDomain", {}),
        ("include", 0, "definitions", []),
        ("include", 0, "synonyms.0", "wrong type"),
        ("include", 3, "semanticType.0", {}),
        ("paths", 0, "paths.0.0", "C1"),
        ("paths", 0, "nodes.0.code", "C999"),
        ("relationships", 0, "relationships.0.kind", "invented"),
        ("relationships", 0, "relationships.0.polarity", "unknown"),
    ]
    + [
        ("element", 0, field, None)
        for field in ("publicId", "version", "longName", "workflowStatus", "registrationStatus")
    ]
    + [
        (family, 0, prefix + "provenance.release." + field, value)
        for family, prefix in (
            ("include", ""),
            ("identity", ""),
            ("paths", "nodes.0."),
            ("maps", "mappings.0."),
            ("relationships", "relationships.0."),
            ("cross-domain", "dataElements.0."),
        )
        for field, value in (("identifier", "old"), ("terminology", "other"))
    ]
    + [
        (family, 0, "provenance.release." + field, value)
        for family in ("element", "sections")
        for field, value in (("identifier", "old"), ("registry", "other"))
    ]
    + [
        ("maps", 0, "mappings.0." + field, value)
        for field in ("targetCode", "targetTerminology", "targetName", "type")
        for value in (MISSING, "")
    ]
    + [
        ("relationships", 0, "relationships.0." + field, "")
        for field in ("terminology", "code", "name")
    ]
    + [
        ("cross-domain", 0, "dataElements.0.dataElement." + field, MISSING)
        for field in ("publicId", "longName")
    ]
    + [
        ("cross-domain", 0, "dataElements.0.provenance.source", value)
        for value in (MISSING, "evs_rest")
    ]
    + [
        (family, 0, field, value)
        for family, field in (
            ("maps", "mappings"),
            ("relationships", "relationships"),
            ("cross-domain", "dataElements"),
            ("paths", "paths"),
            ("include", "synonyms"),
            ("sections", "permissibleValues"),
        )
        for value in (MISSING, None, {}, 42)
    ]
)


@pytest.mark.parametrize(
    ("family", "index", "path", "value"),
    [pytest.param(*row, id=f"{row[0]}-{row[1]}-{row[2]}-{i}") for i, row in enumerate(CORRUPTIONS)],
)
def test_live_checks_reject_single_corruptions(live_suite, family, index, path, value):
    result, outcomes = run_family(live_suite, family, corruption=(index, path, value))
    assert "failed" in outcomes.values(), outcomes
    assert result.ret == 1


def test_live_paths_require_at_least_one_path(live_suite):
    # With no orphan nodes, only the nonempty-path contract can refuse this answer.
    result, outcomes = run_family(live_suite, "paths", replacement={"paths": [], "nodes": []})
    assert "failed" in outcomes.values(), outcomes
    assert result.ret == 1


@pytest.mark.parametrize(
    ("family", "content"),
    [
        ("maps", {"mappings": []}),
        ("relationships", {"relationships": []}),
        ("cross-domain", {"dataElements": []}),
    ],
)
def test_live_content_accepts_valid_empty_lists(live_suite, family, content):
    result, outcomes = run_family(live_suite, family, replacement=content)
    assert outcomes and set(outcomes.values()) == {"passed"}
    assert result.ret == 0
