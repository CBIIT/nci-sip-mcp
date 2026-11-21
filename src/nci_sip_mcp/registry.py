"""One tool declaration and invocation path for MCP, CLI and resources."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Collection, Mapping
from dataclasses import asdict, dataclass, field, make_dataclass
from functools import cache
from importlib.resources import files
from inspect import Parameter, Signature, getdoc, signature
from typing import (
    Annotated,
    Any,
    Concatenate,
    Literal,
    get_args,
    get_origin,
    get_type_hints,
    is_typeddict,
)

from . import cadsr_content, cadsr_matching, content, handlers, seam, workflows
from .audit import AuditClass, audited, secrets
from .bounds import deadline_description, deadline_scope
from .caching import invocation_policy
from .config import DEFAULT_CALL_DEADLINE_SECONDS
from .context import Context
from .invocation import call
from .permissions import require
from .release_selection import selection_scope
from .results import (
    ClassificationSchemesResult,
    CodeMapResource,
    CodeMapsResult,
    Cohort,
    Concept,
    ConceptBatch,
    ConceptResult,
    ConceptSearch,
    ContextsResult,
    DataElement,
    DataElementMatches,
    DataElementSearch,
    DataElementUses,
    ErrorResult,
    Form,
    GroundedValue,
    HarmonizedDictionary,
    Hierarchy,
    IndexManifestResult,
    MappingsResult,
    Neighborhood,
    PermissibleValue,
    PermissibleValueConcept,
    RegistryReleaseResult,
    RelationshipsResult,
    ReleaseAlignment,
    ReleaseResult,
    ResolvedReleaseResult,
    RetiredCode,
    SearchResult,
    StoredValuesResult,
    SubsetsResult,
    TerminologiesResult,
    TraversalResult,
    UnresolvedPermissibleValueConcept,
    ValueMeaningMatches,
    ValueSetExpansion,
)
from .validation import text_limit_description, validate_text_lengths

# The CLI spellings that differ from the handler field: the negative flag of each True-default
# boolean, and singular spellings of repeatable list arguments.
_CLI_FLAGS = {
    "includeModules": "--no-modules",
    "include_hierarchy": "--no-hierarchy",
    "include_roles": "--no-roles",
    "include_associations": "--no-associations",
    "relationship_names": "--relationship-name",
    "edge_types": "--edge-type",
}


@dataclass(frozen=True)
class ToolSpec:
    """One declaration shared by both adapters; input fields come from the handler."""

    # The static form of the rule __post_init__ checks at run time: a type checker cannot
    # require exactly Context (an unannotated, Any or supertype context passes), and handlers
    # declared in tests are not checked.
    handler: Callable[Concatenate[Context, ...], dict[str, Any]]
    group: str
    output: Any
    # None means the handler supplies the whole policy, without a registry default.
    resolution: bool | None = None
    name: str | None = None
    title: str | None = None
    command: str | None = None
    uri: str | None = None
    audit: dict[str, AuditClass] = field(default_factory=dict)
    deadline: bool = True
    input_model: type = field(init=False)
    parameters: tuple[Parameter, ...] = field(init=False)

    def __post_init__(self) -> None:
        hints = get_type_hints(self.handler, include_extras=True)
        parameters = tuple(
            p.replace(annotation=hints[p.name]) for p in _public_parameters(self.handler, hints)
        )
        for parameter in parameters:
            _require_cli_spelling(self.handler.__name__, parameter)
        model = make_dataclass(
            self.handler.__name__ + "Input",
            [_input_field(p) for p in parameters],
        )
        object.__setattr__(self, "parameters", parameters)
        object.__setattr__(self, "input_model", model)

    @property
    def operation(self) -> str:
        return self.handler.__name__

    @property
    def description(self) -> str:
        description = getdoc(self.handler) or ""
        if self.deadline:
            description += "\n\n" + deadline_description(DEFAULT_CALL_DEADLINE_SECONDS)
            description += "\n" + text_limit_description()
        return description

    @property
    def annotations(self) -> dict[str, bool]:
        return {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        }

    def visible_in(self, profile: str) -> bool:
        return profile in ("unified", self.group)

    def arguments(self, args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any]:
        bound = Signature(self.parameters).bind(*args, **kwargs)
        return asdict(self.input_model(**bound.arguments))


_POSITIONAL = (Parameter.POSITIONAL_ONLY, Parameter.POSITIONAL_OR_KEYWORD)


def _public_parameters(handler: Callable[..., Any], hints: Mapping[str, object]) -> list[Parameter]:
    """The inputs after the injected Context, which the handler must take first by position.

    The Context is recognised by its annotation: a public argument may itself be named
    `context` (list_classification_schemes), and a dropped public first parameter would
    silently vanish from the schema and the CLI.
    """
    parameters = list(signature(handler).parameters.values())
    first = parameters[0] if parameters else None
    if first is None or first.kind not in _POSITIONAL or hints.get(first.name) is not Context:
        raise ValueError(
            f"{handler.__name__} must take the Context as its first positional parameter"
        )
    return parameters[1:]


def _require_cli_spelling(handler: str, parameter: Parameter) -> None:
    # The CLI turns a True boolean default into a store_false flag spelled like the parameter,
    # which reads as the opposite of what it does. Any True default must therefore have a
    # spelling declared in _CLI_FLAGS; the check reads no annotation, so a True default on a
    # parameter not annotated bool (which no handler has) is refused too. It cannot tell a
    # negative spelling from a declared rename: the reviewer of the spelling does.
    if parameter.default is True and parameter.name not in _CLI_FLAGS:
        raise ValueError(
            f"{handler}: {parameter.name} defaults to True; declare its negative CLI flag "
            "in registry._CLI_FLAGS"
        )


def _input_field(parameter: Parameter) -> tuple:
    declaration = (parameter.name, parameter.annotation)
    if parameter.default is Parameter.empty:
        return declaration
    return (*declaration, field(default=parameter.default))


SPECS = (
    ToolSpec(
        workflows.ground_value,
        "workflow",
        GroundedValue | ErrorResult,
        name="ground_value",
        title="Cross-registry value grounding",
        command="ground-value",
        audit={
            "conceptCode": "plain",
            "release": "plain",
            "registryRelease": "plain",
            "text": "hash",
            "commons": "hash",
        },
    ),
    ToolSpec(
        workflows.expand_cohort,
        "workflow",
        Cohort | ErrorResult,
        name="expand_cohort",
        title="Cohort concept expansion",
        command="expand-cohort",
        audit={
            "conceptCode": "plain",
            "release": "plain",
            "maxDepth": "plain",
            "includeNegative": "plain",
            "maxNodes": "plain",
        },
    ),
    ToolSpec(
        workflows.harmonize_data_dictionary,
        "workflow",
        HarmonizedDictionary | ErrorResult,
        name="harmonize_data_dictionary",
        title="Data dictionary harmonization",
        command="harmonize-data-dictionary",
        audit={"registryRelease": "plain", "columns": "hash", "filters": "hash"},
    ),
    ToolSpec(
        seam.resolve_stored_value,
        "cross-domain",
        StoredValuesResult | ErrorResult,
        name="resolve_stored_value",
        title="Stored values in a commons",
        command="resolve-stored-value",
        audit={
            "conceptCode": "plain",
            "commons": "hash",
            "release": "plain",
            "dataElementId": "plain",
        },
    ),
    ToolSpec(
        seam.get_release_alignment,
        "cross-domain",
        ReleaseAlignment | ErrorResult,
        name="get_release_alignment",
        title="Terminology and registry alignment",
        command="get-release-alignment",
        audit={"maxIntervalDays": "plain"},
    ),
    ToolSpec(
        seam.find_data_elements_for_concept,
        "cross-domain",
        DataElementUses | ErrorResult,
        name="find_data_elements_for_concept",
        title="Data elements for a concept",
        command="find-data-elements-for-concept",
        audit={
            "conceptCode": "plain",
            "terminology": "plain",
            "release": "plain",
            "expandDescendants": "plain",
            "includePermissibleValues": "plain",
            "limit": "plain",
            "cursor": "hash",
        },
    ),
    ToolSpec(
        seam.get_concept_for_permissible_value,
        "cross-domain",
        PermissibleValueConcept | UnresolvedPermissibleValueConcept | ErrorResult,
        name="get_concept_for_permissible_value",
        title="Concept for a permissible value",
        command="get-concept-for-permissible-value",
        audit={
            "permissibleValueId": "plain",
            "dataElementId": "plain",
            "version": "plain",
            "value": "hash",
            "release": "plain",
        },
    ),
    ToolSpec(
        cadsr_content.get_form,
        "cadsr",
        Form | ErrorResult,
        False,
        name="get_form",
        title="Form details",
        command="get-form",
        audit={
            "publicId": "plain",
            "keyword": "hash",
            "version": "plain",
            "includeModules": "plain",
            "registryRelease": "plain",
        },
    ),
    ToolSpec(
        cadsr_content.get_permissible_value,
        "cadsr",
        PermissibleValue | ErrorResult,
        False,
        name="get_permissible_value",
        title="Permissible value details",
        command="get-permissible-value",
        audit={"permissibleValueId": "plain", "registryRelease": "plain"},
    ),
    ToolSpec(
        cadsr_content.get_code_map,
        "cadsr",
        CodeMapsResult | ErrorResult,
        False,
        name="get_code_map",
        title="Commons value bindings",
        command="get-code-map",
        audit={
            "sourceSystem": "plain",
            "targetContext": "hash",
            "dataElementId": "plain",
            "limit": "plain",
            "cursor": "hash",
            "registryRelease": "plain",
        },
    ),
    ToolSpec(
        cadsr_content.crosswalk_resource,
        "cadsr",
        CodeMapResource | ErrorResult,
        False,
        uri="cadsr://crosswalk/crdc",
    ),
    ToolSpec(
        cadsr_matching.match_data_elements,
        "cadsr",
        DataElementMatches | ErrorResult,
        False,
        name="match_data_elements",
        title="Data element candidates",
        command="match-data-elements",
        audit={
            "entities": "hash",
            "matchLimit": "plain",
            "modelVariant": "hash",
            "similarityThreshold": "plain",
            "filters": "hash",
            "registryRelease": "plain",
        },
    ),
    ToolSpec(
        cadsr_matching.match_value_meanings,
        "cadsr",
        ValueMeaningMatches | ErrorResult,
        False,
        name="match_value_meanings",
        title="Value meaning candidates",
        command="match-value-meanings",
        audit={
            "values": "hash",
            "strictness": "plain",
            "terminologyScope": "plain",
            "registryRelease": "plain",
        },
    ),
    ToolSpec(
        cadsr_content.get_data_element,
        "cadsr",
        DataElement | ErrorResult,
        False,
        name="get_data_element",
        title="Data element details",
        command="get-data-element",
        audit={
            "publicId": "plain",
            "longName": "hash",
            "questionText": "hash",
            "version": "plain",
            "include": "plain",
            "registryRelease": "plain",
        },
    ),
    ToolSpec(
        cadsr_content.search_data_elements,
        "cadsr",
        DataElementSearch | ErrorResult,
        False,
        name="search_data_elements",
        title="Data element search",
        command="search-data-elements",
        audit={
            "query": "hash",
            "mode": "plain",
            "filters": "hash",
            "cursor": "hash",
            "limit": "plain",
            "registryRelease": "plain",
        },
    ),
    ToolSpec(
        cadsr_content.list_contexts,
        "cadsr",
        ContextsResult | ErrorResult,
        False,
        name="list_contexts",
        title="Registry contexts",
        command="list-contexts",
        audit={"limit": "plain", "cursor": "hash", "registryRelease": "plain"},
    ),
    ToolSpec(
        cadsr_content.list_classification_schemes,
        "cadsr",
        ClassificationSchemesResult | ErrorResult,
        False,
        name="list_classification_schemes",
        title="Classification schemes",
        command="list-classification-schemes",
        audit={"context": "hash", "limit": "plain", "cursor": "hash", "registryRelease": "plain"},
    ),
    ToolSpec(
        cadsr_content.resolve_registry_release,
        "cadsr",
        RegistryReleaseResult | ErrorResult,
        True,
        name="resolve_registry_release",
        title="Registry content state",
        command="resolve-registry-release",
    ),
    ToolSpec(
        cadsr_content.data_element_resource,
        "cadsr",
        DataElement | ErrorResult,
        False,
        uri="cadsr://data-element/{publicId}",
        audit={"publicId": "plain"},
    ),
    ToolSpec(
        cadsr_content.data_element_version_resource,
        "cadsr",
        DataElement | ErrorResult,
        False,
        uri="cadsr://data-element/{publicId}/{version}",
        audit={"publicId": "plain", "version": "plain"},
    ),
    ToolSpec(
        cadsr_content.registry_resource,
        "cadsr",
        RegistryReleaseResult | ErrorResult,
        False,
        uri="cadsr://registry/release",
    ),
    ToolSpec(
        content.expand_value_set,
        "evs",
        ValueSetExpansion | ErrorResult,
        False,
        name="expand_value_set",
        title="Subset members",
        audit={
            "terminology": "plain",
            "release": "plain",
            "valueSet": "plain",
            "code": "plain",
            "count": "plain",
            "offset": "plain",
            "activeOnly": "plain",
        },
    ),
    ToolSpec(
        content.get_concept_subsets,
        "evs",
        SubsetsResult | ErrorResult,
        False,
        name="get_concept_subsets",
        title="Concept subset membership",
        audit={"terminology": "plain", "release": "plain", "code": "plain"},
    ),
    ToolSpec(
        content.get_concept_mappings,
        "evs",
        MappingsResult | ErrorResult,
        False,
        name="get_concept_mappings",
        title="Cross-terminology mappings",
        audit={
            "terminology": "plain",
            "release": "plain",
            "code": "plain",
            "targetTerminology": "plain",
        },
    ),
    ToolSpec(
        content.resolve_retired_code,
        "evs",
        RetiredCode | ErrorResult,
        False,
        name="resolve_retired_code",
        title="Retired concept replacements",
        audit={"terminology": "plain", "release": "plain", "code": "plain"},
    ),
    ToolSpec(
        content.list_relationships,
        "evs",
        RelationshipsResult | ErrorResult,
        False,
        name="list_relationships",
        title="Relationship types",
        audit={"terminology": "plain", "release": "plain"},
    ),
    ToolSpec(
        content.get_concepts,
        "evs",
        ConceptBatch | ErrorResult,
        False,
        name="get_concepts",
        title="Concept details in bulk",
        audit={"terminology": "plain", "release": "plain", "codes": "plain", "include": "plain"},
    ),
    ToolSpec(
        content.get_concept,
        "evs",
        Concept | ErrorResult,
        False,
        name="get_concept",
        title="Concept details",
        audit={"terminology": "plain", "release": "plain", "code": "plain", "include": "plain"},
    ),
    ToolSpec(
        content.search_concepts,
        "evs",
        ConceptSearch | ErrorResult,
        False,
        name="search_concepts",
        title="Terminology search",
        audit={
            "terminology": "plain",
            "release": "plain",
            "query": "hash",
            "mode": "plain",
            "limit": "plain",
            "cursor": "hash",
            "retired": "plain",
        },
    ),
    ToolSpec(
        content.get_concept_hierarchy,
        "evs",
        Hierarchy | ErrorResult,
        False,
        name="get_concept_hierarchy",
        title="Concept hierarchy",
        audit={
            "terminology": "plain",
            "release": "plain",
            "code": "plain",
            "direction": "plain",
            "depth": "plain",
            "limit": "plain",
            "cursor": "hash",
        },
    ),
    ToolSpec(
        content.get_concept_neighborhood,
        "evs",
        Neighborhood | ErrorResult,
        False,
        name="get_concept_neighborhood",
        title="Concept relationships",
        audit={
            "terminology": "plain",
            "release": "plain",
            "code": "plain",
            "depth": "plain",
            "kinds": "plain",
            "maxNodes": "plain",
            "maxEdges": "plain",
            "budgetPerKind": "plain",
            "includeNegative": "plain",
        },
    ),
    ToolSpec(
        handlers.resolve_release,
        "evs",
        ResolvedReleaseResult | ErrorResult,
        True,
        name="resolve_release",
        title="Current terminology release",
        command="resolve-release",
        audit={"terminology": "plain", "channel": "plain"},
    ),
    ToolSpec(
        handlers.list_terminologies,
        "evs",
        TerminologiesResult | ErrorResult,
        True,
        name="list_terminologies",
        title="Available terminologies",
        command="list-terminologies",
    ),
    ToolSpec(
        handlers.search,
        "evs",
        SearchResult | ErrorResult,
        False,
        command="search",
        audit={"query": "hash", "limit": "plain", "mode": "plain", "include_raw": "plain"},
    ),
    ToolSpec(
        handlers.lookup,
        "evs",
        ConceptResult | ErrorResult,
        False,
        command="lookup",
        audit={"code": "plain", "live_only": "plain", "include_raw": "plain"},
    ),
    ToolSpec(
        handlers.traverse,
        "evs",
        TraversalResult | ErrorResult,
        False,
        command="traverse",
        audit={
            "start_codes": "plain",
            "direction": "plain",
            "max_depth": "plain",
            "max_nodes": "plain",
            "max_edges": "plain",
            "include_hierarchy": "plain",
            "include_roles": "plain",
            "include_associations": "plain",
            "relationship_names": "hash",
            "edge_types": "plain",
            "budget_per_kind": "plain",
        },
    ),
    ToolSpec(
        handlers.release_info,
        "evs",
        ReleaseResult | ErrorResult,
        True,
        command="release-info",
    ),
    ToolSpec(
        handlers.index_codes,
        "evs",
        dict[str, Any],
        False,
        command="index-sample",
        deadline=False,
        audit={"codes": "plain"},
    ),
    ToolSpec(
        handlers.evaluate,
        "evs",
        dict[str, Any],
        False,
        command="evaluate",
        audit={"build_id": "plain"},
    ),
    ToolSpec(
        handlers.index_build, "evs", dict[str, Any], False, command="index-build", deadline=False
    ),
    ToolSpec(
        handlers.index_builds, "evs", dict[str, Any], False, command="index-builds", deadline=False
    ),
    ToolSpec(
        handlers.index_rebuild,
        "evs",
        dict[str, Any],
        False,
        command="index-rebuild",
        deadline=False,
        audit={"build_id": "plain"},
    ),
    ToolSpec(
        handlers.index_activate,
        "evs",
        dict[str, Any],
        False,
        command="index-activate",
        deadline=False,
        audit={"build_id": "plain"},
    ),
    ToolSpec(
        handlers.concept_resource,
        "evs",
        Concept | ErrorResult,
        False,
        uri="ncit://concept/{release}/{code}",
        audit={"release": "plain", "code": "plain"},
    ),
    ToolSpec(
        handlers.release_resource,
        "evs",
        ResolvedReleaseResult | ErrorResult,
        False,
        uri="ncit://release/{version}",
        audit={"version": "plain"},
    ),
    ToolSpec(
        handlers.index_resource,
        "evs",
        IndexManifestResult | ErrorResult,
        False,
        uri="ncit://index/manifest/{release}",
        audit={"release": "plain"},
    ),
)
OPERATIONS = {spec.operation: spec for spec in SPECS}


def servable_prompts(tools: Collection[str]) -> dict[str, Any]:
    """The packaged prompt templates whose every named tool is in `tools`.

    The one reader of data/prompts.json, which is packaged without a YAML dependency;
    test_prompts verifies it equals spec/. Profile selection and caller permissions both
    apply this rule, so a prompt is never offered that names a tool the caller lacks.
    """

    return {name: t for name, t in _templates().items() if set(t["tools"]) <= set(tools)}


@cache
def _templates() -> dict[str, Any]:
    # Read once per process: the catalogue filter asks per prompt row and per call.
    return json.loads(files("nci_sip_mcp").joinpath("data/prompts.json").read_text())


def invoke(
    context: Context, operation: str, /, *args: Any, _correlation_id: object = None, **kwargs: Any
) -> dict[str, Any]:
    """Invoke any producer under the same correlation, error and cache boundary."""

    spec = OPERATIONS[operation]

    def produce() -> dict[str, Any]:
        require(spec.operation)
        validate_text_lengths(spec.arguments(args, kwargs))
        with invocation_policy(resolution=spec.resolution), selection_scope():
            if not spec.deadline:
                return spec.handler(context, **spec.arguments(args, kwargs))
            with deadline_scope(context.settings.call_deadline_seconds):
                return spec.handler(context, **spec.arguments(args, kwargs))

    arguments = dict(zip((p.name for p in spec.parameters), args, strict=False)) | kwargs
    hidden = secrets(context.settings.evs_license_key, context.settings.cadsr_credential)
    with audited(spec.name or operation, arguments, spec.audit, hidden, _correlation_id) as record:
        result = call(operation, produce)
        record.result = result
        return result


def _argument_type(annotation: Any) -> tuple[Any, bool, tuple[Any, ...]]:
    args = get_args(annotation)
    if get_origin(annotation) is Annotated:
        # The parameter's description and constraints are for the served schema only.
        return _argument_type(args[0])
    if type(None) in args:
        return _argument_type(next(arg for arg in args if arg is not type(None)))
    if get_origin(annotation) is list:
        scalar, _multiple, choices = _argument_type(args[0])
        return scalar, True, choices
    if get_origin(annotation) is Literal:
        return type(args[0]), False, tuple(sorted(args))
    return _scalar_type(annotation), False, ()


def _scalar_type(annotation: Any) -> Any:
    """Structured CLI arguments are JSON objects; scalar arguments use their own parser."""
    if get_origin(annotation) is dict or is_typeddict(annotation):
        return json.loads
    return annotation


def _value_options(parameter: Parameter) -> dict[str, Any]:
    scalar, multiple, choices = _argument_type(parameter.annotation)
    if scalar is bool:
        return {"action": "store_false" if parameter.default else "store_true"}
    options: dict[str, Any] = {"type": scalar}
    if choices:
        options["choices"] = choices
    if multiple:
        if parameter.default is Parameter.empty:
            options["nargs"] = "+"
        else:
            options["action"] = "append"
    return options


def _cli_argument(parameter: Parameter) -> tuple[tuple[str, ...], dict[str, Any]]:
    options = _value_options(parameter)
    flag = parameter.name
    if parameter.default is not Parameter.empty:
        spelling = re.sub(r"(?<!^)(?=[A-Z])", "-", parameter.name).lower().replace("_", "-")
        flag = _CLI_FLAGS.get(parameter.name, "--" + spelling)
        options.update(default=parameter.default, dest=parameter.name)
    return (flag,), options


def cli_arguments(spec: ToolSpec) -> list[tuple[tuple[str, ...], dict[str, Any]]]:
    return [_cli_argument(parameter) for parameter in spec.parameters]
