"""Explicit owned MCP transport; only the outside owner can certify its thread join."""

from __future__ import annotations

import subprocess
import threading
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from contextlib import asynccontextmanager, contextmanager
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
from nci_si_mcp.errors import RequestPolicyError

if TYPE_CHECKING:
    from nci_si_acceptance.request_children import CleanupDeadline
    from nci_si_acceptance.request_supervision import SupervisedRun

type Streams = tuple[
    MemoryObjectReceiveStream[SessionMessage | Exception], MemoryObjectSendStream[SessionMessage]
]

# Preserve the SDK's two-second normal exit and TERM grace within the one run deadline.
EXIT_SECONDS = 2.0


class StdioOwner:
    def __init__(
        self,
        controller: SupervisedRun,
        command: list[str],
        environment: dict[str, str],
        errlog: TextIO,
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
        self.closed: set[str] = set()
        self.pumps: set[str] = set()
        self.waited = self.pumps_settled = False
        self._read_send, self.read = anyio.create_memory_object_stream[SessionMessage | Exception](
            0
        )
        self.write, self._write_receive = anyio.create_memory_object_stream[SessionMessage](0)
        self._stop, self._client_stop = anyio.Event(), anyio.Event()
        self.thread = threading.Thread(target=self._thread_main, name=self.name)

    def _thread_main(self) -> None:
        try:
            anyio.run(self._main)
        except BaseException as error:  # noqa: BLE001 - outside owner re-raises thread failures.
            self.errors.append(error)
            for ready in (self.portal_ready, self.client_ready):
                if not ready.done():
                    ready.set_exception(error)

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
                await portal.stop(cancel_remaining=True)

    async def _io_scope(self, portal: BlockingPortal) -> None:
        try:
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(self._pump, "stdout", self._stdout)
                tasks.start_soon(self._pump, "stdin", self._stdin)
                tasks.start_soon(self._pump, "stderr", self._stderr)
                self.portal_ready.set_result(portal)
                try:
                    await self._stop.wait()
                finally:
                    with anyio.CancelScope(shield=True):
                        await self._cleanup_process()
                    tasks.cancel_scope.cancel()
        finally:
            # TaskGroup exit and the outside thread join additionally prove actual settlement.
            self.pumps_settled = self.pumps == {"stdin", "stdout", "stderr"}

    def _process(self) -> Process:
        if self.process is None:
            raise RequestPolicyError("accounting")
        return self.process

    async def _pump(self, name: str, action: Callable[[], Awaitable[None]]) -> None:
        try:
            await action()
        except anyio.ClosedResourceError, anyio.BrokenResourceError:
            if not self._stop.is_set():
                self.errors.append(RequestPolicyError("transport"))
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
                await self._client_stop.wait()
        except BaseException as error:
            if not self.client_ready.done():
                self.client_ready.set_exception(error)
            if not (self._stop.is_set() and isinstance(error, anyio.get_cancelled_exc_class())):
                self.errors.append(error)
            raise

    async def _end_client(self) -> None:
        self._client_stop.set()

    async def _end_run(self) -> None:
        self._client_stop.set()
        self._stop.set()

    async def _step(
        self, action: Callable[[], Awaitable[object]], deadline: CleanupDeadline
    ) -> None:
        try:
            with anyio.fail_after(deadline.remaining()):
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

    async def _wait(self, seconds: float, deadline: CleanupDeadline) -> bool:
        try:
            with anyio.fail_after(min(seconds, deadline.remaining())):
                code = await self._process().wait()
            return code == self._process().returncode
        except TimeoutError:
            return False

    async def _cleanup_process(self) -> None:
        deadline = self.controller.cleanup_deadline()
        await self._step(lambda: self._close("stdin"), deadline)
        if not await self._wait(EXIT_SECONDS, deadline):
            self._signal(kill=False)
            if not await self._wait(EXIT_SECONDS, deadline):
                self._signal(kill=True)
        self.waited = await self._wait(deadline.remaining(), deadline)
        for name in ("stdout", "stderr"):
            await self._step(lambda name=name: self._close(name), deadline)
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
        try:
            portal = self.portal_ready.result(timeout=deadline.remaining())
            portal.start_task_soon(self._end_run)
        except FutureTimeout, RuntimeError:
            self.errors.append(RequestPolicyError("accounting"))
        finally:
            if self.thread.ident is not None:
                self.thread.join(deadline.remaining())
        if self.thread.is_alive():
            self._signal(kill=True)
            raise RequestPolicyError("accounting")
        if self.errors:
            raise BaseExceptionGroup("Controlled stdio cleanup failed.", self.errors)
        if not all((self.waited, self.pumps_settled, self.closed == {"stdin", "stdout", "stderr"})):
            raise RequestPolicyError("accounting")


@contextmanager
def controlled_session(
    owner: SupervisedRun,
    command: list[str],
    environment: dict[str, str],
    errlog: TextIO,
    timeout: float | None,
) -> Iterator[Session]:
    resource = StdioOwner(owner, command, environment, errlog)
    owner.register_completion(resource, participant="stdio")
    resource.thread.start()
    seconds = READ_TIMEOUT_SECONDS if timeout is None else timeout
    portal = resource.portal_ready.result(timeout=seconds)
    portal.start_task_soon(resource._client, seconds)
    client = resource.client_ready.result(timeout=seconds)
    try:
        yield Session(portal, client)
    finally:
        portal.start_task_soon(resource._end_client)
