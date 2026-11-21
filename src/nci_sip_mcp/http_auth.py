"""Operator-installed SDK authentication and policy integration; no token cryptography."""

from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
from time import time
from typing import TYPE_CHECKING

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings

from .permissions import Authority, AuthorityResolver, Principal

if TYPE_CHECKING:
    from .config import Settings


@dataclass(frozen=True)
class HTTPAuthIntegration:
    """Trusted deployment code supplies all three parts; none come from a request."""

    auth: AuthSettings
    token_verifier: TokenVerifier
    authority_resolver: AuthorityResolver


def configured_auth(settings: Settings) -> HTTPAuthIntegration:
    if not settings.http_auth_factory:
        raise ValueError("Required HTTP authentication needs an installed integration factory")
    module, name = settings.http_auth_factory.split(":")
    integration = getattr(import_module(module), name)(settings)
    if not isinstance(integration, HTTPAuthIntegration):
        raise ValueError("HTTP integration factory must return HTTPAuthIntegration")
    _validate(integration)

    async def resolve() -> Authority | None:
        token = get_access_token()
        if token is None:
            return None
        authority = await integration.authority_resolver()
        if authority is None or authority.principal != _principal(token):
            return None
        return authority

    return HTTPAuthIntegration(integration.auth, _BoundVerifier(integration), resolve)


def _validate(integration: HTTPAuthIntegration) -> None:
    if not isinstance(integration.auth, AuthSettings):
        raise ValueError("HTTP integration needs SDK AuthSettings")
    if not integration.auth.validate_token_resource or not integration.auth.resource_server_url:
        raise ValueError("HTTP integration must enforce its resource audience")
    if not callable(getattr(integration.token_verifier, "verify_token", None)):
        raise ValueError("HTTP integration needs a token verifier")
    if not callable(integration.authority_resolver):
        raise ValueError("HTTP integration needs a caller policy resolver")


def _principal(token: AccessToken) -> Principal:
    claims = token.claims or {}
    return Principal(
        claims.get("iss", ""), token.subject or "", claims.get("tenant"), token.client_id
    )


class _BoundVerifier:
    """Require verified identity and finite expiry; SDK also enforces audience and scopes."""

    def __init__(self, integration: HTTPAuthIntegration) -> None:
        self.integration = integration

    async def verify_token(self, token: str) -> AccessToken | None:
        verified = await self.integration.token_verifier.verify_token(token)
        if verified is None:
            return None
        principal = _principal(verified)
        if principal.issuer != str(self.integration.auth.issuer_url):
            return None
        if not principal.subject or not principal.client or verified.expires_at is None:
            return None
        if verified.expires_at <= time():
            return None
        return verified
