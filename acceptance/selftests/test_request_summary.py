"""The diagnostic reader accepts only the declared sanitized summary contract."""

import copy
import importlib
import importlib.util
import json
import threading

import pytest

from nci_si_acceptance.request_authorization import Authorization, Limits, RequestGrant
from nci_si_mcp.http_client import RequestPolicyError

FAILURE_COUNT = 2


def reader():
    name = "nci_si_acceptance.request_summary"
    assert importlib.util.find_spec(name) is not None, "A diagnostic caller needs read_summary"
    return importlib.import_module(name).read_summary


def record():
    return {
        "schemaVersion": 1,
        "runId": "b" * 32,
        "policySha256": "a" * 64,
        "state": "failed",
        "limits": {
            "logical": 2,
            "attempts": 3,
            "per_logical": 2,
            "concurrent": 2,
            "seconds": 10.0,
            "response_bytes": 8,
            "total_bytes": 20,
        },
        "reservations": {"logical": 1, "attempts": 2, "bodyBytes": 9},
        "observed": {"transportEntries": 1, "bodyBytes": 4, "peakActive": 1},
        "denials": None,
        "observationComplete": False,
    }


def read(path):
    return reader()(path, run_id="b" * 32, policy_sha256="a" * 64)


def test_reader_preserves_known_bytes_in_failed_uncertain_run(tmp_path):
    path = tmp_path / "summary.json"
    path.write_text(json.dumps(record()))
    assert read(path) == record()


@pytest.mark.parametrize(
    "counts,complete,accepted",
    [
        ((0, 0, 5, 0, 5, 0), True, False),
        ((0, 0, 1, 0, 0, 0), False, False),
        ((1, 1, 5, 0, 5, 1), True, False),
        ((1, 1, 10, 0, 0, 1), False, False),
        ((1, 2, 10, 1, 10, 1), True, False),
        ((1, 1, 0, 0, 0, 0), True, False),
        ((1, 1, 9, 1, 9, 1), True, True),
        ((1, 1, 0, 1, 0, 1), True, True),
        ((1, 1, 9, 0, 0, 1), False, True),
        ((1, 1, 0, 0, 0, 1), True, True),
        ((1, 2, 18, 1, 5, 2), False, True),
        pytest.param((3, 2, 9, 1, 4, 1), False, False, id="logical-ceiling"),
        pytest.param((2, 2, 9, 1, 4, 1), False, True, id="logical-boundary"),
        pytest.param((2, 4, 9, 1, 4, 1), False, False, id="global-attempt-ceiling"),
        pytest.param((2, 3, 9, 1, 4, 1), False, True, id="global-attempt-boundary"),
        pytest.param((1, 1, 4, 1, 5, 1), False, False, id="known-reserved-ceiling"),
        pytest.param((1, 1, 5, 1, 5, 1), False, True, id="known-reserved-boundary"),
        pytest.param((2, 3, 21, 1, 4, 2), False, False, id="total-byte-ceiling"),
        pytest.param((2, 3, 20, 1, 4, 2), False, True, id="total-byte-boundary"),
        pytest.param((1, 1, 9, 1, 4, 2), False, False, id="peak-attempt-ceiling"),
        pytest.param((1, 1, 9, 1, 4, 1), False, True, id="peak-attempt-boundary"),
        pytest.param((2, 3, 9, 1, 4, 3), False, False, id="peak-concurrency-ceiling"),
        pytest.param((2, 3, 9, 1, 4, 2), False, True, id="peak-concurrency-boundary"),
    ],
)
def test_reader_enforces_attempt_and_entry_byte_capacity(tmp_path, counts, complete, accepted):
    logical, attempts, reserved, entries, known, peak = counts
    value = record() | {"observationComplete": complete}
    value["reservations"] = {"logical": logical, "attempts": attempts, "bodyBytes": reserved}
    value["observed"] = {"transportEntries": entries, "bodyBytes": known, "peakActive": peak}
    path = tmp_path / "summary.json"
    path.write_text(json.dumps(value))
    if accepted:
        assert read(path) == value
    else:
        with pytest.raises(RequestPolicyError):
            read(path)


@pytest.mark.parametrize(
    "section,key,value",
    [
        (None, "schemaVersion", True),
        (None, "runId", "c" * 32),
        (None, "runId", None),
        (None, "runId", "Z" * 32),
        (None, "policySha256", "c" * 64),
        (None, "state", "closed"),
        (None, "observationComplete", 1),
        (None, "denials", {}),
        (None, "path", "/private/credential"),
        (None, "denials", {"accounting": 0}),
        (None, "limits", None),
        (None, "observed", []),
        (None, "reservations", None),
        ("limits", "seconds", float("nan")),
        ("limits", "seconds", "10"),
        ("limits", "seconds", float("inf")),
        ("limits", "logical", True),
        ("limits", "total_bytes", 8),
        ("limits", "unknown", 1),
        ("reservations", "logical", -1),
        ("reservations", "attempts", 3),
        ("reservations", "bodyBytes", 21),
        ("observed", "bodyBytes", 10),
        ("observed", "transportEntries", 3),
        ("observed", "peakActive", 3),
        ("observed", "bodyBytes", True),
    ],
)
def test_reader_rejects_invalid_summary(tmp_path, section, key, value):
    path = tmp_path / "summary.json"
    invalid = copy.deepcopy(record())
    target = invalid if section is None else invalid[section]
    target[key] = value
    path.write_text(json.dumps(invalid))
    operation = reader()
    with pytest.raises(RequestPolicyError, match=r"^Controlled upstream request refused[.]$"):
        operation(path, run_id="b" * 32, policy_sha256="a" * 64)


def test_completed_reader_requires_complete_observation(tmp_path):
    path = tmp_path / "summary.json"
    path.write_text(json.dumps(record() | {"state": "completed"}))
    operation = reader()
    with pytest.raises(RequestPolicyError):
        operation(path, run_id="b" * 32, policy_sha256="a" * 64)


def test_completed_reader_rejects_unresolved_capacity(tmp_path):
    path = tmp_path / "summary.json"
    value = record() | {"state": "completed", "observationComplete": True}
    value["observed"]["bodyBytes"] = 5
    value["reservations"]["bodyBytes"] = 6
    path.write_text(json.dumps(value))
    operation = reader()
    with pytest.raises(RequestPolicyError):
        operation(path, run_id="b" * 32, policy_sha256="a" * 64)


def test_write_and_cleanup_failures_are_both_sanitized(tmp_path, monkeypatch):
    module = importlib.import_module("nci_si_acceptance.request_summary")

    def replace_failure(_source, _target):
        raise OSError("private initiating path")

    def unlink_failure(_path, **_options):
        raise PermissionError("private cleanup path")

    monkeypatch.setattr(module.os, "replace", replace_failure)
    monkeypatch.setattr(module.Path, "unlink", unlink_failure)
    failure = None
    try:
        module.publish(tmp_path / "summary.json", record())
    except (OSError, RequestPolicyError, ExceptionGroup) as error:
        failure = error
    assert isinstance(failure, ExceptionGroup)
    assert len(failure.exceptions) == FAILURE_COUNT
    assert all(type(error) is RequestPolicyError for error in failure.exceptions)
    assert all(str(error) == "Controlled upstream request refused." for error in failure.exceptions)
    assert "private" not in repr(failure)


def test_successful_replace_needs_no_second_cleanup(tmp_path, monkeypatch):
    module = importlib.import_module("nci_si_acceptance.request_summary")

    def unlink_failure(_path, **_options):
        raise PermissionError("private cleanup path")

    monkeypatch.setattr(module.Path, "unlink", unlink_failure)
    failure = None
    try:
        module.publish(tmp_path / "summary.json", record())
    except OSError as error:
        failure = type(error).__name__
    assert failure is None
    assert json.loads((tmp_path / "summary.json").read_text()) == record()


def managed(directory):
    entry = importlib.import_module("nci_si_acceptance.request_supervision")
    auth = Authorization((RequestGrant("GET", "http://127.0.0.1:9/probe", ()),), Limits(), "a" * 64)
    return entry.SupervisedRun.create(auth, directory)


@pytest.mark.parametrize("phase", ["initial", "final"])
@pytest.mark.parametrize("interruption", [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize("cleanup_fault", [False, True])
def test_interrupted_publication_retains_failure_and_cleanup(
    tmp_path, monkeypatch, interruption, cleanup_fault, phase
):
    owner = managed(tmp_path)
    module = importlib.import_module("nci_si_acceptance.request_summary")
    before = set(owner.run.directory.iterdir())
    original_unlink = module.Path.unlink
    initiating = interruption("synthetic publication interruption")
    temporary_state = "incomplete"
    summary = owner.run.directory / "summary.json"
    initial = None

    def interrupt(_descriptor):
        raise initiating

    def unlink(path, **options):
        if cleanup_fault and path.parent == owner.run.directory:
            raise OSError("synthetic temporary cleanup failure")
        return original_unlink(path, **options)

    def install():
        monkeypatch.setattr("os.fsync", interrupt)
        monkeypatch.setattr(module.Path, "unlink", unlink)

    if phase == "initial":
        install()
    failure = None
    try:
        with owner:
            assert phase == "final", "Interrupted publication must not open admission"
            before = set(owner.run.directory.iterdir())
            initial = summary.read_bytes()
            temporary_state = "completed"
            install()
    except (BaseExceptionGroup, RequestPolicyError, KeyboardInterrupt, SystemExit) as error:
        failure = error
    assert type(failure) is (BaseExceptionGroup if cleanup_fault else interruption)
    errors = failure.exceptions if cleanup_fault else (failure,)
    assert errors[0] is initiating
    assert [type(error) for error in errors] == [interruption] + [
        RequestPolicyError
    ] * cleanup_fault
    assert owner.run.snapshot().state == "closed"
    assert owner.snapshot()["state"] == "failed"
    assert (summary.read_bytes() if summary.exists() else None) == initial
    remaining = set(owner.run.directory.iterdir()) - before
    assert len(remaining) == int(cleanup_fault)
    for path in remaining:
        assert json.loads(path.read_text())["state"] == temporary_state
        original_unlink(path)
    assert set(owner.run.directory.iterdir()) == before


@pytest.mark.parametrize("phase", ["initial", "final"])
@pytest.mark.parametrize("fault", ["write", "flush", "fsync", "replace"])
def test_initial_and_final_publication_faults_are_terminal(tmp_path, monkeypatch, phase, fault):
    module = importlib.import_module("nci_si_acceptance.request_summary")
    original_factory = module.tempfile.NamedTemporaryFile
    owner = managed(tmp_path)
    initial = None

    def fail(*_args, **_kwargs):
        raise OSError("private publication diagnostic")

    def failing_file(**options):
        stream = original_factory(**options)
        monkeypatch.setattr(stream, fault, fail)
        return stream

    def install():
        if fault in {"write", "flush"}:
            monkeypatch.setattr(module.tempfile, "NamedTemporaryFile", failing_file)
        else:
            monkeypatch.setattr(module.os, fault, fail)

    if phase == "initial":
        install()
    with pytest.raises(RequestPolicyError) as caught, owner:
        initial = (owner.run.directory / "summary.json").read_bytes()
        install()
    assert str(caught.value) == "Controlled upstream request refused."
    assert owner.run.snapshot().state != "open"
    assert owner.snapshot()["state"] == "failed"
    path = owner.run.directory / "summary.json"
    if initial is None:
        assert not path.exists()
        assert owner.run.snapshot().logical == 0
    else:
        assert path.read_bytes() == initial
        assert json.loads(initial)["state"] == "incomplete"


def test_new_failed_run_cannot_reuse_old_completed_summary(tmp_path, monkeypatch):
    first = managed(tmp_path)
    with first:
        pass
    path = first.run.directory / "summary.json"
    original = path.read_bytes()
    second = managed(tmp_path)

    def fail(_source, _target):
        raise OSError("synthetic initial publication failure")

    monkeypatch.setattr("os.replace", fail)
    with pytest.raises(RequestPolicyError), second:
        pytest.fail("The fresh run cannot complete its initial publication")
    assert first.run.directory != second.run.directory
    assert path.read_bytes() == original
    assert not (second.run.directory / "summary.json").exists()


def test_reader_sees_whole_old_or_new_record_during_publication(tmp_path, monkeypatch):
    module = importlib.import_module("nci_si_acceptance.request_summary")
    path = tmp_path / "summary.json"
    original, updated = record(), record()
    updated["observed"]["bodyBytes"] = 5
    module.publish(path, original)
    midway, proceed = threading.Event(), threading.Event()
    failures = []

    def partial_dump(value, stream, **_options):
        payload = json.dumps(value)
        middle = len(payload) // 2
        stream.write(payload[:middle])
        stream.flush()
        midway.set()
        assert proceed.wait(3), "The reader did not release its owned writer"
        stream.write(payload[middle:])

    def write():
        try:
            module.publish(path, updated)
        except (RequestPolicyError, BaseExceptionGroup) as error:
            failures.append(type(error).__name__)

    monkeypatch.setattr(module.json, "dump", partial_dump)
    writer = threading.Thread(target=write)
    writer.start()
    try:
        assert midway.wait(3), "The writer did not reach its partial temporary write"
        assert read(path) == original
    finally:
        proceed.set()
        writer.join(timeout=3)
        assert not writer.is_alive()
    assert failures == []
    assert read(path) == updated


@pytest.mark.parametrize("path", [None, "summary.json", True])
def test_reader_refuses_invalid_path_before_attribute_access(path):
    failure = None
    try:
        read(path)
    except (AttributeError, RequestPolicyError) as error:
        failure = error
    assert type(failure) is RequestPolicyError
