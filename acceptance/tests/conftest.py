"""The run of the suite: the server under test, its upstream, scenarios, and the request log.

A test that selects scenarios (`@pytest.mark.scenario("release/unknown")`) gets a
server process of its own, started with the scenarios active and with their
settings, so that nothing the server keeps between calls outlives them. Requests a
server makes while it starts must find fixtures too.

Against a remote server (`NCI_SI_ACCEPTANCE_URL`) the harness starts no process: a probe makes
sure the server answers and reaches the fixture server before the first test, and the operator's
state-change hook, where there is one, gives a scenario set's tests, and each `own_server` test,
the state they need; without it those tests are skipped. A remote server's prepare step is
the operator's (`NCI_SI_ACCEPTANCE_PREPARED`).

The operator's prepare command, where one is given, runs once, before the first test that
starts a server, in the server's environment, with the codes of the index set listed in the file
`NCI_SI_ACCEPTANCE_INDEX_CODES` names; every server then starts from a copy of the data
directory it produced. A prepare command that fails, or whose requests find no fixture,
fails every test that depends on it, without aborting unrelated tests.
"""

from __future__ import annotations

import copy
import shutil
import subprocess
import sys
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import yaml

from nci_si_acceptance.client import (
    INDEX_CODES_VARIABLE,
    Target,
    open_remote_session,
    open_session,
    server_environment,
)
from nci_si_acceptance.environments import NATIVE_HANDOFF
from nci_si_acceptance.fixture_server import (
    MANIFEST,
    FixtureServer,
    fixture_documents,
    load_fixtures,
)
from nci_si_acceptance.remote import StateHook, announcement, probe
from nci_si_acceptance.report import COLLECTOR, write_report
from nci_si_acceptance.suite import (
    NOT_DECLARED_PREPARED,
    NOT_PREPARED,
    OWN_SERVER,
    UNMATCHED_UPSTREAM,
    UNPREPARED,
    UnmatchedUpstream,
    index_set,
    order_for_state_changes,
    scenarios_of,
    select_registry_checks,
    skip_fixture_only,
    skip_remote_own_servers,
    skip_unprepared,
    unmatched_requests,
)
from nci_si_acceptance.tools import Process, Tools

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from typing import TextIO

    from nci_si_acceptance.client import Session
    from nci_si_acceptance.request_native import NativeRun
    from nci_si_acceptance.request_stdio import StderrTransfer

pytest_plugins = ["nci_si_acceptance.report"]

FIXTURES = Path(__file__).parent.parent / "fixtures"
TARGET = pytest.StashKey[Target]()
NATIVE: pytest.StashKey[NativeRun] = pytest.StashKey()


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--controlled-run-dir",
        metavar="PATH",
        help="create an explicitly guarded serial run beneath PATH",
    )


@pytest.hookimpl(trylast=True)
def pytest_configure(config: pytest.Config) -> None:
    try:
        config.stash[TARGET] = Target.from_env()
    except ValueError as error:
        raise pytest.UsageError(str(error)) from error
    if config.stash[TARGET].url and config.getoption("numprocesses", None):
        # Workers would each run the probe and the state-change hook against the one server.
        raise pytest.UsageError("a remote server (NCI_SI_ACCEPTANCE_URL) is tested by one process")
    if config.getoption("controlled_run_dir") is not None:
        # Furnished opt-in keeps ordinary fixture installs independent of the core.
        from nci_si_acceptance.request_native import NativeRun  # noqa: PLC0415

        try:
            config.stash[NATIVE] = NativeRun(config, config.stash[TARGET])
        except ValueError as error:
            raise pytest.UsageError(str(error)) from None


def pytest_collection_finish(session: pytest.Session) -> None:
    if (native := session.config.stash.get(NATIVE, None)) is not None:
        try:
            session.config.stash[TARGET] = native.start(session)
        except ValueError as error:
            native.errors.append(error)
            raise pytest.UsageError(str(error)) from None
        except BaseException as error:
            native.errors.append(error)
            raise


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    target = config.stash[TARGET]
    active = select_registry_checks(items, target.registry_release == "required")
    if target.mode == "live":
        skip_fixture_only(active)
    if not target.has_index:
        skip_unprepared(active, NOT_DECLARED_PREPARED if target.url else NOT_PREPARED)
    if target.url and target.mode == "fixture":
        skip_remote_own_servers(active, target.state_hook is not None)
        if target.state_hook:
            order_for_state_changes(items)


@pytest.hookimpl(wrapper=True)
def pytest_sessionfinish(session: pytest.Session) -> Iterator[None]:
    native = session.config.stash.get(NATIVE, None)
    if native is None:
        yield
        target = session.config.stash[TARGET]
        session.config.stash[COLLECTOR].transport = target.transport
        write_report(session.config, target.mode)
        return
    native.before_teardown()
    try:
        yield
    except BaseException as error:
        native.errors.append(error)
        raise
    finally:
        native.complete(session)


def pytest_sessionstart(session: pytest.Session) -> None:
    selected = session.config.stash[TARGET].environment
    session.config.stash[COLLECTOR].environment = selected.to_record() if selected else None


@pytest.fixture(scope="session")
def target(pytestconfig: pytest.Config) -> Iterator[Target]:
    with pytestconfig.stash[TARGET].managed() as current:
        yield current


@pytest.fixture(scope="session")
def pinned() -> dict[str, str]:
    """The terminology and release the fixture set is pinned to, as a caller names them."""

    manifest = yaml.safe_load((FIXTURES / MANIFEST).read_text(encoding="utf-8"))
    terminology, _, release = manifest["evs"]["release"].partition("_")
    return {"terminology": terminology, "release": release}


@pytest.fixture
def content_pin(tools, target, pinned):
    """Recorded release for fixture content; the current monthly release for live content."""
    if target.mode == "fixture":
        return pinned
    result = tools.call("resolve_release", {"terminology": "ncit", "channel": "monthly"})
    assert not result.is_error, result.content
    assert result.content.get("terminology") == "ncit"
    version = result.content.get("version")
    assert isinstance(version, str) and version.strip(), result.content
    return {"terminology": "ncit", "release": version}


@pytest.fixture(scope="session")
def recorded() -> Callable[[str], Any]:
    """A fixture of the set by its name under fixtures/, recorded or crafted in memory: a test
    derives what it expects from the fixture, never from a copy of it. Each call returns a
    fresh copy."""

    documents = fixture_documents(FIXTURES)
    return lambda name: copy.deepcopy(documents[name])


@pytest.fixture(scope="session")
def upstream(target: Target) -> Iterator[FixtureServer | None]:
    """The fixture server in fixture mode; None in live mode."""

    if target.mode == "live":
        yield None
        return
    with FixtureServer(load_fixtures(FIXTURES), target.fixture_bind or ("127.0.0.1", 0)) as server:
        yield server


@pytest.fixture(scope="session", autouse=True)
def remote_ready(
    request: pytest.FixtureRequest,
    target: Target,
    upstream: FixtureServer | None,
    state_hook: StateHook | None,
) -> None:
    """Before any test, a remote server is told what to reach, and is found to answer and,
    against fixtures, to reach the fixture server; otherwise dependent tests fail. Where the
    operator gave a state-change hook, it runs first: a server answering from a pre-run cache
    asks the fixture server nothing and would fail the probe. What the server asks
    while the hook runs counts as reaching the fixture server."""

    if target.url is None:
        return
    if upstream:
        plugins = request.config.pluginmanager
        reporter = plugins.get_plugin("terminalreporter")
        # Captured output would swallow the lines whenever the interpreter writes unbuffered;
        # under `-p no:capture` there is no capture manager and nothing to disable.
        capture = plugins.get_plugin("capturemanager")
        with capture.global_and_fixture_disabled() if capture else nullcontext():
            for line in announcement(target, upstream):
                reporter.write_line(line)
    started = state_hook.apply((), {}, fresh=True) if state_hook else ()
    pinned = request.getfixturevalue("pinned")
    probe(target.url, target.authorization, upstream, pinned, started)


@pytest.fixture(scope="session")
def state_hook(
    target: Target, upstream: FixtureServer | None, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[StateHook | None]:
    """The operator's state-change hook, where there is one and a fixture server to set up."""

    if target.state_hook is None or upstream is None:
        yield None
        return
    hook = StateHook(target, upstream, tmp_path_factory.mktemp("state-hook") / "output.log")
    yield hook
    hook.restore()


@pytest.fixture(scope="session")
def prepared(
    target: Target, upstream: FixtureServer | None, tmp_path_factory: pytest.TempPathFactory
) -> Path | None:
    """The data directory the operator's prepare command produced, or None without one."""

    if target.prepare is None:
        return None
    data = tmp_path_factory.mktemp("prepared")
    codes = tmp_path_factory.mktemp("index") / "codes.txt"
    manifest = yaml.safe_load((FIXTURES / MANIFEST).read_text(encoding="utf-8"))
    codes.write_text("\n".join(index_set(manifest)) + "\n", encoding="utf-8")
    url = upstream.url if upstream else None
    environment = server_environment(
        target.mode, data, url, environment=target.environment, denied_url=target.denied_url
    ) | {INDEX_CODES_VARIABLE: str(codes)}
    if upstream:
        upstream.reset()
    # Fixture custom commands keep their shell semantics; live uses only harness-owned argv.
    command = target.prepare_command(index_set(manifest)) if target.environment else target.prepare
    ran = subprocess.run(  # noqa: S603 - fixture commands are explicit; live argv is validated.
        command,
        shell=target.environment is None,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    if ran.returncode:
        said = (ran.stdout + ran.stderr)[-2000:]
        pytest.fail(
            f"the prepare command failed with exit status {ran.returncode}:\n{said}", pytrace=False
        )
    if unmatched := unmatched_requests(_startup_requests(upstream)):
        raise UnmatchedUpstream(unmatched, " while preparing")
    return data


@pytest.fixture(scope="session")
def server(
    pytestconfig: pytest.Config,
    target: Target,
    upstream: FixtureServer | None,
    prepared: Path | None,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[Tools]:
    """The server under test, shared by every test that selects no scenario."""

    if target.url:
        with _remote_tools(target, upstream) as tools:
            pytestconfig.stash[COLLECTOR].note_tools(tools)
            yield tools
        return
    with _tools(target, upstream, tmp_path_factory, prepared) as tools:
        pytestconfig.stash[COLLECTOR].note_tools(tools)
        yield tools


def _make_tools(session: Session, upstream: FixtureServer | None, process: Process) -> Tools:
    counted = (lambda: len(upstream.log())) if upstream else None
    return Tools(session, process, counted)


@contextmanager
def _remote_tools(
    target: Target,
    upstream: FixtureServer | None,
    startup: tuple[dict[str, Any], ...] = (),
    *,
    stateful: bool = False,
) -> Iterator[Tools]:
    """A session with the remote server, which the harness neither started nor can read the
    standard error or data directory of."""

    with open_remote_session(target.url, target.authorization, stateful=stateful) as session:
        yield _make_tools(session, upstream, Process(None, None, startup))


@contextmanager
def _session_log(target: Target, path: Path) -> Iterator[tuple[TextIO, StderrTransfer | None]]:
    stream = path.open("w", encoding="utf-8")
    transfer = None
    try:
        if target.controller is not None:
            # Only a controlled caller transfers its diagnostic file.
            from nci_si_acceptance.request_stdio import StderrTransfer  # noqa: PLC0415

            transfer = StderrTransfer(stream)
        yield stream, transfer
    finally:
        if transfer is None or not transfer.accepted:
            stream.close()


@contextmanager
def _tools(
    target: Target,
    upstream: FixtureServer | None,
    tmp_path_factory: pytest.TempPathFactory,
    prepared: Path | None,
    settings: dict[str, str] | None = None,
) -> Iterator[Tools]:
    url = upstream.url if upstream else None
    if upstream:
        upstream.reset()  # what earlier tests left in the log is not this server's
    data = tmp_path_factory.mktemp("data")
    if prepared is not None:
        shutil.copytree(prepared, data, dirs_exist_ok=True)
    environment = server_environment(
        target.mode, data, url, environment=target.environment, denied_url=target.denied_url
    )
    if target.handoff is not None:
        environment[NATIVE_HANDOFF] = target.handoff
    log = tmp_path_factory.mktemp("server") / "stderr.log"
    try:
        with (
            _session_log(target, log) as (errlog, transfer),
            open_session(
                target.command,
                environment | (settings or {}),
                errlog,
                owner=target.controller,
                stderr_transfer=transfer,
            ) as session,
        ):
            startup = _startup_requests(upstream)
            if not (unmatched := unmatched_requests(startup)):
                yield _make_tools(session, upstream, Process(log, data, tuple(startup)))
                return
    finally:
        # The server's standard error, shown with a failing test's other output.
        sys.stderr.write(log.read_text(encoding="utf-8", errors="replace"))
    # Failing outside the session: inside it, the failure would reach pytest wrapped
    # in the session's exception group.
    raise UnmatchedUpstream(unmatched, " while the server started")


def _startup_requests(upstream: FixtureServer | None) -> list[dict[str, Any]]:
    """The requests the server made while it started; the log is reset."""

    if upstream is None:
        return []
    startup = upstream.log()
    upstream.reset()
    return startup


@pytest.fixture
def tools(
    request: pytest.FixtureRequest,
    target: Target,
    upstream: FixtureServer | None,
    prepared: Path | None,
    state_hook: StateHook | None,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[Tools]:
    """The required tools of the server under test, for one test.

    A scenario test, and one marked `own_server`, does not use the shared server: it runs
    against its own, which nothing an earlier test asked can have filled. A remote server is put
    in that state by the operator's hook: once for a scenario set's tests, for every
    `own_server` test.
    """

    scenarios = scenarios_of(request.node)
    unprepared = request.node.get_closest_marker(UNPREPARED) is not None
    stateful = request.node.get_closest_marker("mcp_session") is not None
    own = any(
        request.node.get_closest_marker(mark) for mark in (UNPREPARED, OWN_SERVER, "mcp_session")
    )
    # Live mode reuses the shared server despite unprepared; do not rely on this marker to
    # supply an index-less server.
    if not (scenarios or own) or upstream is None:
        yield request.getfixturevalue("server")
        return
    upstream.activate(*scenarios)
    settings = upstream.fixtures.settings_of(scenarios)
    try:
        if target.url:
            ours = _hooked_tools(
                target, upstream, state_hook, scenarios, settings, stateful=stateful
            )
        else:
            data = None if unprepared else prepared
            ours = _tools(target, upstream, tmp_path_factory, data, settings)
        with ours as its_own:
            request.config.stash[COLLECTOR].note_tools(its_own)
            yield its_own
    finally:
        upstream.activate()


@contextmanager
def _hooked_tools(
    target: Target,
    upstream: FixtureServer,
    state_hook: StateHook,
    scenarios: tuple[str, ...],
    settings: dict[str, str],
    *,
    stateful: bool = False,
) -> Iterator[Tools]:
    """The remote server as the operator's state-change hook leaves it for these scenarios (a
    server of its own, where there are none)."""

    started = state_hook.apply(scenarios, settings, fresh=not scenarios)
    if unmatched := unmatched_requests(started):
        raise UnmatchedUpstream(unmatched, " while the server started")
    with _remote_tools(target, upstream, started, stateful=stateful) as tools:
        yield tools


@pytest.fixture(autouse=True)
def upstream_log(request: pytest.FixtureRequest, upstream: FixtureServer | None) -> Iterator[None]:
    """Each test reads only the upstream requests it caused, and each had a fixture."""

    if upstream is None:
        yield
        return
    upstream.reset()
    yield
    unmatched = unmatched_requests(upstream.log())
    if unmatched and request.node.get_closest_marker(UNMATCHED_UPSTREAM) is None:
        raise UnmatchedUpstream(unmatched)
