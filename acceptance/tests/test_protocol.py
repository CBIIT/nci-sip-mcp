"""Protocol gates (the P requirements of spec/requirements.yaml), run once per server; a gate
that fails fails every tool."""

import re

import pytest
from jsonschema import Draft202012Validator

from nci_sip_acceptance.client import Target
from nci_sip_acceptance.results import (
    CARRIED,
    UNPINNED_REGISTRY,
    hint_fits,
    identity,
    is_count,
    is_iso8601,
    is_name,
    is_timestamp,
    provenance_of,
    release_of,
    wrong_fields,
)
from nci_sip_acceptance.spec import (
    PROMPTS,
    RECORDS,
    RESOURCES,
    TOOLS,
    items_of,
    parameters,
    profile_tools,
    prompts_of,
    resource_call,
    resources_listed,
    resources_of,
    tools_named,
    uri_variables,
)
from nci_sip_acceptance.tools import Read

# M7.1: where a caller's correlation identifier goes, and the header that carries it upstream.
CORRELATION = "acceptance-correlation-0001"
CORRELATION_HEADER = "x-correlation-id"
# Text that marks a description as unfinished (A2.4).
UNFINISHED = re.compile(
    r"\b(TODO|FIXME|XXX|TBD|HACK|lorem ipsum|placeholder|debug)\b|\{\{|\}\}", re.IGNORECASE
)
# A title that is the generator's own type expression (RootModel[Annotated[Union[...]]]).
GENERATED_TITLE = re.compile(r"[\[\]]|\bFieldInfo\b")
VERBS = {name.partition("_")[0] for name in TOOLS}
NAME = re.compile(r"[a-z]+(_[a-z]+)*")
READ_ONLY = {
    "read_only_hint": True,
    "destructive_hint": False,
    "idempotent_hint": True,
    "open_world_hint": True,
}


FIELDS = RECORDS["error"]["fields"]
CODES = FIELDS["code"]["values"]
# The error record with its required fields, each holding text.
RECORD = {name: "text" for name, field in FIELDS.items() if not field.get("optional")}
# Results holding an error record, one for each code and one with details; and two that hold
# none: a code outside the closed set, and no code.
ERRORS = [{"error": RECORD | {"code": code}} for code in CODES]
ERRORS.append({"error": RECORD | {"code": CODES[0], "details": {}}})
MALFORMED = [
    {"error": RECORD | {"code": "no_such_code"}},
    {"error": {name: value for name, value in RECORD.items() if name != "code"}},
]


def _admitting(validators: dict, results: list[dict], admits=all) -> list[str]:
    """The tools whose schema admits `results`, all of them or, with `any`, one of them."""

    return [name for name, check in validators.items() if admits(map(check.is_valid, results))]


def _under(schema: object, keys: tuple[str, ...]) -> list:
    """The values a schema holds under any of `keys`, at any depth."""

    if isinstance(schema, list):
        return [found for value in schema for found in _under(value, keys)]
    if not isinstance(schema, dict):
        return []
    own = [schema[key] for key in keys if key in schema]
    return own + _under(list(schema.values()), keys)


def _descriptions(schema: object) -> list[str]:
    """Every description a schema holds, at any depth."""

    return [text for text in _under(schema, ("description",)) if isinstance(text, str)]


def _titles(schema: object) -> list[str]:
    """Every title a schema holds, at any depth (a property named title is not one)."""

    return [text for text in _under(schema, ("title",)) if isinstance(text, str)]


def _content_calls(server, profile: str, pinned: dict) -> list[tuple[str, dict]]:
    """A content call of each group a server of `profile` serves and implements: a concept of
    the pinned release from EVS, a data element from caDSR, the unified profile making both.
    A server that implements neither gets the first, which the suite reports NOT IMPLEMENTED."""

    calls = [
        call
        for group, call in {
            "evs": ("get_concept", {**pinned, "code": "C4817"}),
            "cadsr": ("get_data_element", {"publicId": "2200604"}),
        }.items()
        if profile in (group, "unified")
    ]
    return [call for call in calls if server.implemented(call[0])] or calls[:1]


def _header(entry: dict, name: str) -> str | None:
    return {key.lower(): value for key, value in entry["headers"].items()}.get(name)


def _surface(tool: str) -> str:
    """The upstream surface a gate's content call reaches."""

    return "cadsr" if tool == "get_data_element" else "evs"


def _listing(result) -> list[dict]:
    return [tool.model_dump(by_alias=True) for tool in result.tools]


@pytest.mark.gate
@pytest.mark.live_capable
@pytest.mark.requirement("P-1")
def test_tools_list_names_the_tools_of_the_profile_and_no_other(server, target):
    assert set(server.available) == profile_tools(target.profile)


@pytest.mark.gate
@pytest.mark.live_capable
@pytest.mark.requirement("P-2")
def test_every_output_schema_admits_the_error_record_and_refuses_a_malformed_one(server):
    schemas = {name: tool.output_schema for name, tool in server.available.items()}

    assert [name for name, schema in schemas.items() if schema is None] == []
    for schema in schemas.values():
        Draft202012Validator.check_schema(schema)
    validators = {name: Draft202012Validator(schema) for name, schema in schemas.items()}
    assert _admitting(validators, ERRORS) == list(validators)
    # A schema that admits anything declares no shape at all (M3.1).
    assert _admitting(validators, MALFORMED, any) == []


@pytest.mark.gate
@pytest.mark.live_capable
@pytest.mark.requirement("P-3")
def test_no_description_holds_placeholder_or_debug_text(server):
    unfinished = {
        name: found for name, tool in server.available.items() if (found := _unfinished(tool))
    }

    assert unfinished == {}


def _unfinished(tool) -> list[str]:
    """The unfinished words in a tool's descriptions and titles, and the titles that are the
    generator's own type expression."""

    schemas = [tool.input_schema, tool.output_schema]
    titles = _titles(schemas)
    texts = [tool.description or "", *_descriptions(schemas), *titles]
    return [m[0] for text in texts if (m := UNFINISHED.search(text))] + [
        title for title in titles if GENERATED_TITLE.search(title)
    ]


def _not_offered(name):
    """What spec/tools.yaml says the platform has and the tool `name` does not expose."""

    return {value for values in TOOLS[name].get("not_offered", {}).values() for value in values}


def _leaves(value) -> list:
    """The scalars a value holds, inside lists and objects too."""

    if isinstance(value, list):
        return [leaf for item in value for leaf in _leaves(item)]
    if isinstance(value, dict):
        return _leaves(list(value.values()))
    return [value]


# Where a schema offers values: those it lists, fixes, defaults to or gives as examples.
OFFERING = ("enum", "const", "default", "examples", "example")
# Spans of a text that show what they hold as a value: backticks, double and typographic quotes.
QUOTED = re.compile('`[^`]*`|"[^"]*"|\u201c[^\u201d]*\u201d|\u2018[^\u2019]*\u2019')


def _shown_as_value(value, texts, patterns):
    """Whether a text names `value` as a value: as a word inside a quoted span or a schema
    pattern, between single quotes, or assigned (retired=exclude); a sentence that says,
    unquoted, that the value is not offered does not."""

    word = re.compile(rf"\b{re.escape(value)}\b")
    assigned = re.compile(rf"'{re.escape(value)}'|=\s*{re.escape(value)}\b")
    spans = [*patterns, *(span for text in texts for span in QUOTED.findall(text))]
    return any(word.search(span) for span in spans) or any(assigned.search(t) for t in texts)


def _shown(name, tool):
    """The values `name` does not offer that its implementation `tool` shows as offered."""

    texts = [tool.description or "", *_descriptions([tool.input_schema, tool.output_schema])]
    values = _leaves(_under(tool.input_schema, OFFERING))
    patterns = [text for text in _under(tool.input_schema, ("pattern",)) if isinstance(text, str)]
    return [
        (name, value)
        for value in sorted(_not_offered(name))
        if value in values or _shown_as_value(value, texts, patterns)
    ]


@pytest.mark.gate
@pytest.mark.live_capable
@pytest.mark.requirement("P-11")
def test_no_description_or_schema_shows_what_the_tool_does_not_offer(server):
    implemented = {name: server.available.get(name) for name in TOOLS if _not_offered(name)}

    shown = [found for name, tool in implemented.items() if tool for found in _shown(name, tool)]

    assert shown == []


@pytest.mark.gate
@pytest.mark.live_capable
@pytest.mark.requirement("P-4")
def test_tool_names_are_verb_led_lowercase_and_underscore_separated(server):
    misnamed = [
        name
        for name in server.available
        if not NAME.fullmatch(name) or name.partition("_")[0] not in VERBS
    ]

    assert misnamed == []


# The list methods and the call that lists each (M2.1), by the name the case carries.
LISTS = {
    "tools": lambda server: server.listing,
    "prompts": lambda server: server.list_prompts(),
    "resources": lambda server: server.list_resources(),
    "templates": lambda server: server.list_resource_templates(),
}


@pytest.mark.gate
@pytest.mark.live_capable
@pytest.mark.requirement("P-5")
@pytest.mark.parametrize("method", LISTS)
def test_each_list_may_be_cached_and_shared(server, method):
    listed = LISTS[method](server)

    assert listed.ttl_ms > 0
    assert listed.cache_scope == "public"


@pytest.mark.gate
@pytest.mark.requirement("P-6")
@pytest.mark.live_capable
def test_tools_list_is_the_same_after_a_content_call(server, target, pinned):
    before = _listing(server.listing)

    called = [server.call(*call) for call in _content_calls(server, target.profile, pinned)]

    assert [result.content for result in called if result.is_error] == []
    assert _listing(server.list_again()) == before


@pytest.mark.gate
@pytest.mark.scenario("upstream/unavailable")
@pytest.mark.requirement("P-6")
def test_tools_list_is_the_same_while_the_platform_is_unavailable(server, target, tools, pinned):
    # A server may notice the outage only when a call fails, so one is made first.
    for call in _content_calls(tools, target.profile, pinned):
        tools.call(*call)

    assert _listing(tools.list_again()) == _listing(server.listing)


@pytest.mark.gate
# A server that answered the same call before may serve it from its cache, asking nothing.
@pytest.mark.own_server
@pytest.mark.requirement("P-7")
def test_a_correlation_identifier_goes_upstream_and_comes_back(tools, target, upstream, pinned):
    meta = {"correlationId": CORRELATION}

    calls = _content_calls(tools, target.profile, pinned)

    results = [tools.call(*call, meta) for call in calls]

    sent = {(entry["surface"], _header(entry, CORRELATION_HEADER)) for entry in upstream.log()}
    # Each group's requests carry it: the unified profile's caDSR requests as much as its EVS.
    assert {surface for surface, _ in sent} >= {_surface(name) for name, _ in calls}
    assert {value for _, value in sent} == {CORRELATION}
    assert [result.content for result in results if result.is_error] == []
    assert {result.content["provenance"]["correlationId"] for result in results} == {CORRELATION}


@pytest.mark.gate
@pytest.mark.live_capable
@pytest.mark.requirement("P-10")
def test_every_tool_is_annotated_read_only_idempotent_and_open_world(server):
    def hints(tool):
        annotations = tool.annotations
        return annotations and {name: getattr(annotations, name) for name in READ_ONLY}

    assert [name for name, tool in server.available.items() if hints(tool) != READ_ONLY] == []


@pytest.mark.gate
@pytest.mark.live_capable
@pytest.mark.requirement("P-12")
def test_each_tool_takes_the_parameters_the_specification_names(server):
    def declared(tool):
        schema = tool.input_schema
        return set(schema.get("properties", {})), set(schema.get("required", []))

    specified = {name: tool for name, tool in server.available.items() if name in TOOLS}
    if not specified:
        pytest.skip("the server lists no tool of the specification")
    differing = {
        name: declared(tool)
        for name, tool in specified.items()
        if declared(tool) != parameters(name)
    }

    assert differing == {}


def _arguments(declared: list[dict]) -> set[tuple[str, bool]]:
    """The arguments a prompt declares, each as its name and whether it is required."""

    return {(each["name"], each["required"]) for each in declared}


@pytest.mark.gate
@pytest.mark.live_capable
@pytest.mark.requirement("P-8")
def test_prompts_list_names_the_prompts_of_the_profile_with_their_arguments(server, target):
    listed = {
        prompt.name: {(each.name, bool(each.required)) for each in prompt.arguments or []}
        for prompt in server.list_prompts().prompts
    }

    assert listed == {
        name: _arguments(prompt["arguments"]) for name, prompt in prompts_of(target.profile).items()
    }


def _messages(server, name: str, required: list[str]) -> str:
    """The text of the messages prompts/get returns for `name`, each required argument given a
    sample value; a prompt that returns none gives an empty text."""

    messages = server.get_prompt(name, {each: f"sample {each}" for each in required}).messages
    return " ".join(message.content.text for message in messages if message.content.type == "text")


def _listed_required(server) -> dict[str, list[str]]:
    """The arguments each prompt the server lists requires."""

    return {
        prompt.name: [each.name for each in prompt.arguments or [] if each.required]
        for prompt in server.list_prompts().prompts
    }


def _stated_required(profile: str) -> dict[str, list[str]]:
    """The arguments each prompt the profile should list requires, as the specification states."""

    return {
        name: [each["name"] for each in prompt["arguments"] if each["required"]]
        for name, prompt in prompts_of(profile).items()
    }


@pytest.mark.gate
@pytest.mark.live_capable
@pytest.mark.requirement("P-8")
def test_a_prompt_returns_messages_naming_the_tools_it_states_and_only_tools_of_the_profile(
    server, target
):
    # A prompt that is only stated is asked for all the same, and a server that lacks it refuses.
    asked = _listed_required(server) | _stated_required(target.profile)

    texts = {name: _messages(server, name, required) for name, required in asked.items()}

    assert [name for name, text in texts.items() if not text] == []
    named = {name: tools_named(text) for name, text in texts.items()}
    available = profile_tools(target.profile)
    assert {
        name: [tool for tool in found if tool not in available] for name, found in named.items()
    } == {name: [] for name in texts}
    # The tools it names, in order of first mention, are the stated ones in their stated order.
    # A prompt the specification does not furnish is the listing's finding, not this test's.
    furnished = {name: found for name, found in named.items() if name in PROMPTS}
    assert furnished == {name: PROMPTS[name]["tools"] for name in furnished}
    assert {name: texts[name] for name in furnished} == {
        name: PROMPTS[name]["template"].format(
            **{
                a["name"]: f"sample {a['name']}" if a["required"] else ""
                for a in PROMPTS[name]["arguments"]
            }
        )
        for name in furnished
    }


@pytest.mark.gate
@pytest.mark.live_capable
@pytest.mark.requirement("P-8")
def test_resources_list_the_concrete_resources_and_templates_list_the_templates_of_the_profile(
    server, target
):
    assert server.listed_resources() == resources_listed(target.profile)


# URIs that no listed resource or template matches: a concept read without its release, which
# is mandatory (M5.1), and a scheme nobody furnishes.
UNMATCHED = ["ncit://concept/C4817", "ncit://nothing/x"]


@pytest.mark.gate
@pytest.mark.live_capable
@pytest.mark.requirement("P-9")
@pytest.mark.parametrize("uri", UNMATCHED)
def test_a_uri_no_resource_or_template_matches_is_an_error_and_never_content(server, uri):
    assert server.resource_refusal(uri) is not None


# What the recorded fixtures hold of each resource: a concept, a data element and its version 1.
CONCEPT = "C4817"
DATA_ELEMENT = "2200604"
OLDER_VERSION = "1"


def _instance(template: str, pinned: dict[str, str]) -> dict[str, str]:
    """The values that fill `template` with something the fixture set holds."""

    release = pinned["release"]
    return {
        "ncit://concept/{release}/{code}": {"release": release, "code": CONCEPT},
        "ncit://release/{version}": {"version": release},
        "ncit://index/manifest/{release}": {"release": release},
        "cadsr://data-element/{publicId}": {"publicId": DATA_ELEMENT},
        "cadsr://data-element/{publicId}/{version}": {
            "publicId": DATA_ELEMENT,
            "version": OLDER_VERSION,
        },
    }.get(template, {})


def _case(key: str, template: str) -> object:
    """One read of the profile under test: a resource's template, the case named by the
    variables it adds where the resource has several; a resource served from the index the
    prepare step builds needs that step."""

    several = len(RESOURCES[key]["uri"]) > 1
    name = "-".join([key, *uri_variables(template)]) if several else key
    marks = [pytest.mark.prepared] if RESOURCES[key].get("prepared") else []
    if not marks:
        marks.append(pytest.mark.live_capable)
    return pytest.param(key, template, id=name, marks=marks)


SERVED = resources_of(Target.from_env().profile)
READS = [_case(key, template) for key, resource in SERVED.items() for template in resource["uri"]]


def _items(tool: str, content: object) -> list:
    """The items a result of `tool` holds, or the result itself where the tool has none."""

    return items_of(tool, content) or [content]


def _read(server, pinned, template: str) -> Read:
    """What the server gives for `template` filled in from the fixture set."""

    return server.read_resource(template.format_map(_instance(template, pinned)))


def _released(items: list) -> set[tuple]:
    """The releases the items name, those that name none left out."""

    return {release_of(item) for item in items} - {(None, None)}


@pytest.mark.gate
@pytest.mark.requirement("P-9")
@pytest.mark.parametrize(("key", "template"), READS)
def test_a_resource_read_is_json_in_its_mime_type_and_matches_its_tool_s_answer(
    server, pinned, key, template
):
    values = _instance(template, pinned)
    tool, arguments = resource_call(key, template, values)

    read = _read(server, pinned, template)
    answer = server.call(tool, arguments)

    assert not answer.is_error, answer.content
    assert read.mime_types == (RESOURCES[key]["mime"],)
    assert isinstance(read.content, dict), read.content
    items, expected = _items(tool, read.content), _items(tool, answer.content)
    # Compared on identity and release, not section by section. A tool whose answer names no
    # release (the registry state's) leaves none to compare; the manifest, which is not the
    # tool's answer, is compared on identity alone (spec/resources.yaml).
    assert {identity(item) for item in items} == {identity(item) for item in expected}
    released = _released(expected)
    if RESOURCES[key].get("compared") != "identity" and released:
        assert _released(items) == released


@pytest.mark.gate
@pytest.mark.requirement("P-9")
@pytest.mark.parametrize(("key", "template"), READS)
def test_a_resource_read_carries_a_provenance_record(server, pinned, key, template):
    read = _read(server, pinned, template)

    assert isinstance(read.content, dict), read.content
    provenances = [provenance_of(item) for item in _items(RESOURCES[key]["tool"], read.content)]
    assert [wrong_fields(each, CARRIED) for each in provenances] == [[]] * len(provenances)
    assert all(is_timestamp(each.get("retrievedAt")) for each in provenances)
    # What the specification states beyond X-7 of this resource's provenance, and for a caDSR
    # resource the registry form of release (X-21): caDSR publishes no registry release.
    stated = RESOURCES[key].get("provenance", {})
    assert [{name: each.get(name) for name in stated} for each in provenances] == [stated] * len(
        provenances
    )
    if RESOURCES[key]["group"] == "cadsr":
        assert [each.get("release") for each in provenances] == [UNPINNED_REGISTRY] * len(
            provenances
        )


@pytest.mark.gate
@pytest.mark.requirement("P-9")
@pytest.mark.parametrize(("key", "template"), READS)
def test_a_resource_read_carries_the_caching_hint_of_what_it_holds(server, pinned, key, template):
    read = _read(server, pinned, template)

    # M2.5: the hint is a field of the result. Its class is M2.2's, the tool's own hint aside.
    assert read.carried
    held = RESOURCES[key]["holds"] == "release-pinned"
    assert hint_fits(read.ttl_ms, read.cache_scope, pinned=held), (read.ttl_ms, read.cache_scope)


MANIFESTS = [key for key, resource in SERVED.items() if resource["returns"] == "index_manifest"]


@pytest.mark.gate
@pytest.mark.prepared
@pytest.mark.requirement("P-9")
@pytest.mark.parametrize("key", MANIFESTS)
def test_the_index_manifest_states_what_the_index_holds(server, pinned, key):
    read = _read(server, pinned, RESOURCES[key]["uri"][0])

    manifest = read.content
    assert isinstance(manifest, dict), manifest
    # Which provider and model built the index is the operator's choice, never asserted.
    embedding = manifest.get("embedding")
    embedding = embedding if isinstance(embedding, dict) else {}
    stated = RESOURCES[key]["provenance"]
    provenance = provenance_of(manifest)
    held = {
        "concepts": is_count(manifest.get("concepts")),
        "embedding.provider": is_name(embedding.get("provider")),
        "embedding.model": is_name(embedding.get("model")),
        "embedding.dimensions": is_count(embedding.get("dimensions")),
        "builtAt": is_iso8601(manifest.get("builtAt")),
        "provenance": {name: provenance.get(name) for name in stated} == stated,
    }
    assert [name for name, fine in held.items() if not fine] == []
