import asyncio
import json
import logging
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import replace
from threading import Event
from unittest.mock import patch

import anyio
import httpx2
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings

from fakes import release
from nci_sip_mcp.audit import JsonFormatter
from nci_sip_mcp.evs import EVSReleaseNotFoundError
from nci_sip_mcp.registry import invoke
from nci_sip_mcp.transport import create_http_app
from ncit_index.embeddings import HashingEmbeddingProvider
from test_release_selection import initialize_http, version
from test_server import ServerFixture


@asynccontextmanager
async def http_app(settings, context, **auth):
    app = create_http_app(settings, context=context, **auth)
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app), base_url="http://127.0.0.1:8000"
        ) as client,
    ):
        yield client


async def concept_response(client, headers, **arguments):
    return await client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "get_concept",
                "arguments": {"terminology": "ncit", "code": "C3262"} | arguments,
            },
        },
    )


def result(response):
    response.raise_for_status()
    return response.json()["result"]


class HTTPTest(ServerFixture):
    def test_health_answers_before_busy_worker_tokens_are_released(self):
        async def scenario():
            limiter = anyio.to_thread.current_default_thread_limiter()
            release_workers = anyio.Event()
            occupied = anyio.Event()
            owners = [object() for _ in range(int(limiter.total_tokens))]

            async def block_workers():
                try:
                    for owner in owners:
                        limiter.acquire_on_behalf_of_nowait(owner)
                    occupied.set()
                    await release_workers.wait()
                finally:
                    for owner in owners:
                        limiter.release_on_behalf_of(owner)

            async with http_app(self.settings, self.context) as client:
                workers = asyncio.create_task(block_workers())
                try:
                    await occupied.wait()
                    response = None
                    with anyio.move_on_after(2):
                        response = await client.get("/health")
                    self.assertIsNotNone(response, "liveness queued behind occupied worker tokens")
                    self.assertFalse(release_workers.is_set())
                    return response
                finally:
                    release_workers.set()
                    await workers

        response = asyncio.run(scenario())
        self.assertEqual((response.status_code, response.json()), (200, {"status": "ok"}))

    def test_http_session_manager_uses_the_documented_idle_timeout_and_capacity(self):
        """Given stateful HTTP, when built, then SDK sessions use the documented limits."""
        app = create_http_app(self.settings, context=self.context)
        endpoint = next(route.endpoint for route in app.routes if route.path == "/mcp")
        manager = endpoint.session_manager

        self.assertEqual((manager.session_idle_timeout, manager.max_sessions), (1800, 10_000))

    def test_legacy_index_readiness_keeps_the_previous_error_identity(self):
        logging.disable(logging.NOTSET)
        invoke(self.context, "index_codes", ["C3262"])

        async def scenario():
            async with http_app(self.settings, self.context) as client:
                ready = await client.get("/ready")
                manifest = self.local_index.get_active_manifest()
                with self.local_index._connect() as connection:
                    connection.execute(
                        "UPDATE manifests SET payload = ?",
                        (json.dumps(replace(manifest, needs_rebuild=True).to_dict()),),
                    )
                with self.assertLogs("nci_sip_mcp.transport", level="WARNING") as captured:
                    blocked = await client.get("/ready")
                return ready, blocked, captured.records

        ready, blocked, records = asyncio.run(scenario())
        self.assertEqual((ready.status_code, ready.json()), (200, {"status": "ready"}))
        self.assertEqual((blocked.status_code, blocked.json()), (503, {"status": "not_ready"}))
        self.assertEqual(records[0].structured["errorType"], "PlatformError")

    def setUp(self):
        super().setUp()
        self.evs.concepts = deepcopy(self.evs.concepts)

    def moved(self, version="26.07d"):
        self.evs.release = release(version)
        self.evs.concepts["C3262"]["version"] = version

    def test_handshake_clients_can_list_all_tools_with_object_union_schemas(self):
        async def scenario():
            async with http_app(self.settings, self.context) as client:
                headers = await initialize_http(client)
                response = await client.post(
                    "/mcp",
                    headers=headers,
                    json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                )
                return result(response)["tools"]

        tools = asyncio.run(scenario())
        self.assertEqual(len(tools), 29)
        self.assertEqual({tool["outputSchema"]["type"] for tool in tools}, {"object"})
        self.assertTrue(all("anyOf" in tool["outputSchema"] for tool in tools))

    def test_stateful_session_belongs_to_one_process_and_explicit_calls_do_not_repin(self):
        async def scenario():
            async with (
                http_app(self.settings, self.context) as first,
                http_app(self.settings, self.context) as other,
            ):
                headers = await initialize_http(first)
                initial = result(await concept_response(first, headers))
                wrong = await concept_response(other, headers)
                self.assertEqual(
                    (wrong.status_code, wrong.json()["error"]["message"]),
                    (404, "Session not found"),
                )
                self.moved()
                explicit = result(await concept_response(first, headers, release="26.07d"))
                new = result(await concept_response(other, await initialize_http(other)))
                self.evs.concepts["C3262"]["version"] = "26.06e"
                retained = result(await concept_response(first, headers))
                return initial, explicit, new, retained

        answers = asyncio.run(scenario())
        self.assertEqual(
            [version(r["structuredContent"]) for r in answers],
            ["26.06e", "26.07d", "26.07d", "26.06e"],
        )
        self.assertEqual([r["_meta"]["ttlMs"] for r in answers], [0, 86400000, 0, 0])
        self.assertEqual(sum(c[0] == "get_terminologies" for c in self.evs.calls), 2)

    def test_stateless_replicas_interchange_without_affinity_and_resolve_each_call(self):
        settings = replace(self.settings, http_sessions="stateless")

        async def scenario():
            async with (
                http_app(settings, self.context) as first,
                http_app(settings, self.context) as other,
            ):
                headers = await initialize_http(first)
                self.assertNotIn("Mcp-Session-Id", headers)
                one = result(await concept_response(first, headers))
                self.moved()
                two = result(await concept_response(other, headers))
                self.moved("26.08d")
                three = result(await concept_response(first, headers))
                explicit = result(await concept_response(other, headers, release="26.08d"))
                return one, two, three, explicit

        answers = asyncio.run(scenario())
        self.assertEqual(
            [version(r["structuredContent"]) for r in answers],
            ["26.06e", "26.07d", "26.08d", "26.08d"],
        )
        self.assertEqual(
            [(r["_meta"]["ttlMs"], r["_meta"]["cacheScope"]) for r in answers],
            [(0, "private")] * 3 + [(86400000, "public")],
        )
        self.assertEqual(sum(c[0] == "get_terminologies" for c in self.evs.calls), 3)

    def test_content_failure_holds_pin_and_withdrawal_asks_for_new_session(self):
        async def scenario():
            async with http_app(self.settings, self.context) as client:
                headers = await initialize_http(client)
                self.evs.concepts["C3262"]["version"] = "wrong"
                failed = result(await concept_response(client, headers))
                self.assertEqual(failed["structuredContent"]["error"]["code"], "release_mismatch")
                self.moved()
                self.evs.concepts["C3262"]["version"] = "26.06e"
                retained = result(await concept_response(client, headers))
                with patch.object(
                    self.evs, "get_concept", side_effect=EVSReleaseNotFoundError("gone")
                ):
                    withdrawn = result(await concept_response(client, headers))
                return retained, withdrawn

        retained, withdrawn = asyncio.run(scenario())
        self.assertEqual(version(retained["structuredContent"]), "26.06e")
        error = withdrawn["structuredContent"]["error"]
        self.assertEqual(
            (error["code"], error["details"]["requested"]), ("release_not_available", "26.06e")
        )
        self.assertIn("start a new session", error["message"].lower())
        self.assertEqual(sum(c[0] == "get_terminologies" for c in self.evs.calls), 1)

    def test_terminated_session_cannot_be_reused_and_new_session_can_advance(self):
        async def scenario():
            async with http_app(self.settings, self.context) as client:
                headers = await initialize_http(client)
                result(await concept_response(client, headers))
                ended = await client.delete("/mcp", headers=headers)
                self.assertEqual(ended.status_code, 200)
                refused = await concept_response(client, headers)
                self.assertEqual(refused.status_code, 404)
                self.moved()
                return result(await concept_response(client, await initialize_http(client)))

        self.assertEqual(version(asyncio.run(scenario())["structuredContent"]), "26.07d")

    def test_single_exchange_protocol_resolves_per_call_even_in_stateful_mode(self):
        async def scenario():
            async with http_app(self.settings, self.context) as client:
                request = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "get_concept",
                        "arguments": {"terminology": "ncit", "code": "C3262"},
                        "_meta": {
                            "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                            "io.modelcontextprotocol/clientInfo": {"name": "test", "version": "1"},
                            "io.modelcontextprotocol/clientCapabilities": {},
                        },
                    },
                }
                headers = {
                    "Accept": "application/json, text/event-stream",
                    "MCP-Protocol-Version": "2026-07-28",
                    "Mcp-Method": "tools/call",
                    "Mcp-Name": "get_concept",
                }
                first = result(await client.post("/mcp", headers=headers, json=request))
                self.moved()
                second = result(await client.post("/mcp", headers=headers, json=request))
                return first, second

        answers = asyncio.run(scenario())
        self.assertEqual([version(r["structuredContent"]) for r in answers], ["26.06e", "26.07d"])
        self.assertEqual([r["_meta"]["ttlMs"] for r in answers], [0, 0])
        self.assertEqual(sum(c[0] == "get_terminologies" for c in self.evs.calls), 2)

    def test_health_and_readiness_do_not_read_upstream_and_verify_a_required_index(self):
        settings = replace(self.settings, http_require_index=True)
        self.context.settings = settings

        async def scenario():
            async with http_app(settings, self.context) as client:
                health = await client.get("/health")
                pending = await client.get("/ready")
                self.assertEqual((health.status_code, health.json()), (200, {"status": "ok"}))
                self.assertEqual(
                    (pending.status_code, pending.json()), (503, {"status": "not_ready"})
                )
                self.assertEqual(self.evs.calls, [])
                invoke(self.context, "index_codes", ["C3262"])
                self.evs.calls.clear()
                ready = await client.get("/ready")
                self.context.embedding_provider = HashingEmbeddingProvider(32)
                wrong_model = await client.get("/ready")
                return ready, wrong_model

        ready, wrong = asyncio.run(scenario())
        self.assertEqual((ready.status_code, wrong.status_code), (200, 503))
        self.assertEqual(self.evs.calls, [])

    def test_optional_index_allows_live_tools_but_a_corrupt_database_is_not_ready(self):
        async def scenario():
            async with http_app(self.settings, self.context) as client:
                ready = await client.get("/ready")
                self.local_index.db_path.write_bytes(b"not SQLite" * 1000)
                corrupt = await client.get("/ready")
                return ready, corrupt

        ready, corrupt = asyncio.run(scenario())
        self.assertEqual((ready.status_code, corrupt.status_code), (200, 503))
        self.assertEqual(corrupt.json(), {"status": "not_ready"})
        self.assertEqual(self.evs.calls, [])

    def test_health_responds_while_readiness_waits_for_index_storage(self):
        entered, released = Event(), Event()
        verify = self.context.index.verify_active

        def waiting_index(provider):
            entered.set()
            released.wait(2)
            return verify(provider)

        async def scenario():
            settings = replace(self.settings, http_require_index=True)
            async with http_app(settings, self.context) as client:
                pending = asyncio.create_task(client.get("/ready"))
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                    health = await asyncio.wait_for(client.get("/health"), timeout=1)
                    self.assertEqual((health.status_code, health.json()), (200, {"status": "ok"}))
                    self.assertFalse(pending.done(), "readiness must still be waiting for storage")
                finally:
                    released.set()
                    response = await pending
                self.assertEqual(
                    (response.status_code, response.json()), (503, {"status": "not_ready"})
                )

        with patch.object(self.context.index, "verify_active", side_effect=waiting_index):
            asyncio.run(scenario())

    def test_readiness_logs_only_failure_transitions_without_exception_messages(self):
        settings = replace(self.settings, http_require_index=True)

        async def scenario():
            async with http_app(settings, self.context) as client:
                missing = await client.get("/ready")
                await client.get("/ready")
                invoke(self.context, "index_codes", ["C3262"])
                recovered = await client.get("/ready")
                self.context.embedding_provider = HashingEmbeddingProvider(32)
                incompatible = await client.get("/ready")
                await client.get("/ready")
                return [r.status_code for r in (missing, recovered, incompatible)]

        logging.disable(logging.NOTSET)
        with self.assertLogs("nci_sip_mcp.transport", level="WARNING") as logs:
            self.assertEqual(asyncio.run(scenario()), [503, 200, 503])
        records = [json.loads(JsonFormatter().format(record)) for record in logs.records]
        self.assertEqual(len(records), 2)
        self.assertEqual(
            [record["errorType"] for record in records],
            ["NoActiveIndexError", "IndexCompatibilityError"],
        )
        self.assertNotIn(str(self.local_index.db_path), json.dumps(records))

    def test_host_and_origin_allow_lists_cover_health_and_mcp_even_on_wildcard_bind(self):
        settings = replace(self.settings, http_host="0.0.0.0")  # noqa: S104 - ASGI test, no socket

        async def scenario():
            async with http_app(settings, self.context) as client:
                for path in ("/health", "/mcp"):
                    bad_host = await client.get(path, headers={"Host": "attacker.example"})
                    bad_origin = await client.get(
                        path, headers={"Origin": "https://attacker.example"}
                    )
                    self.assertEqual((bad_host.status_code, bad_origin.status_code), (421, 403))
                return await client.get("/health", headers={"Origin": "http://localhost:8000"})

        self.assertEqual(asyncio.run(scenario()).status_code, 200)
        self.assertEqual(self.evs.calls, [])

    def test_declared_and_chunked_oversize_bodies_are_refused_before_parsing_or_execution(self):
        settings = replace(self.settings, http_max_request_bytes=64)

        async def chunks():
            yield b"{" * 40
            yield b"x" * 40

        async def scenario():
            async with http_app(settings, self.context) as client:
                for body in (b"{" * 65, chunks()):
                    response = await client.post(
                        "/mcp", content=body, headers={"Content-Type": "application/json"}
                    )
                    self.assertEqual(
                        (response.status_code, response.text), (413, "Request body too large")
                    )

        with patch("nci_sip_mcp.server.invoke", side_effect=AssertionError("must not run")):
            asyncio.run(scenario())
        self.assertEqual(self.evs.calls, [])


class Verifier:
    async def verify_token(self, token):
        if token == "invalid-secret":  # noqa: S105 - deliberately invalid test credential
            return None
        scopes = [] if token == "scope-secret" else ["tools"]  # noqa: S105 - test credential
        return AccessToken(
            token=token,
            client_id="test-client",
            scopes=scopes,
            resource="http://127.0.0.1:8000/mcp",
        )


class HTTPAuthTest(ServerFixture):
    def assert_rejection(self, response, logged, expected):
        self.assertEqual(response.status_code, expected)
        self.assertEqual(response.headers["cache-control"], "no-store")
        records = [json.loads(JsonFormatter().format(record)) for record in logged.records]
        auth_records = [r for r in records if r["event"] == "http_auth_rejected"]
        self.assertEqual([r["status"] for r in auth_records], [expected])
        self.assertNotIn("call_completed", [r["event"] for r in records])
        self.assertNotIn("secret", response.text + json.dumps(records))
        self.assertNotIn("caller-sensitive-text", response.text + json.dumps(records))

    def test_auth_and_scope_rejections_never_invoke_tools_or_log_caller_content(self):
        auth = AuthSettings(
            issuer_url="https://issuer.example",
            resource_server_url="http://127.0.0.1:8000/mcp",
            required_scopes=["tools"],
            validate_token_resource=True,
        )

        async def scenario():
            async with http_app(
                self.settings, self.context, auth=auth, token_verifier=Verifier()
            ) as client:
                for token, expected in (
                    (None, 401),
                    ("invalid-secret", 401),
                    ("scope-secret", 403),
                ):
                    headers = {"Authorization": "Bearer " + token} if token else {}
                    with self.assertLogs(level="DEBUG") as logged:
                        response = await concept_response(
                            client, headers, code="caller-sensitive-text"
                        )
                    self.assert_rejection(response, logged, expected)
                headers = {
                    "Authorization": "Bearer valid-secret",
                    "Accept": "application/json, text/event-stream",
                    "MCP-Protocol-Version": "2025-11-25",
                }
                return await client.post(
                    "/mcp",
                    headers=headers,
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2025-11-25",
                            "capabilities": {},
                            "clientInfo": {"name": "test", "version": "1"},
                        },
                    },
                )

        logging.disable(logging.NOTSET)
        with patch("nci_sip_mcp.server.invoke", side_effect=AssertionError("must not run")):
            allowed = asyncio.run(scenario())
        self.assertEqual(allowed.status_code, 200)
        self.assertIn("mcp-session-id", allowed.headers)
        self.assertEqual(self.evs.calls, [])
