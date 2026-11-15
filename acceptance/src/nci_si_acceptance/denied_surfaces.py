"""An owned write-only local refusal endpoint; no upstream request is forwarded."""

from __future__ import annotations

from socketserver import BaseRequestHandler, TCPServer
from threading import Event, Thread
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nci_si_acceptance.request_children import CleanupDeadline

BODY = b"surface disabled by run policy"
RESPONSE = (
    b"HTTP/1.0 403 Forbidden\r\nContent-Length: "
    + str(len(BODY)).encode()
    + b"\r\nConnection: close\r\n\r\n"
    + BODY
)
# A handler only writes this tiny fixed response. It never waits for client input.
WRITE_SECONDS = 1
POLL_SECONDS = 0.1
JOIN_SECONDS = 3


class _Refusal(BaseRequestHandler):
    def handle(self) -> None:
        self.request.settimeout(WRITE_SECONDS)
        self.request.sendall(RESPONSE)


class _Server(TCPServer):
    failed = False

    def handle_error(self, request: object, client_address: object) -> None:
        # socketserver's default prints exception/request details; do not echo input.
        self.failed = True


class DeniedSurfaces:
    """One listener per owning worker, enclosing prepare and all dependent servers."""

    def __init__(self) -> None:
        self._server = _Server(("127.0.0.1", 0), _Refusal)
        self._server.timeout = POLL_SECONDS
        self._stop = Event()
        self.thread = Thread(target=self._serve, name="acceptance-denied-surfaces")

    def _serve(self) -> None:
        try:
            while not self._stop.is_set():
                self._server.handle_request()
        except Exception as error:  # noqa: BLE001 - owner raises the sanitized thread failure.
            # Background failures are reported by the owner, never by a raw thread traceback.
            if not self._stop.is_set() or not isinstance(error, (OSError, ValueError)):
                self._server.failed = True
        finally:
            self._close()

    def _close(self) -> None:
        try:
            self._server.server_close()
        except OSError:
            self._server.failed = True

    def __enter__(self) -> str:
        try:
            self.thread.start()
        except OSError, RuntimeError:
            self._close()
            raise RuntimeError("owned refusal listener failed to start") from None
        except BaseException:
            self._close()
            raise
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def __exit__(self, *_exception: object) -> None:
        self.finish()

    def finish(self, deadline: CleanupDeadline | None = None) -> None:
        self._stop.set()
        try:
            self._close()
        finally:
            if self.thread.ident is not None:
                self.thread.join(JOIN_SECONDS if deadline is None else deadline.remaining())
        if self.thread.is_alive() or self._server.failed:
            raise RuntimeError("owned refusal listener did not finish cleanly")
