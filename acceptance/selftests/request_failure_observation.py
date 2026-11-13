"""Failure-only evidence at the registered-child snapshot boundary."""

import pytest

from nci_si_acceptance.request_budget import ControlledRun, LedgerSnapshot


def snapshot(run: ControlledRun, node: pytest.Item) -> LedgerSnapshot:
    return run.snapshot()
