"""Call the tool named by the specification, or report NOT IMPLEMENTED when absent."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pytest
from mcp.shared.exceptions import MCPError

from nci_sip_acceptance.spec import TOOLS, Listing

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from mcp import types

    from nci_sip_acceptance.client import Session

NOT_IMPLEMENTED = "NOT IMPLEMENTED"


class NotImplementedSkip(pytest.skip.Exception):
    """A test skipped because the server does not list the required tool it calls; the report
    counts it as NOT IMPLEMENTED by this type, not by its text."""

    def __init__(self, name: str) -> None:
        super().__init__(f"{NOT_IMPLEMENTED}: {name}")


@dataclass(frozen=True, slots=True)
class Result:
    """A tool's answer: the tool that gave it, whether it is an error, its content, its
    `_meta`, and every text block."""

    tool: str
    is_error: bool
    content: Any
    meta: dict[str, Any]
    texts: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Process:
    """What the harness keeps of a server process: its standard error, its data directory,
    and the upstream requests it made while it started. A remote server's standard error and
    data directory are out of the harness's reach (`log` and `data` are None): only what the
    server returns can be checked for what it must not write."""

    log: Path | None
    data: Path | None
    startup: tuple[dict[str, Any], ...]

    def written(self) -> str:
        """Everything the process wrote: its standard error and every file of its data."""

        if self.log is None or self.data is None:
            return ""
        files = [self.log, *sorted(path for path in self.data.rglob("*") if path.is_file())]
        return "".join(path.read_bytes().decode("utf-8", "replace") for path in files)


def _texts(result: types.CallToolResult) -> tuple[str, ...]:
    return tuple(block.text for block in result.content if block.type == "text")


def _content(result: types.CallToolResult) -> Any:
    """The structured content, or the first text block: its JSON, or the text itself."""

    if result.structured_content is not None:
        return result.structured_content
    text = next((block.text for block in result.content if block.type == "text"), None)
    if text is None:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return text


@dataclass(frozen=True, slots=True)
class Read:
    """What resources/read gave for a URI: the base MIME type of each content (its parameters,
    such as a charset, dropped), the first content as JSON (or its text), and the caching hint
    the result carries, with whether both fields of it came on the wire (an omitted one is read
    as its default)."""

    mime_types: tuple[str | None, ...]
    content: Any
    ttl_ms: int
    cache_scope: str
    carried: bool


def _base_type(mime: str | None) -> str:
    """A MIME type without its parameters, in lower case: application/json for
    "application/json; charset=utf-8"."""

    return (mime or "").partition(";")[0].strip().lower()


def _read(result: types.ReadResourceResult) -> Read:
    first = result.contents[0] if result.contents else None
    text = getattr(first, "text", None)
    try:
        content = json.loads(text) if text is not None else None
    except ValueError:
        content = text
    return Read(
        tuple(_base_type(each.mime_type) for each in result.contents),
        content,
        result.ttl_ms,
        result.cache_scope,
        {"ttl_ms", "cache_scope"} <= result.model_fields_set,
    )


def _answered[T](method: str, request: Callable[[], T]) -> T:
    """The server's answer to `request`; its refusal, such as no such method because the server
    lacks the capability, fails the test and says so."""

    try:
        return request()
    except MCPError as refusal:
        pytest.fail(f"{method} was refused: {refusal.message}")


class Tools:
    """The required tools of one server session."""

    def __init__(
        self,
        session: Session,
        process: Process | None = None,
        requests: Callable[[], int] | None = None,
    ) -> None:
        self._session = session
        self.process = process
        # How many upstream requests the server has made so far, where the harness can tell.
        self._requests = requests
        self.listing = session.list_tools()
        self.available = {tool.name: tool for tool in self.listing.tools}

    @property
    def listing_bytes(self) -> int:
        """The size of the tools/list result as JSON: what every client session reads."""

        return len(self.listing.model_dump_json(by_alias=True, exclude_none=True).encode())

    def list_again(self) -> types.ListToolsResult:
        """A fresh tools/list of the same session."""

        return self._session.list_tools()

    def _check_requests(self, name: str, before: int) -> None:
        """Fail the test when a call made more upstream requests than its tool's stated bound,
        retries included (A5.3)."""

        bound = TOOLS.get(name, {}).get("requests")
        if bound is None or self._requests is None:
            return
        made = self._requests() - before
        if made > bound:
            pytest.fail(f"{name} made {made} upstream requests, more than its bound of {bound}")

    def list_prompts(self) -> types.ListPromptsResult:
        """prompts/list; a server without prompts fails the test."""

        return _answered("prompts/list", self._session.list_prompts)

    def get_prompt(self, name: str, arguments: dict[str, str]) -> types.GetPromptResult:
        return _answered("prompts/get", lambda: self._session.get_prompt(name, arguments))

    def list_resources(self) -> types.ListResourcesResult:
        """resources/list, every page; a server without resources fails the test."""

        return _answered("resources/list", self._session.list_resources)

    def list_resource_templates(self) -> types.ListResourceTemplatesResult:
        """resources/templates/list, every page."""

        return _answered("resources/templates/list", self._session.list_resource_templates)

    def listed_resources(self) -> Listing:
        """The concrete URIs resources/list names and the URI templates
        resources/templates/list names, each from its own method."""

        return Listing(
            {each.uri for each in self.list_resources().resources},
            {each.uri_template for each in self.list_resource_templates().resource_templates},
        )

    def read_resource(self, uri: str) -> Read:
        """resources/read of `uri`; a refusal fails the test."""

        return _read(_answered("resources/read", lambda: self._session.read_resource(uri)))

    def resource_refusal(self, uri: str) -> str | None:
        """What the server says in refusing resources/read of `uri`; None where it gave content."""

        try:
            self._session.read_resource(uri)
        except MCPError as refusal:
            return refusal.message
        return None

    def implemented(self, name: str) -> bool:
        """Whether the server lists the required tool `name`."""

        return name in self.available

    def call(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        meta: types.RequestParamsMeta | None = None,
    ) -> Result:
        """Call the required tool `name`, with `meta` as the call's `_meta`; skip the test as
        NOT IMPLEMENTED when nothing answers."""

        arguments = arguments or {}
        if not self.implemented(name):
            raise NotImplementedSkip(name)
        before = self._requests() if self._requests else 0
        result = self._session.call_tool(name, arguments, meta)
        self._check_requests(name, before)
        return Result(
            name, bool(result.is_error), _content(result), result.meta or {}, _texts(result)
        )
