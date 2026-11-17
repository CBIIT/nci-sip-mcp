"""HTTP serving over the same MCP adapter, with process-local session ownership."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any
from urllib.request import parse_http_list

from ncit_index.errors import (
    IndexCapabilityError,
    IndexCompatibilityError,
    IndexStorageError,
    NoActiveIndexError,
)

from .audit import emit
from .config import Settings
from .context import Context
from .errors import PlatformError
from .permissions import AuthorityResolver
from .server import create_mcp

if TYPE_CHECKING:
    from mcp.server.auth.provider import TokenVerifier
    from mcp.server.auth.settings import AuthSettings
    from starlette.applications import Starlette
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)
# Match the deployment policy's SIGTERM drain window; this is not a request timeout.
# Coordinate changes with the orchestrator's termination grace period.
GRACEFUL_SHUTDOWN_SECONDS = 20

# Reclaim inactive SDK sessions; the interval is unmeasured. Re-measure client idle
# durations and session retention before changing it.
SESSION_IDLE_TIMEOUT_SECONDS = 1800
# Bound process-local session state; the capacity is unmeasured. Re-measure concurrent
# session counts and per-session memory before changing it.
MAX_HTTP_SESSIONS = 10_000


class HTTPBoundary:
    """Protect all HTTP routes and record only the status of authentication refusals."""

    def __init__(
        self,
        app: ASGIApp,
        security: Any,
        protected: bool = False,
        required_scopes: tuple[str, ...] = (),
    ) -> None:
        from mcp.server.transport_security import TransportSecurityMiddleware

        self.app = app
        self.protected = protected
        self.required_scopes = required_scopes
        self.security = TransportSecurityMiddleware(security)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        from starlette.requests import Request

        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def report(message: Message) -> None:
            if message["type"] != "http.response.start":
                await send(message)
                return
            if self.protected:
                headers = [
                    (k, v) for k, v in message.get("headers", []) if k.lower() != b"cache-control"
                ]
                message = {**message, "headers": [*headers, (b"cache-control", b"no-store")]}
            if _auth_refusal(message):
                message = {
                    **message,
                    "headers": [
                        _scope_challenge(header, self.required_scopes)
                        for header in message["headers"]
                    ],
                }
                emit(logger, logging.WARNING, "http_auth_rejected", status=message["status"])
            await send(message)

        if refused := await self.security.validate_request(Request(scope)):
            await refused(scope, receive, report)
            return
        await self.app(scope, receive, report)


def _auth_refusal(message: Message) -> bool:
    return message["status"] in (401, 403) and any(
        key.lower() == b"www-authenticate" for key, _ in message.get("headers", [])
    )


def _scope_challenge(header: tuple[bytes, bytes], scopes: tuple[str, ...]) -> tuple[bytes, bytes]:
    key, value = header
    scheme, _, parameters = value.partition(b" ")
    if key.lower() != b"www-authenticate" or scheme.lower() != b"bearer" or not scopes:
        return header
    # Parse only to detect an existing parameter; preserve all original challenge bytes.
    names = [
        part.partition("=")[0].strip().lower()
        for part in parse_http_list(parameters.decode("latin1"))
    ]
    if "scope" in names:
        return header
    return key, value + b', scope="' + " ".join(scopes).encode("ascii") + b'"'


def _readiness_error(context: Context, require_index: bool) -> str | None:
    try:
        if not require_index and context.index.get_active_manifest() is None:
            return None
        context.index.verify_active(context.embedding_provider)
    except (
        IndexStorageError,
        IndexCompatibilityError,
        NoActiveIndexError,
        IndexCapabilityError,
        PlatformError,
    ) as exc:
        return "PlatformError" if isinstance(exc, IndexCapabilityError) else type(exc).__name__
    return None


def create_http_app(
    settings: Settings,
    *,
    context: Context | None = None,
    auth: AuthSettings | None = None,
    token_verifier: TokenVerifier | None = None,
    authority_resolver: AuthorityResolver | None = None,
) -> Starlette:
    """Inject an approved auth provider here; the default accepts unauthenticated clients."""
    from mcp.server.transport_security import TransportSecuritySettings
    from starlette.concurrency import run_in_threadpool
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    if settings.http_auth_mode == "required":
        from .http_auth import configured_auth

        integration = configured_auth(settings)
        auth = integration.auth
        token_verifier = integration.token_verifier
        authority_resolver = integration.authority_resolver
    context = context or Context(settings)
    mcp = create_mcp(
        settings,
        context=context,
        auth=auth,
        token_verifier=token_verifier,
        authority_resolver=authority_resolver,
    )
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=list(settings.http_allowed_hosts),
        allowed_origins=list(settings.http_allowed_origins),
    )
    app = mcp.streamable_http_app(
        stateless_http=settings.http_sessions == "stateless",
        json_response=True,
        max_request_body_size=settings.http_max_request_bytes,
        transport_security=security,
        session_idle_timeout=SESSION_IDLE_TIMEOUT_SECONDS,
        max_sessions=MAX_HTTP_SESSIONS,
    )

    async def health(request: Any) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    was_ready = True

    async def ready(request: Any) -> JSONResponse:
        nonlocal was_ready
        # SQLite can wait on a writer; health and MCP requests must remain responsive.
        error = await run_in_threadpool(_readiness_error, context, settings.http_require_index)
        # Keep the transition atomic on the event loop after the storage check.
        available = error is None
        if was_ready and not available:
            emit(logger, logging.WARNING, "http_not_ready", errorType=error)
        was_ready = available
        return JSONResponse(
            {"status": "ready" if available else "not_ready"},
            status_code=200 if available else 503,
        )

    app.routes.extend([Route("/health", health), Route("/ready", ready)])
    app.add_middleware(
        HTTPBoundary,
        security=security,
        protected=auth is not None or authority_resolver is not None,
        required_scopes=tuple(auth.required_scopes or ()) if auth else (),
    )
    return app


def run_http(settings: Settings, context: Context) -> None:
    """One process owns its sessions; a supervisor can start independent replicas."""
    import uvicorn

    uvicorn.run(
        create_http_app(settings, context=context),
        host=settings.http_host,
        port=settings.http_port,
        log_config=None,
        access_log=False,
        proxy_headers=False,
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS,
    )
