"""Startup overrides preserve the same protocol and configuration protections."""

import asyncio
import json
import logging
import os
import unittest
from dataclasses import replace
from unittest.mock import Mock, patch

import httpx2
from mcp.client import Client
from mcp.server.mcpserver import MCPServer

from nci_sip_mcp import container_entry
from nci_sip_mcp.audit import JsonFormatter
from nci_sip_mcp.config import Settings
from nci_sip_mcp.transport import create_http_app
from test_server import ServerFixture


class AuthorityConfigurationTest(unittest.TestCase):
    def test_programmatic_empty_allowlists_are_rejected(self):
        for field, variable in (
            ("http_allowed_hosts", "NCI_SIP_HTTP_ALLOWED_HOSTS"),
            ("http_allowed_origins", "NCI_SIP_HTTP_ALLOWED_ORIGINS"),
        ):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, variable):
                Settings(**{field: ()})


class StartupBoundaryTest(ServerFixture):
    def test_required_auth_stdio_rejects_image_startup_before_loading_assets(self):
        """Given required auth and stdio transport,
        when the image entrypoint reads those settings,
        then startup rejects before assets and logs only the setting name.
        """
        logging.disable(logging.NOTSET)
        index = Mock()
        index.get_active_manifest.return_value = None
        with (
            patch.dict(
                os.environ,
                {"NCI_SIP_HTTP_AUTH_MODE": "required", "NCI_SIP_TRANSPORT": "stdio"},
                clear=True,
            ),
            patch.object(container_entry, "prepared_index", return_value=index) as prepare,
            patch.object(container_entry, "create_mcp") as create,
            patch.object(container_entry, "run_http") as http,
            self.assertLogs("nci_sip_mcp.container_entry", level="ERROR") as logs,
        ):
            self.assertEqual(container_entry.main(), 1)
        prepare.assert_not_called()
        create.assert_not_called()
        http.assert_not_called()
        records = [json.loads(JsonFormatter().format(record)) for record in logs.records]
        self.assertEqual(
            [
                (record["event"], record["asset"], record["errorType"], record["parameter"])
                for record in records
            ],
            [("container_startup_failed", "settings", "ValueError", "NCI_SIP_HTTP_AUTH_MODE")],
        )
        self.assertNotIn("required", json.dumps(records))
        self.assertNotIn("stdio", json.dumps(records))

    def test_required_auth_http_starts_the_protected_application(self):
        async def exercise(app):
            headers = {
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": "2025-11-25",
            }
            initialize = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "startup-test", "version": "1"},
                },
            }
            async with (
                app.router.lifespan_context(app),
                httpx2.AsyncClient(
                    transport=httpx2.ASGITransport(app=app),
                    base_url="http://127.0.0.1:8000",
                ) as client,
            ):
                refused = await client.post("/mcp", headers=headers, json=initialize)
                accepted = await client.post(
                    "/mcp",
                    headers=headers | {"Authorization": "Bearer synthetic"},
                    json=initialize,
                )
            return refused.status_code, accepted.status_code

        def launch(app, **_options):
            self.responses = asyncio.run(exercise(app))

        with (
            patch.dict(
                os.environ,
                {
                    "NCI_SIP_DATA_DIR": str(self.settings.data_dir),
                    "NCI_SIP_TRANSPORT": "streamable-http",
                    "NCI_SIP_HTTP_SESSIONS": "stateless",
                    "NCI_SIP_HTTP_AUTH_MODE": "required",
                    "NCI_SIP_HTTP_AUTH_FACTORY": "test_http_access:integration",
                },
                clear=True,
            ),
            patch.object(container_entry, "configure_logging"),
            patch("uvicorn.run", side_effect=launch),
        ):
            self.assertEqual(container_entry.main(), 0)
        self.assertEqual(self.responses, (401, 200))

    def test_optional_index_stdio_override_serves_the_protocol_without_an_active_build(self):
        settings = replace(self.settings, transport="stdio", http_require_index=False)
        answers = []

        async def exchange(server):
            async with Client(server) as client:
                return await client.call_tool(
                    "search_concepts",
                    {
                        "terminology": "ncit",
                        "release": "26.06e",
                        "query": "neoplasm",
                        "mode": "semantic",
                    },
                )

        def serve(server):
            answers.append(asyncio.run(exchange(server)))

        with (
            patch.object(container_entry.Settings, "from_env", return_value=settings),
            patch.object(MCPServer, "run", serve),
            patch.object(container_entry, "run_http", side_effect=AssertionError("stdio selected")),
        ):
            self.assertEqual(container_entry.main(), 0)
        (answer,) = answers
        self.assertTrue(answer.is_error)
        self.assertEqual(
            json.loads(answer.content[0].text)["error"]["code"], "capability_unavailable"
        )
        self.assertIsNone(self.context.index.get_active_manifest())
        self.assertEqual(self.local_index.list_builds(), [])

    def test_http_middleware_forwards_application_startup_and_shutdown(self):
        app = create_http_app(self.settings, context=self.context)

        async def scenario():
            incoming = asyncio.Queue()
            incoming.put_nowait({"type": "lifespan.startup"})
            incoming.put_nowait({"type": "lifespan.shutdown"})
            sent = []

            async def send(message):
                sent.append(message)

            await app({"type": "lifespan", "asgi": {"version": "3.0"}}, incoming.get, send)
            return sent

        self.assertEqual(
            asyncio.run(scenario()),
            [{"type": "lifespan.startup.complete"}, {"type": "lifespan.shutdown.complete"}],
        )
