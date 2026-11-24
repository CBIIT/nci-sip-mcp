"""The mutation runner retains its session anchor until owned cleanup is complete."""

import json
import os
import signal
import subprocess
import sys
import textwrap
import unittest
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from scripts.processes import wait_until
from scripts.validation import mutation_run as runner


def reap_remaining(children):
    for child in children:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)


def exception_leaves(error):
    if isinstance(error, BaseExceptionGroup):
        return [leaf for nested in error.exceptions for leaf in exception_leaves(nested)]
    return [error]


class ProgramOwnershipTest(unittest.TestCase):
    def test_scope_exit_adds_missing_latched_cancellation_to_nested_original_failures(self):
        # Given nested unrelated failures, the distinct latched interrupt must appear once.
        body_failure = ValueError("original body failure")
        independent = KeyboardInterrupt("independent interrupt")
        previous = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
        failure = BaseExceptionGroup(
            "body", [body_failure, BaseExceptionGroup("cleanup", [independent])]
        )
        with (
            self.assertRaises(BaseExceptionGroup) as raised,
            runner.runner_termination() as cancellation,
        ):
            os.kill(os.getpid(), signal.SIGTERM)
            interruption = cancellation.requested
            self.assertIsInstance(interruption, KeyboardInterrupt)
            raise failure
        leaves = exception_leaves(raised.exception)
        self.assertEqual(leaves, [body_failure, independent, interruption])
        self.assertIs(raised.exception.exceptions[0], failure)
        self.assertEqual({s: signal.getsignal(s) for s in previous}, previous)

    def test_scope_exit_preserves_cancellation_already_nested_in_failure_group(self):
        previous = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
        body_failure = ValueError("original body failure")
        independent = KeyboardInterrupt("independent interrupt")
        with (
            self.assertRaises(BaseExceptionGroup) as raised,
            runner.runner_termination() as cancellation,
        ):
            os.kill(os.getpid(), signal.SIGTERM)
            interruption = cancellation.requested
            self.assertIsInstance(interruption, KeyboardInterrupt)
            failure = BaseExceptionGroup(
                "body and cleanup failed",
                [body_failure, BaseExceptionGroup("cleanup", [independent, interruption])],
            )
            raise failure
        self.assertIs(raised.exception, failure)
        self.assertEqual(
            exception_leaves(raised.exception), [body_failure, independent, interruption]
        )
        self.assertEqual({s: signal.getsignal(s) for s in previous}, previous)

    def test_first_sigterm_during_handler_restoration_is_diagnostic_and_restores_both(self):
        # Given inner resources have closed, deliver the first real TERM while
        # the latch still owns it, immediately before the first caller restore.
        script = textwrap.dedent("""\
            import json, os, signal, sys
            from unittest.mock import patch
            from scripts.validation import mutation_run as runner

            previous = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
            install = signal.signal
            restored = []
            body_failure, restore_failure = map(int, sys.argv[1:])

            def restore(sig, disposition):
                if disposition is previous[sig]:
                    if not restored:
                        os.kill(os.getpid(), signal.SIGTERM)
                    install(sig, disposition)
                    restored.append(sig.name)
                    if restore_failure and len(restored) == 1:
                        raise OSError('original restoration failure')
                    return
                return install(sig, disposition)

            def leaves(error):
                if isinstance(error, BaseExceptionGroup):
                    return [leaf for nested in error.exceptions for leaf in leaves(nested)]
                return [type(error).__name__]

            try:
                with patch.object(runner.signal, 'signal', side_effect=restore):
                    with runner.runner_termination():
                        if body_failure:
                            raise ValueError('original body failure')
            except BaseException as error:
                print(json.dumps({'errors': leaves(error), 'restored': restored,
                    'handlers_restored': all(signal.getsignal(s) is h
                        for s, h in previous.items())}), flush=True)
                raise
            else:
                print(json.dumps({'errors': [], 'restored': restored,
                    'handlers_restored': all(signal.getsignal(s) is h
                        for s, h in previous.items())}), flush=True)
            """)
        for body_failure, restore_failure in ((0, 0), (1, 0), (0, 1), (1, 1)):
            with self.subTest(body=body_failure, restore=restore_failure):
                result = subprocess.run(  # noqa: S603 - fixed interpreter and authored signal scenario
                    [sys.executable, "-c", script, str(body_failure), str(restore_failure)],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
                evidence = json.loads(result.stdout)
                self.assertNotEqual(result.returncode, 0, result.stderr)
                self.assertEqual(evidence["restored"], ["SIGINT", "SIGTERM"])
                self.assertTrue(evidence["handlers_restored"])
                expected = ["ValueError"] if body_failure else []
                expected += ["OSError"] if restore_failure else []
                self.assertEqual(evidence["errors"], [*expected, "KeyboardInterrupt"])
                self.assertIn("KeyboardInterrupt", result.stderr)
                if body_failure:
                    self.assertIn("ValueError: original body failure", result.stderr)
                if restore_failure:
                    self.assertIn("OSError: original restoration failure", result.stderr)

    def test_first_signal_at_wait_finally_transition_still_cleans_and_reaps(self):
        # Given a ready ignoring child, signal on the first program line after
        # its wait returns, before cleanup begins (not inside a cleanup helper).
        children, returned = [], []
        popen = subprocess.Popen
        previous = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
        previous_trace = sys.gettrace()
        sentinel = popen([sys.executable, "-c", "import signal; signal.pause()"])
        with TemporaryDirectory() as directory:
            root = Path(directory)
            ready = root / "ready"
            script = (
                "import signal; from pathlib import Path; "
                "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
                f"Path({str(ready)!r}).touch(); signal.pause()"
            )

            def launch(*args, **kwargs):
                child = popen(*args, **kwargs)
                children.append(child)
                self.assertTrue(wait_until(ready.exists, 5, interval=0.01))
                return child

            def signal_transition(frame, event, _arg):
                if frame.f_code is runner._wait_exit.__code__ and event == "return":
                    returned.append(frame.f_code)
                if frame.f_code is runner.program.__code__ and event == "line" and returned:
                    returned.clear()
                    os.kill(os.getpid(), signal.SIGTERM)
                return signal_transition

            try:
                with (
                    patch.object(runner.subprocess, "Popen", side_effect=launch),
                    patch.object(runner, "STOP_SECONDS", 0.1),
                    self.assertRaises(BaseException) as raised,
                ):
                    sys.settrace(signal_transition)
                    try:
                        runner.program(
                            [sys.executable, "-c", script], root, dict(os.environ), 0, root / "log"
                        )
                    finally:
                        sys.settrace(previous_trace)
                self.assertEqual(children[0].returncode, -signal.SIGKILL)
                self.assertEqual(
                    [type(e) for e in exception_leaves(raised.exception)], [KeyboardInterrupt]
                )
                with self.assertRaises(ChildProcessError):
                    os.waitpid(children[0].pid, os.WNOHANG)
                self.assertIsNone(sentinel.poll())
                self.assertEqual({s: signal.getsignal(s) for s in previous}, previous)
            finally:
                reap_remaining([*children, sentinel])

    def test_signal_after_actual_log_close_prevents_normal_return(self):
        self.assertEqual(self._signal_after_log_close(None), [KeyboardInterrupt])

    def test_signal_after_log_close_preserves_existing_failure_alongside_cancellation(self):
        for error in (ValueError("close failed"), KeyboardInterrupt("independent interrupt")):
            with self.subTest(error=type(error).__name__):
                self.assertEqual(self._signal_after_log_close(error), [KeyboardInterrupt])

    def _signal_after_log_close(self, failure):
        opened, children = [], []
        path_open, popen = Path.open, subprocess.Popen
        previous = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}

        @contextmanager
        def close_then_signal(path, *args, **kwargs):
            with path_open(path, *args, **kwargs) as stream:
                opened.append(stream)
                yield stream
            os.kill(os.getpid(), signal.SIGTERM)
            if failure is not None:
                raise failure

        def launch(*args, **kwargs):
            child = popen(*args, **kwargs)
            children.append(child)
            return child

        with TemporaryDirectory() as directory:
            root = Path(directory)
            result = None
            try:
                with (
                    patch.object(Path, "open", close_then_signal),
                    patch.object(runner.subprocess, "Popen", side_effect=launch),
                ):
                    try:
                        result = runner.program(
                            [sys.executable, "-c", "pass"], root, dict(os.environ), 5, root / "log"
                        )
                    except BaseException as error:  # noqa: BLE001 - assert caller-visible failure below
                        result = error
                self.assertIsInstance(result, BaseException)
                leaves = exception_leaves(result)
                if failure is not None:
                    self.assertIs(leaves.pop(0), failure)
                self.assertEqual([type(e) for e in leaves], [KeyboardInterrupt])
                self.assertTrue(opened[0].closed)
                self.assertEqual(children[0].returncode, 0)
                with self.assertRaises(ChildProcessError):
                    os.waitpid(children[0].pid, os.WNOHANG)
                self.assertEqual({s: signal.getsignal(s) for s in previous}, previous)
                return [type(e) for e in leaves]
            finally:
                reap_remaining(children)

    def test_repeated_sigterm_while_handling_first_interrupt_still_kills_and_reaps(self):
        # Given a ready TERM-ignoring child, repeat TERM while handling the first
        # interrupt, before cleanup; both signals must leave owned cleanup intact.
        children, sent, interruptions = [], [], []
        popen, wait_exit = subprocess.Popen, runner._wait_exit
        previous = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
        previous_trace = sys.gettrace()
        sentinel = popen([sys.executable, "-c", "import signal; signal.pause()"])
        with TemporaryDirectory() as directory:
            root = Path(directory)
            ready = root / "ready"
            script = (
                "import signal; from pathlib import Path; "
                "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
                f"Path({str(ready)!r}).touch(); signal.pause()"
            )

            def cancel():
                sent.append(signal.SIGTERM)
                os.kill(os.getpid(), signal.SIGTERM)

            def launch(*args, **kwargs):
                child = popen(*args, **kwargs)
                children.append(child)
                self.assertTrue(wait_until(ready.exists, 5, interval=0.01))
                return child

            def cancel_wait(pid, deadline, cancellation_check=None):
                if not sent:
                    cancel()
                return wait_exit(pid, deadline, cancellation_check)

            def repeat_during_exception(frame, event, arg):
                if frame.f_code is not runner.program.__code__:
                    return None
                if (
                    event == "line"
                    and len(sent) == 1
                    and isinstance(sys.exception(), KeyboardInterrupt)
                ):
                    interruptions.append(sys.exception())
                    cancel()
                return repeat_during_exception

            try:
                with (
                    patch.object(runner.subprocess, "Popen", side_effect=launch),
                    patch.object(runner, "_wait_exit", side_effect=cancel_wait),
                    patch.object(runner, "STOP_SECONDS", 0.1),
                    self.assertRaises(BaseException) as raised,
                ):
                    sys.settrace(repeat_during_exception)
                    try:
                        runner.program(
                            [sys.executable, "-c", script], root, dict(os.environ), 5, root / "log"
                        )
                    finally:
                        sys.settrace(previous_trace)
                self.assertEqual(sent, [signal.SIGTERM, signal.SIGTERM])
                self.assertEqual(len(interruptions), 1)
                self.assertEqual(exception_leaves(raised.exception), interruptions)
                self.assertEqual(children[0].returncode, -signal.SIGKILL)
                with self.assertRaises(ChildProcessError):
                    os.waitpid(children[0].pid, os.WNOHANG)
                self.assertIsNone(sentinel.poll())
                self.assertEqual({s: signal.getsignal(s) for s in previous}, previous)
            finally:
                reap_remaining([*children, sentinel])

    def test_sigterm_at_cleanup_entry_cannot_escape_owned_cleanup(self):
        self.assertEqual(self._cancellation_window(signal.SIGTERM, "cleanup"), -signal.SIGKILL)

    def test_sigint_during_launch_and_grace_cannot_escape_owned_cleanup(self):
        for stage in ("launch", "grace"):
            with self.subTest(stage=stage):
                self.assertEqual(self._cancellation_window(signal.SIGINT, stage), -signal.SIGKILL)

    def _cancellation_window(self, sig, stage):
        # Given a ready TERM-ignoring child, real cancellation at each ownership
        # boundary must remain diagnostic only after final KILL and parent reap.
        children, cancelled = [], []
        popen = subprocess.Popen
        cleanup, terminal_group = runner._cleanup_process, runner._terminal_group
        previous = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
        sentinel = popen([sys.executable, "-c", "import signal; signal.pause()"])
        with TemporaryDirectory() as directory:
            root = Path(directory)
            ready = root / "ready"
            script = (
                "import signal; from pathlib import Path; "
                "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
                f"Path({str(ready)!r}).touch(); signal.pause()"
            )

            def cancel():
                cancelled.append(sig)
                os.kill(os.getpid(), sig)

            def launch(*args, **kwargs):
                child = popen(*args, **kwargs)
                children.append(child)
                self.assertTrue(wait_until(ready.exists, 5, interval=0.01))
                if stage == "launch":
                    cancel()
                return child

            def enter_cleanup(*args):
                if stage == "cleanup":
                    cancel()
                return cleanup(*args)

            def inspect(owner, deadline):
                if stage == "grace" and not cancelled:
                    cancel()
                return terminal_group(owner, deadline)

            try:
                with (
                    patch.object(runner.subprocess, "Popen", side_effect=launch),
                    patch.object(runner, "_cleanup_process", side_effect=enter_cleanup),
                    patch.object(runner, "_terminal_group", side_effect=inspect),
                    patch.object(runner, "STOP_SECONDS", 0.1),
                    self.assertRaises(BaseException) as raised,
                ):
                    runner.program(
                        [sys.executable, "-c", script], root, dict(os.environ), 0, root / "log"
                    )
                status = children[0].returncode
                self.assertEqual(status, -signal.SIGKILL)
                self.assertEqual(cancelled, [sig])
                self.assertEqual(
                    [type(e) for e in exception_leaves(raised.exception)], [KeyboardInterrupt]
                )
                with self.assertRaises(ChildProcessError):
                    os.waitpid(children[0].pid, os.WNOHANG)
                self.assertIsNone(sentinel.poll())
                self.assertEqual({s: signal.getsignal(s) for s in previous}, previous)
                return status
            finally:
                reap_remaining([*children, sentinel])

    def test_failed_inspection_preserves_worker_grace_to_reap_nested_runner(self):
        # Given an exited scheduler and a worker owning a separate-session runner,
        # inspection failure must not take away the worker's cooperative grace.
        sentinel = subprocess.Popen([sys.executable, "-c", "import signal; signal.pause()"])
        inspection = ValueError("injected early-leader inspection failure")
        with TemporaryDirectory() as directory:
            root = Path(directory)
            ready, cleaned = root / "ready", root / "cleaned"
            nested = (
                "import os,signal; from pathlib import Path; "
                "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
                f"Path({str(ready)!r}).write_text(str(os.getpid())); signal.pause()"
            )
            worker = (
                "import os,sys; from pathlib import Path\n"
                "from scripts.validation import mutation_run as r\n"
                "r.STOP_SECONDS=0.15\n"
                "children=[]; launch=r.subprocess.Popen\n"
                "def capture(*args,**kwargs):\n"
                "    child=launch(*args,**kwargs); children.append(child); return child\n"
                "r.subprocess.Popen=capture\n"
                "try:\n"
                f"    r.program([sys.executable,'-c',{nested!r}],Path({str(root)!r}),"
                f"dict(os.environ),10,Path({str(root / 'nested.log')!r}))\n"
                "finally:\n"
                f"    Path({str(cleaned)!r}).write_text(str(children[0].returncode))\n"
            )
            leader = (
                "import subprocess,sys,time; from pathlib import Path\n"
                f"subprocess.Popen([sys.executable,'-c',{worker!r}])\n"
                f"while not Path({str(ready)!r}).exists(): time.sleep(0.01)\n"
            )
            try:
                with (
                    patch.object(runner, "_group_snapshot", side_effect=inspection),
                    patch.object(runner, "WORKER_CLEANUP_SECONDS", 0.6),
                    patch.object(runner, "OWNED_CLEANUP_SECONDS", 1),
                    self.assertRaises(BaseException) as raised,
                ):
                    runner.program(
                        [sys.executable, "-c", leader],
                        root,
                        {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
                        5,
                        root / "log",
                        worker_cleanup=True,
                    )
                self.assertIn(inspection, exception_leaves(raised.exception))
                self.assertTrue(cleaned.exists(), "inspection failure cut off nested cleanup")
                self.assertEqual(cleaned.read_text(), str(-signal.SIGKILL))
                with self.assertRaises(ProcessLookupError):
                    os.kill(int(ready.read_text()), 0)
                self.assertIsNone(sentinel.poll())
            finally:
                reap_remaining([sentinel])
                if ready.exists():
                    self._kill_leftover(int(ready.read_text()))

    def test_first_sigterm_during_grace_still_kills_reaps_and_reports_cancellation(self):
        # Given successful TERM of a ready ignoring child, the first cancellation
        # during grace must not bypass final KILL or become a timeout result.
        children = []
        popen, killpg = subprocess.Popen, os.killpg
        terminal_group = runner._terminal_group
        previous = signal.getsignal(signal.SIGTERM)
        sent = []
        sentinel = popen([sys.executable, "-c", "import signal; signal.pause()"])
        with TemporaryDirectory() as directory:
            root = Path(directory)
            ready = root / "ready"
            script = (
                "import signal; from pathlib import Path; "
                "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
                f"Path({str(ready)!r}).touch(); signal.pause()"
            )

            def launch(*args, **kwargs):
                child = popen(*args, **kwargs)
                children.append(child)
                self.assertTrue(wait_until(ready.exists, 5, interval=0.01))
                return child

            def deliver(pid, sig):
                killpg(pid, sig)
                sent.append(sig)

            def cancel_in_grace(owner, deadline):
                if sent == [signal.SIGTERM]:
                    sent.append("cancellation")
                    os.kill(os.getpid(), signal.SIGTERM)
                return terminal_group(owner, deadline)

            try:
                with (
                    patch.object(runner.subprocess, "Popen", side_effect=launch),
                    patch.object(runner.os, "killpg", side_effect=deliver),
                    patch.object(runner, "_terminal_group", side_effect=cancel_in_grace),
                    patch.object(runner, "STOP_SECONDS", 0.25),
                    self.assertRaises(BaseException) as raised,
                ):
                    runner.program(
                        [sys.executable, "-c", script], root, dict(os.environ), 0, root / "log"
                    )
                self.assertEqual(children[0].returncode, -signal.SIGKILL)
                self.assertEqual(sent, [signal.SIGTERM, "cancellation", signal.SIGKILL])
                self.assertEqual(
                    [type(e) for e in exception_leaves(raised.exception)], [KeyboardInterrupt]
                )
                with self.assertRaises(ChildProcessError):
                    os.waitpid(children[0].pid, os.WNOHANG)
                self.assertIsNone(sentinel.poll())
                self.assertEqual(signal.getsignal(signal.SIGTERM), previous)
            finally:
                reap_remaining([*children, sentinel])

    def test_normal_completion_cleanup_cancellation_is_not_lost_when_group_is_terminal(self):
        terminal_group = runner._terminal_group
        previous = signal.getsignal(signal.SIGTERM)
        cancelled = []

        def cancel_terminal(owner, deadline):
            terminal = terminal_group(owner, deadline)
            if terminal and not cancelled:
                cancelled.append(owner.pid)
                os.kill(os.getpid(), signal.SIGTERM)
            return terminal

        with TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch.object(runner, "_terminal_group", side_effect=cancel_terminal),
                self.assertRaises(KeyboardInterrupt),
            ):
                runner.program(
                    [sys.executable, "-c", "pass"], root, dict(os.environ), 5, root / "log"
                )
        self.assertEqual(len(cancelled), 1)
        with self.assertRaises(ChildProcessError):
            os.waitpid(cancelled[0], os.WNOHANG)
        self.assertEqual(signal.getsignal(signal.SIGTERM), previous)

    def test_scheduler_allows_nested_runner_cleanup_before_final_kill(self):
        # Given nested separate-session work, scheduler cancellation must let its
        # worker kill/reap that runner and record completion before escalation.
        with TemporaryDirectory() as directory:
            root = Path(directory)
            ready, cleaned = root / "ready", root / "cleaned"
            nested = (
                "import os,signal; from pathlib import Path; "
                "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
                f"Path({str(ready)!r}).write_text(str(os.getpid())); signal.pause()"
            )
            worker = (
                "import os,sys; from pathlib import Path\n"
                "from scripts.validation import mutation_run as r\n"
                "r.STOP_SECONDS=0.1; r.WORKER_CLEANUP_SECONDS=0.5; r.OWNED_CLEANUP_SECONDS=0.7\n"
                "children=[]; launch=r.subprocess.Popen\n"
                "def capture(*args,**kwargs):\n"
                "    child=launch(*args,**kwargs); children.append(child); return child\n"
                "r.subprocess.Popen=capture\n"
                "try:\n"
                f"    r.program([sys.executable,'-c',{nested!r}],Path({str(root)!r}),"
                f"dict(os.environ),10,Path({str(root / 'nested.log')!r}))\n"
                "finally:\n"
                f"    Path({str(cleaned)!r}).write_text(str(children[0].returncode))\n"
            )
            wait_exit = runner._wait_exit

            def cancel_ready(pid, deadline, cancel=None):
                if not ready.exists():
                    self.assertTrue(wait_until(ready.exists, 5, interval=0.01))
                    return None
                return wait_exit(pid, deadline, cancel)

            try:
                with (
                    patch.object(runner, "STOP_SECONDS", 0.1),
                    patch.object(runner, "WORKER_CLEANUP_SECONDS", 0.5),
                    patch.object(runner, "OWNED_CLEANUP_SECONDS", 0.7),
                    patch.object(runner, "_wait_exit", side_effect=cancel_ready),
                ):
                    result = runner.program(
                        [sys.executable, "-c", worker],
                        root,
                        {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
                        10,
                        root / "worker.log",
                        worker_cleanup=True,
                    )
                self.assertIsNone(result)
                self.assertTrue(cleaned.exists(), "worker did not finish nested cleanup")
                self.assertEqual(cleaned.read_text(), str(-signal.SIGKILL))
                with self.assertRaises(ProcessLookupError):
                    os.kill(int(ready.read_text()), 0)
            finally:
                if ready.exists():
                    self._kill_leftover(int(ready.read_text()))

    @staticmethod
    def _kill_leftover(pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            return

    def test_failed_grace_inspection_still_kills_and_reaps_live_owned_child(self):
        # Given successful TERM of a ready ignoring child, failed inspection must
        # remain visible without leaking that child or touching an unrelated one.
        children = []
        popen = subprocess.Popen
        inspection = ValueError("injected grace inspection failure")
        sentinel = popen([sys.executable, "-c", "import signal; signal.pause()"])
        with TemporaryDirectory() as directory:
            root = Path(directory)
            ready = root / "ready"

            def launch(*args, **kwargs):
                child = popen(*args, **kwargs)
                children.append(child)
                self.assertTrue(wait_until(ready.exists, 5, interval=0.01))
                return child

            script = (
                "import signal; from pathlib import Path; "
                "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
                f"Path({str(ready)!r}).touch(); signal.pause()"
            )
            try:
                with (
                    patch.object(runner.subprocess, "Popen", side_effect=launch),
                    patch.object(runner, "STOP_SECONDS", 0.25),
                    patch.object(runner, "_terminal_group", side_effect=inspection),
                    self.assertRaises(BaseException) as raised,
                ):
                    runner.program(
                        [sys.executable, "-c", script], root, dict(os.environ), 0, root / "log"
                    )
                self.assertIn(inspection, exception_leaves(raised.exception))
                self.assertEqual(children[0].returncode, -signal.SIGKILL)
                with self.assertRaises(ChildProcessError):
                    os.waitpid(children[0].pid, os.WNOHANG)
                self.assertIsNone(sentinel.poll())
            finally:
                reap_remaining([*children, sentinel])

    def test_sigterm_at_popen_return_cleans_registered_child_and_restores_handler(self):
        children = []
        popen = subprocess.Popen
        previous = signal.getsignal(signal.SIGTERM)
        with TemporaryDirectory() as directory:
            root = Path(directory)

            def launch(*args, **kwargs):
                child = popen(*args, **kwargs)
                children.append(child)
                os.kill(os.getpid(), signal.SIGTERM)
                return child

            try:
                with (
                    patch.object(runner.subprocess, "Popen", side_effect=launch),
                    patch.object(runner, "STOP_SECONDS", 0.25),
                    self.assertRaises(KeyboardInterrupt),
                ):
                    runner.program(
                        [sys.executable, "-c", "import signal; signal.pause()"],
                        root,
                        dict(os.environ),
                        5,
                        root / "log",
                    )
                self.assertIsNotNone(children[0].returncode)
                with self.assertRaises(ChildProcessError):
                    os.waitpid(children[0].pid, os.WNOHANG)
                self.assertEqual(signal.getsignal(signal.SIGTERM), previous)
            finally:
                reap_remaining(children)

    def test_initiating_inspection_and_final_signal_errors_all_survive_reap(self):
        interruption = KeyboardInterrupt("initiating cancellation")
        inspection = ValueError("grace inspection failed")
        final = PermissionError("final signal failed after delivery")
        children = []
        popen, killpg = subprocess.Popen, os.killpg
        with TemporaryDirectory() as directory:
            root = Path(directory)
            ready = root / "ready"
            script = (
                "import signal; from pathlib import Path; "
                "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
                f"Path({str(ready)!r}).touch(); signal.pause()"
            )

            def launch(*args, **kwargs):
                child = popen(*args, **kwargs)
                children.append(child)
                self.assertTrue(wait_until(ready.exists, 5, interval=0.01))
                return child

            def deliver(pid, sig):
                killpg(pid, sig)
                if sig == signal.SIGKILL:
                    raise final

            try:
                with (
                    patch.object(runner.subprocess, "Popen", side_effect=launch),
                    patch.object(runner, "_wait_exit", side_effect=interruption),
                    patch.object(runner, "_terminal_group", side_effect=inspection),
                    patch.object(runner.os, "killpg", side_effect=deliver),
                    patch.object(runner, "STOP_SECONDS", 0.25),
                    self.assertRaises(BaseExceptionGroup) as raised,
                ):
                    runner.program(
                        [sys.executable, "-c", script], root, dict(os.environ), 5, root / "log"
                    )
                self.assertEqual(
                    exception_leaves(raised.exception), [interruption, inspection, final]
                )
                self.assertEqual(children[0].returncode, -signal.SIGKILL)
            finally:
                reap_remaining(children)

    def test_waitid_observation_is_repeatable_until_actual_parent_reaps(self):
        child = subprocess.Popen(
            [sys.executable, "-c", "import sys; sys.stdin.readline(); raise SystemExit(23)"],
            stdin=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            self.assertEqual(os.getpgid(child.pid), child.pid)
            child.stdin.close()
            self.assertEqual(runner._wait_exit(child.pid, runner.monotonic() + 5), 23)
            self.assertEqual(runner._observe(child.pid), 23)
            self.assertIsNone(child.returncode)
            self.assertEqual(child.wait(timeout=5), 23)
            with self.assertRaises(ChildProcessError):
                runner._observe(child.pid)
        finally:
            child.stdin.close()
            child.wait(timeout=5)

    def test_zombie_only_owned_group_retains_anchor_until_cleanup_then_reaps(self):
        # Given an exited runner, cleanup must still own its unreaped session leader.
        # This injects Darwin's zombie-only EPERM at the real group-signal boundary;
        # the snapshot and actual-parent reap remain real on both supported platforms.
        anchors = []

        def denied_owned_group(pid, sig):
            observed = os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            anchors.append((pid, observed.si_status if observed else None))
            raise PermissionError("injected zombie-only group denial")

        with TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(runner.os, "killpg", side_effect=denied_owned_group):
                try:
                    result = runner.program(
                        [sys.executable, "-c", "raise SystemExit(7)"],
                        root,
                        dict(os.environ),
                        5,
                        root / "runner.log",
                    )
                except (ChildProcessError, PermissionError) as error:
                    result = str(error)
            self.assertEqual(result, 7)
            self.assertEqual(len(anchors), 1)
            self.assertEqual(anchors[0][1], 7)
            with self.assertRaises(ChildProcessError):
                os.waitpid(anchors[0][0], os.WNOHANG)

    def test_real_zombie_group_cleanup_preserves_signal_exit_status(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            status = runner.program(
                [sys.executable, "-c", "import os,signal; os.kill(os.getpid(),signal.SIGUSR1)"],
                root,
                dict(os.environ),
                5,
                root / "runner.log",
            )
        self.assertEqual(status, -signal.SIGUSR1)

    def test_real_zombie_snapshot_and_kernel_signal_outcome(self):
        report = "import os; print(os.nice(0), flush=True)"
        cases = {
            "inherited priority": report,
            "lowered priority": "import os; os.nice(5); " + report[len("import os; ") :],
        }
        for name, code in cases.items():
            with self.subTest(name):
                self._assert_zombie_snapshot(code, lowered=name == "lowered priority")

    def _assert_zombie_snapshot(self, code, lowered):
        child = subprocess.Popen(  # noqa: S603 - fixed interpreter and authored child code
            [sys.executable, "-c", code], stdout=subprocess.PIPE, start_new_session=True
        )
        try:
            niceness = int(child.stdout.readline())
            if lowered:
                self.assertGreater(niceness, 0)
            self.assertEqual(runner._wait_exit(child.pid, runner.monotonic() + 5), 0)
            owner = runner._Owner(child.pid, os.geteuid(), os.getuid())
            rows = runner._group_snapshot(owner, runner.monotonic() + 5)
            self.assertEqual(len(rows), 1)
            self.assertEqual(
                (
                    rows[0].pid,
                    rows[0].ppid,
                    rows[0].pgid,
                    rows[0].uid,
                    rows[0].ruid,
                    rows[0].state[:1],
                ),
                (child.pid, os.getpid(), child.pid, os.geteuid(), os.getuid(), "Z"),
            )
            if sys.platform == "darwin":
                with self.assertRaises(PermissionError):
                    os.killpg(child.pid, signal.SIGKILL)
            else:
                os.killpg(child.pid, signal.SIGKILL)
                self.assertEqual(rows[0].sid, child.pid)
            self.assertEqual(child.wait(timeout=5), 0)
        finally:
            child.wait(timeout=5)
            child.stdout.close()

    def test_early_leader_exit_allows_worker_cleanup_without_group_departure(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            cleaned = root / "cleaned"
            worker = (
                "import os,signal; from pathlib import Path\n"
                "def finish(signum,frame):\n"
                f"    Path({str(cleaned)!r}).write_text(str(os.getpgrp()))\n"
                "    raise SystemExit(0)\n"
                "signal.signal(signal.SIGTERM,finish)\n"
                "print('ready',flush=True)\n"
                "signal.pause()\n"
            )
            leader = (
                "import os,subprocess,sys; "
                f"child=subprocess.Popen([sys.executable,'-c',{worker!r}],stdout=subprocess.PIPE); "
                "child.stdout.readline(); print(os.getpid(),flush=True)"
            )
            status = runner.program(
                [sys.executable, "-c", leader], root, dict(os.environ), 5, root / "log"
            )
            self.assertEqual(status, 0)
            self.assertEqual(cleaned.read_text().strip(), (root / "log").read_text().strip())

    def test_timeout_kills_uncooperative_child_reaps_it_and_leaves_sentinel_alive(self):
        sentinel = subprocess.Popen([sys.executable, "-c", "import signal; signal.pause()"])
        observed = []
        sent = []
        wait_exit = runner._wait_exit
        killpg = os.killpg
        with TemporaryDirectory() as directory:
            root = Path(directory)
            ready = root / "ready"

            def timeout_after_ready(pid, deadline, cancel=None):
                if not observed:
                    self.assertTrue(wait_until(ready.exists, 5, interval=0.01))
                    observed.append(pid)
                    return None
                return wait_exit(pid, deadline, cancel)

            def owned_signal(pid, sig):
                self.assertEqual(pid, observed[0])
                sent.append(sig)
                return killpg(pid, sig)

            script = (
                "import signal; from pathlib import Path; "
                "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
                f"Path({str(ready)!r}).touch(); signal.pause()"
            )
            try:
                with (
                    patch.object(runner, "_wait_exit", side_effect=timeout_after_ready),
                    patch.object(runner, "STOP_SECONDS", 0.25),
                    patch.object(runner.os, "killpg", side_effect=owned_signal),
                ):
                    result = runner.program(
                        [sys.executable, "-c", script], root, dict(os.environ), 5, root / "log"
                    )
                self.assertIsNone(result)
                self.assertEqual(sent, [signal.SIGTERM, signal.SIGKILL])
                with self.assertRaises(ChildProcessError):
                    os.waitpid(observed[0], os.WNOHANG)
                self.assertIsNone(sentinel.poll())
            finally:
                sentinel.kill()
                sentinel.wait(timeout=5)

    def test_expired_execution_deadline_returns_timeout_after_cleanup(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            result = runner.program(
                [sys.executable, "-c", "import signal; signal.pause()"],
                root,
                dict(os.environ),
                0,
                root / "log",
            )
        self.assertIsNone(result)

    def test_injected_cancellation_and_cleanup_denial_preserve_both_original_errors(self):
        interruption = KeyboardInterrupt("initiating cancellation")
        denial = PermissionError("injected live-group denial")
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch.object(runner, "_wait_exit", side_effect=interruption),
                patch.object(runner, "_cleanup_group", side_effect=denial),
                self.assertRaises(BaseExceptionGroup) as raised,
            ):
                runner.program(
                    [sys.executable, "-c", "pass"], root, dict(os.environ), 5, root / "log"
                )
        self.assertEqual(raised.exception.exceptions, (interruption, denial))


class InjectedInspectionTest(unittest.TestCase):
    """Injected denial tables supplement the real platform zombie and live paths."""

    def test_denied_term_with_unknown_inspection_never_escalates_to_kill(self):
        sent = []
        denial = PermissionError("genuinely denied TERM")
        inspection = ValueError("unknown group state")

        def deny(pid, sig):
            sent.append((pid, sig))
            raise denial

        with (
            patch.object(runner, "_observe", return_value=None),
            patch.object(runner.os, "killpg", side_effect=deny),
            patch.object(runner, "_terminal_group", side_effect=inspection),
            self.assertRaises(PermissionError) as raised,
        ):
            runner._cleanup_group(runner._Owner(123, 501, 502), runner.monotonic() + 5, 1)
        self.assertIs(raised.exception, denial)
        self.assertIs(raised.exception.__cause__, inspection)
        self.assertEqual(sent, [(123, signal.SIGTERM)])

    def test_platform_queries_are_scoped_to_original_owned_group_or_session(self):
        with patch.object(runner.sys, "platform", "darwin"):
            self.assertEqual(
                runner._inspection_command(123),
                (["/bin/ps", "-g", "123", "-o", "pid=,ppid=,pgid=,uid=,ruid=,state="], False),
            )
        with patch.object(runner.sys, "platform", "linux"):
            self.assertEqual(
                runner._inspection_command(123),
                (["/bin/ps", "-s", "123", "-o", "pid=,ppid=,pgid=,sid=,uid=,ruid=,state="], True),
            )

    def test_consumed_anchor_prevents_any_further_group_signal(self):
        sent = []
        with (
            patch.object(runner, "_observe", side_effect=ChildProcessError("consumed anchor")),
            patch.object(runner.os, "killpg", side_effect=lambda *args: sent.append(args)),
            self.assertRaisesRegex(ChildProcessError, "consumed anchor"),
        ):
            runner._signal_owned(
                runner._Owner(123, 501, 502), signal.SIGKILL, runner.monotonic() + 5
            )
        self.assertEqual(sent, [])

    def test_inspection_consumes_the_same_cleanup_deadline(self):
        with (
            patch.object(runner, "monotonic", side_effect=(5, 6)),
            patch.object(
                runner.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 0, "123 99 123 501 502 Z\n", ""),
            ),
            self.assertRaisesRegex(TimeoutError, "cleanup deadline exhausted"),
        ):
            runner._group_snapshot(runner._Owner(123, 501, 502), 6)

    def test_only_complete_expected_owner_zombies_allow_suppressing_injected_eperm(self):
        owner = runner._Owner(123, 501, 502)
        cases = (
            ("terminal", "123 99 123 501 502 Z\n", True),
            ("live", "123 99 123 501 502 S\n", False),
            ("mixed", "123 99 123 501 502 Z\n124 123 123 501 502 S\n", False),
            ("foreign effective uid", "123 99 123 503 502 Z\n", False),
            ("foreign real uid", "123 99 123 501 503 Z\n", False),
            ("foreign group", "123 99 124 501 502 Z\n", False),
            ("foreign parent", "123 98 123 501 502 Z\n", False),
            ("unknown state", "123 99 123 501 502 X?\n", False),
        )
        for label, output, allowed in cases:
            with self.subTest(label=label):
                result = subprocess.CompletedProcess([], 0, output, "")
                denial = PermissionError(label)
                with (
                    patch.object(runner.sys, "platform", "darwin"),
                    patch.object(runner.os, "getpid", return_value=99),
                    patch.object(runner, "_observe", return_value=0),
                    patch.object(runner.os, "killpg", side_effect=denial),
                    patch.object(runner.subprocess, "run", return_value=result),
                ):
                    if allowed:
                        self.assertFalse(
                            runner._signal_owned(owner, signal.SIGKILL, runner.monotonic() + 5)
                        )
                    else:
                        with self.assertRaises(PermissionError) as raised:
                            runner._signal_owned(owner, signal.SIGKILL, runner.monotonic() + 5)
                        self.assertIs(raised.exception, denial)

    def test_macos_state_modifiers_follow_the_state_letter_when_classifying_terminal(self):
        owner = runner._Owner(123, 501, 502)
        cases = (
            ("niced zombie", "123 99 123 501 502 ZN\n", True),
            ("foreground zombie", "123 99 123 501 502 Z+\n", True),
            ("leader zombie", "123 99 123 501 502 Zs\n", True),
            ("niced sleeping", "123 99 123 501 502 SN\n", False),
            ("running foreground", "123 99 123 501 502 R+\n", False),
            ("sleeping", "123 99 123 501 502 S\n", False),
            ("idle", "123 99 123 501 502 I\n", False),
            ("foreign zombie", "123 99 123 503 502 ZN\n", False),
            (
                "niced zombie with live member",
                "123 99 123 501 502 ZN\n124 123 123 501 502 S\n",
                False,
            ),
        )
        for label, output, allowed in cases:
            with self.subTest(label=label):
                denial = PermissionError(label)
                with (
                    patch.object(runner.sys, "platform", "darwin"),
                    patch.object(runner.os, "getpid", return_value=99),
                    patch.object(runner, "_observe", return_value=0),
                    patch.object(runner.os, "killpg", side_effect=denial),
                    patch.object(
                        runner.subprocess,
                        "run",
                        return_value=subprocess.CompletedProcess([], 0, output, ""),
                    ),
                ):
                    if allowed:
                        self.assertFalse(
                            runner._signal_owned(owner, signal.SIGTERM, runner.monotonic() + 5)
                        )
                    else:
                        with self.assertRaises(PermissionError) as raised:
                            runner._signal_owned(owner, signal.SIGTERM, runner.monotonic() + 5)
                        self.assertIs(raised.exception, denial)

    def test_incomplete_or_failed_injected_inspection_retains_denial_with_diagnostic(self):
        cases = (
            ("empty", "", "", 0),
            ("truncated", "123 99 123 501 502 Z", "", 0),
            ("stderr", "123 99 123 501 502 Z\n", "warning", 0),
            ("malformed", "123 99 123 501 Z\n", "", 0),
            ("nonnumeric", "123 99 123 invalid 502 Z\n", "", 0),
            ("missing anchor", "124 99 123 501 502 Z\n", "", 0),
            ("duplicate", "123 99 123 501 502 Z\n123 99 123 501 502 Z\n", "", 0),
        )
        for label, output, stderr, status in cases:
            with self.subTest(label=label):
                denial = PermissionError(label)
                with (
                    patch.object(runner.sys, "platform", "darwin"),
                    patch.object(runner.os, "getpid", return_value=99),
                    patch.object(runner, "_observe", return_value=0),
                    patch.object(runner.os, "killpg", side_effect=denial),
                    patch.object(
                        runner.subprocess,
                        "run",
                        return_value=subprocess.CompletedProcess([], status, output, stderr),
                    ),
                    self.assertRaises(PermissionError) as raised,
                ):
                    runner._signal_owned(
                        runner._Owner(123, 501, 502), signal.SIGTERM, runner.monotonic() + 5
                    )
                self.assertIs(raised.exception, denial)
                self.assertIsNotNone(raised.exception.__cause__)

    def test_injected_linux_snapshot_filters_other_groups_and_rejects_foreign_sid(self):
        owner = runner._Owner(123, 501, 502)
        for sid, terminal in ((123, True), (999, False)):
            with self.subTest(sid=sid):
                output = f"123 99 123 {sid} 501 502 Z\n124 99 124 123 501 502 S\n"
                with (
                    patch.object(runner.sys, "platform", "linux"),
                    patch.object(runner.os, "getpid", return_value=99),
                    patch.object(runner, "_observe", return_value=0),
                    patch.object(
                        runner.subprocess,
                        "run",
                        return_value=subprocess.CompletedProcess([], 0, output, ""),
                    ),
                ):
                    self.assertEqual(
                        runner._terminal_group(owner, runner.monotonic() + 5), terminal
                    )

    def test_injected_inspection_timeout_or_nonzero_retains_original_permission_error(self):
        errors = (
            subprocess.TimeoutExpired("/bin/ps", 1),
            subprocess.CalledProcessError(1, "/bin/ps", stderr="failed inspection"),
        )
        for inspection in errors:
            with self.subTest(error=type(inspection).__name__):
                denial = PermissionError("injected inspection failure")
                with (
                    patch.object(runner, "_observe", return_value=0),
                    patch.object(runner.os, "killpg", side_effect=denial),
                    patch.object(runner.subprocess, "run", side_effect=inspection),
                    self.assertRaises(PermissionError) as raised,
                ):
                    runner._signal_owned(
                        runner._Owner(123, 501, 502), signal.SIGTERM, runner.monotonic() + 5
                    )
                self.assertIs(raised.exception, denial)
                self.assertIs(raised.exception.__cause__, inspection)
