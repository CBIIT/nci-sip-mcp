"""Explicit owned MCP transport; only the outside owner can certify its thread join."""

from __future__ import annotations

import subprocess
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from contextlib import asynccontextmanager, contextmanager, nullcontext
from contextvars import copy_context
from typing import TYPE_CHECKING, TextIO

import anyio
from anyio.abc import Process
from anyio.from_thread import BlockingPortal
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from anyio.streams.text import TextReceiveStream
from mcp import types
from mcp.client import Client, Transport
from mcp.shared.message import SessionMessage

from nci_si_acceptance.client import READ_TIMEOUT_SECONDS, Session
from nci_si_acceptance.request_children import Completions
from nci_si_mcp.errors import RequestPolicyError

if TYPE_CHECKING:
    from nci_si_acceptance.request_children import CleanupDeadline
    from nci_si_acceptance.request_supervision import SupervisedRun

type Streams = tuple[
    MemoryObjectReceiveStream[SessionMessage | Exception], MemoryObjectSendStream[SessionMessage]
]

# Preserve the SDK's two-second normal exit and TERM grace within the one run deadline.
EXIT_SECONDS = 2.0
# Poll explicit thread-safe stop signals promptly; actual joins, not elapsed polls, prove exit.
STOP_POLL_SECONDS = 0.01


class _PortalCall[T]:
    def __init__(self, operation: Callable[[], T]) -> None:
        self.operation = operation
        self.result: Future[T] = Future()
        self.started: Future[None] = Future()
        self.name = "controlled-portal-call"
        self.thread = threading.Thread(target=self._invoke, name=self.name, context=copy_context())

    def _invoke(self) -> None:
        try:
            self.result.set_result(self.operation())
        except BaseException as error:  # noqa: BLE001 - caller receives the original event.
            self.result.set_exception(error)

    def start(self) -> None:
        try:
            self.thread.start()
        except BaseException as error:
            self.started.set_exception(error)
            raise
        self.started.set_result(None)

    def finish(self, deadline: CleanupDeadline) -> None:
        try:
            self.started.result(timeout=deadline.remaining())
        finally:
            if self.thread.ident is not None:
                self.thread.join(deadline.remaining())
        if self.thread.is_alive():
            raise RequestPolicyError("accounting")


class _OwnedSession(Session):
    def __init__(self, resource: StdioOwner, portal: BlockingPortal, client: Client) -> None:
        super().__init__(portal, client)
        self._resource = resource

    def _call[T](self, operation: Callable[[], Awaitable[T]]) -> T:
        return self._resource.call(lambda: self._portal.call(operation))


class StdioOwner:
    def __init__(
        self,
        controller: SupervisedRun,
        command: list[str],
        environment: dict[str, str],
        errlog: TextIO,
        *,
        read_seconds: float = READ_TIMEOUT_SECONDS,
    ) -> None:
        self.controller, self.command, self.environment, self.errlog = (
            controller,
            tuple(command),
            dict(environment),
            errlog,
        )
        self.name = "controlled-stdio"
        self.process: Process | None = None
        self.portal_ready: Future[BlockingPortal] = Future()
        self.client_ready: Future[Client] = Future()
        self.errors: list[BaseException] = []
        self.initiating: BaseException | None = None
        self.read_seconds = read_seconds
        self._calls = Completions(
            controller.run.descriptor.run_id, controller.run.descriptor.policy_sha256
        )
        self.closed: set[str] = set()
        self.pumps: set[str] = set()
        self.waited = self.pumps_settled = self.process_closed = False
        self._read_send, self.read = anyio.create_memory_object_stream[SessionMessage | Exception](
            0
        )
        self.write, self._write_receive = anyio.create_memory_object_stream[SessionMessage](0)
        self._stop, self._client_stop = threading.Event(), threading.Event()
        self._cleanup_started = self._pumps_started = False
        self.thread = threading.Thread(target=self._thread_main, name=self.name)

    def _thread_main(self) -> None:
        try:
            anyio.run(self._main)
        except BaseException as error:  # noqa: BLE001 - outside owner re-raises thread failures.
            self._initiate(error)
            for ready in (self.portal_ready, self.client_ready):
                if not ready.done():
                    ready.set_exception(error)

    def _initiate(self, error: BaseException) -> None:
        if self.initiating is None:
            self.initiating = error
            self.controller._record_failure(error, "transport")

    async def _main(self) -> None:
        async with BlockingPortal() as portal:
            try:
                # Custody exists before launch; no cancellation checkpoint can lose the handle.
                with anyio.CancelScope(shield=True):
                    self.process = await anyio.open_process(
                        self.command,
                        env=self.environment,
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                    )
                await self._io_scope(portal)
            finally:
                with anyio.CancelScope(shield=True):
                    if self.process is not None and not self._cleanup_started:
                        await self._cleanup_process()
                    if not self._pumps_started:
                        self.pumps_settled = True  # No pump task was created before this failure.
                    await portal.stop(cancel_remaining=True)

    async def _io_scope(self, portal: BlockingPortal) -> None:
        try:
            async with anyio.create_task_group() as tasks:
                self._pumps_started = True
                tasks.start_soon(self._pump, "stdout", self._stdout)
                tasks.start_soon(self._pump, "stdin", self._stdin)
                tasks.start_soon(self._pump, "stderr", self._stderr)
                tasks.start_soon(self._client, self.read_seconds)
                self.portal_ready.set_result(portal)
                try:
                    await self._wait_signal(self._stop)
                finally:
                    with anyio.CancelScope(shield=True):
                        await self._cleanup_process()
                    tasks.cancel_scope.cancel()
        finally:
            # TaskGroup exit and the outside thread join additionally prove actual settlement.
            self.pumps_settled = self.pumps == {"stdin", "stdout", "stderr"}

    @staticmethod
    async def _wait_signal(event: threading.Event) -> None:
        # A cross-thread stop must never wait for event-loop acknowledgement.
        while not event.is_set():  # noqa: ASYNC110
            await anyio.sleep(STOP_POLL_SECONDS)

    def _process(self) -> Process:
        if self.process is None:
            raise RequestPolicyError("accounting")
        return self.process

    async def _pump(self, name: str, action: Callable[[], Awaitable[None]]) -> None:
        try:
            await action()
        except anyio.ClosedResourceError, anyio.BrokenResourceError:
            if not self._stop.is_set():
                self._initiate(RequestPolicyError("transport"))
        except BaseException as error:
            if not (self._stop.is_set() and isinstance(error, anyio.get_cancelled_exc_class())):
                self._initiate(error)
            raise
        finally:
            self.pumps.add(name)

    async def _stdout(self) -> None:
        stream = self._process().stdout
        if stream is None:
            raise RequestPolicyError("accounting")
        discard = False
        async with self._read_send:
            buffer = ""
            async for chunk in TextReceiveStream(stream, encoding="utf-8", errors="strict"):
                lines = (buffer + chunk).split("\n")
                buffer = lines.pop()
                for line in lines:
                    if not discard:
                        discard = not await self._deliver(line)

    async def _deliver(self, line: str) -> bool:
        try:
            message: SessionMessage | Exception = SessionMessage(
                types.jsonrpc_message_adapter.validate_json(line, by_name=False)
            )
        except ValueError as error:
            message = error
        try:
            await self._read_send.send(message)
            return True
        except anyio.ClosedResourceError, anyio.BrokenResourceError:
            return False  # Drain the owned pipe after its session consumer has closed.

    async def _stdin(self) -> None:
        stream = self._process().stdin
        if stream is None:
            raise RequestPolicyError("accounting")
        async with self._write_receive:
            async for message in self._write_receive:
                wire = message.message.model_dump_json(by_alias=True, exclude_unset=True)
                await stream.send((wire + "\n").encode("utf-8"))

    async def _stderr(self) -> None:
        stream = self._process().stderr
        if stream is None:
            raise RequestPolicyError("accounting")
        async for text in TextReceiveStream(stream, encoding="utf-8", errors="replace"):
            self.errlog.write(text)

    @asynccontextmanager
    async def _transport(self) -> AsyncIterator[Streams]:
        yield self.read, self.write

    async def _client(self, read_seconds: float) -> None:
        transport: Transport = self._transport()
        try:
            async with Client(transport, cache=None, read_timeout_seconds=read_seconds) as client:
                self.client_ready.set_result(client)
                await self._wait_signal(self._client_stop)
        except BaseException as error:
            if not self.client_ready.done():
                self.client_ready.set_exception(error)
            if not (self._stop.is_set() and isinstance(error, anyio.get_cancelled_exc_class())):
                self._initiate(error)
            raise

    def call[T](self, operation: Callable[[], T]) -> T:
        seconds = self.controller.run.timeout(self.read_seconds)
        end = time.monotonic() + seconds
        call = _PortalCall(operation)
        self._calls.register(call, "stdio-rpc")
        call.start()
        try:
            return call.result.result(timeout=max(0.0, end - time.monotonic()))
        except FutureTimeout:
            error = RequestPolicyError("deadline")
            self.controller._record_failure(error, "deadline")
            raise error from None

    async def _step(
        self, action: Callable[[], Awaitable[object]], deadline: CleanupDeadline | None
    ) -> None:
        try:
            with nullcontext() if deadline is None else anyio.fail_after(deadline.remaining()):
                await action()
        except OSError, RuntimeError, TimeoutError:
            self.errors.append(RequestPolicyError("accounting"))
        except BaseException as error:  # noqa: BLE001 - outside owner re-raises cleanup failures.
            self.errors.append(error)

    async def _close(self, name: str) -> None:
        process = self._process()
        stream = {"stdin": process.stdin, "stdout": process.stdout, "stderr": process.stderr}[name]
        if stream is None:
            raise RequestPolicyError("accounting")
        await stream.aclose()
        self.closed.add(name)

    async def _close_process(self) -> None:
        # Stream wrappers alone do not close asyncio's underlying read-pipe transports.
        # This public closer shields cancellation; only the outside thread join bounds it.
        await self._process().aclose()
        self.process_closed = True

    async def _wait(self, seconds: float, deadline: CleanupDeadline) -> bool:
        try:
            with anyio.fail_after(min(seconds, deadline.remaining())):
                code = await self._process().wait()
            return code == self._process().returncode
        except TimeoutError:
            return False
        except BaseException as error:  # noqa: BLE001 - preserve cleanup failure and keep stopping.
            self.errors.append(error)
            return False

    async def _cleanup_process(self) -> None:
        self._cleanup_started = True
        deadline = self.controller.cleanup_deadline()
        self._client_stop.set()
        self._stop.set()
        try:
            self.controller._close_admission()
        except BaseException as error:  # noqa: BLE001 - preserve closure and independent cleanup errors.
            self.errors.append(error)
        await self._step(lambda: self._close("stdin"), deadline)
        if not await self._wait(EXIT_SECONDS, deadline):
            self._signal(kill=False)
            if not await self._wait(EXIT_SECONDS, deadline):
                self._signal(kill=True)
        self.waited = await self._wait(deadline.remaining(), deadline)
        for name in ("stdout", "stderr"):
            await self._step(lambda name=name: self._close(name), deadline)
        # Its shielded wait cannot honor an inner timeout; outside finish retains custody.
        await self._step(self._close_process, None)
        for stream in (self.read, self.write, self._read_send, self._write_receive):
            await self._step(stream.aclose, deadline)

    def _signal(self, *, kill: bool) -> None:
        process = self.process
        if process is not None and process.returncode is None:
            try:
                process.kill() if kill else process.terminate()
            except OSError:
                self.errors.append(RequestPolicyError("accounting"))

    def finish(self, deadline: CleanupDeadline) -> None:
        self._client_stop.set()
        self._stop.set()
        calls = self._calls.seal()
        if self.thread.ident is not None:
            self.thread.join(deadline.remaining())
        self.errors.extend(
            self.controller._collect(
                lambda token=token: self._calls.settle(token, deadline) for token in calls
            )
        )
        if self.thread.is_alive():
            self._signal(kill=True)
            raise RequestPolicyError("accounting")
        if self.errors:
            raise BaseExceptionGroup("Controlled stdio cleanup failed.", self.errors)
        if not all(
            (
                self.waited,
                self.pumps_settled,
                self.process_closed,
                self.closed == {"stdin", "stdout", "stderr"},
            )
        ):
            raise RequestPolicyError("accounting")


@contextmanager
def controlled_session(
    owner: SupervisedRun,
    command: list[str],
    environment: dict[str, str],
    errlog: TextIO,
    timeout: float | None,
) -> Iterator[Session]:
    seconds = owner.run.timeout(READ_TIMEOUT_SECONDS if timeout is None else timeout)
    resource = StdioOwner(owner, command, environment, errlog, read_seconds=seconds)
    try:
        owner.register_completion(resource, participant="stdio")
    except BaseException:
        for stream in (resource.read, resource.write, resource._read_send, resource._write_receive):
            stream.close()
        raise
    end = time.monotonic() + seconds
    resource.thread.start()
    portal = resource.portal_ready.result(timeout=max(0.0, end - time.monotonic()))
    client = resource.client_ready.result(timeout=max(0.0, end - time.monotonic()))
    try:
        yield _OwnedSession(resource, portal, client)
    finally:
        resource._client_stop.set()
