"""Explicit catalogue entry preparation; admission still uses legacy exact grants."""

from dataclasses import dataclass

from nci_si_acceptance.environments import Environment
from nci_si_acceptance.request_authorization import Authorization, Limits, RequestGrant
from nci_si_mcp.evs import LICENSE_KEY_HEADER
from nci_si_mcp.http_client import RequestPolicyError


@dataclass(frozen=True, slots=True)
class CompiledPolicy:
    authorization: Authorization


def compile_policy(environment: Environment) -> CompiledPolicy:
    """Wrap the existing exact metadata GET; no variable form or caller is activated."""
    origin = environment.origins.get("evs")
    if origin is None:
        raise RequestPolicyError("authorization")
    headers = [("accept", "application/json")]
    if credential := environment.credentials.get("NCI_SI_EVS_LICENSE_KEY"):
        headers.append((LICENSE_KEY_HEADER, credential))
    grant = RequestGrant(
        "GET", origin.rstrip("/") + "/api/v1/version", headers, credential_origin=origin
    )
    return CompiledPolicy(Authorization((grant,), Limits(), environment.policy_sha256))
