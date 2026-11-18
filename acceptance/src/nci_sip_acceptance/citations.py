"""One anonymous API observation compared with MCP at the same explicit item version."""

# These are acceptance assertions, shared with the harness's negative controls.
# ruff: noqa: S101

import json
from typing import Any
from urllib.request import Request, urlopen


def _identity(item):
    assert all(isinstance(item[key], str) and item[key].strip() for key in ("publicId", "version"))
    return {key: item[key] for key in ("publicId", "version")}


def _raw_citations(raw):
    dec, domain = raw["DataElementConcept"], raw["ValueDomain"]
    parts = [
        ("objectClass", dec["ObjectClass"]),
        ("property", dec["Property"]),
        ("representationTerm", domain["RepresentationTerm"]),
    ]
    identities = [_identity(item) for item in [raw, dec, domain, *(p for _, p in parts)]]
    concepts = []
    for role, part in parts:
        assert part["Concepts"], f"API {role} has no derivation evidence"
        concepts.extend(_raw_concept(role, row) for row in part["Concepts"])
    return identities, concepts


def _raw_concept(role, row):
    assert row["primaryIndicator"] in ("Yes", "No"), "API primary must be explicit"
    assert isinstance(row["displayOrder"], str), "API order must be integer text"
    order = row["displayOrder"].strip()
    assert order and order.lstrip("+-").isascii() and order.lstrip("+-").isdigit()
    assert all(isinstance(row[k], str) and row[k].strip() for k in ("conceptCode", "evsSource"))
    return {
        "role": role,
        "conceptCode": row["conceptCode"],
        "terminology": row["evsSource"],
        "primary": row["primaryIndicator"] == "Yes",
        "displayOrder": int(order),
    }


def _mcp_citations(content):
    dec, domain = content["dataElementConcept"], content["valueDomain"]
    parts = [
        content,
        dec,
        domain,
        dec["objectClass"],
        dec["property"],
        domain["RepresentationTerm"],
    ]
    concepts = content["conceptAssociations"]
    assert all(
        type(row["primary"]) is bool and type(row["displayOrder"]) is int for row in concepts
    )
    keys = ("role", "conceptCode", "terminology", "primary", "displayOrder")
    return [_identity(item) for item in parts], [{k: row[k] for k in keys} for row in concepts]


def verify_citations(tools: Any, base_url: str) -> dict[str, Any]:
    """GET 2200604/v4 once; failures and in-place edit races stay visible without retry."""
    request = Request(  # noqa: S310 - the harness's fixture URL; controlled live callers refuse before entry.
        base_url.rstrip("/") + "/NCIAPI/1.0/api/DataElement/2200604?version=4",
        headers={"Accept": "application/json"},
    )
    with urlopen(request, timeout=120) as response:  # noqa: S310 - same controlled URL.
        body = json.load(response)
    assert body["apiResponse"]["type"] == "S", "API did not return success"
    raw = body["DataElement"]
    expected = _raw_citations(raw)
    assert _identity(raw) == {"publicId": "2200604", "version": "4"}
    result = tools.call(
        "get_data_element",
        {
            "publicId": "2200604",
            "version": "4",
            "include": ["dataElementConcept", "conceptAssociations", "valueDomain"],
        },
    )
    assert not result.is_error, result.content
    content = result.content
    dates = (
        f"API dateModified={raw.get('dateModified')}; "
        f"MCP dateModified={content.get('dateModified')}"
    )
    try:
        assert _mcp_citations(content) == expected, "citation identities or derivations differ"
    except (AssertionError, KeyError, TypeError) as error:
        raise AssertionError(dates) from error
    return content
