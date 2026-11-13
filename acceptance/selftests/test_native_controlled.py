"""An explicit exact native node uses one controller ledger and a furnished isolated child."""

import json
import os
import subprocess
import sys
import sysconfig
import venv
from pathlib import Path
from xml.etree import ElementTree

import pytest
import yaml
from http_receiver import WireReceiver
from test_request_release_selection import response

from nci_si_acceptance import environments
from nci_si_acceptance.suite_identity import identity

NODE = "tests/test_evs.py::test_a_discovered_release_identifies_the_concept_and_its_provenance"
DISCOVERY = "/api/v1/metadata/terminologies?terminology=ncit&latest=true&tag=monthly"
CONTENT = "/api/v1/concept/ncit_26.09d/C4817?include=minimal"


@pytest.fixture
def native_tree(monkeypatch):
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
    row = {
        "terminology": "ncit",
        "latest": True,
        "tags": {"monthly": "true"},
        "version": "26.09d",
        "terminologyVersion": "ncit_26.09d",
    }
    concept = json.loads(
        (tree / "acceptance/fixtures/recorded/evs/concepts/C4817.json").read_text()
    )["response"]["body"]
    with WireReceiver(
        [response([row]), response([row]), response([row]), response(concept)]
    ) as receiver:
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
        NODE: {
            "tool": "get_concept",
            "gate": False,
            "outcome": "passed" if logical_limit is None else "failed",
            "unmatched": [],
        }
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
    summary = json.loads(summaries[0].read_text())
    assert report["run"] == {
        "exit_status": expected_exit,
        "selected": 1,
        "finished": 1,
        "worker_crashes": 0,
    }
    assert summary["state"] == ("completed" if logical_limit is None else "failed")
    assert summary["observationComplete"] is True
    assert summary["observed"]["transportEntries"] == len(expected)
    assert summary["reservations"]["logical"] == len(expected)


@pytest.mark.parametrize("late", [False, True])
def test_native_publication_failure_keeps_an_incomplete_or_prior_report(native_tree, late):
    tree, python, env = native_tree
    plugin = tree / "acceptance/src/native_publication_probe.py"
    plugin.write_text(
        f"late = {late!r}\n"
        + """
from pathlib import Path
from nci_si_acceptance import report
original, count = report._publish_native, 0
def publish(path, text):
    global count
    count += 1
    if not late or count > 1:
        raise OSError('owned native publication failure')
    original(path, text)
    Path('tmp/initial-native-report.json').write_bytes(path.read_bytes())
report._publish_native = publish
"""
    )
    directory = tree / "acceptance/tmp"
    directory.mkdir()
    destination, prior = directory / "native-report.json", b'{"prior":true}\n'
    destination.write_bytes(prior)
    row = {
        "terminology": "ncit",
        "latest": True,
        "tags": {"monthly": "true"},
        "version": "26.09d",
        "terminologyVersion": "ncit_26.09d",
    }
    concept = json.loads(
        (tree / "acceptance/fixtures/recorded/evs/concepts/C4817.json").read_text()
    )["response"]["body"]
    expected = (
        [
            ("GET", DISCOVERY, b""),
            ("GET", DISCOVERY, b""),
            ("GET", "/api/v1/metadata/terminologies", b""),
            ("GET", CONTENT, b""),
        ]
        if late
        else []
    )
    with WireReceiver(
        [response([row]), response([row]), response([row]), response(concept)]
    ) as receiver:
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
        cases = ElementTree.parse(  # noqa: S314 - owned pytest artifact, not external XML.
            directory / "native-junit.xml"
        ).findall(".//testcase")
        assert len(cases) == 1
        assert cases[0].find("failure") is None and cases[0].find("error") is None
    else:
        assert destination.read_bytes() == prior
        assert not (directory / "native-run").exists()


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


def test_native_handoff_refuses_before_operational_imports(native_tree):
    tree, python, env = native_tree
    script = """
import json, os, sys
from pathlib import Path
from nci_si_acceptance.environments import selection
from nci_si_acceptance.request_catalogue import compile_policy
from nci_si_acceptance.request_budget import ControlledRun
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
    assert _handoff_run(str(path)).participant == 'native-serial'
    os.environ['NCI_SI_ACCEPTANCE_RUN_HANDOFF'] = str(path)
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
            [str(python), "-I", "-c", script],
            cwd=tree,
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        assert outcome.returncode == 0, outcome.stdout + outcome.stderr
        assert receiver.contacts == 0
    assert outcome.stdout.strip() == "authenticated attachment and four pre-import refusals"


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
    row = {
        "terminology": "ncit",
        "latest": True,
        "tags": {"monthly": "true"},
        "version": "26.09d",
        "terminologyVersion": "ncit_26.09d",
    }
    with WireReceiver([response([row])]) as receiver:
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
    assert report["tests"] == {
        NODE: {"tool": "get_concept", "gate": False, "outcome": "failed", "unmatched": []}
    }
    assert report["run"] == {
        "exit_status": 1,
        "selected": 1,
        "finished": 1,
        "worker_crashes": 0,
    }
    summaries = tuple((directory / "native-run").glob("*/summary.json"))
    assert len(summaries) == 1
    summary = json.loads(summaries[0].read_text())
    assert summary["state"] == "failed"
    assert summary["observationComplete"] is True
