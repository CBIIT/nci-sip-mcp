import asyncio
import logging
import os
from dataclasses import replace
from time import time
from unittest.mock import patch
from urllib.request import parse_http_list, parse_keqv_list

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from starlette.applications import Starlette
from starlette.responses import Response
from starlette.routing import Route

from fakes import captured, records
from nci_sip_mcp.audit import compact, hashed
from nci_sip_mcp.config import Settings
from nci_sip_mcp.http_auth import HTTPAuthIntegration, configured_auth
from nci_sip_mcp.permissions import Authority, Principal
from nci_sip_mcp.transport import create_http_app
from test_release_selection import initialize_http
from test_server import ServerFixture
from test_transport import concept_response, http_app, result


class LocalPolicy:
    def __init__(self):
        self.token = AccessToken(
            token="synthetic",  # noqa: S106 - local test double, not a credential
            client_id="client",
            subject="caller",
            scopes=["mcp"],
            expires_at=int(time()) + 600,
            resource="http://127.0.0.1:8000/mcp",
            claims={"iss": "https://issuer.example", "tenant": "tenant"},
        )
        self.principal = Principal("https://issuer.example", "caller", "tenant", "client")
        self.capabilities = frozenset({"get_concept"})
        self.bundle = HTTPAuthIntegration(
            AuthSettings.model_validate(
                {
                    "issuer_url": "https://issuer.example",
                    "resource_server_url": "http://127.0.0.1:8000/mcp",
                    "required_scopes": ["mcp"],
                    "validate_token_resource": True,
                }
            ),
            self,
            self.resolve,
        )

    async def verify_token(self, token):
        return self.token if token == "synthetic" else None  # noqa: S105 - test double

    async def resolve(self):
        if get_access_token() is None:
            return None
        return Authority(self.principal, self.capabilities, "v1", time() + 60)


def integration(settings):
    return LocalPolicy().bundle


class GovernedFixture(ServerFixture):
    def configured(self):
        return replace(
            self.settings,
            http_auth_mode="required",
            http_auth_factory="test_http_access:integration",
            http_sessions="stateless",
        )


class OAuthDiscoveryTest(GovernedFixture):
    def policy(self):
        policy = LocalPolicy()
        policy.bundle = replace(
            policy.bundle,
            auth=AuthSettings.model_validate(
                {
                    "issuer_url": "https://issuer.example",
                    "resource_server_url": "http://127.0.0.1:8123/mcp",
                    "required_scopes": ["mcp", "records:read"],
                    "validate_token_resource": True,
                }
            ),
        )
        policy.token.resource = "http://127.0.0.1:8123/mcp"
        return policy

    def test_initial_challenge_leads_to_configured_protected_resource_metadata(self):
        async def scenario():
            async with http_app(self.configured(), self.context) as client:
                response = await concept_response(client, {})
                self.assertEqual(response.status_code, 401)
                scheme, _, parameters = response.headers.get("www-authenticate", "").partition(" ")
                self.assertEqual(scheme, "Bearer")
                challenge = parse_keqv_list(parse_http_list(parameters))
                url = "http://127.0.0.1:8123/.well-known/oauth-protected-resource/mcp"
                self.assertEqual(challenge.get("resource_metadata"), url)
                metadata = await client.get(challenge["resource_metadata"])
                self.assertEqual(metadata.status_code, 200)
                document = metadata.json()
                self.assertEqual(document.get("resource"), "http://127.0.0.1:8123/mcp")
                self.assertEqual(document.get("authorization_servers"), ["https://issuer.example"])
                self.assertEqual(document.get("scopes_supported"), ["mcp", "records:read"])
                self.assertEqual(challenge.get("scope"), "mcp records:read")

        with patch("test_http_access.integration", return_value=self.policy().bundle):
            asyncio.run(scenario())
        self.assertEqual(self.evs.calls, [])

    def test_insufficient_scope_challenge_advertises_all_required_scopes(self):
        async def scenario():
            async with http_app(self.configured(), self.context) as client:
                response = await concept_response(client, {"Authorization": "Bearer synthetic"})
                self.assertEqual(response.status_code, 403)
                self.assertEqual(
                    response.headers.get("www-authenticate"),
                    'Bearer error="insufficient_scope", '
                    'error_description="Required scope: records:read", '
                    'resource_metadata="http://127.0.0.1:8123/'
                    '.well-known/oauth-protected-resource/mcp", '
                    'scope="mcp records:read"',
                )

        with patch("test_http_access.integration", return_value=self.policy().bundle):
            asyncio.run(scenario())
        self.assertEqual(self.evs.calls, [])

    def test_metadata_keeps_host_and_origin_admission(self):
        async def scenario():
            async with http_app(self.configured(), self.context) as client:
                url = "http://127.0.0.1:8123/.well-known/oauth-protected-resource/mcp"
                for headers, status in (
                    ({"Origin": "http://localhost:8000"}, 200),
                    ({"Host": "untrusted.example"}, 421),
                    ({"Origin": "https://untrusted.example"}, 403),
                ):
                    with self.subTest(headers=headers):
                        self.assertEqual(
                            (await client.get(url, headers=headers)).status_code, status
                        )

        with patch("test_http_access.integration", return_value=self.policy().bundle):
            asyncio.run(scenario())

    def test_boundary_preserves_challenge_bytes_and_never_duplicates_scope(self):
        challenge = (
            'Bearer error="insufficient_scope", error_description="needs scope=read, then write", '
            'resource_metadata="http://127.0.0.1:8123/.well-known/oauth-protected-resource/mcp"'
        )

        async def scenario(status, original, expected):
            async def reply(_request):
                return Response(status_code=status, headers={"WWW-Authenticate": original})

            app = Starlette(routes=[Route("/mcp", reply, methods=["POST"])])
            with patch("mcp.server.mcpserver.MCPServer.streamable_http_app", return_value=app):
                async with http_app(self.configured(), self.context) as client:
                    response = await concept_response(client, {})
                    self.assertEqual(response.status_code, status)
                    self.assertEqual(response.headers["www-authenticate"], expected)

        cases = (
            (401, challenge, challenge + ', scope="mcp records:read"'),
            (403, challenge + ', scope="existing other"', challenge + ', scope="existing other"'),
            (401, 'Bearer SCOPE = "existing other"', 'Bearer SCOPE = "existing other"'),
            (401, 'Basic realm="local"', 'Basic realm="local"'),
            (200, challenge, challenge),
            (
                401,
                'Bearer error_description="needs read, scope=x"',
                'Bearer error_description="needs read, scope=x", scope="mcp records:read"',
            ),
            (401, 'Bearer scope_x="custom"', 'Bearer scope_x="custom", scope="mcp records:read"'),
        )
        configured = [(*case, ["mcp", "records:read"]) for case in cases]
        configured.append((401, challenge, challenge, []))
        for status, original, expected, scopes in configured:
            policy = self.policy()
            policy.bundle.auth.required_scopes = scopes
            with (
                self.subTest(status=status, original=original, scopes=scopes),
                patch("test_http_access.integration", return_value=policy.bundle),
            ):
                asyncio.run(scenario(status, original, expected))

    async def boundary_reply(self, status, headers, protected=True):
        async def reply(_request):
            return Response(status_code=status, headers=headers)

        app = Starlette(routes=[Route("/mcp", reply, methods=["POST"])])
        settings = self.configured() if protected else self.settings
        with (
            patch("mcp.server.mcpserver.MCPServer.streamable_http_app", return_value=app),
            patch("test_http_access.integration", return_value=self.policy().bundle),
        ):
            async with http_app(settings, self.context) as client:
                return await concept_response(client, {})

    def test_only_protected_responses_replace_downstream_cache_policy(self):
        for protected, expected in ((False, "public"), (True, "no-store")):
            with self.subTest(protected=protected):
                response = asyncio.run(
                    self.boundary_reply(200, {"Cache-Control": "public"}, protected)
                )
                self.assertEqual(response.headers.get_list("cache-control"), [expected])

    def test_bare_forbidden_response_is_not_logged_as_authentication_refusal(self):
        logging.disable(logging.NOTSET)
        with self.assertNoLogs("nci_sip_mcp.transport", level="WARNING"):
            response = asyncio.run(self.boundary_reply(403, {}))
        self.assertEqual(response.status_code, 403)

    def test_refusal_leaves_non_challenge_bearer_header_unchanged(self):
        response = asyncio.run(
            self.boundary_reply(
                401, {"WWW-Authenticate": "Bearer", "X-Context": 'Bearer context="opaque"'}
            )
        )
        self.assertEqual(response.headers["x-context"], 'Bearer context="opaque"')


class RequiredAccessTest(GovernedFixture):
    def test_a_configured_resolver_cannot_grant_without_a_verified_transport_identity(self):
        bundle = configured_auth(self.configured())
        self.assertIsNone(asyncio.run(bundle.authority_resolver()))

    def test_incomplete_or_unbound_integration_never_starts(self):
        policy = LocalPolicy()
        invalid = [
            None,
            replace(policy.bundle, auth=None),
            replace(policy.bundle, token_verifier=None),
            replace(policy.bundle, authority_resolver=None),
            replace(
                policy.bundle,
                auth=policy.bundle.auth.model_copy(update={"validate_token_resource": False}),
            ),
        ]
        for bundle in invalid:
            with (
                self.subTest(bundle=bundle),
                patch("test_http_access.integration", return_value=bundle),
                self.assertRaisesRegex(ValueError, "integration"),
            ):
                create_http_app(self.configured(), context=self.context)

    def test_wrong_issuer_is_an_authentication_refusal_before_any_read(self):
        policy = LocalPolicy()
        policy.token.claims["iss"] = "https://other.example"

        async def scenario():
            async with http_app(self.configured(), self.context) as client:
                response = await concept_response(client, {"Authorization": "Bearer synthetic"})
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.headers["cache-control"], "no-store")

        with patch("test_http_access.integration", return_value=policy.bundle):
            asyncio.run(scenario())
        self.assertEqual(self.evs.calls, [])

    def test_policy_for_another_principal_is_refused(self):
        policy = LocalPolicy()
        policy.principal = replace(policy.principal, tenant="other")

        async def scenario():
            async with http_app(self.configured(), self.context) as client:
                response = await concept_response(
                    client,
                    {
                        "Authorization": "Bearer synthetic",
                        "Accept": "application/json, text/event-stream",
                        "MCP-Protocol-Version": "2025-11-25",
                    },
                )
                self.assertEqual(
                    result(response)["structuredContent"]["error"]["code"], "permission_denied"
                )

        with patch("test_http_access.integration", return_value=policy.bundle):
            asyncio.run(scenario())
        self.assertEqual(self.evs.calls, [])

    def test_required_mode_without_an_integration_fails_before_serving(self):
        settings = replace(self.settings, http_auth_mode="required")
        with self.assertRaisesRegex(ValueError, "integration"):
            create_http_app(settings, context=self.context)
        self.assertEqual(self.evs.calls, [])

    def test_environment_selects_required_mode_and_factory(self):
        with patch.dict(
            os.environ,
            {
                "NCI_SIP_HTTP_AUTH_MODE": "required",
                "NCI_SIP_HTTP_AUTH_FACTORY": "test_http_access:integration",
            },
        ):
            settings = Settings.from_env()
        self.assertEqual(settings.http_auth_mode, "required")
        self.assertEqual(settings.http_auth_factory, "test_http_access:integration")

    def test_invalid_mode_and_factory_are_rejected(self):
        for fields in (
            {"http_auth_mode": "automatic"},
            {"http_auth_mode": "required", "http_auth_factory": "bad/path:factory"},
            {"http_auth_factory": "test_http_access:integration"},
        ):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                replace(self.settings, **fields)


class GovernedBoundaryTest(GovernedFixture):
    def test_incomplete_verified_identities_are_refused(self):
        policy = LocalPolicy()
        original = policy.token

        async def scenario():
            async with http_app(self.configured(), self.context) as client:
                for fields in (
                    {"subject": None},
                    {"client_id": ""},
                    {"expires_at": None},
                    {"claims": None},
                ):
                    policy.token = original.model_copy(update=fields)
                    response = await concept_response(client, {"Authorization": "Bearer synthetic"})
                    self.assertEqual(response.status_code, 401)

        with patch("test_http_access.integration", return_value=policy.bundle):
            asyncio.run(scenario())
        self.assertEqual(self.evs.calls, [])

    def test_single_exchange_authenticates_each_request_and_obeys_policy_changes(self):
        policy = LocalPolicy()
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
            "Authorization": "Bearer synthetic",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2026-07-28",
            "Mcp-Method": "tools/call",
            "Mcp-Name": "get_concept",
        }

        async def scenario():
            async with http_app(self.configured(), self.context) as client:
                allowed = await client.post("/mcp", headers=headers, json=request)
                self.assertEqual(result(allowed)["structuredContent"]["code"], "C3262")
                self.assertNotIn("mcp-session-id", allowed.headers)
                policy.capabilities = frozenset({"unknown"})
                denied = await client.post("/mcp", headers=headers, json=request)
                self.assertEqual(
                    result(denied)["structuredContent"]["error"]["code"], "permission_denied"
                )
                self.assertEqual(denied.headers["cache-control"], "no-store")

        with patch("test_http_access.integration", return_value=policy.bundle):
            asyncio.run(scenario())

    def test_missing_bad_expired_or_misaddressed_tokens_are_rejected(self):
        policy = LocalPolicy()
        original = policy.token
        variants = [
            ({}, original, 401),
            ({"Authorization": "Bearer invalid"}, original, 401),
            (
                {"Authorization": "Bearer synthetic"},
                original.model_copy(update={"expires_at": 0}),
                401,
            ),
            (
                {"Authorization": "Bearer synthetic"},
                original.model_copy(update={"resource": "https://other.example/mcp"}),
                401,
            ),
            (
                {"Authorization": "Bearer synthetic"},
                original.model_copy(update={"scopes": []}),
                403,
            ),
        ]

        async def scenario():
            async with http_app(self.configured(), self.context) as client:
                for headers, token, status in variants:
                    policy.token = token
                    response = await concept_response(client, headers)
                    self.assertEqual(response.status_code, status)
                    self.assertEqual(response.headers["cache-control"], "no-store")

        with patch("test_http_access.integration", return_value=policy.bundle):
            asyncio.run(scenario())
        self.assertEqual(self.evs.calls, [])

    def test_spoofed_identity_is_not_authority_and_health_remains_public(self):
        async def scenario():
            async with http_app(self.configured(), self.context) as client:
                response = await concept_response(
                    client,
                    {
                        "X-Forwarded-User": "caller",
                        "X-Forwarded-Tenant": "tenant",
                        "X-Forwarded-Authorization": "Bearer synthetic",
                    },
                )
                self.assertEqual(response.status_code, 401)
                for path, status in (("/health", "ok"), ("/ready", "ready")):
                    probe = await client.get(path)
                    self.assertEqual(probe.json(), {"status": status})
                    self.assertEqual(probe.headers["cache-control"], "no-store")
                    refused = await client.get(path, headers={"Host": "untrusted.example"})
                    self.assertEqual(refused.status_code, 421)

        asyncio.run(scenario())
        self.assertEqual(self.evs.calls, [])

    def test_stateful_replicas_require_affinity_and_reauthentication_after_restart(self):
        policy = LocalPolicy()
        settings = replace(self.configured(), http_sessions="stateful")

        async def scenario():
            async with (
                http_app(settings, self.context) as first,
                http_app(settings, self.context) as other,
            ):
                first.headers["Authorization"] = "Bearer synthetic"
                other.headers["Authorization"] = "Bearer synthetic"
                headers = await initialize_http(first)
                allowed = result(await concept_response(first, headers))
                self.assertEqual(allowed["structuredContent"]["code"], "C3262")
                self.assertEqual((await concept_response(other, headers)).status_code, 404)
                policy.capabilities = frozenset()
                denied = result(await concept_response(first, headers))
                self.assertEqual(denied["structuredContent"]["error"]["code"], "permission_denied")
            async with http_app(settings, self.context) as restarted:
                restarted.headers["Authorization"] = "Bearer synthetic"
                self.assertEqual((await concept_response(restarted, headers)).status_code, 404)

        with patch("test_http_access.integration", return_value=policy.bundle):
            asyncio.run(scenario())


HEADERS = {
    "Authorization": "Bearer synthetic",
    "Accept": "application/json, text/event-stream",
    "MCP-Protocol-Version": "2025-11-25",
}


class GovernedPrivacyTest(GovernedFixture):
    def test_governed_transport_refusals_are_not_cacheable(self):
        async def scenario():
            async with http_app(self.configured(), self.context) as client:
                for headers, status in (
                    ({"Host": "attacker.example"}, 421),
                    ({"Origin": "https://attacker.example"}, 403),
                ):
                    response = await concept_response(client, HEADERS | headers)
                    self.assertEqual(response.status_code, status)
                    self.assertEqual(response.headers.get("cache-control"), "no-store")
                    self.assertNotIn("C3262", response.text)

        asyncio.run(scenario())
        self.assertEqual(self.evs.calls, [])

    def test_verified_token_without_caller_policy_cannot_read_content(self):
        policy = LocalPolicy()

        async def scenario():
            async with http_app(
                replace(self.settings, http_sessions="stateless"),
                self.context,
                auth=policy.bundle.auth,
                token_verifier=policy,
            ) as client:
                response = await concept_response(client, HEADERS, release="26.06e")
                payload = result(response)
                self.assertTrue(payload["isError"])
                self.assertEqual(payload["structuredContent"]["error"]["code"], "permission_denied")
                self.assertEqual(response.headers["cache-control"], "no-store")

        asyncio.run(scenario())
        self.assertEqual(self.evs.calls, [])

    def test_secured_completion_audit_hashes_retrieved_and_denied_identifiers(self):
        policy = LocalPolicy()

        async def scenario():
            async with http_app(self.configured(), self.context) as client:
                allowed = result(await concept_response(client, HEADERS, release="26.06e"))
                self.assertEqual(allowed["structuredContent"]["code"], "C3262")
                policy.capabilities = frozenset()
                denied = result(await concept_response(client, HEADERS, release="26.06e"))
                self.assertEqual(denied["structuredContent"]["error"]["code"], "permission_denied")

        with patch("test_http_access.integration", return_value=policy.bundle), captured() as log:
            asyncio.run(scenario())
        audit = records(log)
        self.assertEqual([row["responseCode"] for row in audit], ["ok", "permission_denied"])
        for row in audit:
            self.assertEqual(row["parameters"][compact(hashed("code"))], hashed("C3262"))
            self.assertNotIn("code", row["parameters"])
        self.assertNotIn("C3262", log.getvalue())
