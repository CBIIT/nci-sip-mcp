"""Thin MCP adapter over the shared tool registry."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from functools import update_wrapper
from importlib.metadata import version
from inspect import Parameter, Signature
from typing import TYPE_CHECKING, Annotated, Any, get_args, get_origin

from . import __version__
from .audit import audited, compact, hashed, result_text, secrets
from .caching import LONG_TTL_MS, cache_call, cache_hint
from .config import Settings, configure_logging
from .context import Context
from .errors import InputValidationError, is_error_record, safe_correlation, validate_correlation
from .invocation import call
from .parameters import Described
from .permissions import AuthorityResolver
from .registry import SPECS, ToolSpec, invoke, servable_prompts
from .release_selection import SessionRelease, session_scope

if TYPE_CHECKING:
    from mcp.server.auth.provider import TokenVerifier
    from mcp.server.auth.settings import AuthSettings

INSTRUCTIONS = (
    "NCI Thesaurus (NCIt) lookup and relationship traversal against live NCI EVS, "
    "plus local indexed search and caDSR data-element lookup, matching and registry discovery. "
    "caDSR keyword search is a requested upstream capability not served today. Every item a tool "
    "returns carries a provenance record that names its terminology release or registry state, "
    "the surface that supplied it and the call's correlationId. A failed tool "
    "call is flagged as an error. Failures the server handles carry the error record "
    "{error: {code, message, details?, correlationId}}: code is one of invalid_request, "
    "not_found, release_not_available, release_mismatch, upstream_unavailable, timeout, "
    "bound_exceeded, capability_unavailable, cursor_expired, permission_denied or internal_error, "
    "and message "
    "names the next step; correlationId echoes the _meta.correlationId of the call, or is "
    "generated. Invalid tool arguments use the same error record.\n\n"
    "Releases: for NCIt, omitting release (or null) resolves the configured monthly or weekly "
    "channel once per call; the first implicit pin is reused for stateful handshake HTTP "
    "sessions and stdio connections. Sessionless 2026-07-28 HTTP resolves per call even in "
    "stateful mode; pass the release from the first result's provenance for stable content. "
    "An explicit release overrides it for that call only and never changes the pin. "
    "Other terminologies require release. The CLI resolves anew per invocation.\n\n"
    "Choosing a tool:\n"
    "- One concept: get_concept; several codes: get_concepts; only a text: search_concepts "
    "(results carry no sections, so read the concepts for definitions).\n"
    "- Around a concept: get_concept_hierarchy for parents, children or paths to the root; "
    "get_concept_neighborhood for roles and associations too; expand_cohort for its "
    "descendants without the codes its exclusion roles withhold.\n"
    "- Subsets: get_concept_subsets for the subsets a concept belongs to; expand_value_set "
    "for the members of a subset.\n"
    "- A data element: get_data_element by publicId or question text; match_data_elements "
    "from a described column or field, the discovery route while search_data_elements is not "
    "served; match_value_meanings for values.\n"
    "- Concept to data elements: find_data_elements_for_concept. Data element to concepts: "
    "get_data_element with include conceptAssociations. A permissible value to its concept: "
    "get_concept_for_permissible_value.\n"
    "- A CRDC field name to its data element and values: get_code_map (page it and match "
    "crdcName). The value a commons stores for a concept: resolve_stored_value.\n"
    "- A retired code to its replacement: resolve_retired_code. A code in another "
    "terminology: get_concept_mappings. A caDSR form: get_form.\n"
    "- Contexts and classification schemes: list_contexts; list_classification_schemes is not "
    "served (OP-C13), so an element's own schemes come with get_data_element include "
    "classificationSchemes. A permissible value alone is not served (OP-C10): read its data "
    "element with include permissibleValues.\n"
    "- Both sides of a text or code at once: ground_value. A whole data dictionary: "
    "harmonize_data_dictionary.\n"
    "- Which release: resolve_release, list_terminologies, resolve_registry_release. Call "
    "get_release_alignment before joining NCIt content with caDSR content."
)


def _require_session_connection(session_type: type) -> None:
    # Stateful HTTP pins and secured-mode principal binding read the SDK's private
    # ServerSession._connection (see _session_state); fail at startup, not on the first call.
    # It is set in __init__, so the class has no attribute to test for; the code object's
    # names hold the attribute without needing the SDK's source files.
    if "_connection" not in session_type.__init__.__code__.co_names:
        raise RuntimeError(
            f"mcp {version('mcp')} no longer gives ServerSession a '_connection' attribute, "
            "which HTTP session identity depends on; install the mcp version in pdm.lock"
        )


def create_mcp(
    settings: Settings | None = None,
    *,
    context: Context | None = None,
    auth: AuthSettings | None = None,
    token_verifier: TokenVerifier | None = None,
    authority_resolver: AuthorityResolver | None = None,
):
    # Optional dependencies are imported only when building the MCP adapter.
    try:
        from mcp.server.caching import CacheHint
        from mcp.server.mcpserver import MCPServer
        from mcp.server.mcpserver.exceptions import ResourceError
        from mcp.server.session import ServerSession
        from mcp.types import CallToolResult, TextContent, ToolAnnotations
        from pydantic import Field, RootModel
    except ImportError as exc:
        raise RuntimeError(
            "The MCP server needs the 'server' extra, which installs mcp>=2,<3 "
            f"(pdm install). Import failed: {exc}"
        ) from exc

    _require_session_connection(ServerSession)
    resolved_settings = settings or Settings.from_env()
    configure_logging(resolved_settings.log_level)
    context = context or Context(resolved_settings)
    from .server_permissions import authorization

    protected = authority_resolver is not None or auth is not None
    mcp = MCPServer(
        "nci-sip-mcp",
        instructions=INSTRUCTIONS,
        version=__version__,
        auth=auth,
        token_verifier=token_verifier,
        cache_hints=dict.fromkeys(
            (
                "tools/list",
                "prompts/list",
                "resources/list",
                "resources/templates/list",
                "server/discover",
            ),
            CacheHint(ttl_ms=LONG_TTL_MS, scope="public"),
        ),
        middleware=[
            _audit_tools(context, resolved_settings.profile, protected=protected),
            *(
                [authorization(resolved_settings.profile, authority_resolver, _session_state)]
                if protected
                else []
            ),
            _cache_results,
            _validate_inputs(resolved_settings.profile),
            _release_session,
        ],
    )

    def tool_call(spec: ToolSpec, arguments: dict[str, Any]) -> Any:
        result = invoke(context, spec.operation, **arguments)
        text = compact(result)
        result_text(text)
        return CallToolResult(
            content=[TextContent(type="text", text=text)],
            structured_content=result,
            is_error=is_error_record(result),
        )

    def resource_call(spec: ToolSpec, arguments: dict[str, Any]) -> Any:
        result = invoke(context, spec.operation, **arguments)
        if is_error_record(result):
            raise ResourceError(compact(result))
        return result

    def register(spec: ToolSpec) -> None:
        if spec.name and spec.visible_in(resolved_settings.profile):
            # The SDK wraps a bare union in a synthetic result field. RootModel keeps
            # the existing top-level object and all optional wire fields unchanged.
            # Handshake-era MCP requires outputSchema.type=object even for a union.
            # Every registry arm is an object; retain the union's detailed validation.
            record = Annotated[spec.output, Field(json_schema_extra={"type": "object"})]
            output = Annotated[CallToolResult, RootModel[record]]
            fn = _callback(spec, tool_call, output_type=output)
            mcp.add_tool(
                fn,
                name=spec.name,
                title=spec.title,
                annotations=ToolAnnotations.model_validate(spec.annotations),
                meta={"group": spec.group},
                structured_output=True,
            )
            _serve_schemas(mcp, spec.name)
        if spec.uri and spec.visible_in(resolved_settings.profile):
            fn = _callback(spec, resource_call)
            mcp.resource(spec.uri, mime_type="application/json")(fn)

    for spec in SPECS:
        register(spec)
    _register_prompts(mcp, resolved_settings.profile)
    return mcp


def _serve_schemas(mcp: Any, name: str) -> None:
    """Replace the schemas the SDK derived for tool `name` with the ones the server publishes.

    pydantic's generated titles carry no meaning for a caller, so both schemas lose them; the
    output schema is named for its tool instead. `mcp._tool_manager` is a private seam (AGENTS.md
    lists the SDK internals relied on): the SDK offers no hook between deriving a schema and
    listing it."""

    tool: Any = mcp._tool_manager.get_tool(name)
    tool.parameters = _served_input_schema(_drop_titles(tool.parameters))
    metadata = tool.fn_metadata
    metadata.output_schema = {"title": f"{name} result", **_drop_titles(metadata.output_schema)}


def _served_input_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """The closed input schema, with descriptions and the shared per-path text bounds.

    The server refuses any argument the tool does not declare, so the schema says so."""

    from .parameters import FIELD_DESCRIPTIONS
    from .validation import TEXT_LIMITS

    definitions = {
        name: _described_record(record, FIELD_DESCRIPTIONS.get(name, {}))
        for name, record in schema.get("$defs", {}).items()
    }

    def bounded_text(node: Any, path: str) -> Any:
        maximum = TEXT_LIMITS.get(path)
        if maximum is None:
            return node
        if node.get("type") == "string":
            node["maxLength"] = maximum
        node["description"] = (
            node.get("description", "")
            + f" At most {maximum} Unicode code points per text value; longer input "
            "is refused with invalid_request before upstream or index work, "
            "never shortened."
        ).strip()
        if path in {"query", "questionText"}:
            node["description"] += (
                " URL-carried text also has a separate 7,000-byte encoded request-target "
                "ceiling, including filters and pins; for caDSR this is conservative "
                "server policy, not measured capacity."
            )
        return node

    def descend(node: Any, path: str) -> Any:
        for key in ("anyOf", "oneOf", "allOf"):
            if key in node:
                node[key] = [project(item, path) for item in node[key]]
        if "items" in node:
            node["items"] = project(node["items"], path)
        return node

    def project(node: Any, path: str) -> Any:
        if "$ref" in node:
            name = node["$ref"].rpartition("/")[2]
            definitions[name] = project(definitions[name], path)
        result = {**node}
        if "properties" in node:
            result["properties"] = {
                name: project(item, f"{path}.{name}".lstrip("."))
                for name, item in node["properties"].items()
            }
        return bounded_text(descend(result, path), path)

    projected = project({key: value for key, value in schema.items() if key != "$defs"}, "")
    return {
        **projected,
        **({"$defs": definitions} if definitions else {}),
        "additionalProperties": False,
    }


def _described_record(record: dict[str, Any], descriptions: dict[str, str]) -> dict[str, Any]:
    properties = {
        name: {**spec, "description": descriptions[name]} if name in descriptions else spec
        for name, spec in record["properties"].items()
    }
    return {**record, "properties": properties, "additionalProperties": False}


def _field_annotation(annotation: Any) -> Any:
    """`annotation` with its `Described` metadata as the pydantic `Field` that states it."""

    from pydantic import Field

    if get_origin(annotation) is not Annotated:
        return annotation
    base, *metadata = get_args(annotation)
    fields = [Field(**m.field_arguments()) if isinstance(m, Described) else m for m in metadata]
    return Annotated[(base, *fields)]


def _drop_titles(node: Any) -> Any:
    """`node` without its `title` keywords; the names under `properties` are kept."""

    if isinstance(node, list):
        return [_drop_titles(item) for item in node]
    if not isinstance(node, dict):
        return node
    return {
        key: _named(value) if key == "properties" else _drop_titles(value)
        for key, value in node.items()
        if key != "title"
    }


def _named(properties: Any) -> Any:
    return {name: _drop_titles(schema) for name, schema in properties.items()}


def _register_prompts(mcp: Any, profile: str) -> None:
    from mcp.server.mcpserver.prompts import Prompt
    from mcp.server.mcpserver.prompts.base import PromptArgument

    tools = {spec.name for spec in SPECS if spec.name and spec.visible_in(profile)}
    for name, template in servable_prompts(tools).items():
        mcp.add_prompt(
            Prompt(
                name=name,
                title=template["title"],
                description=template["adds"],
                arguments=[PromptArgument(**argument) for argument in template["arguments"]],
                fn=_prompt_callback(template),
                context_kwarg=None,
            )
        )


def _prompt_callback(template: dict[str, Any]) -> Callable[..., str]:
    def render(**arguments: str) -> str:
        values = {a["name"]: "" for a in template["arguments"] if not a["required"]}
        return template["template"].format(**(values | arguments))

    return render


def _callback(
    spec: ToolSpec,
    call: Callable[..., Any],
    *,
    output_type: Any = dict[str, Any],
) -> Callable[..., Any]:
    def callback(**arguments: Any) -> Any:
        return call(spec, arguments)

    parameters = [p.replace(annotation=_field_annotation(p.annotation)) for p in spec.parameters]
    update_wrapper(callback, spec.handler)
    callback.__doc__ = spec.description
    callback.__name__ = spec.name or spec.operation
    callback.__dict__["__signature__"] = Signature(parameters, return_annotation=output_type)
    callback.__annotations__ = {p.name: p.annotation for p in parameters} | {"return": output_type}
    return callback


def _audit_tools(context: Context, profile: str, *, protected: bool = False) -> Callable[..., Any]:
    specs = {spec.name: spec for spec in SPECS if spec.name and spec.visible_in(profile)}
    # Secured calls hash every argument: identifiers are not disclosed in telemetry
    # (docs/caller-permissions.md).
    fields = {name: {} if protected else spec.audit for name, spec in specs.items()}
    hidden = secrets(context.settings.evs_license_key, context.settings.cadsr_credential)

    async def record_call(ctx: Any, call_next: Callable[[Any], Awaitable[Any]]) -> Any:
        if ctx.method != "tools/call":
            return await call_next(ctx)
        params = ctx.params or {}
        name = params.get("name", "")
        spec = specs.get(name)
        arguments = params.get("arguments", {})
        with audited(
            name if spec else compact(hashed(name)),
            arguments if isinstance(arguments, dict) else {"arguments": arguments},
            fields.get(name, {}),
            hidden,
            safe_correlation(ctx.meta.get("correlationId") if ctx.meta else None),
        ) as record:
            refused = call(name, lambda: _validate_meta_correlation(ctx.meta))
            if is_error_record(refused):
                result = _invalid_tool_result(refused)
            else:
                result = await call_next(ctx)
            record.result = result.get("structuredContent")
            return result

    return record_call


def _validate_meta_correlation(meta: Any) -> Any:
    return validate_correlation(
        meta.get("correlationId") if meta else None,
        supplied=bool(meta and "correlationId" in meta),
    )


def _invalid_tool_result(result: Any) -> Any:
    from mcp.types import CallToolResult, TextContent

    return CallToolResult(
        is_error=True,
        structured_content=result,
        content=[TextContent(type="text", text=compact(result))],
    ).model_dump(by_alias=True, exclude_none=True)


def _validate_inputs(profile: str) -> Callable[..., Any]:
    # The SDK renders argument-validation failures as plain text. Validate the same
    # registry fields first so invalid requests use the platform's one error path.
    from mcp.types import CallToolResult, TextContent

    models = {
        spec.name: _input_model(spec) for spec in SPECS if spec.name and spec.visible_in(profile)
    }

    async def validate(ctx: Any, call_next: Callable[[Any], Awaitable[Any]]) -> Any:
        params = ctx.params or {}
        model = models.get(params.get("name", "")) if ctx.method == "tools/call" else None
        if model is not None:
            result = call(
                params["name"],
                lambda: _check_arguments(model, params.get("arguments", {})),
            )
            if is_error_record(result):
                return CallToolResult(
                    is_error=True,
                    structured_content=result,
                    content=[TextContent(type="text", text=compact(result))],
                ).model_dump(by_alias=True, exclude_none=True)
        return await call_next(ctx)

    return validate


def _input_model(spec: ToolSpec) -> Any:
    from pydantic import ConfigDict, create_model

    fields: dict[str, Any] = {
        p.name: (
            _field_annotation(p.annotation),
            ... if p.default is Parameter.empty else p.default,
        )
        for p in spec.parameters
    }
    return create_model(
        spec.operation + "Arguments", __config__=ConfigDict(strict=True, extra="forbid"), **fields
    )


def _check_arguments(model: Any, arguments: Any) -> dict[str, Any]:
    from pydantic import ValidationError

    try:
        model.model_validate(arguments)
    except ValidationError as exc:
        error = exc.errors(include_input=False)[0]
        parameter = str(error["loc"][0]) if error["loc"] else "arguments"
        if error["type"] == "missing" and len(error["loc"]) > 1:
            # Name the incomplete object, as callers must supply its required fields.
            parameter = ".".join(str(part) for part in error["loc"][:-1])
        raise InputValidationError(error["msg"], parameter) from None
    return {}


async def _cache_results(ctx: Any, call_next: Callable[[Any], Awaitable[Any]]) -> Any:
    """Keep tool hints in protocol metadata and resource hints on the protocol result."""

    if ctx.method not in {"tools/call", "resources/read"}:
        return await call_next(ctx)
    with cache_call() as decision:
        result = await call_next(ctx)
        hint = cache_hint(error=True) if result.get("isError", False) else decision
        if not hint:
            raise RuntimeError("A registered tool or resource must declare its cache policy.")
        if ctx.method == "tools/call":
            return {**result, "_meta": {**result.get("_meta", {}), **hint}}
        return {**result, **hint}


async def _release_session(ctx: Any, call_next: Callable[[Any], Awaitable[Any]]) -> Any:
    state = _session_state(ctx)
    pin = None
    if state is not None:
        pin = state.setdefault("nci_si_implicit_release", SessionRelease())
    with session_scope(pin):
        return await call_next(ctx)


def _session_state(ctx: Any) -> dict[str, Any] | None:
    # MCP 2 creates ServerSession per request. Its connection owns the validated
    # HTTP session, not the proxy or an untrusted Mcp-Session-Id header.
    connection = ctx.session._connection
    if ctx.request is None:
        # A stdio lifespan is one session, including the SDK's envelope protocol
        # which creates a fresh Connection for each request on that same stream.
        return ctx.lifespan_context
    if connection.session_id is not None:
        return connection.state
    return None
