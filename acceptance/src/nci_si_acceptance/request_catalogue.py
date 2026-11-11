"""Reviewed finite request declarations for explicit controlled diagnostic callers."""

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from yaml.nodes import Node, ScalarNode

from nci_si_acceptance.environments import Environment, _mapping, environment_record
from nci_si_acceptance.request_authorization import (
    Authorization,
    ExactRequestFamily,
    Limits,
    RequestGrant,
)
from nci_si_acceptance.request_budget import ControlledRun
from nci_si_acceptance.request_transport import ControlledHttpPolicy
from nci_si_mcp.evs import LICENSE_KEY_HEADER, EVSClient
from nci_si_mcp.http_client import RequestPolicyError, using_request_policy
from nci_si_mcp.release import ReleaseContext, resolve_evs_release
from nci_si_mcp.validation import RELEASE_CHANNELS, ReleaseChannel

POLICY = Path(__file__).parents[2] / "fixtures/request-policy.yaml"


def _scalar(value: Node, tag: str, choices: tuple[str, ...]) -> str:
    if not isinstance(value, ScalarNode) or value.tag != "tag:yaml.org,2002:" + tag:
        raise RequestPolicyError("authorization")
    if value.value not in choices:
        raise RequestPolicyError("authorization")
    return value.value


def _declaration() -> tuple[bool, str]:
    try:
        raw = POLICY.read_text(encoding="utf-8")
        value = _mapping(yaml.compose(raw))
    except OSError, UnicodeError, ValueError, yaml.YAMLError:
        raise RequestPolicyError("authorization") from None
    if set(value) != {"version", "correlation"}:
        raise RequestPolicyError("authorization")
    _scalar(value["version"], "int", ("1",))
    return _scalar(value["correlation"], "bool", ("true", "false")) == "true", raw


@dataclass(frozen=True, slots=True)
class CompiledPolicy:
    authorization: Authorization
    _origin: str = field(repr=False)
    _license_key: str | None = field(repr=False)

    def select_release(self, run: ControlledRun, *, channel: ReleaseChannel) -> ReleaseContext:
        """Resolve through this controller's policy; the entry grants no request authority."""
        if type(run) is not ControlledRun:
            raise RequestPolicyError("authorization")
        if (
            not run._owner
            or run.authorization is not self.authorization
            or channel not in RELEASE_CHANNELS
        ):
            run.refuse("authorization")
        run.check_wait(0)
        # HttpClient captures the policy at construction, before the resolver makes any call.
        with using_request_policy(ControlledHttpPolicy(run)):
            client = EVSClient(self._origin, license_key=self._license_key)
            return resolve_evs_release(client, "ncit", channel)


def compile_policy(environment: Environment) -> CompiledPolicy:
    """Compile explicit metadata permission; no furnished caller is activated."""
    if type(environment) is not Environment:
        raise RequestPolicyError("authorization")
    try:
        selected = environment_record(environment.to_record())
    except ValueError:
        raise RequestPolicyError("authorization") from None
    correlation, raw = _declaration()
    origin = selected["origins"]["evs"]
    if origin is None:
        raise RequestPolicyError("authorization")
    headers = [("accept", "application/json")]
    if credential := environment.credentials.get("NCI_SI_EVS_LICENSE_KEY"):
        headers.append((LICENSE_KEY_HEADER, credential))
    grant = RequestGrant(
        "GET", origin.rstrip("/") + "/api/v1/version", headers, credential_origin=origin
    )
    identity = hashlib.sha256(
        json.dumps(selected, sort_keys=True, separators=(",", ":")).encode() + b"\0" + raw.encode()
    ).hexdigest()
    return CompiledPolicy(
        Authorization(
            (grant,), Limits(), identity, families=(ExactRequestFamily(grant, correlation),)
        ),
        origin,
        credential,
    )
