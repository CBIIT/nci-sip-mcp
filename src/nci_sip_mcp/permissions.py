"""Request-scoped caller authority, independent of an identity provider or transport."""

from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import cache
from math import isfinite
from time import time
from typing import Any, NoReturn

from .errors import PlatformError


@dataclass(frozen=True)
class Principal:
    """Identity supplied by a trusted verifier, never by tool arguments or headers."""

    issuer: str
    subject: str
    tenant: str | None = None
    client: str | None = None


@dataclass(frozen=True)
class Authority:
    """One policy snapshot; the resolver must refresh it for every request."""

    principal: Principal
    capabilities: frozenset[str]
    policy_version: str
    expires_at: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "capabilities", frozenset(self.capabilities))


AuthorityResolver = Callable[[], Awaitable[Authority | None]]


class PolicyUnavailableError(Exception):
    """A trusted resolver could not obtain a current policy; protected work is denied."""


# Absence of a scope is the explicit trusted-local path. A scoped None is denial,
# never a fallback to that path when a required policy cannot be obtained.
_authority: ContextVar[Authority | None] = ContextVar("caller_authority")
_UNSET: Any = object()

RESOURCE_CAPABILITIES = {
    "concept_resource": "get_concept",
    "release_resource": "resolve_release",
    "index_resource": "search_concepts",
    "data_element_resource": "get_data_element",
    "data_element_version_resource": "get_data_element",
    "registry_resource": "resolve_registry_release",
    "crosswalk_resource": "get_code_map",
}

DEPENDENCIES = {
    "ground_value": ("get_concept", "find_data_elements_for_concept"),
    "expand_cohort": ("get_concept_hierarchy", "get_concept_neighborhood"),
    "harmonize_data_dictionary": ("match_data_elements",),
    "get_concept_for_permissible_value": ("get_data_element", "get_concept"),
}


@contextmanager
def authority_scope(authority: Authority | None) -> Iterator[None]:
    """Install verified authority for this call and its child operations only."""

    token = _authority.set(authority)
    try:
        yield
    finally:
        _authority.reset(token)


def secured() -> bool:
    """Whether this request requires verified caller authority."""

    return _authority.get(_UNSET) is not _UNSET


@cache
def _tool_names() -> frozenset[str]:
    # Registry remains the source of the supported surface; no wildcard or unknown
    # policy label becomes a grant. Import lazily to avoid a declaration cycle.
    from .registry import SPECS  # noqa: PLC0415 - registry producers import this module

    return frozenset(spec.name for spec in SPECS if spec.name)


def permits(capability: str) -> bool:
    """Check freshness again at each restricted producer boundary."""

    if not secured():
        return True
    capability = RESOURCE_CAPABILITIES.get(capability, capability)
    if capability not in _tool_names():
        return False
    authority = _authority.get()
    if authority is None:
        return False
    return _valid(authority) and capability in authority.capabilities


def _valid(authority: Authority) -> bool:
    return bool(
        authority.principal.issuer
        and authority.principal.subject
        and authority.policy_version
        and isfinite(authority.expires_at)
        and authority.expires_at > time()
    )


def require(capability: str) -> None:
    """Refuse without exposing a restricted target or its required permission."""

    if not permits(capability):
        deny()


def deny() -> NoReturn:
    raise PlatformError(
        "permission_denied",
        "Access is not permitted. Contact the service operator to review your access.",
    )


def require_current() -> Authority:
    """An empty valid policy can list an empty catalogue; absent policy is an error."""

    authority = _authority.get()
    if authority is None or not _valid(authority):
        deny()
    return authority


def require_operation(operation: str, arguments: dict[str, Any]) -> None:
    """Preflight all argument-selected dependencies before the first content read."""

    require(operation)
    for dependency in DEPENDENCIES.get(operation, ()):
        require(dependency)
    for dependency in _selected_dependencies(operation, arguments):
        require(dependency)


def _selected_dependencies(operation: str, arguments: dict[str, Any]) -> list[str]:
    dependencies = []
    if operation == "ground_value":
        dependencies.extend(_ground_dependencies(arguments))
    if operation in {"ground_value", "resolve_stored_value"} and arguments.get("commons") not in (
        None,
        "GDC",
    ):
        dependencies.append("get_code_map")
    if operation == "harmonize_data_dictionary" and any(
        column.get("sampleValues") for column in arguments.get("columns", [])
    ):
        dependencies.append("match_value_meanings")
    return dependencies


def _ground_dependencies(arguments: dict[str, Any]) -> list[str]:
    dependencies = []
    if arguments.get("text") is not None:
        dependencies.append("search_concepts")
    if arguments.get("commons") is not None:
        dependencies.append("resolve_stored_value")
    return dependencies
