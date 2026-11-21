"""A required tool is called by its name, or reported NOT IMPLEMENTED."""

from types import SimpleNamespace

import pytest
from mcp.shared.exceptions import MCPError
from mcp_types import METHOD_NOT_FOUND

from nci_sip_acceptance.spec import TOOLS, Listing
from nci_sip_acceptance.tools import NOT_IMPLEMENTED, Process, Tools


def test_process_evidence_includes_nested_binary_files_and_stderr(tmp_path):
    log = tmp_path / "stderr.log"
    data = tmp_path / "data"
    nested = data / "build" / "vectors"
    nested.mkdir(parents=True)
    log.write_text("stderr evidence\n", encoding="utf-8")
    (data / "manifest.json").write_text("manifest evidence", encoding="utf-8")
    (nested / "fields.bin").write_bytes(b"\xffstored evidence\xfe")

    written = Process(log, data, ()).written()

    assert written.startswith("stderr evidence\n")
    assert "manifest evidence" in written
    assert "\ufffdstored evidence\ufffd" in written


class Session:
    """A server session that has `names` and answers with `result`, recording each call."""

    def __init__(self, names, result):
        self.names, self.result, self.calls = names, result, []

    def list_tools(self):
        return SimpleNamespace(tools=[SimpleNamespace(name=name) for name in self.names])

    def call_tool(self, name, arguments, meta=None):
        self.calls.append((name, arguments, meta))
        return self.result


def answer(*blocks, structured=None, is_error=False, meta=None):
    return SimpleNamespace(
        content=list(blocks), structured_content=structured, is_error=is_error, meta=meta
    )


def text(value):
    return SimpleNamespace(type="text", text=value)


def test_a_required_tool_the_server_has_is_called_by_its_own_name():
    session = Session(
        ["get_concept_neighborhood", "ncit_traverse"], answer(structured={"nodes": []})
    )

    result = Tools(session).call("get_concept_neighborhood", {"code": "C3262"})

    assert session.calls == [("get_concept_neighborhood", {"code": "C3262"}, None)]
    assert (result.tool, result.is_error, result.content) == (
        "get_concept_neighborhood",
        False,
        {"nodes": []},
    )


def test_the_call_meta_reaches_the_server_and_the_result_meta_comes_back():
    session = Session(["get_concept"], answer(structured={}, meta={"ttlMs": 0}))

    result = Tools(session).call("get_concept", {"code": "C3262"}, {"correlationId": "c-1"})

    assert session.calls == [("get_concept", {"code": "C3262"}, {"correlationId": "c-1"})]
    assert result.meta == {"ttlMs": 0}


def test_a_json_text_error_keeps_its_content_and_error_flag():
    session = Session(["get_concept_neighborhood"], answer(text('{"nodes": []}'), is_error=True))
    tools = Tools(session)

    result = tools.call("get_concept_neighborhood", {"code": "C3262", "depth": 1})

    assert session.calls == [("get_concept_neighborhood", {"code": "C3262", "depth": 1}, None)]
    assert (result.tool, result.is_error, result.content) == (
        "get_concept_neighborhood",
        True,
        {"nodes": []},
    )
    assert tools.implemented("get_concept_neighborhood") is True


def test_a_text_answer_that_is_not_json_is_kept_as_text():
    message = "Error executing tool get_concept: 1 validation error"
    session = Session(["get_concept"], answer(text(message), is_error=True))

    result = Tools(session).call("get_concept", {"code": "C3262"})

    assert (result.is_error, result.content) == (True, message)


def test_a_result_without_content_has_none():
    session = Session(["list_terminologies"], answer(SimpleNamespace(type="image")))

    assert Tools(session).call("list_terminologies").content is None


@pytest.mark.parametrize("names", [[], ["ncit_traverse"]])
def test_an_absent_required_name_is_not_implemented(names):
    tools = Tools(Session(names, None))

    with pytest.raises(pytest.skip.Exception, match=f"{NOT_IMPLEMENTED}: get_concept_neighborhood"):
        tools.call("get_concept_neighborhood", {"code": "C3262"})
    assert tools.implemented("get_concept_neighborhood") is False


class Counted(Session):
    """A session whose every call makes `made` upstream requests, as the harness counts them."""

    def __init__(self, names, result, made):
        super().__init__(names, result)
        self.made, self.requests = made, 0

    def call_tool(self, name, arguments, meta=None):
        self.requests += self.made
        return super().call_tool(name, arguments, meta)


def test_a_call_over_its_tool_s_request_bound_fails_the_test_and_one_within_passes():
    bound = TOOLS["get_concept_neighborhood"]["requests"]
    over = Counted(["get_concept_neighborhood"], answer(structured={"nodes": []}), bound + 1)
    within = Counted(["get_concept_neighborhood"], answer(structured={"nodes": []}), bound)

    with pytest.raises(pytest.fail.Exception, match=f"{bound + 1} upstream requests"):
        Tools(over, requests=lambda: over.requests).call("get_concept_neighborhood")
    result = Tools(within, requests=lambda: within.requests).call("get_concept_neighborhood")

    assert result.content == {"nodes": []}


class Surface(Session):
    """A session that answers the prompt and resource methods, or refuses them."""

    def __init__(self, refusal=False):
        super().__init__(["get_concept"], None)
        self.refusal = refusal

    def _answered(self, result):
        if self.refusal:
            raise MCPError(METHOD_NOT_FOUND, "Method not found")
        return result

    def list_prompts(self):
        return self._answered(SimpleNamespace(prompts=["a prompt"]))

    def get_prompt(self, name, arguments):
        return self._answered(SimpleNamespace(messages=[name, arguments]))

    def list_resources(self):
        return self._answered(SimpleNamespace(resources=[SimpleNamespace(uri="cadsr://a/b")]))

    def list_resource_templates(self):
        template = SimpleNamespace(uri_template="ncit://c/{x}")
        return self._answered(SimpleNamespace(resource_templates=[template]))

    def read_resource(self, uri):
        return self._answered(READS[uri])


def read_result(*texts, mime="application/json", fields=("ttl_ms", "cache_scope")):
    contents = [SimpleNamespace(text=each, mime_type=mime) for each in texts]
    return SimpleNamespace(
        contents=contents, ttl_ms=5, cache_scope="public", model_fields_set=set(fields)
    )


READS = {
    "json": read_result('{"code": "C4817"}'),
    "prose": read_result("not json", mime="text/plain"),
    "bare": read_result(fields=()),
    "parameters": read_result('{"a": 1}', mime="Application/JSON; charset=utf-8"),
    "typeless": read_result("{}", mime=None),
}


def test_a_resource_read_gives_its_json_its_mime_type_and_the_hint_it_carries():
    tools = Tools(Surface())

    read = tools.read_resource("json")

    assert (read.content, read.mime_types) == ({"code": "C4817"}, ("application/json",))
    assert (read.ttl_ms, read.cache_scope, read.carried) == (5, "public", True)
    # Content that is no JSON is its text; no content is none; a hint left out is not carried.
    assert tools.read_resource("prose").content == "not json"
    assert tools.read_resource("bare").content is None
    assert not tools.read_resource("bare").carried


def test_a_mime_type_is_read_on_its_base_without_parameters_or_case():
    tools = Tools(Surface())

    assert tools.read_resource("parameters").mime_types == ("application/json",)
    assert tools.read_resource("typeless").mime_types == ("",)


def test_the_concrete_resources_and_the_templates_are_listed_each_by_its_own_method():
    listed = Tools(Surface()).listed_resources()

    assert listed == Listing({"cadsr://a/b"}, {"ncit://c/{x}"})
    assert (listed.uris, listed.templates) == ({"cadsr://a/b"}, {"ncit://c/{x}"})


def test_the_refusal_of_a_read_is_what_the_server_said_and_content_is_no_refusal():
    refusing, giving = Tools(Surface(refusal=True)), Tools(Surface())

    assert refusing.resource_refusal("json") == "Method not found"
    assert giving.resource_refusal("json") is None


def test_the_prompts_of_a_server_are_listed_and_got_by_name_with_their_arguments():
    tools = Tools(Surface())

    assert tools.list_prompts().prompts == ["a prompt"]
    assert tools.get_prompt("p", {"a": "b"}).messages == ["p", {"a": "b"}]


@pytest.mark.parametrize(
    ("method", "call"),
    [
        ("prompts/list", lambda tools: tools.list_prompts()),
        ("prompts/get", lambda tools: tools.get_prompt("p", {})),
        ("resources/list", lambda tools: tools.listed_resources()),
        ("resources/templates/list", lambda tools: tools.list_resource_templates()),
        ("resources/read", lambda tools: tools.read_resource("json")),
    ],
)
def test_a_server_that_refuses_a_prompt_or_resource_method_fails_the_test_naming_it(method, call):
    tools = Tools(Surface(refusal=True))

    with pytest.raises(pytest.fail.Exception, match=f"{method} was refused: Method not found"):
        call(tools)
