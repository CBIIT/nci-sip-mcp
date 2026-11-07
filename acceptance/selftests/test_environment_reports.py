"""Selected live provenance survives strict records without inventing historical data."""

import hashlib
import json
import os
from copy import deepcopy
from types import SimpleNamespace

import pytest

from conftest import REVIEWED_ORIGINS, STAND_IN
from nci_si_acceptance import environments
from nci_si_acceptance.report import Collector, NativeReport, ReportFieldError, complete, main

pytest_plugins = ["pytester"]


ENVIRONMENT = {
    "name": "evs-only",
    "origins": REVIEWED_ORIGINS,
    "policy_sha256": "a" * 64,
}


def native():
    return {
        "mode": "live",
        "transport": "stdio",
        "failed_gates": [],
        "unrun_gates": [],
        "tools_list_bytes": None,
        "tools": {},
        "tests": {},
        "suite": {"version": "1", "fixture_set": "recorded", "digest": "b" * 64},
    }


@pytest.mark.parametrize("worker", [None, {}, ENVIRONMENT | {"policy_sha256": "b" * 64}])
def test_current_workers_cannot_omit_or_conflict_with_controller_selection(worker):
    collector = Collector()
    collector.environment = deepcopy(ENVIRONMENT)
    output = {} if worker is None else {"nci_si_environment": worker}
    collector.pytest_testnodedown(SimpleNamespace(workeroutput=output), None)
    with pytest.raises(ValueError, match="environment"):
        collector.report("live")


@pytest.mark.parametrize("workers", [0, 2])
def test_native_producer_preserves_selection(compliant, monkeypatch, workers):
    for key in [key for key in os.environ if key.startswith("NCI_SI_")]:
        monkeypatch.delenv(key)
    monkeypatch.setenv("NCI_SI_ACCEPTANCE_MODE", "live")
    monkeypatch.setenv(environments.VARIABLE, "evs-only")
    monkeypatch.setenv("Python_ROOT_DIR", "/ci-python")
    plugin = compliant.path / f"{STAND_IN}.py"
    plugin.write_text(
        "import os\nfrom nci_si_acceptance.environments import external_runtime_setting\n"
        "for key in list(os.environ):\n"
        "    if external_runtime_setting(key): os.environ.pop(key)\n" + plugin.read_text()
    )
    (compliant.path / "tests/test_selected.py").write_text(
        "import pytest\n@pytest.mark.live_capable\n@pytest.mark.gate\n"
        "def test_selected(target):\n    assert target.environment.name == 'evs-only'\n"
    )
    output = compliant.path / "native.json"
    result = compliant.runpytest_subprocess(
        "tests/test_selected.py", "-p", STAND_IN, "-n", str(workers), f"--report={output}"
    )
    assert result.ret == 0, result.stdout.str() + result.stderr.str()
    result.assert_outcomes(passed=1)
    produced = json.loads(output.read_text())
    raw_policy = environments.POLICY.read_bytes()
    expected = ENVIRONMENT | {"policy_sha256": hashlib.sha256(raw_policy).hexdigest()}
    assert produced.get("environment") == expected
    assert NativeReport.from_dict(produced).to_dict() == produced


@pytest.mark.parametrize(
    ("mode", "environment"),
    [
        ("fixture", ENVIRONMENT),
        ("live", None),
        ("live", ENVIRONMENT | {"origins": None}),
        ("live", ENVIRONMENT | {"name": None}),
        ("live", ENVIRONMENT | {"origins": ENVIRONMENT["origins"] | {"evs": False}}),
        ("live", ENVIRONMENT | {"policy_sha256": "private-marker"}),
        ("live", ENVIRONMENT | {"origins": {"evs": "https://private-marker@example.com/"}}),
        (
            "live",
            ENVIRONMENT
            | {"origins": ENVIRONMENT["origins"] | {"evs": "https://private-marker@example.com/"}},
        ),
        ("live", ENVIRONMENT | {"private-marker": "private-marker"}),
    ],
)
def test_invalid_environment_records_fail_at_the_sanitized_field_boundary(mode, environment):
    with pytest.raises(ReportFieldError) as error:
        NativeReport.from_dict(native() | {"mode": mode, "environment": environment})
    assert str(error.value) == "Invalid acceptance field: environment"
    assert "private-marker" not in str(error.value)


def test_crashed_fixture_worker_retains_its_incomplete_report():
    collector = Collector()
    collector.environment = None
    collector.pytest_testnodedown(SimpleNamespace(workeroutput={}), "worker exited")
    collector.tests["test_fixture"] = {
        "tool": "get_form",
        "gate": False,
        "outcome": "passed",
        "unmatched": [],
    }
    collector.run = {
        "exit_status": 1,
        "selected": 1,
        "finished": 1,
        "worker_crashes": collector.worker_crashes,
    }
    try:
        emitted = collector.report("fixture")
    except ReportFieldError:
        emitted = None
    assert emitted is not None, "fixture worker crash must retain its report"
    assert emitted["run"] == {"exit_status": 1, "selected": 1, "finished": 1, "worker_crashes": 1}
    assert "environment" not in emitted
    assert not complete(emitted)


@pytest.mark.parametrize(
    ("selected", "reported", "error"),
    [
        (ENVIRONMENT, None, "worker exited"),
        (None, ENVIRONMENT, "worker exited"),
        (None, None, None),
    ],
)
def test_workers_cannot_omit_required_or_report_unexpected_provenance(selected, reported, error):
    collector = Collector()
    collector.environment = selected
    output = {} if reported is None else {"nci_si_environment": reported}
    collector.pytest_testnodedown(SimpleNamespace(workeroutput=output), error)
    with pytest.raises(ReportFieldError) as failure:
        collector.report("live" if selected else "fixture")
    assert str(failure.value) == "Invalid acceptance field: environment"


def combined_inputs(tmp_path, environment):
    fixture = native() | {
        "mode": "fixture",
        "run": {"exit_status": 0, "selected": 0, "finished": 0, "worker_crashes": 0},
    }
    live = fixture | {"mode": "live"}
    if environment != "absent":
        live["environment"] = environment
    first, second = tmp_path / "fixture.json", tmp_path / "live.json"
    first.write_text(json.dumps(fixture))
    second.write_text(json.dumps(live))
    return first, second


def test_combined_cli_displays_only_supplied_live_environment_and_escapes_fields(tmp_path, capsys):
    environment = deepcopy(ENVIRONMENT)
    environment["origins"]["evs"] = "https://example.test/<script>private-marker</script>|edge"
    first, second = combined_inputs(tmp_path, environment)
    original = first.read_bytes(), second.read_bytes()
    assert main([str(first), "--live", str(second)]) == 0
    displayed = capsys.readouterr().out
    expected = (
        "Live environment (recorded configuration; not access approval):\n\n"
        "| Field | Recorded value |\n|---|---|\n"
        "| name | evs-only |\n"
        "| evs | https://example.test/&lt;script&gt;private-marker&lt;/script&gt;&#124;edge |\n"
        "| evs-fhir | https://api-evsrest.nci.nih.gov/fhir/r4 |\n"
        "| cadsr | disabled |\n| cadsr-ftp | disabled |\n| cadsr-contracts | disabled |\n"
        "| ssis | disabled |\n| ssis-sparql | disabled |\n"
        f"| policy_sha256 | {'a' * 64} |\n"
    )
    assert expected in displayed
    assert "<script>" not in displayed
    assert (first.read_bytes(), second.read_bytes()) == original


@pytest.mark.parametrize(
    "environment",
    [
        None,
        {},
        ENVIRONMENT | {"name": None},
        ENVIRONMENT | {"origins": None},
        ENVIRONMENT | {"policy_sha256": "private-marker"},
        ENVIRONMENT | {"private-marker": "private-marker"},
        ENVIRONMENT
        | {"origins": ENVIRONMENT["origins"] | {"evs": "https://private-marker@example.test"}},
    ],
)
def test_combined_cli_rejects_invalid_present_metadata_before_output(tmp_path, capsys, environment):
    first, second = combined_inputs(tmp_path, environment)
    with pytest.raises(ReportFieldError) as caught:
        main([str(first), "--live", str(second)])
    assert str(caught.value) == "Invalid acceptance field: environment"
    assert capsys.readouterr().out == ""
    assert "private-marker" not in str(caught.value)


def test_combined_cli_does_not_invent_environment_for_historical_or_fixture_input(tmp_path, capsys):
    first, second = combined_inputs(tmp_path, "absent")
    fixture = json.loads(first.read_text()) | {"environment": ENVIRONMENT}
    first.write_text(json.dumps(fixture))
    assert main([str(first), "--live", str(second)]) == 0
    assert "Live environment" not in capsys.readouterr().out
    assert main([str(first)]) == 0
    assert "Live environment" not in capsys.readouterr().out


@pytest.mark.parametrize(
    "suffix,encoded",
    [
        ("**recorded**", "&#42;&#42;recorded&#42;&#42;"),
        (
            "![label](https://destination.invalid/image.png)",
            "&#33;&#91;label&#93;&#40;https://destination.invalid/image.png&#41;",
        ),
        (
            "[label](https://destination.invalid/)",
            "&#91;label&#93;&#40;https://destination.invalid/&#41;",
        ),
        ("`recorded`", "&#96;recorded&#96;"),
        ("\\recorded\\path", "&#92;recorded&#92;path"),
        ("_recorded_", "&#95;recorded&#95;"),
        ("{recorded}", "&#123;recorded&#125;"),
    ],
)
def test_combined_cli_preserves_markdown_delimiters_as_literal_origin(
    tmp_path, capsys, suffix, encoded
):
    environment = deepcopy(ENVIRONMENT)
    origin = "https://example.test/" + suffix
    environment["origins"]["evs"] = origin
    first, second = combined_inputs(tmp_path, environment)
    original = second.read_bytes()
    assert main([str(first), "--live", str(second)]) == 0
    displayed = capsys.readouterr().out
    assert f"| evs | https://example.test/{encoded} |\n" in displayed
    assert json.loads(second.read_text())["environment"]["origins"]["evs"] == origin
    assert second.read_bytes() == original
