"""Legacy child close, observer and external caller share the original cleanup allowance."""

import subprocess
import sys
import threading
from contextlib import contextmanager

from test_request_supervision import authorization

from nci_si_acceptance import request_children, request_supervision
from nci_si_acceptance.request_stdio import _PortalCall
from nci_si_acceptance.request_supervision import SupervisedRun


@contextmanager
def held_pipe_child(release, closing):
    child = subprocess.Popen(
        [sys.executable, "-c", "pass"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stream = child.stdout

    class HeldPipe:
        def close(self):
            closing.set()
            release.wait()
            stream.close()

    try:
        assert child.wait(timeout=3) == 0
        child.stdout = HeldPipe()
        yield child
    finally:
        release.set()
        child.stdout = stream
        for pipe in (child.stdin, child.stdout, child.stderr):
            pipe.close()
        assert child.wait(timeout=3) == 0
        assert all(pipe.closed for pipe in (child.stdin, child.stdout, child.stderr))


def test_mixed_owners_retain_the_original_deadline_and_actual_pending_custody(
    tmp_path, monkeypatch
):
    release, pulse, closing, finished = (threading.Event() for _ in range(4))
    owner = SupervisedRun.create(authorization(), tmp_path / "run")
    call = _PortalCall(release.wait)
    actual_pulse, actual_stop, actual_finish = (
        owner.run.heartbeat,
        request_supervision.stop_child,
        call.finish,
    )
    seen, failures = [], []

    def heartbeat():
        actual_pulse()
        pulse.set()
        release.wait()

    def stop(process, **options):
        seen.append(options["deadline"])
        return actual_stop(process, **options)

    def finish(deadline):
        seen.append(deadline)
        actual_finish(deadline)

    def shutdown():
        try:
            owner.__exit__(None, None, None)
        except BaseException as error:  # noqa: BLE001 - assert the retained shutdown outcome below.
            failures.append(error)
        finally:
            finished.set()

    finisher = threading.Thread(target=shutdown, name="owned-mixed-finisher")
    for target, name, value in (
        (owner.run, "heartbeat", heartbeat),
        (request_supervision, "stop_child", stop),
        (request_supervision, "PULSE_SECONDS", 0.01),
        (request_children, "CLEANUP_SECONDS", 0.1),
        (call, "finish", finish),
    ):
        monkeypatch.setattr(target, name, value)
    with held_pipe_child(release, closing) as child:
        try:
            owner.__enter__()
            owner.register_child(child, participant="worker", role="worker")
            owner.register_completion(call, participant="caller")
            call.start()
            assert pulse.wait(2)
            deadline = owner.cleanup_deadline()
            finisher.start()
            returned = finished.wait(0.5)
            assert returned and closing.wait(1)
            assert [item is deadline for item in seen] == [True, True]
            assert deadline.remaining() == 0
            assert owner._observer.is_alive() and call.thread.is_alive()
            assert set(owner.pending_owners) == {"controlled-child-close-worker", call.name}
            assert (
                owner.snapshot()["state"] == "failed"
                and not owner.snapshot()["observationComplete"]
            )
            assert failures
        finally:
            release.set()
            owner._stop.set()
            for thread in [finisher, owner._observer, call.thread, *owner._closing_threads]:
                if thread.ident is not None:
                    thread.join(5)
                assert not thread.is_alive()
