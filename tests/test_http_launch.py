"""The configured HTTP server does not trust caller-supplied forwarding headers."""

import asyncio
from unittest.mock import patch

import httpx2
import uvicorn
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from nci_sip_mcp.transport import run_http
from test_server import ServerFixture


class HTTPLaunchTest(ServerFixture):
    def test_http_shutdown_has_the_documented_twenty_second_drain(self):
        configs = []

        def serve(app, **options):
            configs.append(uvicorn.Config(app, **options))

        with patch("uvicorn.run", serve):
            run_http(self.settings, self.context)
        self.assertEqual(
            (
                configs[0].host,
                configs[0].port,
                configs[0].proxy_headers,
                configs[0].timeout_graceful_shutdown,
            ),
            ("127.0.0.1", 8000, False, 20),
        )

    def test_forwarded_headers_do_not_rewrite_client_identity_or_scheme(self):
        async def identity(request):
            return JSONResponse({"host": request.client.host, "scheme": request.url.scheme})

        async def request(app):
            async with httpx2.AsyncClient(
                transport=httpx2.ASGITransport(app, client=("127.0.0.1", 1234)),
                base_url="http://localhost:8000",
            ) as client:
                return await client.get(
                    "/", headers={"X-Forwarded-For": "203.0.113.8", "X-Forwarded-Proto": "https"}
                )

        def serve(app, **options):
            config = uvicorn.Config(app, **options)
            config.load()
            self.response = asyncio.run(request(config.loaded_app))

        app = Starlette(routes=[Route("/", identity)])
        with (
            patch("nci_sip_mcp.transport.create_http_app", return_value=app),
            patch("uvicorn.run", serve),
        ):
            run_http(self.settings, self.context)
        self.assertEqual(self.response.status_code, 200)
        self.assertEqual(self.response.json(), {"host": "127.0.0.1", "scheme": "http"})
