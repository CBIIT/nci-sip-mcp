"""Secured MCP behavior through an operator-supplied local HTTP fixture adapter.

The adapter consumes the synthetic authority file and upstream environment. The suite
does not import a server implementation or select a production identity provider.
"""

import json
import secrets
import shlex
import socket
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http import HTTPStatus
from threading import Barrier

import httpx2
import pytest
from mcp.shared.exceptions import MCPError

from nci_sip_acceptance.client import (
    ADAPTER_AUTHORITY_VARIABLE,
    ADAPTER_PORT_VARIABLE,
    open_remote_session,
    server_environment,
)


class SecuredServer:
    def __init__(self, path, endpoint):
        self.path, self.endpoint = path, endpoint
        self.tokens = {name: secrets.token_urlsafe(24) for name in ("evs", "cadsr")}
        self.policies = {
            "evs": {"capabilities": ["get_concept"], "expires_at": time.time() + 300},
            "cadsr": {"capabilities": ["get_data_element"], "expires_at": time.time() + 300},
        }
        self.save()

    def save(self):
        self.path.write_text(json.dumps({"tokens": self.tokens, "policies": self.policies}))

    def connect(self, actor):
        return open_remote_session(self.endpoint, "Bearer " + self.tokens[actor])

    def request(self, actor, method, **params):
        params["_meta"] = {
            "io.modelcontextprotocol/protocolVersion": "2026-07-28",
            "io.modelcontextprotocol/clientInfo": {"name": "acceptance", "version": "1"},
            "io.modelcontextprotocol/clientCapabilities": {},
        }
        return httpx2.post(
            self.endpoint,
            headers={
                "Authorization": "Bearer " + self.tokens[actor],
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": "2026-07-28",
                "Mcp-Method": method,
                **({"Mcp-Name": params["name"]} if "name" in params else {}),
            },
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        )


@pytest.mark.gate
@pytest.mark.requirement("X-28")
@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer not-a-valid-token"},
        {"X-Forwarded-User": "evs", "X-Forwarded-Tenant": "tenant"},
    ],
    ids=["missing", "invalid", "spoofed"],
)
def test_unauthenticated_and_spoofed_callers_cannot_enter_mcp(secured_server, upstream, headers):
    response = httpx2.post(
        secured_server.endpoint,
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/list",
        },
    )
    assert response.status_code == HTTPStatus.UNAUTHORIZED
    assert response.headers["cache-control"] == "no-store"
    assert "www-authenticate" in response.headers
    assert upstream.log() == []


@pytest.mark.gate
@pytest.mark.requirement("X-28")
def test_public_probes_reveal_only_status_and_reject_an_untrusted_host(secured_server):
    base = secured_server.endpoint.removesuffix("/mcp")
    for path, expected in (("/health", "ok"), ("/ready", "ready")):
        response = httpx2.get(base + path)
        assert response.status_code == HTTPStatus.OK
        assert response.json() == {"status": expected}
        assert response.headers["cache-control"] == "no-store"
        refused = httpx2.get(base + path, headers={"Host": "untrusted.example"})
        assert refused.status_code == HTTPStatus.MISDIRECTED_REQUEST


def _port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def _wait_ready(process, endpoint):
    deadline = time.monotonic() + 15
    while process.poll() is None and time.monotonic() < deadline:
        try:
            if httpx2.get(endpoint + "/health", timeout=1).status_code == HTTPStatus.OK:
                return
        except httpx2.ConnectError:
            pass  # The owned process has not bound its socket yet; deadline below is fatal.
        time.sleep(0.05)
    raise RuntimeError("Secured fixture adapter did not become ready")


@contextmanager
def _process(command, environment, log):
    with log.open("w") as output:
        process = subprocess.Popen(  # noqa: S603 - operator-supplied fixture adapter
            shlex.split(command), env=environment, stdout=output, stderr=output
        )
        try:
            yield process
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


@pytest.fixture
def secured_server(target, upstream, tmp_path):
    command = target.security_server
    if not command:
        pytest.skip(
            "No secured fixture adapter supplied; this is not evidence of secured conformance"
        )
    if upstream is None:
        pytest.skip(
            "Synthetic authority is fixture-only; production identity is separately validated"
        )
    port = _port()
    endpoint = f"http://127.0.0.1:{port}"
    server = SecuredServer(tmp_path / "authority.json", endpoint + "/mcp")
    environment = server_environment("fixture", tmp_path / "data", upstream.url) | {
        ADAPTER_AUTHORITY_VARIABLE: str(server.path),
        ADAPTER_PORT_VARIABLE: str(port),
    }
    server.log = tmp_path / "server.log"
    try:
        with _process(command, environment, server.log) as process:
            _wait_ready(process, endpoint)
            yield server
    finally:
        server.path.unlink(missing_ok=True)
        server.log.unlink(missing_ok=True)


@pytest.mark.gate
@pytest.mark.requirement("X-25")
@pytest.mark.parametrize("actor,tool", [("evs", "get_concept"), ("cadsr", "get_data_element")])
def test_each_caller_sees_only_its_permitted_tools_and_resources(secured_server, actor, tool):
    with secured_server.connect(actor) as client:
        tools = client.list_tools()
        assert [row.name for row in tools.tools] == [tool]
        assert (tools.ttl_ms, tools.cache_scope) == (0, "private")
        assert client.list_prompts().prompts == []
        assert client.list_resources().resources == []
        templates = client.list_resource_templates().resource_templates
        expected = (
            ["ncit://concept/{release}/{code}"]
            if actor == "evs"
            else ["cadsr://data-element/{publicId}", "cadsr://data-element/{publicId}/{version}"]
        )
        assert sorted(str(row.uri_template) for row in templates) == sorted(expected)


@pytest.mark.gate
@pytest.mark.requirement("X-25", "X-27")
def test_a_guessed_tool_is_refused_without_upstream_reads_or_sensitive_logs(
    secured_server, upstream
):
    with secured_server.connect("cadsr") as client:
        result = client.call_tool("get_concept", {"code": "caller-sensitive-value"})
    assert result.is_error
    assert result.structured_content["error"]["code"] == "permission_denied"
    assert upstream.log() == []
    assert "caller-sensitive-value" not in str(result)
    logged = secured_server.log.read_text()
    assert all(token not in logged for token in secured_server.tokens.values())
    assert "caller-sensitive-value" not in logged


@pytest.mark.gate
@pytest.mark.requirement("X-25")
def test_guessed_resources_and_prompts_cannot_bypass_permissions(secured_server, upstream):
    with secured_server.connect("cadsr") as client:
        with pytest.raises(MCPError) as resource:
            client.read_resource("ncit://concept/26.09d/C3262")
        assert resource.value.data["error"]["code"] == "permission_denied"
        with pytest.raises(MCPError) as prompt:
            client.get_prompt("protocol_authoring", {"concepts": "C3262"})
        assert prompt.value.data["error"]["code"] == "permission_denied"
    assert upstream.log() == []


@pytest.mark.gate
@pytest.mark.requirement("X-27")
def test_explicit_release_content_and_discovery_disable_shared_http_caching(secured_server, pinned):
    for method, arguments in (
        ("tools/call", {"name": "get_concept", "arguments": pinned | {"code": "C3262"}}),
        ("server/discover", {}),
    ):
        response = secured_server.request("evs", method, **arguments)
        assert response.status_code == HTTPStatus.OK, response.text
        assert response.headers["cache-control"] == "no-store"
        result = response.json()["result"]
        hints = result["_meta"] if method == "tools/call" else result
        assert (hints["ttlMs"], hints["cacheScope"]) == (0, "private")


@pytest.mark.gate
@pytest.mark.requirement("X-27")
def test_revoked_and_expired_policy_deny_the_next_request(secured_server, pinned, upstream):
    with secured_server.connect("evs") as client:
        assert not client.call_tool("get_concept", pinned | {"code": "C3262"}).is_error
        before = list(upstream.log())
        for policy in (
            {"capabilities": [], "expires_at": time.time() + 300},
            {"capabilities": ["get_concept"], "expires_at": 0},
        ):
            secured_server.policies["evs"] = policy
            secured_server.save()
            denied = client.call_tool("get_concept", pinned | {"code": "C3262"})
            assert denied.is_error
            assert denied.structured_content["error"]["code"] == "permission_denied"
            assert upstream.log() == before


@pytest.mark.gate
@pytest.mark.requirement("X-26")
@pytest.mark.parametrize(
    "parent,arguments",
    [
        ("ground_value", {"conceptCode": "C3262"}),
        ("expand_cohort", {"conceptCode": "C3262"}),
        ("harmonize_data_dictionary", {"columns": [{"name": "stage", "sampleValues": ["II"]}]}),
    ],
)
def test_allowed_workflows_cannot_read_denied_children(secured_server, upstream, parent, arguments):
    secured_server.policies["evs"]["capabilities"] = [parent]
    secured_server.save()
    with secured_server.connect("evs") as client:
        denied = client.call_tool(parent, arguments)
    assert denied.is_error
    assert denied.structured_content["error"]["code"] == "permission_denied"
    assert upstream.log() == []


@pytest.mark.gate
@pytest.mark.requirement("X-27")
def test_concurrent_verified_callers_keep_separate_catalogues_and_results(secured_server, pinned):
    together = Barrier(2)

    def actor_call(actor):
        with secured_server.connect(actor) as client:
            tools = [row.name for row in client.list_tools().tools]
            together.wait(timeout=10)
            concept = client.call_tool("get_concept", pinned | {"code": "C3262"})
            element = client.call_tool("get_data_element", {"publicId": "2200604"})
            return tools, concept, element

    with ThreadPoolExecutor(max_workers=2) as pool:
        evs, cadsr = list(pool.map(actor_call, ["evs", "cadsr"]))
    assert evs[0] == ["get_concept"]
    assert evs[1].structured_content["code"] == "C3262"
    assert evs[2].structured_content["error"]["code"] == "permission_denied"
    assert cadsr[0] == ["get_data_element"]
    assert cadsr[1].structured_content["error"]["code"] == "permission_denied"
    assert cadsr[2].structured_content["publicId"] == "2200604"


@pytest.mark.gate
@pytest.mark.requirement("X-26")
@pytest.mark.parametrize(
    "missing",
    [
        "get_concept",
        "find_data_elements_for_concept",
        "search_concepts",
        "resolve_stored_value",
        "get_code_map",
    ],
)
def test_grounding_checks_each_selected_child_before_any_read(secured_server, upstream, missing):
    required = {
        "ground_value",
        "get_concept",
        "find_data_elements_for_concept",
        "search_concepts",
        "resolve_stored_value",
        "get_code_map",
    }
    secured_server.policies["evs"]["capabilities"] = sorted(required - {missing})
    secured_server.save()
    with secured_server.connect("evs") as client:
        result = client.call_tool("ground_value", {"text": "Neoplasm", "commons": "CRDC"})
    assert result.is_error
    assert result.structured_content["error"]["code"] == "permission_denied"
    assert upstream.log() == []


@pytest.mark.gate
@pytest.mark.requirement("X-27")
def test_missing_policy_never_falls_back_to_trusted_local_access(secured_server, pinned, upstream):
    del secured_server.policies["evs"]
    secured_server.save()
    response = secured_server.request(
        "evs", "tools/call", name="get_concept", arguments=pinned | {"code": "C3262"}
    )
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["result"]["structuredContent"]["error"]["code"] == "permission_denied"
    assert upstream.log() == []
