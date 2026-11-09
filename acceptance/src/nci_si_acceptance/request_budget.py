"""Explicit atomic accounting; callers own children, transport and private evidence custody."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import sqlite3
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn, Self, get_args

from nci_si_acceptance.request_authorization import (
    MAX_COUNTER,
    Authorization,
    Limits,
    RequestGrant,
    RunDescriptor,
)
from nci_si_acceptance.request_summary import read_summary
from nci_si_mcp.http_client import RequestPolicyError
from nci_si_mcp.validation import LedgerState, PolicyReason

# Bound cooperative contention, not filesystem or operating-system scheduling latency.
BUSY_SECONDS = 0.1
KEY_BYTES = 32
# Observe cooperative owner loss within two seconds, allowing one missed one-second pulse.
STALE_SECONDS = 2.0
STATES: dict[str, LedgerState] = {value: value for value in get_args(LedgerState.__value__)}
REASONS: dict[str, PolicyReason] = {value: value for value in get_args(PolicyReason.__value__)}


def _clock() -> float:
    try:
        now = time.monotonic()
    except OSError:
        raise RequestPolicyError("accounting") from None
    if type(now) is not float or not math.isfinite(now):
        raise RequestPolicyError("accounting") from None
    return now


def _participant(value: str) -> None:
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9._-]{1,64}", value) is None:
        raise RequestPolicyError("authorization")


def _authorization(value: Authorization) -> None:
    if type(value) is not Authorization:
        raise RequestPolicyError("authorization")


@dataclass(frozen=True, slots=True)
class LedgerSnapshot:
    run_id: str
    policy_sha256: str
    state: LedgerState
    logical: int
    attempts: int
    reserved_body_bytes: int
    transport_entries: int
    consumed_body_bytes: int
    active: int
    peak_active: int
    pending_receipts: int
    observation_complete: bool
    failure: PolicyReason | None


class ControlledRun:
    def __init__(self, descriptor: RunDescriptor, authorization: Authorization, participant: str):
        _authorization(authorization)
        if type(descriptor) is not RunDescriptor:
            raise RequestPolicyError("authorization")
        _participant(participant)
        self.descriptor, self.authorization, self.participant = (
            descriptor,
            authorization,
            participant,
        )
        self.directory = descriptor.directory
        self.started = _clock()
        if not math.isfinite(self.started + authorization.limits.seconds):
            raise RequestPolicyError("accounting") from None
        self._owner = False
        self._failed: PolicyReason | None = None

    @classmethod
    def create(cls, authorization: Authorization, owned_directory: Path) -> Self:
        _authorization(authorization)
        if not isinstance(owned_directory, Path):
            raise RequestPolicyError("authorization")
        try:
            directory = owned_directory.resolve() / uuid.uuid4().hex
            directory.mkdir(mode=0o700, parents=True)
            descriptor = RunDescriptor(directory.name, authorization.policy_sha256, directory)
            run = cls(descriptor, authorization, "controller")
            run._owner = True
            key = os.urandom(KEY_BYTES)
            with (directory / "key").open("xb") as stream:
                os.chmod(stream.name, 0o600)
                stream.write(key)
            run._initialize(key)
            return run
        except OSError, sqlite3.Error, OverflowError, ValueError:
            raise RequestPolicyError("accounting") from None

    def _binding(self, key: bytes, started: float, supervised: int = 0) -> bytes:
        # Bind the recorded monotonic origin as well as the complete private authorization.
        identity = json.dumps(
            [self.descriptor.run_id, started, supervised], separators=(",", ":")
        ).encode()
        return hmac.digest(key, self.authorization.binding(key) + identity, hashlib.sha256)

    def _initialize(self, key: bytes) -> None:
        with closing(sqlite3.connect(self.directory / "ledger.sqlite")) as connection, connection:
            connection.executescript("""
                CREATE TABLE run (
                    id TEXT, policy TEXT, binding BLOB, started REAL, state TEXT,
                    logical INTEGER, attempts INTEGER, active INTEGER, peak INTEGER,
                    reserved INTEGER, consumed INTEGER, entered INTEGER
                ) STRICT;
                CREATE TABLE workers (name TEXT PRIMARY KEY) STRICT;
                CREATE TABLE supervision (enabled INTEGER, heartbeat REAL) STRICT;
                INSERT INTO supervision VALUES (0, NULL);
                CREATE TABLE requests (
                    id TEXT PRIMARY KEY, participant TEXT, attempts INTEGER
                ) STRICT;
                CREATE TABLE leases (
                    id TEXT PRIMARY KEY, request TEXT, participant TEXT,
                    allowance INTEGER, consumed INTEGER, pending INTEGER, entered INTEGER,
                    retired INTEGER
                ) STRICT;
            """)
            connection.execute(
                "INSERT INTO run VALUES (?,?,?,?, 'created',0,0,0,0,0,0,0)",
                (
                    self.descriptor.run_id,
                    self.descriptor.policy_sha256,
                    self._binding(key, self.started),
                    self.started,
                ),
            )
            connection.execute("INSERT INTO workers VALUES (?)", (self.participant,))
        os.chmod(self.directory / "ledger.sqlite", 0o600)

    @classmethod
    def attach(
        cls, descriptor: RunDescriptor, authorization: Authorization, *, participant: str
    ) -> Self:
        if participant == "controller":
            raise RequestPolicyError("authorization")
        run = cls(descriptor, authorization, participant)
        with run._connection() as connection:
            run._guard(connection)
        return run

    def _identity(self, connection: sqlite3.Connection) -> None:
        rows = connection.execute("SELECT * FROM run LIMIT 2").fetchall()
        if len(rows) != 1:
            self._deny("accounting")
        row = rows[0]
        key = self._key()
        started = float(row["started"])
        if not math.isfinite(started) or not math.isfinite(
            started + self.authorization.limits.seconds
        ):
            self._deny("authorization")
        identity = (row["id"], row["policy"], self.descriptor.policy_sha256)
        expected = (
            self.descriptor.run_id,
            self.authorization.policy_sha256,
            self.authorization.policy_sha256,
        )
        if identity != expected:
            self._deny("authorization")
        supervised, _heartbeat = self._supervision(connection)
        if not hmac.compare_digest(row["binding"], self._binding(key, started, supervised)):
            self._deny("authorization")
        if (
            connection.execute(
                "SELECT name FROM workers WHERE name=?", (self.participant,)
            ).fetchone()
            is None
        ):
            self._deny("authorization")
        self.started = started

    def _key(self) -> bytes:
        with (self.directory / "key").open("rb") as stream:
            key = stream.read(KEY_BYTES + 1)
        if len(key) != KEY_BYTES:
            self._deny("authorization")
        return key

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = None
        try:
            try:
                remaining = self.started + self.authorization.limits.seconds - _clock()
            except RequestPolicyError:
                # Close admission, but allow identity-valid receipts without waiting.
                self._mark_failed("accounting")
                remaining = 0.0
            connection = sqlite3.connect(
                (self.directory / "ledger.sqlite").as_uri() + "?mode=rw",
                uri=True,
                timeout=max(0, min(BUSY_SECONDS, remaining)),
            )
            connection.row_factory = sqlite3.Row
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                self._identity(connection)
                yield connection
        except sqlite3.Error, OSError, ValueError, TypeError, OverflowError:
            self._deny("accounting")
        finally:
            if connection is not None:
                connection.close()

    def _mark_failed(self, reason: PolicyReason) -> bool:
        self._failed = reason
        try:
            with (self.directory / "failed").open("x") as stream:
                stream.write(reason)
            return True
        except FileExistsError:
            # An existing marker already closes admission; do not replace it.
            return False
        except OSError:
            # This participant still refuses; inability to publish never authorizes traffic.
            raise RequestPolicyError("accounting") from None

    def _deny(self, reason: PolicyReason) -> NoReturn:
        self._mark_failed(reason)
        raise RequestPolicyError(reason) from None

    def _count(self, value: object) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= MAX_COUNTER:
            self._deny("accounting")
        return value

    def _failure(self) -> PolicyReason | None:
        try:
            text = (self.directory / "failed").read_text()
        except FileNotFoundError:
            return self._failed
        if text not in REASONS:
            self._deny("accounting")
        return REASONS[text]

    def _guard(self, connection: sqlite3.Connection, delay: float = 0) -> None:
        try:
            now = _clock()
        except RequestPolicyError:
            self._deny("accounting")
        if self._failure() is not None:
            raise RequestPolicyError("closed")
        if connection.execute("SELECT state FROM run").fetchone()[0] != "open":
            self._deny("closed")
        if now + delay >= self.started + self.authorization.limits.seconds:
            self._deny("deadline")
        self._fresh(connection, now)

    def _supervision(self, connection: sqlite3.Connection) -> tuple[int, float | None]:
        rows = connection.execute("SELECT enabled,heartbeat FROM supervision LIMIT 2").fetchall()
        if len(rows) != 1:
            self._deny("accounting")
        enabled, heartbeat = rows[0]
        if type(enabled) is not int or enabled not in (0, 1):
            self._deny("accounting")
        return enabled, self._heartbeat_value(enabled, heartbeat)

    def _heartbeat_value(self, enabled: int, heartbeat: object) -> float | None:
        if not enabled:
            if heartbeat is not None:
                self._deny("accounting")
            return None
        if type(heartbeat) is not float or not math.isfinite(heartbeat):
            self._deny("accounting")
        return heartbeat

    def _fresh(self, connection: sqlite3.Connection, now: float) -> None:
        enabled, heartbeat = self._supervision(connection)
        if enabled and (heartbeat is None or not 0 <= now - heartbeat <= STALE_SECONDS):
            self._deny("accounting")

    def enable_supervision(self) -> None:
        with self._connection() as connection:
            enabled, _heartbeat = self._supervision(connection)
            state = connection.execute("SELECT state FROM run").fetchone()[0]
            if not self._owner or enabled or state != "created" or self._failure() is not None:
                self._deny("authorization")
            connection.execute("UPDATE supervision SET enabled=1,heartbeat=?", (_clock(),))
            connection.execute(
                "UPDATE run SET binding=?", (self._binding(self._key(), self.started, 1),)
            )

    def heartbeat(self) -> None:
        with self._connection() as connection:
            if not self._owner:
                self._deny("authorization")
            if connection.execute("SELECT state FROM run").fetchone()[0] in ("closed", "failed"):
                raise RequestPolicyError("closed")
            self._guard(connection)
            enabled, _heartbeat = self._supervision(connection)
            if not enabled:
                self._deny("authorization")
            connection.execute("UPDATE supervision SET heartbeat=?", (_clock(),))

    def require_supervision_owner(self) -> None:
        if not self._owner:
            raise RequestPolicyError("authorization")
        with self._connection() as connection:
            enabled, _heartbeat = self._supervision(connection)
            state = connection.execute("SELECT state FROM run").fetchone()[0]
            if not enabled or state != "created" or self._failure() is not None:
                raise RequestPolicyError("authorization")
            self._fresh(connection, _clock())

    def participant_pending(self, participant: str) -> bool:
        _participant(participant)
        with self._connection() as connection:
            if (
                not self._owner
                or connection.execute(
                    "SELECT name FROM workers WHERE name=?", (participant,)
                ).fetchone()
                is None
            ):
                self._deny("authorization")
            return (
                connection.execute(
                    "SELECT id FROM leases WHERE participant=? AND retired=0 LIMIT 1",
                    (participant,),
                ).fetchone()
                is not None
            )

    def open(self) -> None:
        with self._connection() as connection:
            if not self._owner or self._failure() is not None:
                self._deny("closed")
            self._initial_summary(connection)
            changed = connection.execute(
                "UPDATE run SET state='open' WHERE state='created'"
            ).rowcount
            if changed != 1:
                self._deny("closed")
            self._guard(connection)

    def _initial_summary(self, connection: sqlite3.Connection) -> None:
        enabled, _heartbeat = self._supervision(connection)
        if not enabled:
            return
        try:
            summary = read_summary(
                self.directory / "summary.json",
                run_id=self.descriptor.run_id,
                policy_sha256=self.descriptor.policy_sha256,
            )
        except RequestPolicyError:
            self._deny("accounting")
        if summary["state"] != "incomplete":
            self._deny("accounting")

    def close(self) -> None:
        with self._connection() as connection:
            if not self._owner:
                self._deny("authorization")
            connection.execute(
                "UPDATE run SET state=?", ("failed" if self._failure() else "closed",)
            )

    def authorize_participant(self, participant: str) -> None:
        _participant(participant)
        with self._connection() as connection:
            self._guard(connection)
            if not self._owner or participant == "controller":
                self._deny("authorization")
            connection.execute("INSERT INTO workers VALUES (?)", (participant,))

    def check_wait(self, delay_seconds: float) -> None:
        try:
            valid = isinstance(delay_seconds, (int, float)) and not isinstance(delay_seconds, bool)
            if not valid or not math.isfinite(delay_seconds) or delay_seconds < 0:
                self._deny("authorization")
            with self._connection() as connection:
                self._guard(connection, delay_seconds)
        except OverflowError:
            self._deny("authorization")

    @contextmanager
    def logical(self, grant: RequestGrant) -> Iterator[LogicalRequest]:
        if grant not in self.authorization.grants:
            self._deny("authorization")
        request_id = uuid.uuid4().hex
        with self._connection() as connection:
            self._guard(connection)
            if (
                connection.execute(
                    "UPDATE run SET logical=logical+1 WHERE logical<?",
                    (self.authorization.limits.logical,),
                ).rowcount
                != 1
            ):
                self._deny("logical_limit")
            connection.execute(
                "INSERT INTO requests VALUES (?,?,0)", (request_id, self.participant)
            )
        yield LogicalRequest(self, request_id)

    def snapshot(self) -> LedgerSnapshot:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM run").fetchone()
            state = STATES.get(row["state"])
            if state is None:
                self._deny("accounting")
            pending = self._count(
                connection.execute("SELECT count(*) FROM leases WHERE pending>0").fetchone()[0]
            )
            failure = self._failure()
            result = LedgerSnapshot(
                run_id=self.descriptor.run_id,
                policy_sha256=self.descriptor.policy_sha256,
                state="failed" if failure else state,
                logical=self._count(row["logical"]),
                attempts=self._count(row["attempts"]),
                reserved_body_bytes=self._count(row["reserved"]),
                transport_entries=self._count(row["entered"]),
                consumed_body_bytes=self._count(row["consumed"]),
                active=self._count(row["active"]),
                peak_active=self._count(row["peak"]),
                pending_receipts=pending,
                observation_complete=pending == 0 and row["active"] == 0,
                failure=failure,
            )
            self._validate_snapshot(result)
            return result

    def _validate_snapshot(self, snapshot: LedgerSnapshot) -> None:
        limits = self.authorization.limits
        bounds = (
            snapshot.logical <= limits.logical,
            snapshot.attempts <= min(limits.attempts, snapshot.logical * limits.per_logical),
            snapshot.transport_entries <= snapshot.attempts,
            snapshot.consumed_body_bytes <= snapshot.reserved_body_bytes <= limits.total_bytes,
            snapshot.active <= snapshot.peak_active <= limits.concurrent,
            snapshot.peak_active <= snapshot.attempts,
            snapshot.pending_receipts <= snapshot.active,
        )
        if not all(bounds):
            self._deny("accounting")


class LogicalRequest:
    def __init__(self, run: ControlledRun, request_id: str) -> None:
        self.run, self.request_id = run, request_id

    @contextmanager
    def attempt(self, *, response_limit: int) -> Iterator[AttemptLease]:
        if type(response_limit) is not int or not 0 < response_limit <= MAX_COUNTER:
            self.run._deny("authorization")
        allowance = min(response_limit, self.run.authorization.limits.response_bytes) + 1
        lease_id = uuid.uuid4().hex
        with self.run._connection() as connection:
            self.run._guard(connection)
            self._reserve(connection, allowance)
            connection.execute(
                "INSERT INTO leases VALUES (?,?,?,?,0,0,0,0)",
                (lease_id, self.request_id, self.run.participant, allowance),
            )
        lease = AttemptLease(self.run, lease_id)
        try:
            yield lease
        finally:
            lease.release()

    def _reserve(self, connection: sqlite3.Connection, allowance: int) -> None:
        limits: Limits = self.run.authorization.limits
        if (
            connection.execute(
                "UPDATE requests SET attempts=attempts+1 "
                "WHERE id=? AND participant=? AND attempts<?",
                (self.request_id, self.run.participant, limits.per_logical),
            ).rowcount
            != 1
        ):
            self.run._deny("attempt_limit")
        if (
            connection.execute(
                "UPDATE run SET attempts=attempts+1 WHERE attempts<?", (limits.attempts,)
            ).rowcount
            != 1
        ):
            self.run._deny("attempt_limit")
        if (
            connection.execute(
                "UPDATE run SET active=active+1,peak=max(peak,active+1) WHERE active<?",
                (limits.concurrent,),
            ).rowcount
            != 1
        ):
            self.run._deny("concurrency_limit")
        # Subtraction avoids SQLite promoting an overflowing integer addition to REAL.
        if (
            connection.execute(
                "UPDATE run SET reserved=reserved+? WHERE reserved<=?",
                (allowance, limits.total_bytes - allowance),
            ).rowcount
            != 1
        ):
            self.run._deny("byte_limit")


class AttemptLease:
    def __init__(self, run: ControlledRun, lease_id: str) -> None:
        self.run, self.lease_id = run, lease_id

    def _row(self, connection: sqlite3.Connection, *, allow_retired: bool = False) -> sqlite3.Row:
        row = connection.execute(
            "SELECT id,request,participant,allowance,consumed,pending,entered,retired "
            "FROM leases WHERE id=? AND participant=?",
            (self.lease_id, self.run.participant),
        ).fetchone()
        if row is None:
            self.run._deny("accounting")
        if row["retired"] not in (0, 1) or (row["retired"] and not allow_retired):
            self.run._deny("accounting")
        return row

    def entered(self) -> None:
        with self.run._connection() as connection:
            self.run._guard(connection)
            row = self._row(connection)
            if row["entered"]:
                self.run._deny("accounting")
            connection.execute("UPDATE leases SET entered=1 WHERE id=?", (self.lease_id,))
            connection.execute("UPDATE run SET entered=entered+1")

    def read(self, reader: Callable[[int], bytes], maximum: int) -> bytes:
        with self.run._connection() as connection:
            self.run._guard(connection)
            row = self._row(connection)
            self._read_capacity(row, maximum)
            connection.execute("UPDATE leases SET pending=? WHERE id=?", (maximum, self.lease_id))
        try:
            payload = reader(maximum)
        except OSError, ValueError:
            self.run._deny("transport")
        self._receipt(payload, maximum)
        self.run.check_wait(0)
        return payload

    def _read_capacity(self, row: sqlite3.Row, maximum: int) -> None:
        if type(maximum) is not int or maximum <= 0:
            self.run._deny("authorization")
        if not row["entered"] or row["pending"]:
            self.run._deny("accounting")
        if maximum > row["allowance"] - row["consumed"]:
            self.run._deny("response_limit")

    def _receipt(self, payload: bytes, maximum: int) -> None:
        if not isinstance(payload, bytes) or len(payload) > maximum:
            self.run._deny("accounting")
        # Admission closure cannot erase bytes already returned by the reader.
        with self.run._connection() as connection:
            row = self._row(connection)
            if row["pending"] != maximum:
                self.run._deny("accounting")
            connection.execute(
                "UPDATE leases SET consumed=consumed+?,pending=0 WHERE id=?",
                (len(payload), self.lease_id),
            )
            connection.execute("UPDATE run SET consumed=consumed+?", (len(payload),))

    def release(self) -> None:
        with self.run._connection() as connection:
            row = self._row(connection, allow_retired=True)
            if row["retired"]:
                return  # The connection still validates authority on every repeat.
            if row["pending"]:
                return  # Unknown read progress retains its whole lease and capacity.
            unused = row["allowance"] - row["consumed"]
            connection.execute(
                "UPDATE run SET active=active-1,reserved=reserved-? WHERE active>0", (unused,)
            )
            # Retain at most one retirement fact per admitted, finite-budget attempt.
            connection.execute("UPDATE leases SET retired=1 WHERE id=?", (self.lease_id,))
