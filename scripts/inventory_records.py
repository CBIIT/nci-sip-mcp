"""Original inventory records, shared by capture and evidence import."""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, fields
from typing import Any, Literal

from scripts.evidence_envelope import EvidenceError, decode_json
from scripts.formats import SHA256_HEX

from nci_sip_acceptance.report import RANK, Attribution

ORIGINAL = ("catalogue", "stories", "expectations", "selection")


def require(condition: bool, path: str) -> None:
    if not condition:
        raise EvidenceError(f"Invalid inventory field: {path}")


def shape(value: Any, record: type, path: str) -> None:
    require(isinstance(value, dict), path)
    names = {item.name for item in fields(record)}
    for name in sorted(names):
        require(name in value, name if path == "binding" else f"{path}.{name}")
    require(set(value) == names, path)


def identifier(value: Any, path: str) -> None:
    require(
        isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_-]{0,99}", value) is not None, path
    )


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def decode_inventory(raw: bytes, path: str) -> Any:
    try:
        return decode_json(raw)
    except EvidenceError:
        raise EvidenceError(f"Invalid inventory field: {path}") from None


@dataclass(frozen=True)
class Binding:
    commit: str
    digests: dict[str, str]

    @classmethod
    def from_dict(cls, value: Any, expected: Binding) -> Binding:
        shape(value, cls, "binding")
        require(value["commit"] == expected.commit, "commit")
        actual = value["digests"]
        require(isinstance(actual, dict) and set(actual) == set(expected.digests), "digests")
        for name, hashed in expected.digests.items():
            require(actual[name] == hashed, f"digests.{name}")
        return cls(**value)


def _attribution(value: Any, tools: dict[str, str], path: str) -> Attribution:
    shape(value, Attribution, path)
    require(type(value["gate"]) is bool, f"{path}.gate")
    tool = value["tool"]
    require(tool is None or (isinstance(tool, str) and tool in tools), f"{path}.tool")
    return Attribution(**value)


@dataclass(frozen=True)
class Catalogue:
    schema: Literal[1]
    suite_digest: str
    tools: dict[str, str]
    cases: dict[str, Attribution]

    @classmethod
    def from_dict(cls, value: Any) -> Catalogue:
        shape(value, cls, "catalogue")
        require(type(value["schema"]) is int and value["schema"] == 1, "catalogue.schema")
        hashed = value["suite_digest"]
        require(
            isinstance(hashed, str) and re.fullmatch(SHA256_HEX, hashed) is not None,
            "catalogue.suite_digest",
        )
        require(isinstance(value["tools"], dict), "catalogue.tools")
        require(isinstance(value["cases"], dict), "catalogue.cases")
        return cls(1, hashed, _tools(value["tools"]), _cases(value["cases"], value["tools"]))


def _tools(value: dict[str, str]) -> dict[str, str]:
    for index, (name, group) in enumerate(value.items()):
        identifier(name, f"catalogue.tools[{index}].name")
        identifier(group, f"catalogue.tools[{index}].group")
    return value


def _cases(value: dict[str, Any], tools: dict[str, str]) -> dict[str, Attribution]:
    result = {}
    for index, (name, row) in enumerate(value.items()):
        path = f"catalogue.cases[{index}]"
        require(bool(name), path)
        result[name] = _attribution(row, tools, path)
    return result


@dataclass(frozen=True)
class Snapshots:
    catalogue: Catalogue
    stories: dict[str, str]
    expectations: dict[str, str]
    selection: list[str] | dict[str, Any]

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Snapshots:
        catalogue = Catalogue.from_dict(value["catalogue"])
        for name in ("stories", "expectations"):
            require(
                isinstance(value[name], dict) and set(value[name]) == set(catalogue.cases), name
            )
        for index, story in enumerate(value["stories"].values()):
            identifier(story, f"stories[{index}]")
        for index, expected in enumerate(value["expectations"].values()):
            require(isinstance(expected, str) and expected in RANK, f"expectations[{index}]")
        _selection(value["selection"], catalogue.cases)
        return cls(catalogue, value["stories"], value["expectations"], value["selection"])


def _selection(selected: Any, cases: dict[str, Attribution]) -> None:
    require(isinstance(selected, list), "selection")
    for index, name in enumerate(selected):
        require(isinstance(name, str), f"selection[{index}]")
    require(len(set(selected)) == len(selected), "selection")
    require(bool(selected) and set(selected) <= set(cases), "selection")


def read_snapshots(record: dict[str, Any], snapshots: dict[str, bytes]) -> dict[str, Any]:
    result = {}
    for name, raw in snapshots.items():
        require(digest(raw) == record[name + "_sha256"], name)
        result[name] = decode_inventory(raw, name)
    return asdict(Snapshots.from_dict(result))
