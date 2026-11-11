"""Reviewed finite request declarations for explicit controlled diagnostic callers."""

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlencode

import yaml
from yaml.nodes import Node, ScalarNode, SequenceNode

from nci_si_acceptance.environments import Environment, _mapping, environment_record
from nci_si_acceptance.request_authorization import (
    Authorization,
    ConceptRequestFamily,
    ExactRequestFamily,
    Limits,
    ReleaseTarget,
    RequestGrant,
)
from nci_si_acceptance.request_budget import ControlledRun
from nci_si_acceptance.request_transport import ControlledHttpPolicy
from nci_si_mcp.evs import LICENSE_KEY_HEADER, EVSClient
from nci_si_mcp.http_client import RequestPolicyError, using_request_policy
from nci_si_mcp.release import ReleaseContext, resolve_evs_release
from nci_si_mcp.validation import RELEASE_CHANNELS, ReleaseChannel

POLICY = Path(__file__).parents[2] / "fixtures/request-policy.yaml"
MANIFEST = POLICY.with_name("manifest.yaml")
CHANNELS: dict[str, ReleaseChannel] = {value: value for value in RELEASE_CHANNELS}


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
    values = tuple(
        _scalar(item, "str", (item.value,)) for item in node.value if isinstance(item, ScalarNode)
    )
    if len(values) != len(node.value) or len(set(values)) != len(values):
        raise RequestPolicyError("authorization")
    return values


def _declaration() -> tuple[dict[str, Node], str]:
    value, raw = _read_mapping(POLICY)
    if set(value) != {"version", "correlation", "channels", "includes", "relations"}:
        raise RequestPolicyError("authorization")
    _scalar(value["version"], "int", ("1",))
    return value, raw


def _concepts(
    declaration: dict[str, Node],
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], str]:
    manifest, raw = _read_mapping(MANIFEST)
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
    return codes, includes, relations, raw


@dataclass(frozen=True, slots=True)
class CompiledPolicy:
    authorization: Authorization
    _origin: str = field(repr=False)
    _license_key: str | None = field(repr=False)
    _target: ReleaseTarget

    def select_release(self, run: ControlledRun, *, channel: ReleaseChannel) -> ReleaseContext:
        """Resolve through this controller's policy; the entry grants no request authority."""
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
        run._commit_release(selection, self._target)
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
    codes, includes, relations, manifest_raw = _concepts(declaration)
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
    ).hexdigest()
    return CompiledPolicy(
        Authorization(
            (grant,),
            Limits(),
            identity,
            families=(ExactRequestFamily(grant, correlation), *discovery, concept),
        ),
        origin,
        credential,
        target,
    )
