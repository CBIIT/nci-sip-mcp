"""Run the server under test and talk to it over MCP, in fixture or live mode.

Fixture and remote targets use the public MCP interface; managed live targets use the
furnished CLI. Each stdio command receives the upstream environment of the run mode:
in `fixture` mode every upstream base URL points at the fixture server, in `live`
mode one reviewed named environment supplies every origin and permitted credential.
Blocked surfaces use an owned refusal endpoint; production defaults never apply.

    NCI_SI_ACCEPTANCE_MODE     fixture (default) or live
    NCI_SI_ACCEPTANCE_ENVIRONMENT  required live: evs-only (acceptance README)
    NCI_SI_ACCEPTANCE_SERVER   fixture command; live only "nci-si-mcp serve"
    NCI_SI_ACCEPTANCE_PROFILE  fixture: evs, cadsr or unified (default); live: unified only
    NCI_SI_ACCEPTANCE_PREPARE  fixture shell command; live only "nci-si-mcp index-sample"
                               with harness-owned argv; without it the tests
                               that need its result are NOT RUN

A remote server, named by its streamable-HTTP endpoint in place of a command, is tested
through these settings (the acceptance README, "Remote server"):

    NCI_SI_ACCEPTANCE_URL            the endpoint, in place of NCI_SI_ACCEPTANCE_SERVER
    NCI_SI_ACCEPTANCE_AUTHORIZATION  sent as the Authorization header of every request: a
                                     credential, never written anywhere
    NCI_SI_ACCEPTANCE_FIXTURE_BIND   HOST[:PORT] the fixture server listens on
    NCI_SI_ACCEPTANCE_FIXTURE_URL    the base URL the remote server reaches the fixture server by
    NCI_SI_ACCEPTANCE_STATE_HOOK     the operator's command that applies the settings of a
                                     scenario set to the server and makes it forget every
                                     upstream answer cached so far
    NCI_SI_ACCEPTANCE_STATE_HOOK_TIMEOUT  seconds the command may take to return, and again the
                                     endpoint to answer after it
    NCI_SI_ACCEPTANCE_PREPARED       1: the operator has prepared the server's index
"""

from __future__ import annotations

import os
import shlex
import socket
import subprocess
import sys
import time
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field, replace
from functools import partial
from typing import TYPE_CHECKING, Any, Literal, Self, TextIO
from urllib.parse import urlsplit

import httpx2
from anyio.from_thread import BlockingPortal, start_blocking_portal
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

from nci_si_acceptance.denied_surfaces import DeniedSurfaces
from nci_si_acceptance.environments import (
    CREDENTIAL_SURFACES,
    LAUNCHER,
    Environment,
    external_runtime_setting,
    selection,
    validate_live_inputs,
)
from nci_si_acceptance.fixture_server import UPSTREAM_VARIABLES
from nci_si_acceptance.owned_process import stop_child
from nci_si_acceptance.spec import PROFILES

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping
    from pathlib import Path

    from mcp import types

type Mode = Literal["fixture", "live"]

MODE_VARIABLE = "NCI_SI_ACCEPTANCE_MODE"
SERVER_VARIABLE = "NCI_SI_ACCEPTANCE_SERVER"
PROFILE_VARIABLE = "NCI_SI_ACCEPTANCE_PROFILE"
PREPARE_VARIABLE = "NCI_SI_ACCEPTANCE_PREPARE"
URL_VARIABLE = "NCI_SI_ACCEPTANCE_URL"
AUTHORIZATION_VARIABLE = "NCI_SI_ACCEPTANCE_AUTHORIZATION"
FIXTURE_BIND_VARIABLE = "NCI_SI_ACCEPTANCE_FIXTURE_BIND"
FIXTURE_URL_VARIABLE = "NCI_SI_ACCEPTANCE_FIXTURE_URL"
STATE_HOOK_VARIABLE = "NCI_SI_ACCEPTANCE_STATE_HOOK"
STATE_HOOK_TIMEOUT_VARIABLE = "NCI_SI_ACCEPTANCE_STATE_HOOK_TIMEOUT"
PREPARED_VARIABLE = "NCI_SI_ACCEPTANCE_PREPARED"
SECURITY_SERVER_VARIABLE = "NCI_SI_ACCEPTANCE_SECURITY_SERVER"
REGISTRY_RELEASE_VARIABLE = "NCI_SI_ACCEPTANCE_REGISTRY_RELEASE"
CREDENTIAL_VARIABLES = tuple(CREDENTIAL_SURFACES)

# The settings that mean something only for a remote server: naming one without the URL is a
# usage error, never silently ignored.
REMOTE_ONLY = (
    AUTHORIZATION_VARIABLE,
    FIXTURE_URL_VARIABLE,
    STATE_HOOK_VARIABLE,
    STATE_HOOK_TIMEOUT_VARIABLE,
    PREPARED_VARIABLE,
)
# The settings the harness gives the secured fixture adapter: its loopback port and the file of
# generated tokens and policies it rereads on every request.
ADAPTER_PORT_VARIABLE = "NCI_SI_TEST_HTTP_PORT"
ADAPTER_AUTHORITY_VARIABLE = "NCI_SI_TEST_AUTHORITY_FILE"
# The file of concept codes the prepare command indexes, which the suite names to it.
INDEX_CODES_VARIABLE = "NCI_SI_ACCEPTANCE_INDEX_CODES"
DEFAULT_SERVER = "nci-si-mcp serve"
DEFAULT_STATE_HOOK_TIMEOUT_SECONDS = 60.0
WITHHELD = "[authorization withheld]"
MIN_TOKEN_LENGTH = 8
# The statuses of a credential the endpoint will not accept, however long the harness waits.
REFUSALS = (401, 403)
HTTP_ERROR = 400
# How long the harness waits for any one answer of the server, startup included.
READ_TIMEOUT_SECONDS = 60

type Transport = Literal["stdio", "streamable-http"]


@dataclass(frozen=True, slots=True)
class Target:
    """The server under test, the profile it serves and the run mode.

    A command starts a server over stdio; a `url` names a remote one over streamable HTTP,
    which the harness does not start and, but for the operator's `state_hook` command, cannot
    set the environment of. The `authorization` is a credential: it has no place in the
    representation.
    """

    mode: Mode
    command: list[str]
    profile: str = "unified"
    prepare: str | None = None
    url: str | None = None
    authorization: str | None = field(default=None, repr=False)
    fixture_bind: tuple[str, int] | None = None
    fixture_url: str | None = None
    state_hook: str | None = None
    state_hook_timeout: float = DEFAULT_STATE_HOOK_TIMEOUT_SECONDS
    prepared: bool = False
    # The command of the secured fixture adapter (X-25 to X-28), for a local and a remote target
    # alike: the harness starts it itself beside the server under test.
    security_server: str | None = None
    registry_release: Literal["required", "optional"] = "optional"
    environment: Environment | None = None
    denied_url: str | None = field(default=None, repr=False)

    @classmethod
    def from_env(cls, *, mode_override: Mode | None = None) -> Target:
        mode = (
            mode_override if mode_override is not None else os.environ.get(MODE_VARIABLE, "fixture")
        )
        if mode not in ("fixture", "live"):
            raise ValueError(f"{MODE_VARIABLE} must be fixture or live, not {mode!r}")
        environment = selection(mode)
        if environment is not None:
            validate_live_inputs(environment)
        profile = os.environ.get(PROFILE_VARIABLE, "unified")
        if profile not in PROFILES:
            raise ValueError(f"{PROFILE_VARIABLE} must be one of {', '.join(PROFILES)}")
        prepare = _text(PREPARE_VARIABLE)
        bind = _bind(os.environ.get(FIXTURE_BIND_VARIABLE))
        security_server = _text(SECURITY_SERVER_VARIABLE)
        registry_release = _registry_release()
        if (url := _endpoint()) is None:
            _refuse_remote_only()
            return cls(
                mode,
                [*LAUNCHER, "serve"] if environment is not None else _command(),
                profile,
                prepare,
                fixture_bind=bind,
                security_server=security_server,
                registry_release=registry_release,
                environment=environment,
            )
        return cls(
            mode,
            [],
            profile,
            None,
            url,
            _text(AUTHORIZATION_VARIABLE),
            bind,
            _fixture_url(),
            _text(STATE_HOOK_VARIABLE),
            _seconds(STATE_HOOK_TIMEOUT_VARIABLE, DEFAULT_STATE_HOOK_TIMEOUT_SECONDS),
            _declared(PREPARED_VARIABLE),
            security_server,
            registry_release,
        )

    @contextmanager
    def managed(self) -> Iterator[Target]:
        if self.environment is None:
            yield self
            return
        with DeniedSurfaces() as url:
            yield replace(self, denied_url=url)

    def prepare_command(self, codes: list[str]) -> list[str]:
        # Standalone fixture/remote harnesses do not install the furnished server.
        from nci_si_mcp.validation import NCIT_CODE_RE  # noqa: PLC0415 - managed path only.

        if not codes or not all(NCIT_CODE_RE.fullmatch(code) for code in codes):
            raise ValueError("live preparation requires the validated harness concept list")
        return [*LAUNCHER, "index-sample", *codes]

    @property
    def transport(self) -> Transport:
        return "stdio" if self.url is None else "streamable-http"

    @property
    def has_index(self) -> bool:
        """Whether the server holds the index set: prepared by the harness's prepare command,
        or declared prepared by the operator of a remote server."""

        return self.prepared if self.url else self.prepare is not None


def _registry_release() -> Literal["required", "optional"]:
    value = os.environ.get(REGISTRY_RELEASE_VARIABLE, "optional")
    if value not in ("required", "optional"):
        raise ValueError(f"{REGISTRY_RELEASE_VARIABLE} must be required or optional, not {value!r}")
    return value


def _command() -> list[str]:
    command = shlex.split(os.environ.get(SERVER_VARIABLE, DEFAULT_SERVER))
    if not command:
        raise ValueError(f"{SERVER_VARIABLE} must name a command")
    return command


def _text(name: str) -> str | None:
    """The setting `name`, or None where it is unset or empty."""

    return os.environ.get(name) or None


def _endpoint() -> str | None:
    """The remote endpoint, where one is named; naming a command too, or a prepare command
    the harness would never run against it, is a usage error."""

    url = os.environ.get(URL_VARIABLE)
    if not url:
        return None
    if os.environ.get(SERVER_VARIABLE):
        raise ValueError(f"{URL_VARIABLE} and {SERVER_VARIABLE} are both set; name one server")
    if os.environ.get(PREPARE_VARIABLE):
        raise ValueError(
            f"{PREPARE_VARIABLE} is never run against a remote server; "
            f"prepare it yourself and set {PREPARED_VARIABLE}=1"
        )
    if urlsplit(url).scheme not in ("http", "https") or not urlsplit(url).hostname:
        raise ValueError(f"{URL_VARIABLE} must be an http or https URL")
    return url


def _refuse_remote_only() -> None:
    if named := [name for name in REMOTE_ONLY if os.environ.get(name)]:
        raise ValueError(f"{', '.join(named)} apply only with {URL_VARIABLE}")


def _fixture_url() -> str | None:
    url = os.environ.get(FIXTURE_URL_VARIABLE)
    return url.rstrip("/") if url else None


def _bind(value: str | None) -> tuple[str, int] | None:
    """`HOST` or `HOST:PORT` as the address and port the fixture server listens on."""

    if not value:
        return None
    host, colon, port = value.partition(":")
    if not host or (colon and not port.isdecimal()):
        raise ValueError(f"{FIXTURE_BIND_VARIABLE} must be HOST or HOST:PORT, not {value!r}")
    return host, int(port or 0)


def _seconds(name: str, default: float) -> float:
    value = os.environ.get(name)
    if not value:
        return default
    try:
        seconds = float(value)
    except ValueError:
        seconds = 0.0
    if seconds <= 0:
        raise ValueError(f"{name} must be a positive number of seconds, not {value!r}")
    return seconds


def _declared(name: str) -> bool:
    value = os.environ.get(name)
    if value not in (None, "", "1"):
        raise ValueError(f"{name} must be 1 or unset, not {value!r}")
    return value == "1"


def _upstream_settings(fixture_url: str | None) -> dict[str, str]:
    """Every fixture upstream names the owned fixture server, never a runtime default."""

    if fixture_url is None:
        raise ValueError("fixture mode needs the fixture server's URL")
    return {name: f"{fixture_url}/{surface}" for name, surface in UPSTREAM_VARIABLES.items()}


def withhold_authorization(text: str) -> str:
    """`text` without the operator's credential, which no output of the harness may carry: the
    whole value, and the token after its scheme (`Bearer TOKEN`) where that is long enough not
    to withhold ordinary words."""

    if not (secret := os.environ.get(AUTHORIZATION_VARIABLE)):
        return text
    token = secret.partition(" ")[2]
    for part in (secret, token) if len(token) >= MIN_TOKEN_LENGTH else (secret,):
        text = text.replace(part, WITHHELD)
    return text


def remote_settings(fixture_url: str) -> dict[str, str]:
    """What the operator sets on a remote server in fixture mode: the harness cannot set the
    environment of a process it did not start."""

    return {"NCI_SI_UPSTREAM_MODE": "fixture"} | _upstream_settings(fixture_url)


def without_nci_si_settings(environment: Mapping[str, str]) -> dict[str, str]:
    """`environment` less the developer's own `NCI_SI_*` settings, which no server under test
    gets: a run depends only on its mode."""

    return {key: value for key, value in environment.items() if not key.startswith("NCI_SI_")}


def server_environment(
    mode: Mode,
    data_dir: Path,
    fixture_url: str | None,
    *,
    environment: Environment | None = None,
    denied_url: str | None = None,
) -> dict[str, str]:
    """The environment of the server under test.

    Fixture bases name the owned fixture server. Live bases and private credentials
    come from the single immutable selection; blocked bases name its owned refusal
    endpoint. A live caller cannot fall through to production defaults.
    """

    inherited = without_nci_si_settings(os.environ)
    inherited |= {"NCI_SI_UPSTREAM_MODE": mode, "NCI_SI_DATA_DIR": str(data_dir)}
    if mode == "live":
        return _live_environment(inherited, environment, denied_url)
    return inherited | _upstream_settings(fixture_url)


def _live_environment(
    inherited: dict[str, str], environment: Environment | None, denied_url: str | None
) -> dict[str, str]:
    if environment is None or denied_url is None:
        raise ValueError("live mode requires a selected environment and owned refusal endpoint")
    inherited = {
        key: value for key, value in inherited.items() if not external_runtime_setting(key)
    }
    return (
        inherited
        | {
            variable: environment.origins[surface] or denied_url
            for variable, surface in UPSTREAM_VARIABLES.items()
        }
        | dict(environment.credentials)
    )


class Session:
    """A synchronous MCP client session, for tests."""

    def __init__(self, portal: BlockingPortal, client: Client) -> None:
        self._portal = portal
        self._client = client

    def list_tools(self) -> types.ListToolsResult:
        return self._portal.call(self._client.list_tools)

    def call_tool(
        self, name: str, arguments: dict[str, Any], meta: types.RequestParamsMeta | None = None
    ) -> types.CallToolResult:
        return self._portal.call(partial(self._client.call_tool, name, arguments, meta=meta))

    def _every_page[T: types.PaginatedResult](
        self, list_page: Callable[..., Awaitable[T]], field: str
    ) -> T:
        """Every page of a list method, followed by nextCursor: the first page's result (and
        so its caching hint) holding the items of all of them."""

        first = page = self._portal.call(list_page)
        items = list(getattr(page, field))
        while page.next_cursor:
            page = self._portal.call(partial(list_page, cursor=page.next_cursor))
            items += getattr(page, field)
        return first.model_copy(update={field: items, "next_cursor": None})

    def list_prompts(self) -> types.ListPromptsResult:
        return self._every_page(self._client.list_prompts, "prompts")

    def get_prompt(self, name: str, arguments: dict[str, str]) -> types.GetPromptResult:
        return self._portal.call(partial(self._client.get_prompt, name, arguments))

    def list_resources(self) -> types.ListResourcesResult:
        return self._every_page(self._client.list_resources, "resources")

    def list_resource_templates(self) -> types.ListResourceTemplatesResult:
        return self._every_page(self._client.list_resource_templates, "resource_templates")

    def read_resource(self, uri: str) -> types.ReadResourceResult:
        return self._portal.call(partial(self._client.read_resource, uri))


@contextmanager
def open_session(
    command: list[str],
    environment: dict[str, str],
    errlog: TextIO = sys.stderr,
    *,
    timeout: float | None = None,
) -> Iterator[Session]:
    """Start the server's command, its standard error going to `errlog`, and hold one MCP
    session with it."""

    server = StdioServerParameters(command=command[0], args=command[1:], env=environment)
    transport = stdio_client(server, errlog=errlog)
    with (
        start_blocking_portal() as portal,
        portal.wrap_async_context_manager(
            # No response cache: each tools/list must reach the server (P-6).
            Client(
                transport,
                read_timeout_seconds=READ_TIMEOUT_SECONDS if timeout is None else timeout,
                cache=None,
            )
        ) as client,
    ):
        yield Session(portal, client)


async def _note_refusal(statuses: list[int], response: httpx2.Response) -> None:
    if response.status_code >= HTTP_ERROR:
        statuses.append(response.status_code)


@asynccontextmanager
async def _http_transport(
    url: str, authorization: str | None, seconds: float | None, statuses: list[int]
) -> AsyncIterator[Any]:
    """The streamable-HTTP transport of an endpoint, the credential on every request. Every
    HTTP error status is noted in `statuses`, which the MCP client's own errors do not carry."""

    headers = {"Authorization": authorization} if authorization else None
    bound = httpx2.Timeout(seconds) if seconds else None
    async with (
        create_mcp_http_client(headers=headers, timeout=bound) as http,
        streamable_http_client(url, http_client=http) as streams,
    ):
        http.event_hooks["response"].append(partial(_note_refusal, statuses))
        yield streams


@contextmanager
def open_remote_session(
    url: str,
    authorization: str | None = None,
    *,
    timeout: float | None = None,
    statuses: list[int] | None = None,
    stateful: bool = False,
) -> Iterator[Session]:
    """Hold one MCP session with the endpoint at `url`, no request taking longer than `timeout`
    seconds where that is given. The error of a failed connection names neither the URL nor the
    credential; the HTTP error statuses the endpoint answered are appended to `statuses`."""

    with (
        start_blocking_portal() as portal,
        portal.wrap_async_context_manager(
            Client(
                _http_transport(url, authorization, timeout, [] if statuses is None else statuses),
                read_timeout_seconds=timeout or READ_TIMEOUT_SECONDS,
                cache=None,
                # Session-specific tests must opt into the handshake. Auto may select
                # the 2026 single-exchange protocol, which carries native cache hints.
                mode="legacy" if stateful else "auto",
            )
        ) as client,
    ):
        yield Session(portal, client)


def wait_for_endpoint(url: str, authorization: str | None, seconds: float) -> str | None:
    """None once the endpoint answers a tools/list within `seconds`, tried again until then;
    else why it did not: the HTTP status it answered (a 401 or 403, which waiting does not
    mend, ends the wait at once), or that nothing came in the time given."""

    deadline = time.monotonic() + seconds
    status = None
    while (remaining := deadline - time.monotonic()) > 0:
        refused: list[int] = []
        try:
            with open_remote_session(url, authorization, timeout=remaining, statuses=refused) as s:
                s.list_tools()
            return None
        except Exception:  # noqa: BLE001 - any failure to answer means not yet
            status = refused[0] if refused else None
            if status in REFUSALS:
                break
            time.sleep(min(0.5, max(deadline - time.monotonic(), 0)))
    return f"HTTP {status}" if status else f"nothing within {seconds:g} s"


STOP_SECONDS = 10


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def _listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return True
    except OSError:
        return False


class OwnedServer:
    """The server under test as a child process this object owns, serving streamable HTTP on a
    free local port; it is reaped on leaving the scope, an exception included.

    `environment` is the child's base environment, less any `NCI_SI_*` setting; each
    `start` adds the given settings and the fixed ones that make the server a stateful HTTP
    one with its data in `data_dir`. Its standard error is appended to `log`.
    """

    def __init__(
        self,
        environment: Mapping[str, str],
        log: Path,
        *,
        data_dir: Path,
        require_index: bool,
        ready_seconds: float = 15,
    ) -> None:
        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}/mcp"
        self._environment = without_nci_si_settings(environment)
        self._log, self._data_dir = log, data_dir
        self._require_index, self._ready_seconds = require_index, ready_seconds
        self._process: subprocess.Popen[bytes] | None = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exception: object) -> None:
        self.stop()

    def start(self, settings: Mapping[str, str]) -> None:
        """Launch the server with `settings` and return once it accepts connections; if it
        exits first (its log is named in the error) or the time runs out, reap it and fail.
        Refused while a server is running, or when something else already listens on the port."""

        if self._process is not None:
            raise RuntimeError("The owned server is already running")
        if _listening(self.port):
            raise RuntimeError(
                f"The owned server's port {self.port} is already in use by another process"
            )

        environment = (
            self._environment
            | dict(settings)
            | {
                "NCI_SI_DATA_DIR": str(self._data_dir),
                "NCI_SI_TRANSPORT": "streamable-http",
                "NCI_SI_HTTP_PORT": str(self.port),
                "NCI_SI_HTTP_SESSIONS": "stateful",
                "NCI_SI_HTTP_REQUIRE_INDEX": "1" if self._require_index else "0",
            }
        )
        with self._log.open("ab") as log:
            self._process = subprocess.Popen(
                [sys.executable, "-m", "nci_si_mcp.cli", "serve"],
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=log,
            )
        try:
            self._wait_ready()
        except BaseException:
            self.stop()
            raise

    def stop(self) -> None:
        process, self._process = self._process, None
        if process is not None:
            stop_child(process, seconds=STOP_SECONDS)

    def restart(self, settings: Mapping[str, str]) -> None:
        self.stop()
        self.start(settings)

    def _wait_ready(self) -> None:
        deadline = time.monotonic() + self._ready_seconds
        while self._process is not None and self._process.poll() is None:
            if _listening(self.port):
                return
            if time.monotonic() >= deadline:
                raise TimeoutError(f"The owned server did not become ready; see {self._log}")
            time.sleep(0.05)
        raise RuntimeError(f"The owned server exited before readiness; see {self._log}")
