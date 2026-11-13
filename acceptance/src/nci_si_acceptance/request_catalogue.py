"""Reviewed finite request declarations for explicit controlled diagnostic callers."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import parse_qsl, urlencode, urlsplit

import yaml
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

from nci_si_acceptance.environments import (
    Environment,
    EnvironmentRecord,
    _mapping,
    environment_record,
)
from nci_si_acceptance.request_authorization import (
    Authorization,
    ConceptRequestFamily,
    ExactRequestFamily,
    Limits,
    ReleaseTarget,
    RequestGrant,
)
from nci_si_acceptance.request_budget import ControlledRun
from nci_si_mcp.errors import RequestPolicyError
from nci_si_mcp.request_policy import LICENSE_KEY_HEADER, using_request_policy
from nci_si_mcp.validation import RELEASE_CHANNELS, ReleaseChannel

if TYPE_CHECKING:
    from nci_si_mcp.release import ReleaseContext

POLICY = Path(__file__).parents[2] / "fixtures/request-policy.yaml"
MANIFEST = POLICY.with_name("manifest.yaml")
REGISTER = Path(__file__).parents[3] / "docs/upstream/ssis-queries.yaml"
CHANNELS: dict[str, ReleaseChannel] = {value: value for value in RELEASE_CHANNELS}
# Response/fixture matching metadata never becomes executable transport permission.
RECORD_FIELDS = frozenset(
    ("fixture", "operation", "rationale", "surface", "path", "status", "ignored")
)


def _scalar(value: Node, tag: str, choices: tuple[str, ...]) -> str:
    if not isinstance(value, ScalarNode) or value.tag != "tag:yaml.org,2002:" + tag:
        raise RequestPolicyError("authorization")
    if value.value not in choices:
        raise RequestPolicyError("authorization")
    return value.value


def _read_mapping(path: Path) -> tuple[dict[str, Node], str]:
    try:
        raw = path.read_text(encoding="utf-8")
        value = _mapping(yaml.compose(raw))
    except OSError, UnicodeError, ValueError, yaml.YAMLError:
        raise RequestPolicyError("authorization") from None
    return value, raw


def _values(node: Node) -> tuple[str, ...]:
    if not isinstance(node, SequenceNode) or not node.value:
        raise RequestPolicyError("authorization")
    values = tuple(_text(item) for item in node.value)
    if len(set(values)) != len(values):
        raise RequestPolicyError("authorization")
    return values


def _text(node: Node) -> str:
    if not isinstance(node, ScalarNode) or node.tag != "tag:yaml.org,2002:str":
        raise RequestPolicyError("authorization")
    return node.value


def _reference(node: Node) -> str | None:
    if not isinstance(node, MappingNode):
        raise RequestPolicyError("authorization")
    values = [
        value for key, value in node.value if isinstance(key, ScalarNode) and key.value == "fixture"
    ]
    if len(values) > 1:
        raise RequestPolicyError("authorization")
    return _text(values[0]) if values else None


def _rows(node: Node, *, references: set[str] | None = None) -> tuple[dict[str, Node], ...]:
    if not isinstance(node, SequenceNode):
        raise RequestPolicyError("authorization")
    # Only explicit references are executable; legacy merged held rows are not declarations.
    return tuple(
        _mapping(item)
        for item in node.value
        if references is None or _reference(item) in references
    )


def _declaration() -> tuple[dict[str, Node], str]:
    value, raw = _read_mapping(POLICY)
    if set(value) != {
        "version",
        "correlation",
        "channels",
        "includes",
        "relations",
        "exact",
        "paused_queries",
    }:
        raise RequestPolicyError("authorization")
    _scalar(value["version"], "int", ("1",))
    return value, raw


def _concepts(
    declaration: dict[str, Node],
    manifest: dict[str, Node],
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    try:
        table = _mapping(_mapping(manifest["evs"])["concepts"])
        includes = _values(declaration["includes"])
        relations = _values(declaration["relations"])
        names = set(_mapping(table["include"]))
        if any(not set(include.split(",")) <= names for include in includes):
            raise ValueError("unreviewed include")
        if not set(relations) <= set(_values(table["relations"])):
            raise ValueError("unreviewed relation")
        recorded = _mapping(_mapping(manifest["record"])["concepts"])
        codes = tuple(sorted({code for node in recorded.values() for code in _values(node)}))
    except KeyError, ValueError:
        raise RequestPolicyError("authorization") from None
    return codes, includes, relations


def _registered(declaration: dict[str, Node]) -> str:
    try:
        register, raw = _read_mapping(REGISTER)
        rows = _rows(register["queries"])
        identifiers = {_text(row["id"]) for row in rows}
        if (
            len(identifiers) != len(rows)
            or set(_values(declaration["paused_queries"])) != identifiers
        ):
            raise ValueError("unreviewed query register")
    except KeyError, ValueError:
        raise RequestPolicyError("authorization") from None
    # Recognized declarations bind identity, but never become transport grants while held.
    return raw


def _exact_url(path: str, pin: str) -> tuple[str, bool]:
    parts = urlsplit(path)
    if any((parts.scheme, parts.netloc, parts.fragment, not path.startswith("/"))):
        raise RequestPolicyError("authorization")
    segments = parts.path.split("/")
    if any(re.fullmatch(r"[a-z]+_[0-9].*", segment) and segment != pin for segment in segments):
        raise RequestPolicyError("authorization")
    pinned = pin in segments
    concrete = "/".join("{release}" if part == pin else part for part in segments)
    return concrete + _canonical_query(parts.query), pinned


def _canonical_query(query: str) -> str:
    pairs = parse_qsl(query, strict_parsing=True, keep_blank_values=True)
    if len(dict(pairs)) != len(pairs):
        raise RequestPolicyError("authorization")
    return "?" + urlencode(pairs) if pairs else ""


def _record_family(
    row: dict[str, Node],
    selected: EnvironmentRecord,
    pin: str,
    headers: list[tuple[str, str]],
    target: ReleaseTarget,
    correlation: bool,
) -> ExactRequestFamily | None:
    surface = _text(row["surface"])
    if surface not in ("evs", "evs-fhir") or not set(row) <= RECORD_FIELDS:
        raise RequestPolicyError("authorization")
    path, pinned = _exact_url(_text(row["path"]), pin)
    origin = selected["origins"][surface]
    if origin is None:
        return None
    grant = RequestGrant(
        "GET",
        origin.rstrip("/") + path,
        headers if surface == "evs" else [("accept", "application/json")],
        credential_origin=origin,
    )
    return ExactRequestFamily(grant, correlation, target if pinned else None)


def _chosen(rows: tuple[dict[str, Node], ...], references: set[str]) -> tuple[dict[str, Node], ...]:
    found = tuple(_text(row["fixture"]) for row in rows)
    if len(set(found)) != len(found) or set(found) != references:
        raise RequestPolicyError("authorization")
    return rows


def _recorded(
    declaration: dict[str, Node],
    manifest: dict[str, Node],
    selected: EnvironmentRecord,
    headers: list[tuple[str, str]],
    target: ReleaseTarget,
    correlation: bool,
) -> tuple[ExactRequestFamily, ...]:
    try:
        references = set(_values(declaration["exact"]))
        rows = _rows(_mapping(manifest["record"])["requests"], references=references)
        chosen = _chosen(rows, references)
        pin = _text(_mapping(manifest["evs"])["release"])
        if not target.accepts(pin):
            raise RequestPolicyError("authorization")
        families = tuple(
            _record_family(row, selected, pin, headers, target, correlation) for row in chosen
        )
        return tuple(family for family in families if family is not None)
    except KeyError, ValueError:
        raise RequestPolicyError("authorization") from None


@dataclass(frozen=True, slots=True)
class CompiledPolicy:
    authorization: Authorization
    _origin: str = field(repr=False)
    _license_key: str | None = field(repr=False)
    _target: ReleaseTarget

    def select_release(self, run: ControlledRun, *, channel: ReleaseChannel) -> ReleaseContext:
        """Discover and bind only a declared channel through this controller's policy."""
        # Validation/attachment must not import operational clients during child bootstrap.
        from nci_si_acceptance.request_transport import ControlledHttpPolicy  # noqa: PLC0415
        from nci_si_mcp.evs import EVSClient  # noqa: PLC0415
        from nci_si_mcp.release import resolve_evs_release  # noqa: PLC0415

        if type(run) is not ControlledRun:
            raise RequestPolicyError("authorization")
        if (
            not run._owner
            or run.authorization is not self.authorization
            or channel not in self._target.channels
        ):
            run.refuse("authorization")
        run.check_wait(0)
        # HttpClient captures the policy at construction, before the resolver makes any call.
        with using_request_policy(ControlledHttpPolicy(run)):
            client = EVSClient(self._origin, license_key=self._license_key)
            selection = resolve_evs_release(client, self._target.terminology, channel)
        run._commit_release(selection)
        return selection


def _discovery(
    origin: str, headers: list[tuple[str, str]], target: ReleaseTarget, correlation: bool
) -> tuple[ExactRequestFamily, ...]:
    return tuple(
        ExactRequestFamily(
            RequestGrant(
                "GET",
                origin.rstrip("/")
                + "/api/v1/metadata/terminologies?"
                + urlencode({"terminology": target.terminology, "latest": "true", "tag": channel}),
                headers,
                credential_origin=origin,
            ),
            correlation,
        )
        for channel in target.channels
    )


def compile_policy(environment: Environment) -> CompiledPolicy:
    """Compile reviewed finite declarations; no furnished caller is activated."""
    if type(environment) is not Environment:
        raise RequestPolicyError("authorization")
    try:
        selected = environment_record(environment.to_record())
    except ValueError:
        raise RequestPolicyError("authorization") from None
    declaration, raw = _declaration()
    correlation = _scalar(declaration["correlation"], "bool", ("true", "false")) == "true"
    manifest, manifest_raw = _read_mapping(MANIFEST)
    codes, includes, relations = _concepts(declaration, manifest)
    register_raw = _registered(declaration)
    try:
        target = ReleaseTarget(
            "ncit", tuple(CHANNELS[channel] for channel in _values(declaration["channels"]))
        )
    except KeyError:
        raise RequestPolicyError("authorization") from None
    origin = selected["origins"]["evs"]
    if origin is None:
        raise RequestPolicyError("authorization")
    headers = [("accept", "application/json")]
    if credential := environment.credentials.get("NCI_SI_EVS_LICENSE_KEY"):
        headers.append((LICENSE_KEY_HEADER, credential))
    grant = RequestGrant(
        "GET", origin.rstrip("/") + "/api/v1/version", headers, credential_origin=origin
    )
    discovery = _discovery(origin, headers, target, correlation)
    recorded = _recorded(declaration, manifest, selected, headers, target, correlation)
    concept = ConceptRequestFamily(
        RequestGrant(
            "GET",
            origin.rstrip("/") + "/api/v1/concept/{release}",
            headers,
            credential_origin=origin,
        ),
        codes,
        includes,
        relations,
        target,
        correlation,
    )
    identity = hashlib.sha256(
        json.dumps(selected, sort_keys=True, separators=(",", ":")).encode()
        + b"\0"
        + raw.encode()
        + b"\0"
        + manifest_raw.encode()
        + b"\0"
        + register_raw.encode()
    ).hexdigest()
    return CompiledPolicy(
        Authorization(
            (grant,),
            Limits(),
            identity,
            families=(ExactRequestFamily(grant, correlation), *discovery, concept, *recorded),
        ),
        origin,
        credential,
        target,
    )
