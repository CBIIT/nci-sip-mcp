"""An explicit exact native node uses one controller ledger and a furnished isolated child."""

import json
import os
import stat
import subprocess
import sys
import sysconfig
import venv
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from xml.etree import ElementTree

import pytest
import yaml
from coverage import Coverage, CoverageData
from http_receiver import WireReceiver
from test_request_release_selection import response

from nci_si_acceptance import environments
from nci_si_acceptance.suite_identity import identity

NODE = "tests/test_evs.py::test_a_discovered_release_identifies_the_concept_and_its_provenance"
DISCOVERY = "/api/v1/metadata/terminologies?terminology=ncit&latest=true&tag=monthly"
CONTENT = "/api/v1/concept/ncit_26.09d/C4817?include=minimal"
RELEASE_ROW = {
    "terminology": "ncit",
    "latest": True,
    "tags": {"monthly": "true"},
    "version": "26.09d",
    "terminologyVersion": "ncit_26.09d",
}
EXPECTED_CASE = {"tool": "get_concept", "gate": False, "unmatched": []}
ONE_CASE = {"selected": 1, "finished": 1, "worker_crashes": 0}


def _native_replies(tree):
    concept = json.loads(
        (tree / "acceptance/fixtures/recorded/evs/concepts/C4817.json").read_text()
    )["response"]["body"]
    return [response([RELEASE_ROW]) for _ in range(3)] + [response(concept)]


@pytest.fixture
def native_tree(monkeypatch, request):
    root = Path(__file__).resolve().parents[2]
    monkeypatch.syspath_prepend(str(root))
    # Repository tooling is intentionally absent from the standalone harness import path.
    from scripts.validation import mutation_run  # noqa: PLC0415

    with mutation_run.snapshot(root) as tree:
        environment = tree / ".venv"
        venv.EnvBuilder(symlinks=True).create(environment)
        packages = (
            environment
            / "lib"
            / f"python{sys.version_info.major}.{sys.version_info.minor}"
            / "site-packages"
        )
        (packages / "furnished-test.pth").write_text(
            "\n".join(
                map(
                    str,
                    [tree / "src", tree / "acceptance/src", Path(sysconfig.get_path("purelib"))],
                )
            )
            + "\n"
        )
        env = {
            key: os.environ[key]
            for key in ("PATH", "HOME", "TMPDIR", "LANG", "SYSTEMROOT")
            if key in os.environ
        }
        env.update(NCI_SI_ACCEPTANCE_MODE="live", NCI_SI_ACCEPTANCE_ENVIRONMENT="evs-only")
        with _measured_native(tree, root, packages, env, request.node.nodeid):
            python = environment / "bin/python"
            probe = subprocess.run(  # noqa: S603 - fixed owned venv interpreter and isolated probe.
                [
                    str(python),
                    "-I",
                    "-c",
                    "import json,sys;import nci_si_acceptance.client as c;"
                    "import nci_si_mcp.http_client as h;print(json.dumps([sys.prefix,"
                    "c.__file__,h.__file__,c.open_session.__wrapped__.__code__.co_filename,"
                    "h.HttpClient.get_json.__code__.co_filename]))",
                ],
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            assert probe.returncode == 0, probe.stderr
            prefix, *compiled = json.loads(probe.stdout)
            assert Path(prefix).resolve() == environment.resolve()
            assert all(Path(path).resolve().is_relative_to(tree) for path in compiled)
            if "NATIVE_COVERAGE_CONTEXT" in env:
                env["NATIVE_COVERAGE_CONTEXT"] = request.node.nodeid + "|run"
            yield tree, python, env


def run_native(tree, python, env, *arguments):
    return subprocess.run(  # noqa: S603 - owned copied interpreter; test-supplied selectors.
        [
            str(python),
            "-I",
            "-m",
            "pytest",
            "--controlled-run-dir=tmp/native-run",
            "--report=tmp/native-report.json",
            *arguments,
        ],
        cwd=tree / "acceptance",
        env=env,
        input="",
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        umask=0o022,
    )


def local_policy(tree, receiver):
    path = tree / "acceptance/fixtures/environments.yaml"
    policy = yaml.safe_load(path.read_text())
    origins = {
        surface: receiver.origin if surface == "evs" else None for surface in environments.SURFACES
    }
    policy["allowed"] = origins
    policy["environments"]["evs-only"] = origins
    path.write_text(yaml.safe_dump(policy))


@pytest.mark.parametrize("logical_limit", [None, 3])
def test_native_serial_node_shares_controller_release_and_budget(native_tree, logical_limit):
    tree, python, env = native_tree
    env["NCI_SI_EVS_LICENSE_KEY"] = "native-synthetic-key"
    (tree / "acceptance/src/native_credentials_probe.py").write_text(
        "import os\n"
        "def pytest_sessionstart(session):\n"
        "    os.environ['NCI_SI_EVS_LICENSE_KEY'] = 'later-synthetic-key'\n"
    )
    if logical_limit is not None:
        # Only this owned copy changes; controller and isolated child compile the same limit.
        source = tree / "acceptance/src/nci_si_acceptance/request_authorization.py"
        original = source.read_text()
        assert original.count("DEFAULT_LOGICAL = 256") == 1
        source.write_text(original.replace("DEFAULT_LOGICAL = 256", "DEFAULT_LOGICAL = 3"))
    expected = [
        ("GET", DISCOVERY, b""),
        ("GET", DISCOVERY, b""),
        ("GET", "/api/v1/metadata/terminologies", b""),
    ]
    if logical_limit is None:
        expected.append(("GET", CONTENT, b""))
    with WireReceiver(_native_replies(tree)) as receiver:
        local_policy(tree, receiver)
        outcome = run_native(
            tree,
            python,
            env,
            NODE,
            "-q",
            "-n",
            "0",
            "--junitxml=tmp/native-junit.xml",
            "-p",
            "native_credentials_probe",
        )
        expected_exit = 0 if logical_limit is None else 1
        assert outcome.returncode == expected_exit, outcome.stdout + outcome.stderr
        assert [
            (method, path, body) for method, path, _headers, body in receiver.requests
        ] == expected
        assert receiver.contacts == len(receiver.requests)
        assert [
            {key.lower(): value for key, value in headers}["x-evsrestapi-license-key"]
            for _method, _path, headers, _body in receiver.requests
        ] == ["native-synthetic-key"] * len(expected)
    report = json.loads((tree / "acceptance/tmp/native-report.json").read_text())
    assert "synthetic-key" not in json.dumps(report)
    assert report["tests"] == {
        NODE: EXPECTED_CASE | {"outcome": "passed" if logical_limit is None else "failed"}
    }
    if logical_limit is not None:
        failure = ElementTree.parse(  # noqa: S314 - owned pytest artifact, not external XML.
            tree / "acceptance/tmp/native-junit.xml"
        ).find(".//testcase/failure")
        assert failure is not None
        assert "assert not result.is_error, result.content" in (failure.text or "")
        assert "Controlled upstream request refused." in (failure.text or "")
    summaries = tuple((tree / "acceptance/tmp/native-run").glob("*/summary.json"))
    assert len(summaries) == 1
    assert stat.S_IMODE(summaries[0].with_name("native.json").stat().st_mode) == (
        stat.S_IRUSR | stat.S_IWUSR
    )
    summary = json.loads(summaries[0].read_text())
    assert report["run"] == ONE_CASE | {"exit_status": expected_exit}
    assert summary["state"] == ("completed" if logical_limit is None else "failed")
    assert summary["observationComplete"] is True
    assert summary["observed"]["transportEntries"] == len(expected)
    assert summary["reservations"]["logical"] == len(expected)


@pytest.mark.parametrize("phase", ["initial", "final", "pre-entry-final"])
def test_native_publication_failure_keeps_an_incomplete_or_prior_report(native_tree, phase):
    tree, python, env = native_tree
    late, before_entry = phase != "initial", phase == "pre-entry-final"
    plugin = tree / "acceptance/src/native_publication_probe.py"
    plugin.write_text(
        f"late, before_entry = {late!r}, {before_entry!r}\n"
        + """
import json
from pathlib import Path
from nci_si_acceptance import report
from nci_si_acceptance.request_native import NativeRun
original, count = report._publish_native, 0
original_complete = NativeRun.complete
publication_error = OSError('owned native publication failure')
def publish(path, text):
    global count
    count += 1
    if not late or count > 1:
        raise publication_error
    original(path, text)
    Path('tmp/initial-native-report.json').write_bytes(path.read_bytes())
report._publish_native = publish
def complete(self, *args, **kwargs):
    first = self.errors[0] if len(self.errors) == 1 else None
    try:
        return original_complete(self, *args, **kwargs)
    except BaseException as error:
        if before_entry:
            children = error.exceptions if isinstance(error, BaseExceptionGroup) else ()
            Path('tmp/combined-native-failures.json').write_text(json.dumps({
                'value_error': type(first) is ValueError,
                'refusal': str(first) == (
                    'controlled run requires the exact supported serial selection'),
                'children': [{'collection': child is first,
                              'publication': child is publication_error} for child in children]}))
        raise
NativeRun.complete = complete
def pytest_collection_modifyitems(items):
    if before_entry:
        items.clear()
"""
    )
    directory = tree / "acceptance/tmp"
    directory.mkdir()
    destination, prior = directory / "native-report.json", b'{"prior":true}\n'
    destination.write_bytes(prior)
    expected = (
        [
            ("GET", DISCOVERY, b""),
            ("GET", DISCOVERY, b""),
            ("GET", "/api/v1/metadata/terminologies", b""),
            ("GET", CONTENT, b""),
        ]
        if phase == "final"
        else []
    )
    with WireReceiver(_native_replies(tree)) as receiver:
        local_policy(tree, receiver)
        expected_suite = identity(tree)
        outcome = run_native(
            tree,
            python,
            env,
            NODE,
            "-p",
            "native_publication_probe",
            "-n",
            "0",
            "--junitxml=tmp/native-junit.xml",
        )
        assert outcome.returncode != 0
        assert "owned native publication failure" in outcome.stdout + outcome.stderr
        assert [
            (method, path, body) for method, path, _headers, body in receiver.requests
        ] == expected
        assert receiver.contacts == len(expected)
    if late:
        assert destination.read_bytes() == (directory / "initial-native-report.json").read_bytes()
        report = json.loads(destination.read_text())
        assert report["run"] is None
        assert report["tests"] == {}
        assert report["suite"] == expected_suite
        if before_entry:
            assert json.loads((directory / "combined-native-failures.json").read_text()) == {
                "value_error": True,
                "refusal": True,
                "children": [
                    {"collection": True, "publication": False},
                    {"collection": False, "publication": True},
                ],
            }
            assert not (directory / "native-run").exists()
        else:
            cases = ElementTree.parse(  # noqa: S314 - owned pytest artifact, not external XML.
                directory / "native-junit.xml"
            ).findall(".//testcase")
            assert len(cases) == 1
            assert cases[0].find("failure") is None and cases[0].find("error") is None
    else:
        assert destination.read_bytes() == prior
        assert not (directory / "native-run").exists()


def test_initial_publication_failure_cannot_be_retried_by_framework_cleanup(native_tree):
    tree, python, env = native_tree
    (tree / "acceptance/src/initial_publication_probe.py").write_text("""
from nci_si_acceptance import report

actual_publish = report._publish_native
first_publication = True

def publish(destination, text):
    global first_publication
    if first_publication:
        first_publication = False
        raise OSError('owned one-shot initial publication failure')
    return actual_publish(destination, text)

report._publish_native = publish
""")
    directory = tree / "acceptance/tmp"
    directory.mkdir()
    destination, prior = directory / "native-report.json", b'{"prior":true}\n'
    destination.write_bytes(prior)
    with WireReceiver({}) as receiver:
        local_policy(tree, receiver)
        outcome = run_native(tree, python, env, NODE, "-p", "initial_publication_probe", "-n", "0")
        assert receiver.requests == [] and receiver.contacts == 0
    assert destination.read_bytes() == prior
    assert outcome.returncode != 0
    assert "owned one-shot initial publication failure" in outcome.stdout + outcome.stderr
    assert not (directory / "native-run").exists()


def test_native_cleanup_failure_marks_a_passing_case_failed(native_tree):
    tree, python, env = native_tree
    (tree / "acceptance/src/native_cleanup_only_probe.py").write_text("""
from pathlib import Path
import pytest
from nci_si_acceptance.request_native import NativeRun

actual_start = NativeRun.start
cleanup_failure = LookupError('owned native listener cleanup failure')

def start(self, session):
    result = actual_start(self, session)
    listener = self.listener
    assert listener is not None
    actual_finish = listener.finish
    def finish(deadline):
        actual_finish(deadline)
        raise cleanup_failure
    listener.finish = finish
    return result

NativeRun.start = start

def contains_cleanup(error):
    if error is cleanup_failure:
        return True
    return isinstance(error, BaseExceptionGroup) and any(
        contains_cleanup(child) for child in error.exceptions)

@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_sessionfinish(session):
    try:
        result = yield
    except BaseException as error:
        Path('tmp/cleanup-visible.txt').write_text(
            'visible' if contains_cleanup(error) else 'missing')
        raise
    Path('tmp/cleanup-visible.txt').write_text('no-outward-error')
    return result
""")
    expected_requests = [
        ("GET", DISCOVERY, b""),
        ("GET", DISCOVERY, b""),
        ("GET", "/api/v1/metadata/terminologies", b""),
        ("GET", CONTENT, b""),
    ]
    with WireReceiver(_native_replies(tree)) as receiver:
        local_policy(tree, receiver)
        expected_suite = identity(tree)
        outcome = run_native(tree, python, env, NODE, "-p", "native_cleanup_only_probe", "-n", "0")
        assert [
            (method, path, body) for method, path, _headers, body in receiver.requests
        ] == expected_requests
        assert receiver.contacts == len(expected_requests)
    report = json.loads((tree / "acceptance/tmp/native-report.json").read_text())
    assert report["tests"] == {NODE: EXPECTED_CASE | {"outcome": "passed"}}
    assert report["run"] == ONE_CASE | {"exit_status": 1}
    assert report["suite"] == expected_suite
    assert outcome.returncode != 0, outcome.stdout + outcome.stderr
    assert (tree / "acceptance/tmp/cleanup-visible.txt").read_text() == "visible"


@pytest.mark.parametrize(
    "arguments",
    [
        (NODE, "--collect-only"),
        (NODE, "-k", "never"),
        (NODE, "-m", "live_capable"),
        (NODE, "--deselect=" + NODE),
        (NODE, NODE),
        (NODE, "tests/test_evs.py"),
        (NODE, "-n", "2"),
        pytest.param((NODE, "--controlled-run-dir="), id="empty-run-directory"),
        pytest.param((NODE, "--setup-only"), id="setup-only"),
        pytest.param((NODE, "--setup-plan"), id="setup-plan"),
        pytest.param((NODE, "--fixtures"), id="fixtures"),
        pytest.param((NODE, "--fixtures-per-test"), id="fixtures-per-test"),
        pytest.param((NODE, "--pdb"), id="pdb"),
        pytest.param((NODE, "--trace"), id="trace"),
    ],
)
def test_unsupported_controlled_selections_have_no_resources_or_contacts(native_tree, arguments):
    tree, python, env = native_tree
    with WireReceiver() as receiver:
        local_policy(tree, receiver)
        outcome = run_native(tree, python, env, *arguments)
        assert outcome.returncode == pytest.ExitCode.USAGE_ERROR
        assert not (tree / "acceptance/tmp/native-run").exists()
        assert not (tree / "acceptance/tmp/native-report.json").exists()
        assert receiver.contacts == 0
        assert "controlled run requires the exact supported serial selection" in outcome.stderr


@pytest.mark.parametrize("before_collection", [False, True])
def test_native_startup_or_effective_collection_failure_acquires_nothing(
    native_tree, before_collection
):
    tree, python, env = native_tree
    plugin = tree / "acceptance/src/native_boundary_probe.py"
    plugin.write_text(
        "def pytest_sessionstart(session):\n    raise RuntimeError('owned startup stop')\n"
        if before_collection
        else "def pytest_collection_modifyitems(items):\n    items.clear()\n"
    )
    with WireReceiver() as receiver:
        local_policy(tree, receiver)
        expected_suite = identity(tree)
        outcome = run_native(tree, python, env, NODE, "-p", "native_boundary_probe", "-n", "0")
        assert outcome.returncode != 0, outcome.stdout + outcome.stderr
        assert receiver.contacts == 0
        assert not (tree / "acceptance/tmp/native-run").exists()
    report = json.loads((tree / "acceptance/tmp/native-report.json").read_text())
    assert report["tests"] == {}
    assert report["suite"] == expected_suite
    assert report["environment"]["name"] == "evs-only"
    if before_collection:
        assert report["run"] is None
    else:
        assert report["run"] == {
            "exit_status": 4,
            "selected": 0,
            "finished": 0,
            "worker_crashes": 0,
        }


@pytest.mark.parametrize(
    "origin", ["furnished", "interpreter", "module", "compiled", "unattributed"]
)
def test_native_handoff_refuses_before_operational_imports(native_tree, origin):
    tree, python, env = native_tree
    catalogue = tree / "acceptance/src/nci_si_acceptance/request_catalogue.py"
    catalogue.with_name("foreign_catalogue.py").write_bytes(catalogue.read_bytes())
    (tree / "acceptance/src/foreign_wrapper.py").write_text(
        "from functools import wraps\n"
        "def decorate(function):\n"
        "    @wraps(function)\n"
        "    def wrapped(*args, **kwargs):\n"
        "        return function(*args, **kwargs)\n"
        "    return wrapped\n"
    )
    script = """
import importlib.util, inspect, json, os, runpy, sys
from pathlib import Path
root = Path.cwd()
if sys.argv[1] == 'interpreter':
    sys.path[:0] = [str(root / 'src'), str(root / 'acceptance/src')]
from nci_si_acceptance.environments import selection
from nci_si_acceptance.request_catalogue import compile_policy
from nci_si_acceptance.request_budget import ControlledRun
from nci_si_acceptance import request_native as native
from nci_si_acceptance.request_native import _handoff_run, native_policy
from nci_si_mcp.errors import RequestPolicyError
blocked = {'nci_si_mcp.http_client', 'nci_si_mcp.evs', 'nci_si_mcp.registry',
           'nci_si_acceptance.request_transport', 'nci_si_acceptance.client'}
class Guard:
    def find_spec(self, fullname, path=None, target=None):
        assert fullname not in blocked, fullname
sys.meta_path.insert(0, Guard())
assert not blocked.intersection(sys.modules)
environment = selection('live')
assert environment is not None
run = ControlledRun.create(compile_policy(environment).authorization, Path('tmp/bootstrap-run'))
try:
    run.open()
    run.authorize_participant('native-serial')
    path = run.directory / 'handoff.json'
    record = {'environment': environment.to_record(), 'run': run.descriptor.to_record(),
              'participant': 'native-serial'}
    path.write_text(json.dumps(record))
    os.environ['NCI_SI_ACCEPTANCE_RUN_HANDOFF'] = str(path)
    origin = sys.argv[1]
    if origin == 'module':
        spec = importlib.util.spec_from_file_location(compile_policy.__module__,
            root / 'acceptance/src/nci_si_acceptance/foreign_catalogue.py')
        foreign = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(foreign)
        sys.modules[compile_policy.__module__] = foreign
    elif origin == 'compiled':
        from foreign_wrapper import decorate
        native.compile_policy = decorate(native.compile_policy)
    elif origin == 'unattributed':
        native.compile_policy = runpy.run_path(
            str(root / 'acceptance/src/nci_si_acceptance/request_catalogue.py'))['compile_policy']
    expected = root / 'acceptance/src/nci_si_acceptance/request_catalogue.py'
    module = inspect.getmodule(native.compile_policy)
    origins = [Path(sys.prefix).resolve() == (root / '.venv').resolve(),
        module is not None and Path(module.__file__).resolve() == expected,
        Path(inspect.getfile(native.compile_policy)).resolve() == expected]
    if origin != 'furnished':
        refused = False
        try:
            with native_policy():
                raise AssertionError('foreign origin admitted')
        except RequestPolicyError as error:
            refused = error.reason == 'authorization'
        assert not blocked.intersection(sys.modules)
        print(json.dumps({'origins': origins, 'refused': refused}))
        sys.exit(0)
    assert origins == [True, True, True]
    assert _handoff_run(str(path)).participant == 'native-serial'
    for defect in ('shape', 'environment', 'participant', 'descriptor'):
        candidate = json.loads(json.dumps(record))
        if defect == 'shape': candidate = {}
        elif defect == 'environment': candidate['environment']['policy_sha256'] = '0' * 64
        elif defect == 'participant': candidate['participant'] = 'foreign'
        else: candidate['run'] = {}
        path.write_text(json.dumps(candidate))
        try:
            with native_policy():
                raise AssertionError('invalid handoff admitted')
        except RequestPolicyError as error:
            assert error.reason == 'authorization'
        assert not blocked.intersection(sys.modules)
finally:
    run.close()
print('authenticated attachment and four pre-import refusals')
"""
    with WireReceiver() as receiver:
        local_policy(tree, receiver)
        outcome = subprocess.run(  # noqa: S603 - owned furnished isolated interpreter.
            [
                sys.executable if origin == "interpreter" else str(python),
                "-I",
                "-c",
                script,
                origin,
            ],
            cwd=tree,
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        assert outcome.returncode == 0, outcome.stdout + outcome.stderr
        assert receiver.contacts == 0
    if origin == "furnished":
        assert outcome.stdout.strip() == "authenticated attachment and four pre-import refusals"
    else:
        expected = {
            "interpreter": [False, True, True],
            "module": [True, False, True],
            "compiled": [True, True, False],
            "unattributed": [True, False, True],
        }[origin]
        assert json.loads(outcome.stdout) == {"origins": expected, "refused": True}


def test_native_postspawn_failure_reaps_child_and_closes_transferred_stderr(native_tree):
    tree, python, env = native_tree
    (tree / "acceptance/src/native_postspawn_probe.py").write_text("""
import json
from pathlib import Path
from nci_si_acceptance.request_stdio import StdioOwner, StderrTransfer
from nci_si_mcp.errors import RequestPolicyError
original_finish, original_stderr = StdioOwner.finish, StderrTransfer.finish
async def fail_after_handle(self, portal):
    assert self.process is not None
    raise LookupError('native postspawn failure')
def finish(self, deadline):
    try:
        return original_finish(self, deadline)
    finally:
        try:
            self.controller.run.authorize_participant('too-late')
            admission = 'admitted'
        except RequestPolicyError as error:
            admission = error.reason
        states = (self.read.statistics(), self.write.statistics())
        Path('tmp/native-postspawn.json').write_text(json.dumps({
            'returncode': None if self.process is None else self.process.returncode,
            'waited': self.waited, 'process_closed': self.process_closed,
            'thread_alive': self.thread.is_alive(), 'admission': admission,
            'endpoints': [[s.open_send_streams, s.open_receive_streams] for s in states]}))
def stderr_finish(self, deadline):
    original_stderr(self, deadline)
    Path('tmp/native-stderr-closed.json').write_text(json.dumps(self.stream.closed))
StdioOwner._io_scope, StdioOwner.finish = fail_after_handle, finish
StderrTransfer.finish = stderr_finish
""")
    with WireReceiver([response([RELEASE_ROW])]) as receiver:
        local_policy(tree, receiver)
        expected_suite = identity(tree)
        outcome = run_native(tree, python, env, NODE, "-p", "native_postspawn_probe", "-n", "0")
        assert outcome.returncode != 0
        assert "native postspawn failure" in outcome.stdout + outcome.stderr
        assert [(method, path, body) for method, path, _headers, body in receiver.requests] == [
            ("GET", DISCOVERY, b"")
        ]
        assert receiver.contacts == 1
    directory = tree / "acceptance/tmp"
    receipt = json.loads((directory / "native-postspawn.json").read_text())
    assert type(receipt.pop("returncode")) is int
    assert receipt == {
        "waited": True,
        "process_closed": True,
        "thread_alive": False,
        "admission": "closed",
        "endpoints": [[0, 0], [0, 0]],
    }
    assert json.loads((directory / "native-stderr-closed.json").read_text()) is True
    report = json.loads((directory / "native-report.json").read_text())
    assert report["suite"] == expected_suite
    assert report["tests"] == {NODE: EXPECTED_CASE | {"outcome": "failed"}}
    assert report["run"] == ONE_CASE | {"exit_status": 1}
    summaries = tuple((directory / "native-run").glob("*/summary.json"))
    assert len(summaries) == 1
    summary = json.loads(summaries[0].read_text())
    assert summary["state"] == "failed"
    assert summary["observationComplete"] is True


def _matching_native_files(data, tree, root):
    matches = {}
    for name in data.measured_files():
        copied = Path(name).resolve()
        if not copied.is_relative_to(tree):
            continue
        original = root / copied.relative_to(tree)
        if original.is_file() and copied.is_file() and original.read_bytes() == copied.read_bytes():
            matches[name] = str(original)
    return matches


def _merge_native_measurements(parent, tree, root, node):
    combined = CoverageData(basename=str(tree / ".native-coverage-merged"))
    for path in tree.glob(".native-coverage-data.*"):
        observed = CoverageData(basename=str(path))
        observed.read()
        matches = _matching_native_files(observed, tree, root)
        for context in observed.measured_contexts():
            if context not in {node + "|setup", node + "|run"}:
                raise ValueError("foreign native coverage context")
            observed.set_query_context(context)
            combined.set_context(context)
            combined.add_arcs(
                {target: observed.arcs(name) or [] for name, target in matches.items()}
            )
    parent.get_data().update(combined)


@contextmanager
def _measured_native(tree, root, packages, environment, node):
    parent = Coverage.current()
    if parent is None:
        yield
        return
    if not parent.get_option("run:branch"):
        raise ValueError("native measurement requires branch coverage")
    sources = parent.get_option("run:source")
    if not sources:
        raise ValueError("native measurement requires explicit source scope")
    sources = [
        str(tree / Path(source).relative_to(root)) if Path(source).is_absolute() else source
        for source in sources
    ]
    config = tree / ".native-coverage.ini"
    config.write_text(
        "[run]\nbranch = True\nparallel = True\ncore = ctrace\npatch = subprocess\n"
        "source =\n    " + "\n    ".join(sources) + "\n"
        f"data_file = {tree / '.native-coverage-data'}\ncontext = ${{NATIVE_COVERAGE_CONTEXT?}}\n"
    )
    (packages / "z-native-coverage.pth").write_text("import coverage; coverage.process_startup()\n")
    environment["COVERAGE_PROCESS_START"] = str(config)
    environment["NATIVE_COVERAGE_CONTEXT"] = node + "|setup"
    try:
        yield
    finally:
        _merge_native_measurements(parent, tree, root, node)


@pytest.mark.parametrize(
    ("mode", "relative", "body", "context", "expected"),
    [
        ("matching", "copy/a.py", b"pass\n", "owner|run", [(-1, 1), (1, -1)]),
        ("matching-setup", "copy/a.py", b"pass\n", "owner|setup", [(-1, 1), (1, -1)]),
        ("foreign-path", "outside.py", b"pass\n", "owner|run", []),
        ("changed-bytes", "copy/a.py", b"pass # changed\n", "owner|run", []),
        ("foreign-context", "copy/a.py", b"pass\n", "foreign|run", []),
        ("disabled", "copy/a.py", b"pass\n", "owner|run", []),
    ],
)
def test_native_measurement_attests_source_and_context(
    tmp_path, monkeypatch, mode, relative, body, context, expected
):
    # Seeded rejection records stay isolated; they never enter the live session's coverage.
    root, tree, packages = (tmp_path / name for name in ("root", "copy", "packages"))
    for folder in (root, tree, packages):
        folder.mkdir()
    if mode == "disabled":
        monkeypatch.setattr(Coverage, "current", lambda: None)
        environment = {}
        with _measured_native(tree, root, packages, environment, "owner"):
            assert environment == {}
        assert list(tree.iterdir()) == [] and list(packages.iterdir()) == []
        return
    node = "selftests/test_native_controlled.py::measurement_owner"
    context = context.replace("owner|", node + "|")
    original, copied = root / "a.py", tmp_path / relative
    original.write_bytes(b"pass\n")
    copied.write_bytes(body)
    observed = CoverageData(basename=str(tree / ".native-coverage-data.child"))
    observed.set_context(context)
    observed.add_arcs({str(copied): [(-1, 1), (1, -1)]})
    target = CoverageData(basename=str(tmp_path / ".isolated-parent-coverage"))
    parent = SimpleNamespace(get_data=lambda: target)
    if mode == "foreign-context":
        with pytest.raises(ValueError, match="foreign native coverage context"):
            _merge_native_measurements(parent, tree, root, node)
    else:
        _merge_native_measurements(parent, tree, root, node)
    assert sorted(target.arcs(str(original)) or []) == expected
    assert target.measured_files() == ({str(original)} if expected else set())

    assert target.measured_contexts() == ({context} if expected else set())
    if expected:
        target.set_query_context(context)
        assert sorted(target.arcs(str(original))) == expected
        monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2]))
        from scripts.validation import mutation_run  # noqa: PLC0415

        mapping = {"selftest": mutation_run.coverage_map(Path(target.data_filename()), root)}
        assert mutation_run.selection(mapping, "a.py", 1, 1) == {
            "selftest": [node] if context.endswith("|run") else []
        }


@pytest.mark.parametrize("scope", ["harness", "mutation", "missing", "foreign"])
def test_native_measurement_preserves_the_parent_source_scope(tmp_path, monkeypatch, scope):
    root, tree, packages = (tmp_path / name for name in ("root", "copy", "packages"))
    for folder in (root, tree, packages):
        folder.mkdir()
    sources = {
        "harness": ["nci_si_acceptance"],
        "mutation": [str(root / name) for name in ("src", "scripts", "acceptance/src")],
        "missing": None,
        "foreign": [str(tmp_path / "another-checkout/src")],
    }[scope]
    parent = Coverage(
        branch=True, source=sources, config_file=False, data_file=str(tmp_path / "parent-coverage")
    )
    monkeypatch.setattr(Coverage, "current", lambda: parent)
    environment = {}
    if scope in {"missing", "foreign"}:
        with (
            pytest.raises(ValueError),
            _measured_native(tree, root, packages, environment, "owner"),
        ):
            raise AssertionError("unsupported source scope admitted")
        assert environment == {} and list(tree.iterdir()) == []
        return
    with _measured_native(tree, root, packages, environment, "owner"):
        monkeypatch.setenv("NATIVE_COVERAGE_CONTEXT", "owner|setup")
        child = Coverage(config_file=environment["COVERAGE_PROCESS_START"])
        assert child.get_option("run:source") == (
            ["nci_si_acceptance"]
            if scope == "harness"
            else [str(tree / name) for name in ("src", "scripts", "acceptance/src")]
        )
        assert parent.get_option("run:source") == sources
