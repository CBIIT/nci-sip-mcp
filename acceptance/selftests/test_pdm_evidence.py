"""A failed PDM launch of a self-test leaves its evidence in the self-test JUnit report."""

import shutil
from pathlib import Path
from xml.etree import ElementTree

from pdm_launch import create_project, pdm_run

ROOT = Path(__file__).resolve().parents[2]
CANARY = "canary-7e41b0"
INNER = """
import os

from pdm_launch import pdm_run

VALUE = {canary!r}


def test_expected_error(monkeypatch):
    monkeypatch.setenv("LEAK", VALUE)
    result = pdm_run(
        "python", "-c",
        "import os, sys; print('ctx-out', os.environ.get('LEAK', 'absent')); sys.exit(2)",
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_missing_executable(tmp_path, monkeypatch):
    monkeypatch.setenv("LEAK", VALUE)
    pdm_run("python", "-c", "pass", timeout=5, environment={{"PATH": str(tmp_path)}})
"""


def test_the_self_test_configuration_keeps_launch_evidence_in_junit(tmp_path):
    inner = create_project(tmp_path / "inner")
    (inner / "test_support").mkdir()
    shutil.copy(ROOT / "test_support/pdm_launch.py", inner / "test_support/pdm_launch.py")
    (inner / "acceptance/selftests").mkdir(parents=True)
    shutil.copy(ROOT / "acceptance/pyproject.toml", inner / "acceptance/pyproject.toml")
    (inner / "acceptance/selftests/test_inner.py").write_text(INNER.format(canary=CANARY))
    report = inner / "acceptance/tmp/test-results/selftest.xml"

    ran = pdm_run(
        "python",
        "-m",
        "pytest",
        "selftests",
        "-p",
        "no:cacheprovider",
        f"--junitxml={report}",
        cwd=inner / "acceptance",
        project=inner,
        timeout=300,
    )

    assert ran.returncode == 1, ran.stdout + ran.stderr
    text = report.read_text()
    assert CANARY not in text
    cases = {
        case.get("name"): "".join(
            part.text or ""
            for part in case.iter()
            if part.tag in ("failure", "error", "system-err")
        )
        for case in ElementTree.fromstring(text).iter("testcase")  # noqa: S314 - our own report
    }
    expected = cases.get("test_expected_error", "")
    assert "assert 2 == 0" in expected
    assert "pdm-launch argv" in expected
    assert '"returncode=2"' in expected
    assert "ctx-out absent" in expected
    missing = cases.get("test_missing_executable", "")
    assert "FileNotFoundError: pdm executable not found on the launch PATH" in missing
    assert "pdm-launch outcome: " in missing
