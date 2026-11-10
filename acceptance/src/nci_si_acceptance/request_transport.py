"""Explicit exact-grant HTTP transport for an already supervised diagnostic run."""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http import HTTPStatus
from http.client import HTTPConnection, HTTPException, HTTPResponse, HTTPSConnection
from urllib.parse import urlsplit
from urllib.request import Request

from nci_si_acceptance.request_authorization import RequestGrant, destination
from nci_si_acceptance.request_budget import AttemptLease, ControlledRun, LogicalRequest
from nci_si_mcp.bounds import check_deadline
from nci_si_mcp.request_policy import ControlledRequest, Exchange, TransportStart

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
        with self.logical.attempt(response_limit=max(response_limit, ERROR_BYTES)) as lease:
            scheme, host, port = destination(self.grant.url)
            connection_type = HTTPSConnection if scheme == "https" else HTTPConnection
            connection = connection_type(host, port, timeout=self.run.timeout(timeout_seconds))
            try:
                response = lease.transport(on_start, lambda: self._open(connection))
                try:
                    return self._read(response, lease, response_limit)
                finally:
                    response.close()
            except TimeoutError:
                raise TimeoutError("Controlled upstream transport timed out.") from None
            except HTTPException:
                raise HTTPException("Controlled upstream response framing failed.") from None
            except OSError:
                raise OSError("Controlled upstream transport failed.") from None
            finally:
                connection.close()

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
        policy_limit = self.run.authorization.limits.response_bytes
        limit = min(success_limit, policy_limit) + 1
        if response.status >= HTTPStatus.MULTIPLE_CHOICES:
            limit = min(ERROR_BYTES, policy_limit + 1)
        body = bytearray()
        while len(body) < limit:
            chunk = lease.read(lambda maximum: _read_chunk(response, maximum), limit - len(body))
            check_deadline()
            body.extend(chunk)
            if not chunk:
                break
        if len(body) > policy_limit:
            self.run.refuse("response_limit")
        if response.status < HTTPStatus.MULTIPLE_CHOICES and len(body) > success_limit:
            self.run.refuse("response_limit")
        return Exchange(response.status, _response_headers(response), bytes(body))


def _read_chunk(response: HTTPResponse, maximum: int) -> bytes:
    try:
        return response.read(maximum)
    except HTTPException:
        # The lease must retain unknown progress before a core retry can be considered.
        raise OSError("Controlled upstream response read failed.") from None
