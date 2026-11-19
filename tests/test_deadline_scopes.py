"""Concurrent invocations and interrupted scopes cannot share elapsed-time budgets."""

import asyncio
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch

from nci_sip_mcp.bounds import (
    CallDeadlineError,
    check_deadline,
    current_deadline,
    deadline_scope,
)
from nci_sip_mcp.registry import SPECS


class DeadlineScopeTest(unittest.TestCase):
    def test_only_declared_index_operators_are_exempt_from_call_deadlines(self):
        self.assertEqual(
            {spec.operation for spec in SPECS if not spec.deadline},
            {"index_codes", "index_build", "index_builds", "index_rebuild", "index_activate"},
        )

    def test_refused_attempt_does_not_increment_the_actual_request_count(self):
        now = [0.0]
        with patch("nci_sip_mcp.bounds.monotonic", side_effect=lambda: now[0]), deadline_scope(1):
            deadline = current_deadline()
            deadline.request("evs")
            now[0] = 1
            with self.assertRaises(CallDeadlineError) as raised:
                deadline.request("cadsr")
        self.assertEqual(raised.exception.details, {"surface": "cadsr", "attempts": 1})

    def test_nested_work_keeps_the_original_budget_and_cancellation_resets_it(self):
        now = [0.0]
        with patch("nci_sip_mcp.bounds.monotonic", side_effect=lambda: now[0]):
            with self.assertRaises(asyncio.CancelledError), deadline_scope(1):
                now[0] = 2
                with deadline_scope(100), self.assertRaises(CallDeadlineError):
                    check_deadline()
                raise asyncio.CancelledError
            self.assertIsNone(current_deadline())
            with deadline_scope(1):
                check_deadline()
                self.assertEqual(current_deadline().remaining(), 1)
        self.assertIsNone(current_deadline())

    def test_concurrent_calls_keep_their_own_budget_and_attempt_counts(self):
        together = Barrier(2)
        short_budget = 10

        def work(seconds):
            with deadline_scope(seconds):
                deadline = current_deadline()
                together.wait(timeout=5)
                deadline.request("evs")
                if seconds == short_budget:
                    deadline.request("cadsr")
                together.wait(timeout=5)
                return deadline.seconds, deadline.attempts, deadline.surface

        with patch("nci_sip_mcp.bounds.monotonic", return_value=0), ThreadPoolExecutor(2) as pool:
            self.assertEqual(list(pool.map(work, [10, 20])), [(10, 2, "cadsr"), (20, 1, "evs")])
        self.assertIsNone(current_deadline())
