"""Controller-owned lifecycle and sanitized summary for explicit diagnostic callers."""

from __future__ import annotations

from pathlib import Path
from types import TracebackType
from typing import Self

from nci_si_acceptance.request_authorization import Authorization
from nci_si_acceptance.request_budget import ControlledRun
from nci_si_acceptance.request_summary import RunSummary, project, publish
from nci_si_mcp.http_client import RequestPolicyError
from nci_si_mcp.validation import SummaryState


class SupervisedRun:
    def __init__(self, run: ControlledRun) -> None:
        self.run = run
        self._state: SummaryState = "incomplete"

    @classmethod
    def create(cls, authorization: Authorization, owned_directory: Path) -> Self:
        return cls(ControlledRun.create(authorization, owned_directory))

    def snapshot(self) -> RunSummary:
        return project(self.run.snapshot(), self.run.authorization.limits, self._state)

    def _publish(self) -> None:
        publish(self.run.directory / "summary.json", self.snapshot())

    def __enter__(self) -> Self:
        try:
            self._publish()
            self.run.open()
        except RequestPolicyError:
            self.run.close()
            raise
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.run.close()
        snapshot = self.run.snapshot()
        self._state = "completed" if error is None and snapshot.observation_complete else "failed"
        self._publish()
