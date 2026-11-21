"""MCP surface selection using the same authority as the shared producers."""

import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

from mcp.shared.exceptions import MCPError
from mcp.types import CallToolResult, TextContent

from .audit import compact, emit
from .caching import cache_hint
from .errors import PlatformError, correlated, current_correlation_id, serialise
from .permissions import (
    Authority,
    AuthorityResolver,
    PolicyUnavailableError,
    authority_scope,
    deny,
    permits,
    require_current,
)
from .registry import SPECS, servable_prompts

logger = logging.getLogger(__name__)


def authorization(
    profile: str,
    resolver: AuthorityResolver | None,
    session_state: Callable[[Any], dict[str, Any] | None],
) -> Callable[..., Any]:
    """Resolve fresh trusted authority per request, never from MCP arguments or metadata."""

    specs = [spec for spec in SPECS if spec.visible_in(profile)]

    async def authorize(ctx: Any, call_next: Callable[[Any], Awaitable[Any]]) -> Any:
        try:
            authority = await resolver() if resolver else None
        except PolicyUnavailableError as exc:
            # Denying is right; the type alone tells an operator the backend is down.
            emit(logger, logging.WARNING, "policy_unavailable", errorType=type(exc).__name__)
            authority = None
        with (
            authority_scope(authority),
            correlated(current_correlation_id() or (ctx.meta or {}).get("correlationId")),
        ):
            try:
                current = require_current()
                _session_owner(session_state(ctx), current)
                _check_target(ctx, specs)
                result = await call_next(ctx)
                return _filter_result(ctx.method, result, specs)
            except PlatformError as exc:
                return _refusal(ctx.method, exc)

    return authorize


def _session_owner(state: dict[str, Any] | None, authority: Authority) -> None:
    # The SDK binds issuer/subject/client when supplied, but does not include tenant.
    # Bind the complete verified identity before accessing this replica's release pin.
    if state is None:
        return
    owner = state.setdefault("nci_si_principal", authority.principal)
    if owner != authority.principal:
        deny()


def _prompt_permitted(name: str, specs: list[Any]) -> bool:
    available = {spec.name for spec in specs if spec.name and permits(spec.operation)}
    return name in servable_prompts(available)


def _resource_permitted(uri: str, specs: list[Any]) -> bool:
    return any(spec.uri and _matches(spec.uri, uri) and permits(spec.operation) for spec in specs)


def _matches(template: str, uri: str) -> bool:
    pattern = re.sub(r"\\\{[^}]+\\\}", "[^/]+", re.escape(template))
    return re.fullmatch(pattern, uri) is not None


def _check_target(ctx: Any, specs: list[Any]) -> None:
    params = ctx.params or {}
    checks = {
        "tools/call": lambda: any(
            spec.name == params.get("name") and permits(spec.operation)
            for spec in specs
            if spec.name
        ),
        "resources/read": lambda: _resource_permitted(params.get("uri", ""), specs),
        "prompts/get": lambda: _prompt_permitted(params.get("name", ""), specs),
    }
    if ctx.method in checks and not checks[ctx.method]():
        deny()


def _filter_result(method: str, result: Any, specs: list[Any]) -> Any:
    if result is None:
        return result
    filters = {
        "tools/list": ("tools", lambda row: permits(row["name"])),
        "prompts/list": ("prompts", lambda row: _prompt_permitted(row["name"], specs)),
        "resources/list": ("resources", lambda row: _resource_permitted(row["uri"], specs)),
        "resources/templates/list": (
            "resourceTemplates",
            lambda row: _resource_permitted(row["uriTemplate"], specs),
        ),
    }
    if method in filters:
        key, allowed = filters[method]
        result = {**result, key: list(filter(allowed, result[key]))}
    if method == "tools/call":
        return {**result, "_meta": {**result.get("_meta", {}), **cache_hint(error=True)}}
    if method in filters or method in {"resources/read", "server/discover"}:
        return {**result, **cache_hint(error=True)}
    return result


def _refusal(method: str, error: PlatformError) -> Any:
    result = serialise(error)
    if method == "tools/call":
        return CallToolResult(
            is_error=True,
            structured_content=result,
            content=[TextContent(type="text", text=compact(result))],
            _meta=cache_hint(error=True),
        ).model_dump(by_alias=True, exclude_none=True)
    raise MCPError(-32001, error.message, result)
