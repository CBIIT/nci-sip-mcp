"""Explicit exact-grant HTTP transport for an already supervised diagnostic run."""

import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http import HTTPStatus
from http.client import HTTPConnection, HTTPException, HTTPResponse, HTTPSConnection
from urllib.parse import urlsplit
from urllib.request import Request

from nci_si_acceptance.request_authorization import RequestGrant, destination
from nci_si_acceptance.request_budget import AttemptLease, ControlledRun, LogicalRequest
from nci_si_mcp.bounds import CallDeadlineError, RequestBudgetError, check_deadline
from nci_si_mcp.http_client import RequestPolicyError
from nci_si_mcp.request_policy import ControlledRequest, Exchange, TransportStart
from nci_si_mcp.validation import PolicyReason

# These fields belong to direct transport framing, not caller-selected request content.
TRANSPORT_HEADERS = frozenset(
    (
        "host",
        "content-length",
        "transfer-encoding",
        "connection",
        "proxy-authorization",
        "proxy-connection",
        "expect",
        "upgrade",
        "te",
        "trailer",
        "accept-encoding",
        "cookie",
    )
)
# Core error classification consumes a bounded prefix, independently of the success limit.
ERROR_BYTES = 4096


class TransportCleanupError(RequestPolicyError):
    """Fixed public refusal with separately retained sanitized expected failures."""

    def __init__(
        self,
        reason: PolicyReason,
        initiating: Exception | None,
        cleanup_errors: tuple[Exception, ...],
    ) -> None:
        super().__init__(reason)
        self.initiating, self.cleanup_errors = initiating, cleanup_errors


def _sanitized(error: BaseException) -> Exception | None:
    if isinstance(error, (RequestPolicyError, CallDeadlineError, RequestBudgetError)):
        return error
    if isinstance(error, TimeoutError):
        return TimeoutError("Controlled upstream transport timed out.")
    if isinstance(error, HTTPException):
        return HTTPException("Controlled upstream response framing failed.")
    if isinstance(error, OSError):
        return OSError("Controlled upstream transport failed.")
    return None


def _close(resources: list[Callable[[], object]]) -> list[BaseException]:
    failures: list[BaseException] = []
    for close in reversed(resources):
        try:
            close()
        except BaseException as error:  # noqa: BLE001 - retain every cleanup failure; never discard bugs
            # Cleanup ownership requires attempting every resource, including on interruption.
            failures.append(error)
    return failures


def _cleanup_expected(error: BaseException) -> Exception:
    if isinstance(error, RequestPolicyError):
        return RequestPolicyError(error.reason)
    return OSError("Controlled upstream transport cleanup failed.")


def _unexpected(initiating: BaseException | None, failures: list[BaseException]) -> bool:
    errors = ([initiating] if initiating is not None else []) + failures
    return any(_sanitized(error) is None for error in errors)


def _cleanup_failure(
    run: ControlledRun, initiating: BaseException | None, failures: list[BaseException]
) -> None:
    original = _sanitized(initiating) if initiating else None
    if _unexpected(initiating, failures):
        raise BaseExceptionGroup(
            "Controlled transport and cleanup failed.",
            ([initiating] if initiating is not None else []) + failures,
        ) from None
    reason = original.reason if isinstance(original, RequestPolicyError) else "transport"
    cleanup = tuple(map(_cleanup_expected, failures))
    try:
        run.refuse(reason)
    except RequestPolicyError as refusal:
        raise TransportCleanupError(refusal.reason, original, cleanup) from None


@contextmanager
def _resources(run: ControlledRun) -> Iterator[list[Callable[[], object]]]:
    resources: list[Callable[[], object]] = []
    initiating: BaseException | None = None
    try:
        yield resources
    except BaseException as error:
        initiating = error
        raise
    finally:
        failures = _close(resources)
        if failures:
            _cleanup_failure(run, initiating, failures)


def _request_headers(request: Request) -> tuple[tuple[str, str], ...]:
    return tuple(
        (name.lower(), value)
        for fields in (request.headers, request.unredirected_hdrs)
        for name, value in fields.items()
    )


def _response_headers(response: HTTPResponse) -> dict[str, str]:
    headers: dict[str, str] = {}
    for name, value in response.getheaders():
        headers.setdefault(name.lower(), value)
    return headers


class ControlledHttpPolicy:
    """Opt-in policy; construction neither opens admission nor adopts any caller."""

    def __init__(self, run: ControlledRun) -> None:
        self.run = run

    def _grant(self, request: Request) -> RequestGrant:
        pairs = _request_headers(request)
        names = [name for name, _ in pairs]
        if len(set(names)) != len(names) or TRANSPORT_HEADERS.intersection(names):
            self.run.refuse("authorization")
        headers = tuple(sorted(pairs))
        for grant in self.run.authorization.grants:
            if (request.get_method(), request.full_url, headers, request.data) == (
                grant.method,
                grant.url,
                grant.headers,
                grant.body,
            ):
                return grant
        self.run.refuse("authorization")

    @contextmanager
    def request(self, request: Request) -> Iterator[ControlledRequest]:
        grant = self._grant(request)
        with self.run.logical(grant) as logical:
            yield _Request(self.run, logical, grant)


@dataclass(frozen=True, slots=True)
class _Request:
    run: ControlledRun = field(repr=False)
    logical: LogicalRequest = field(repr=False)
    grant: RequestGrant = field(repr=False)

    def check_wait(self, delay_seconds: float) -> None:
        self.run.check_wait(delay_seconds)

    def exchange(
        self, *, timeout_seconds: float, response_limit: int, on_start: TransportStart
    ) -> Exchange:
        with _resources(self.run) as cleanup:
            admission = self.logical.attempt(response_limit=max(response_limit, ERROR_BYTES))
            lease = admission.__enter__()
            cleanup.append(lambda: admission.__exit__(None, None, None))
            scheme, host, port = destination(self.grant.url)
            connection_type = HTTPSConnection if scheme == "https" else HTTPConnection
            connection = connection_type(host, port, timeout=self.run.timeout(timeout_seconds))
            cleanup.append(connection.close)
            try:
                response = lease.transport(on_start, lambda: self._open(connection))
                cleanup.append(response.close)
                return self._read(response, lease, response_limit)
            except TimeoutError:
                raise TimeoutError("Controlled upstream transport timed out.") from None
            except HTTPException:
                raise HTTPException("Controlled upstream response framing failed.") from None
            except OSError:
                raise OSError("Controlled upstream transport failed.") from None

    def _open(self, connection: HTTPConnection) -> HTTPResponse:
        parts = urlsplit(self.grant.url)
        target = parts.path or "/"
        if parts.query:
            target += "?" + parts.query
        connection.putrequest(self.grant.method, target, skip_accept_encoding=True)
        for name, value in self.grant.headers:
            connection.putheader(name, value)
        connection.putheader("Accept-Encoding", "identity")
        connection.putheader("Connection", "close")
        if self.grant.body is not None:
            connection.putheader("Content-Length", str(len(self.grant.body)))
        connection.endheaders(self.grant.body)
        return connection.getresponse()

    def _read(self, response: HTTPResponse, lease: AttemptLease, success_limit: int) -> Exchange:
        self._framing(response)
        policy_limit = self.run.authorization.limits.response_bytes
        limit = min(success_limit, policy_limit) + 1
        if response.status >= HTTPStatus.MULTIPLE_CHOICES:
            limit = min(ERROR_BYTES, policy_limit + 1)
        body = _body(response, lease, limit)
        if len(body) > policy_limit:
            self.run.refuse("response_limit")
        if response.status < HTTPStatus.MULTIPLE_CHOICES and len(body) > success_limit:
            self.run.refuse("response_limit")
        return _exchange(response, bytes(body), limit)

    def _framing(self, response: HTTPResponse) -> None:
        lengths = response.headers.get_all("Content-Length", [])
        transfers = response.headers.get_all("Transfer-Encoding", [])
        encodings = response.headers.get_all("Content-Encoding", [])
        # A present declaration rejected by the HTTP parser must not become EOF framing.
        if lengths and response.length is None:
            self.run.refuse("transport")
        if lengths and transfers:
            self.run.refuse("transport")
        _one_field(self.run, lengths, r"[0-9]+")
        _one_field(self.run, transfers, "chunked")
        _one_field(self.run, encodings, "identity")


def _one_field(run: ControlledRun, values: list[str], pattern: str) -> None:
    if len(values) > 1:
        run.refuse("transport")
    if values and re.fullmatch(pattern, values[0].strip().lower()) is None:
        run.refuse("transport")


def _exchange(response: HTTPResponse, body: bytes, limit: int) -> Exchange:
    if response.length and len(body) < limit:
        if response.status >= HTTPStatus.BAD_REQUEST:
            return Exchange(response.status, _response_headers(response), body, "unreadable")
        raise HTTPException("Controlled upstream response framing failed.")
    return Exchange(response.status, _response_headers(response), body)


def _body(response: HTTPResponse, lease: AttemptLease, limit: int) -> bytearray:
    body = bytearray()
    while len(body) < limit:
        chunk = lease.read(lambda maximum: _read_chunk(response, maximum), limit - len(body))
        check_deadline()
        body.extend(chunk)
        if not chunk:
            break
    return body


def _read_chunk(response: HTTPResponse, maximum: int) -> bytes:
    try:
        return response.read(maximum)
    except HTTPException:
        # The lease must retain unknown progress before a core retry can be considered.
        raise OSError("Controlled upstream response read failed.") from None
