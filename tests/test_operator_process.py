"""Cancellation and deadlines terminate only the worker process group created for this job."""

import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from scripts.operator_process import LIFELINE, MAX_REPORT_BYTES, _wait, run_owned
from scripts.operator_worker import clean_environment


class OperatorProcessTest(unittest.TestCase):
    # The reap is this test's claim; the length of the grace between SIGTERM and SIGKILL is
    # owned by tests/test_processes.py, so a resistant worker is not waited out for 10 s.
    @patch("scripts.processes.STOP_SECONDS", 0.5)
    def test_running_cancellation_reaps_normal_and_sigterm_resistant_workers(self):
        for resistant in (False, True):
            with self.subTest(resistant=resistant), TemporaryDirectory() as temporary:
                directory = Path(temporary)
                ready = directory / "ready"
                late = directory / "late"
                handler = "signal.SIG_IGN" if resistant else "signal.SIG_DFL"
                code = (
                    "import os,signal,time; "
                    f"signal.signal(signal.SIGTERM, {handler}); "
                    f"open({str(ready)!r},'w').write(str(os.getpid())); time.sleep(300); "
                    f"open({str(late)!r},'w').write('unexpected')"
                )
                cancelled = threading.Event()
                with ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(
                        run_owned,
                        [sys.executable, "-c", code],
                        directory=directory,
                        environment=clean_environment(directory),
                        seconds=60,
                        cancelled=cancelled,
                    )
                    try:
                        deadline = time.monotonic() + 5
                        while not ready.exists() and time.monotonic() < deadline:
                            time.sleep(0.01)
                        self.assertTrue(ready.exists())
                    finally:
                        cancelled.set()
                    result = future.result(timeout=15)
                self.assertEqual((result["state"], result["reason"]), ("cancelled", "cancelled"))
                self.assertNotEqual(result["exit_code"], 0)
                with self.assertRaises(ProcessLookupError):
                    os.kill(int(ready.read_text()), 0)
                self.assertFalse(late.exists())

    def test_successful_worker_cannot_bypass_report_size_or_symlink_checks(self):
        for linked, reason in ((False, "output_limit"), (True, "invalid_evidence")):
            with self.subTest(linked=linked), TemporaryDirectory() as temporary:
                directory = Path(temporary)
                bundle = directory / "bundle"
                bundle.mkdir()
                private = directory / "private"
                private.write_text("PRIVATE-CANARY")
                report = bundle / "report.json"
                if linked:
                    report.symlink_to(private)
                else:
                    with report.open("wb") as stream:
                        stream.truncate(MAX_REPORT_BYTES + 1)
                result = run_owned(
                    [sys.executable, "-c", "pass"],
                    directory=directory,
                    environment=clean_environment(directory),
                    seconds=5,
                    cancelled=threading.Event(),
                )
                self.assertEqual((result["state"], result["reason"]), ("failed", reason))
                self.assertEqual(private.read_text(), "PRIVATE-CANARY")

    def test_a_report_that_appears_as_the_worker_exits_is_still_judged(self):
        # The worker is seen running with no report, then finished with a linked one: only the
        # check after the wait can see it.
        with TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "bundle").mkdir()
            (directory / "private").write_text("PRIVATE-CANARY")
            answers = [None, 0]

            def poll():
                answer = answers.pop(0)
                if answer is not None:
                    (directory / "bundle/report.json").symlink_to(directory / "private")
                return answer

            process = SimpleNamespace(poll=poll)
            reason = _wait(process, directory, 5, threading.Event())
        self.assertEqual(reason, "invalid_evidence")

    def test_normal_worker_exit_stops_a_descendant_that_ignores_termination(self):
        with TemporaryDirectory() as temporary:
            directory = Path(temporary)
            marker = directory / "child.json"
            child_code = (
                "import os,signal,time,json; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                f"open({str(marker)!r},'w').write(json.dumps(os.getpid())); time.sleep(300)"
            )
            leader_code = (
                "import subprocess,sys,time,pathlib; "
                f"subprocess.Popen([sys.executable,'-c',{child_code!r}]); "
                f"marker=pathlib.Path({str(marker)!r}); "
                "\nwhile not marker.exists(): time.sleep(.01)"
            )
            result = run_owned(
                [sys.executable, "-c", leader_code],
                directory=directory,
                environment=clean_environment(directory),
                seconds=5,
                cancelled=threading.Event(),
            )
            pid = json.loads(marker.read_text())
            try:
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        break
                    time.sleep(0.01)
                else:
                    self.fail("Owned descendant survived normal worker completion")
            finally:
                with suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)
        self.assertEqual(result["state"], "completed")

    def test_lost_parent_pipe_stops_the_owned_worker_without_a_pid_file(self):
        read_fd, write_fd = os.pipe()
        with TemporaryDirectory() as temporary:
            directory = Path(temporary)
            marker = directory / "ready"
            code = (
                "from scripts.operator_process import watch_parent; import time,signal; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                f"watch_parent(); open({str(marker)!r},'w').write('ready'); time.sleep(300)"
            )
            child = subprocess.Popen(  # noqa: S603 - owned lifeline test worker
                [sys.executable, "-c", code],
                pass_fds=(read_fd,),
                start_new_session=True,
                env=clean_environment(directory) | {LIFELINE: str(read_fd)},
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            os.close(read_fd)
            try:
                deadline = time.monotonic() + 5
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(marker.exists())
                os.close(write_fd)
                write_fd = -1
                self.assertEqual(child.wait(timeout=5), -signal.SIGKILL)
            finally:
                if write_fd >= 0:
                    os.close(write_fd)
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=5)

    def test_deadline_reaps_owned_worker_and_does_not_claim_success(self):
        # The worker's identity comes from the real launch, so the proof does not depend on how
        # quickly a new interpreter starts (#660; it once failed waiting for a child-written PID).
        started = []

        class Recording(subprocess.Popen):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                started.append(self)

        with TemporaryDirectory() as temporary:
            directory = Path(temporary)
            try:
                with patch("scripts.operator_process.subprocess.Popen", Recording):
                    result = run_owned(
                        [sys.executable, "-c", "import time; time.sleep(300)"],
                        directory=directory,
                        environment=clean_environment(directory),
                        seconds=1,
                        cancelled=threading.Event(),
                    )
                self.assertEqual(result["state"], "failed")
                self.assertEqual(result["reason"], "deadline")
                self.assertNotEqual(result["exit_code"], 0)
                self.assertEqual(len(started), 1)
                # Reaped by run_owned: the handle has a return code and the PID is gone.
                self.assertIsNotNone(started[0].returncode)
                with self.assertRaises(ProcessLookupError):
                    os.kill(started[0].pid, 0)
            finally:
                for process in started:
                    if process.poll() is None:
                        process.kill()
                        process.wait()

    def test_already_cancelled_job_starts_no_process(self):
        cancelled = threading.Event()
        cancelled.set()
        with TemporaryDirectory() as temporary:
            directory = Path(temporary)
            marker = directory / "started"
            result = run_owned(
                [sys.executable, "-c", f"open({str(marker)!r},'w').write('started')"],
                directory=directory,
                environment=clean_environment(directory),
                seconds=5,
                cancelled=cancelled,
            )
            self.assertFalse(marker.exists())
        self.assertEqual(result["state"], "cancelled")
        self.assertIsNone(result["exit_code"])

    def test_normal_completion_keeps_actual_nonzero_exit_without_a_retry(self):
        with TemporaryDirectory() as temporary:
            directory = Path(temporary)
            result = run_owned(
                [sys.executable, "-c", "raise SystemExit(7)"],
                directory=directory,
                environment=clean_environment(directory),
                seconds=5,
                cancelled=threading.Event(),
            )
        self.assertEqual(result, {"state": "failed", "exit_code": 7, "reason": "worker_failed"})


class WorkerLogTest(unittest.TestCase):
    PART = 512 * 1024  # the documented bound: the first and the last 512 KiB

    def run_worker(self, directory, code, *, cancelled=None, seconds=60):
        return run_owned(
            [sys.executable, "-c", code],
            directory=directory,
            environment=clean_environment(directory),
            seconds=seconds,
            cancelled=cancelled or threading.Event(),
        )

    def test_worker_output_is_kept_in_a_private_job_log_bounded_to_its_head_and_tail(self):
        part = self.PART
        flood = f"sys.stdout.buffer.write(b'H' * {part} + b'x' * {part} + b'T' * {part})"
        for output, expected in (
            ("sys.stdout.write('OUT\\n')", b"ERR\nOUT\n"),
            (
                flood,
                b"ERR\n"
                + b"H" * (part - 4)
                + f"\n[worker.log: {part + 4} bytes omitted]\n".encode()
                + b"T" * part,
            ),
        ):
            with self.subTest(size=len(expected)), TemporaryDirectory() as temporary:
                directory = Path(temporary)
                code = (
                    "import sys; sys.stderr.write('ERR\\n'); sys.stderr.flush(); "
                    f"{output}; sys.stdout.flush(); sys.exit(3)"
                )
                result = self.run_worker(directory, code)
                log = directory / "worker.log"
                self.assertEqual(
                    result, {"state": "failed", "exit_code": 3, "reason": "worker_failed"}
                )
                self.assertTrue(log.is_file())
                self.assertEqual(log.stat().st_mode & 0o777, 0o600)
                self.assertEqual(log.read_bytes(), expected)

    def test_a_cancelled_worker_flooding_its_output_is_reaped_and_its_log_kept(self):
        with TemporaryDirectory() as temporary:
            directory = Path(temporary)
            ready = directory / "ready"
            log = directory / "worker.log"
            code = (
                "import os,sys; "
                f"open({str(ready)!r},'w').write(str(os.getpid()))\n"
                "while True: sys.stdout.buffer.write(b'x' * 65536); sys.stdout.flush()"
            )
            cancelled = threading.Event()
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(self.run_worker, directory, code, cancelled=cancelled)
                try:
                    # More than a pipe buffer has been copied while the worker still writes.
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline and not (
                        ready.exists() and log.exists() and log.stat().st_size > 4 * 65536
                    ):
                        time.sleep(0.01)
                    self.assertTrue(log.is_file() and log.stat().st_size > 4 * 65536)
                finally:
                    cancelled.set()
                result = future.result(timeout=15)
            self.assertEqual((result["state"], result["reason"]), ("cancelled", "cancelled"))
            with self.assertRaises(ProcessLookupError):
                os.kill(int(ready.read_text()), 0)
            parts = re.split(rb"\n\[worker\.log: \d+ bytes omitted\]\n", log.read_bytes())
            self.assertIn(len(parts), (1, 2))
            for part in parts:
                self.assertEqual(part, b"x" * len(part))
                self.assertLessEqual(len(part), self.PART)

    def test_a_log_that_cannot_be_written_never_blocks_the_worker(self):
        with TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "worker.log").mkdir()
            code = "import sys; sys.stdout.buffer.write(b'x' * (2 * 1024 * 1024)); sys.exit(0)"
            result = self.run_worker(directory, code, seconds=30)
            self.assertEqual(result, {"state": "completed", "exit_code": 0, "reason": None})

    @patch("scripts.operator_process.LOG_JOIN_SECONDS", 0.2)
    def test_a_pipe_held_outside_the_stopped_group_ends_the_run_with_a_diagnostic(self):
        with TemporaryDirectory() as temporary:
            directory = Path(temporary)
            holder = directory / "holder"
            # The holder leaves the worker's group, so stopping the group leaves the pipe open.
            code = (
                "import subprocess,sys,pathlib; "
                "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(300)'], "
                "start_new_session=True); "
                f"pathlib.Path({str(holder)!r}).write_text(str(p.pid))"
            )
            pool = ThreadPoolExecutor(max_workers=1)
            try:
                with self.assertLogs("scripts.operator_process", logging.ERROR) as logs:
                    result = pool.submit(self.run_worker, directory, code).result(timeout=30)
                self.assertEqual(result, {"state": "completed", "exit_code": 0, "reason": None})
                self.assertEqual(
                    [json.loads(line.split(":", 2)[2])["event"] for line in logs.output],
                    ["worker_log_open"],
                )
            finally:
                if holder.exists():
                    with suppress(ProcessLookupError):
                        os.kill(int(holder.read_text()), signal.SIGKILL)
                pool.shutdown()

    def test_a_log_that_cannot_be_written_is_diagnosed_by_its_error_type(self):
        with TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "worker.log").mkdir()
            with self.assertLogs("scripts.operator_process", logging.ERROR) as logs:
                self.run_worker(directory, "print('x')", seconds=30)
            records = [json.loads(line.split(":", 2)[2]) for line in logs.output]
            self.assertEqual(
                [(r["event"], r["errorType"]) for r in records],
                [("worker_log_failed", "FileExistsError")],
            )

    def test_the_log_holds_the_output_so_far_while_the_worker_still_runs(self):
        with TemporaryDirectory() as temporary:
            directory = Path(temporary)
            release = directory / "release"
            log = directory / "worker.log"
            code = (
                "import sys,time,pathlib; sys.stdout.write('MARKER\\n'); sys.stdout.flush()\n"
                f"while not pathlib.Path({str(release)!r}).exists(): time.sleep(.01)"
            )
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(self.run_worker, directory, code)
                try:
                    # Polling is a hang guard; the claim is the content seen before the exit.
                    deadline = time.monotonic() + 15
                    while time.monotonic() < deadline:
                        if log.is_file() and log.read_bytes() == b"MARKER\n":
                            break
                        time.sleep(0.01)
                    self.assertFalse(future.done())
                    self.assertEqual(log.read_bytes(), b"MARKER\n")
                finally:
                    release.write_text("go")
                result = future.result(timeout=30)
            self.assertEqual(result["state"], "completed")


def _open_descriptors():
    return set(os.listdir("/dev/fd"))


class DescriptorHygieneTest(unittest.TestCase):
    def run_worker(self, directory, command):
        return run_owned(
            command,
            directory=directory,
            environment=clean_environment(directory),
            seconds=30,
            cancelled=threading.Event(),
        )

    def test_a_run_leaves_no_descriptor_open_whether_or_not_the_worker_starts(self):
        for name, command in (
            ("finished", [sys.executable, "-c", "print('x')"]),
            ("missing", ["/nonexistent/worker"]),
        ):
            with self.subTest(name), TemporaryDirectory() as temporary:
                directory = Path(temporary)
                before = _open_descriptors()
                if name == "missing":
                    with self.assertRaises(FileNotFoundError):
                        self.run_worker(directory, command)
                else:
                    self.assertEqual(self.run_worker(directory, command)["state"], "completed")
                self.assertEqual(_open_descriptors(), before)

    def test_a_copier_that_cannot_start_leaves_neither_worker_nor_descriptor(self):
        real_start = threading.Thread.start
        started = []

        def start(thread):
            if thread.name == "validation-worker-log":
                raise RuntimeError("can't start new thread")
            real_start(thread)

        class Recording(subprocess.Popen):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                started.append(self)

        with TemporaryDirectory() as temporary:
            directory = Path(temporary)
            before = _open_descriptors()
            try:
                with (
                    patch.object(threading.Thread, "start", start),
                    patch("scripts.operator_process.subprocess.Popen", Recording),
                    self.assertRaisesRegex(RuntimeError, "can't start new thread"),
                ):
                    self.run_worker(
                        directory, [sys.executable, "-c", "import time; time.sleep(300)"]
                    )
                self.assertEqual(_open_descriptors(), before)
                self.assertEqual(len(started), 1)
                # Reaped by the cleanup: a worker still running has no return code.
                self.assertIsNotNone(started[0].returncode)
            finally:
                for process in started:
                    process.kill()
                    process.wait()

    def test_the_copier_thread_never_keeps_an_exited_interpreter_alive(self):
        # A holder in another session keeps the pipe open past the run; the interpreter that
        # called run_owned must still exit.
        root = Path(__file__).resolve().parents[1]
        with TemporaryDirectory() as temporary:
            directory = Path(temporary)
            holder = directory / "holder"
            worker = (
                "import subprocess,sys,pathlib; "
                "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(300)'], "
                "start_new_session=True); "
                f"pathlib.Path({str(holder)!r}).write_text(str(p.pid))"
            )
            driver = (
                "import sys, threading; from pathlib import Path\n"
                "import scripts.operator_process as op\n"
                "op.LOG_JOIN_SECONDS = 0.2\n"
                "from scripts.operator_worker import clean_environment\n"
                f"d = Path({str(directory)!r})\n"
                f"r = op.run_owned([sys.executable, '-c', {worker!r}], directory=d,\n"
                "    environment=clean_environment(d), seconds=30, cancelled=threading.Event())\n"
                "print(r['state'])\n"
            )
            try:
                done = subprocess.run(  # noqa: S603 - a fixed driver of this test
                    [sys.executable, "-c", driver],
                    cwd=root,
                    env=clean_environment(directory) | {"PYTHONPATH": str(root)},
                    capture_output=True,
                    timeout=120,  # a hang guard only
                    check=False,
                )
            finally:
                if holder.exists():
                    with suppress(ProcessLookupError):
                        os.kill(int(holder.read_text()), signal.SIGKILL)
            self.assertEqual((done.returncode, done.stdout.strip()), (0, b"completed"))
