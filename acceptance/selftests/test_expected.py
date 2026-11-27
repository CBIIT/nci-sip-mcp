"""The expected outcomes: a report is compared with them test by test, and they are rewritten
from a report on request."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from pdm_stand_in import changed, hostile_selectors, outer_stand_in, tree

from nci_sip_acceptance.expected import (
    ABSENT_FROM_EXPECTED,
    ABSENT_FROM_REPORT,
    EXPECTED,
    differences,
    main,
)
from nci_sip_acceptance.report import RANK

BASE_SHA = "a" * 40
OLD_SHA = "b" * 40
ROOT = Path(__file__).resolve().parents[2]


def pdm_path():
    path = shutil.which("pdm")
    if path is None:
        raise RuntimeError("pdm is required to test the acceptance-expected entry point")
    return path


PDM = pdm_path()


def report(tests, mode):
    return {
        "mode": mode,
        "transport": "stdio",
        "run": {
            "exit_status": 1,
            "selected": len(tests),
            "finished": len(tests),
            "worker_crashes": 0,
        },
        "tests": {nodeid: {"tool": "get_form", "outcome": tests[nodeid]} for nodeid in tests},
    }


@pytest.fixture
def files(tmp_path):
    """Write the expected outcomes and a report; return the arguments of `check`."""

    def write(expected, tests, mode="fixture"):
        (tmp_path / "expected.json").write_text(json.dumps(expected), encoding="utf-8")
        (tmp_path / "report.json").write_text(json.dumps(report(tests, mode)), encoding="utf-8")
        return [
            "check",
            str(tmp_path / "report.json"),
            "--expected",
            str(tmp_path / "expected.json"),
        ]

    return write


def approval(test, after, *, base_sha=BASE_SHA, before="passed"):
    return {
        "base_sha": base_sha,
        "test": test,
        "before": before,
        "after": after,
        "reason": "Reviewed regression",
        "reference": "#403",
    }


def run_expected(arguments):
    return subprocess.run(  # noqa: S603 - fixed PDM entry point and test-owned paths
        [PDM, "run", "acceptance-expected", *arguments],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def test_the_entry_point_ignores_inherited_pdm_selectors(files, tmp_path, monkeypatch):
    arguments = files({"t.py::a": "passed"}, {"t.py::a": "passed"})
    outer = outer_stand_in(tmp_path)
    before = tree(outer)
    for name, value in hostile_selectors(outer).items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("PDM_IGNORE_ACTIVE_VENV", raising=False)

    ran = run_expected(arguments)

    assert changed(before, tree(outer)) == {}, ran.stderr
    assert (ran.returncode, ran.stdout, ran.stderr) == (
        0,
        "All 1 tests have the outcome expected.\n",
        "",
    )


def assert_invalid_primary_input(ran):
    assert ran.returncode != 0
    assert ran.stderr.startswith("Invalid expected outcomes or fixture report:")
    assert ran.stdout == ""
    assert "Traceback" not in ran.stderr


@pytest.fixture
def historical_files(files, tmp_path):
    """Write current, base and approval inputs for the historical check mode."""

    def write(base, current, actual, approvals):
        arguments = files(current, actual)
        base_path = tmp_path / "base.json"
        approvals_path = tmp_path / "regressions.yaml"
        base_path.write_text(json.dumps(base), encoding="utf-8")
        approvals_path.write_text(
            yaml.safe_dump({"version": 1, "approvals": approvals}), encoding="utf-8"
        )
        return [
            *arguments,
            "--base",
            str(base_path),
            "--base-sha",
            BASE_SHA,
            "--approvals",
            str(approvals_path),
        ]

    return write


def test_equal_outcomes_are_no_difference(files, capsys):
    arguments = files(
        {"t.py::a": "passed", "t.py::b": "failed"}, {"t.py::b": "failed", "t.py::a": "passed"}
    )

    assert main(arguments) == 0
    assert capsys.readouterr().out == "All 2 tests have the outcome expected.\n"


def test_a_changed_outcome_is_a_difference_in_a_table_with_the_hint(files, capsys):
    arguments = files(
        {"t.py::a": "passed", "t.py::b": "failed"}, {"t.py::a": "passed", "t.py::b": "passed"}
    )

    assert main(arguments) == 1
    output = capsys.readouterr().out
    assert "1 of 2 tests differ from the expected outcomes." in output
    assert "| `t.py::b` | failed | passed |" in output
    assert "t.py::a" not in output
    assert "pdm run acceptance-expected update" in output


def test_a_test_the_report_lacks_and_one_the_expected_outcomes_lack_are_differences(files, capsys):
    arguments = files({"t.py::gone": "passed"}, {"t.py::new": "passed"})

    assert main(arguments) == 1
    output = capsys.readouterr().out
    assert f"| `t.py::gone` | passed | {ABSENT_FROM_REPORT} |" in output
    assert f"| `t.py::new` | {ABSENT_FROM_EXPECTED} | passed |" in output


def test_a_pipe_in_a_test_id_does_not_break_the_table(files, capsys):
    main(files({"t.py::a[x|y]": "passed"}, {"t.py::a[x|y]": "failed"}))

    assert "| `t.py::a[x\\|y]` | passed | failed |" in capsys.readouterr().out


def test_a_report_of_a_live_run_is_refused(files):
    with pytest.raises(SystemExit, match="is the report of a live run, not a fixture run"):
        main(files({}, {}, mode="live"))


def test_differences_are_listed_in_test_order():
    found = differences({"b": "passed", "a": "passed"}, {"b": "failed", "a": "failed"})

    assert [test for test, _, _ in found] == ["a", "b"]


def test_update_rewrites_the_outcomes_from_a_report_and_nothing_else(files, tmp_path, capsys):
    arguments = files({"t.py::old": "passed"}, {"t.py::b": "failed", "t.py::a": "not_implemented"})

    assert main(["update", *arguments[1:]]) == 0

    written = json.loads((tmp_path / "expected.json").read_text(encoding="utf-8"))
    assert written == {"t.py::a": "not_implemented", "t.py::b": "failed"}
    assert list(written) == ["t.py::a", "t.py::b"]
    assert "Wrote the outcomes of 2 tests" in capsys.readouterr().out
    assert main(arguments) == 0


def test_update_writes_no_message_or_request_the_report_holds(files, tmp_path):
    arguments = files({}, {"t.py::a": "failed"})
    detailed = report({"t.py::a": "no_fixture"}, "fixture")
    detailed["tests"]["t.py::a"] |= {"message": "assert 1 == 2", "unmatched": ["GET evs /x {}"]}
    (tmp_path / "report.json").write_text(json.dumps(detailed), encoding="utf-8")

    main(["update", *arguments[1:]])

    written = (tmp_path / "expected.json").read_text(encoding="utf-8")
    assert json.loads(written) == {"t.py::a": "no_fixture"}
    assert "assert" not in written
    assert "GET" not in written


@pytest.mark.parametrize(
    ("expected", "actual"),
    [("not_implemented", "not_live"), ("not_live", "not_implemented")],
)
def test_outcomes_that_share_a_prefix_are_a_difference(files, capsys, expected, actual):
    assert main(files({"t.py::a": expected}, {"t.py::a": actual})) == 1
    assert f"| `t.py::a` | {expected} | {actual} |" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ([], "expected outcomes must be an object"),
        ({"t.py::a": 1}, "expected outcomes must map test strings to outcome strings"),
        ({"t.py::a": "unknown"}, "expected outcomes has an unknown outcome: unknown"),
    ],
)
def test_malformed_expected_outcomes_are_refused(files, tmp_path, value, message):
    arguments = files({}, {})
    (tmp_path / "expected.json").write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(SystemExit) as error:
        main(arguments)
    assert str(error.value) == f"Invalid expected outcomes or fixture report: {message}"


def test_exact_approvals_admit_failed_and_removed_base_passes(historical_files, capsys):
    arguments = historical_files(
        {"t.py::failed": "passed", "t.py::gone": "passed", "t.py::improved": "failed"},
        {"t.py::failed": "failed", "t.py::improved": "passed", "t.py::new": "passed"},
        {"t.py::failed": "failed", "t.py::improved": "passed", "t.py::new": "passed"},
        [approval("t.py::failed", "failed"), approval("t.py::gone", "absent")],
    )

    assert main(arguments) == 0
    assert capsys.readouterr().out.endswith("Approved 2 regressions from passed outcomes.\n")


@pytest.mark.parametrize("after", [outcome for outcome in RANK if outcome != "passed"])
def test_each_nonpass_outcome_requires_its_exact_approval(historical_files, capsys, after):
    arguments = historical_files(
        {"t.py::a": "passed"},
        {"t.py::a": after},
        {"t.py::a": after},
        [approval("t.py::a", after)],
    )

    assert main(arguments) == 0
    assert capsys.readouterr().out.endswith("Approved 1 regressions from passed outcomes.\n")


@pytest.mark.parametrize(
    ("after", "approved_after"),
    [
        ("skipped", "failed"),
        ("not_implemented", "skipped"),
        ("not_live", "not_implemented"),
        ("failed", "no_fixture"),
        ("no_fixture", "failed"),
    ],
)
def test_approval_for_another_nonpass_outcome_does_not_match(
    historical_files, capsys, after, approved_after
):
    arguments = historical_files(
        {"t.py::a": "passed"},
        {"t.py::a": after},
        {"t.py::a": after},
        [approval("t.py::a", approved_after)],
    )

    assert main(arguments) == 1
    output = capsys.readouterr().out
    assert "1 regression from passed needs approval: t.py::a" in output
    assert "Remove 1 stale approval for this base: t.py::a" in output


def test_approval_never_excuses_current_expected_report_difference(historical_files, capsys):
    arguments = historical_files(
        {"t.py::a": "passed"},
        {"t.py::a": "passed"},
        {"t.py::a": "failed"},
        [approval("t.py::a", "failed")],
    )

    assert main(arguments) == 1
    assert "differ from the expected outcomes" in capsys.readouterr().out


def test_wrong_base_does_not_approve_and_older_records_are_inert(historical_files, capsys):
    arguments = historical_files(
        {"t.py::a": "passed"},
        {"t.py::a": "failed"},
        {"t.py::a": "failed"},
        [approval("t.py::a", "failed", base_sha=OLD_SHA)],
    )

    assert main(arguments) == 1
    output = capsys.readouterr().out
    assert "1 regression from passed needs approval" in output
    assert "t.py::a" in output


def test_same_test_can_have_approvals_for_different_bases(historical_files, capsys):
    arguments = historical_files(
        {"t.py::a": "passed"},
        {"t.py::a": "failed"},
        {"t.py::a": "failed"},
        [
            approval("t.py::a", "failed"),
            approval("t.py::a", "no_fixture", base_sha=OLD_SHA),
        ],
    )

    assert main(arguments) == 0
    assert capsys.readouterr().out.endswith("Approved 1 regressions from passed outcomes.\n")


def test_current_base_approval_without_its_exact_regression_is_stale(historical_files, capsys):
    arguments = historical_files(
        {"t.py::a": "passed"},
        {"t.py::a": "passed"},
        {"t.py::a": "passed"},
        [approval("t.py::a", "failed"), approval("t.py::old", "failed", base_sha=OLD_SHA)],
    )

    assert main(arguments) == 1
    assert "Remove 1 stale approval for this base" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("document", "message"),
    [
        (
            {
                "version": 1,
                "approvals": [approval("t.py::a", "failed"), approval("t.py::a", "failed")],
            },
            "a base and test may have only one approval",
        ),
        (
            {
                "version": 1,
                "approvals": [
                    approval("t.py::a", "failed"),
                    approval("t.py::a", "no_fixture"),
                ],
            },
            "a base and test may have only one approval",
        ),
        (
            {"version": 1, "approvals": [approval("t.py::a", "unknown")]},
            "approval transition has an unknown outcome",
        ),
        (
            {"version": 1, "approvals": [{**approval("t.py::a", "failed"), "extra": "x"}]},
            "each approval must have exactly the six documented fields",
        ),
        (
            {"version": 1, "approvals": [{**approval("t.py::a", "failed"), "reason": ""}]},
            "test, reason and reference must be nonempty",
        ),
        (
            {"version": 1, "approvals": [{**approval("t.py::a", "failed"), "test": " \t"}]},
            "test, reason and reference must be nonempty",
        ),
        (
            {"version": 1, "approvals": [{**approval("t.py::a", "failed"), "reason": " \n"}]},
            "test, reason and reference must be nonempty",
        ),
        (
            {
                "version": 1,
                "approvals": [{**approval("t.py::a", "failed"), "reference": "  "}],
            },
            "test, reason and reference must be nonempty",
        ),
        ([], "root must be an object"),
        (
            {"version": 1, "approvals": [], "extra": True},
            "root must contain exactly version and approvals",
        ),
        ({"version": True, "approvals": []}, "version must be integer 1"),
        ({"version": 1.0, "approvals": []}, "version must be integer 1"),
        ({"version": 2, "approvals": []}, "version must be integer 1"),
        ({"version": 1, "approvals": {}}, "approvals must be a list"),
        ({"version": 1, "approvals": [[]]}, "each approval must be an object"),
        (
            {"version": 1, "approvals": [{**approval("t.py::a", "failed"), "reason": 1}]},
            "every approval field must be a string",
        ),
        (
            {"version": 1, "approvals": [approval("t.py::a", "failed", base_sha="bad")]},
            "base_sha must be 40 lowercase hexadecimal characters",
        ),
        (
            {
                "version": 1,
                "approvals": [approval("t.py::a", "failed", base_sha=BASE_SHA + "a")],
            },
            "base_sha must be 40 lowercase hexadecimal characters",
        ),
        (
            {
                "version": 1,
                "approvals": [approval("t.py::a", "failed", base_sha=BASE_SHA + "\n")],
            },
            "base_sha must be 40 lowercase hexadecimal characters",
        ),
        (
            {"version": 1, "approvals": [approval("t.py::a", "failed", before="failed")]},
            "approval transition has an unknown outcome",
        ),
        (
            {"version": 1, "approvals": [approval("t.py::a", "passed")]},
            "approval transition has an unknown outcome",
        ),
    ],
)
def test_duplicate_conflicting_and_malformed_approvals_fail(historical_files, document, message):
    arguments = historical_files(
        {"t.py::a": "passed"}, {"t.py::a": "failed"}, {"t.py::a": "failed"}, []
    )
    with open(arguments[-1], "w", encoding="utf-8") as approvals_file:
        yaml.safe_dump(document, approvals_file)

    with pytest.raises(SystemExit) as error:
        main(arguments)
    assert str(error.value) == f"Invalid regression approvals: {message}"


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ("version: 1\nversion: 1\napprovals: []\n", "duplicate mapping key: version"),
        ("version: 1\napprovals: []\napprovals: []\n", "duplicate mapping key: approvals"),
        (
            f"""version: 1
approvals:
  - base_sha: {BASE_SHA}
    test: t.py::a
    before: passed
    after: failed
    reason: first
    reason: second
    reference: issue
""",
            "duplicate mapping key: reason",
        ),
        (
            f"""version: 1
approvals:
  - <<: &approval
      base_sha: {BASE_SHA}
      test: t.py::a
      before: passed
      after: failed
      reason: first
      reference: issue
    reason: second
""",
            "YAML merge keys are not allowed",
        ),
        (
            f"""version: 1
approvals:
  - !!merge inherited: &approval
      base_sha: {BASE_SHA}
      test: t.py::a
      before: passed
      after: failed
      reason: first
      reference: issue
    reason: second
""",
            "YAML merge keys are not allowed",
        ),
        (
            "version: 1\napprovals:\n  ? [not, scalar]\n  : value\n",
            "mapping keys must be scalars",
        ),
        (
            "version: 1\napprovals: &rows\n  - *rows\n",
            "YAML aliases are not allowed",
        ),
        (
            f"""version: 1
approvals:
  - base_sha: {BASE_SHA}
    test: t.py::a
    before: passed
    after: failed
    reason: &text reviewed
    reference: *text
""",
            "YAML aliases are not allowed",
        ),
        (
            f"""version: 1
approvals:
  - base_sha: {BASE_SHA}
    test: &field reason
    before: passed
    after: failed
    *field: reviewed
    reference: issue
""",
            "YAML aliases are not allowed",
        ),
    ],
)
def test_ambiguous_or_aliased_approval_yaml_fails(historical_files, document, message):
    arguments = historical_files(
        {"t.py::a": "passed"}, {"t.py::a": "failed"}, {"t.py::a": "failed"}, []
    )
    with open(arguments[-1], "w", encoding="utf-8") as approvals_file:
        approvals_file.write(document)

    with pytest.raises(SystemExit) as error:
        main(arguments)
    assert str(error.value) == f"Invalid regression approvals: {message}"


def test_only_base_passes_that_changed_need_approval(historical_files, capsys):
    arguments = historical_files(
        {"t.py::steady": "passed", "t.py::nonpass": "failed"},
        {"t.py::steady": "passed", "t.py::nonpass": "no_fixture"},
        {"t.py::steady": "passed", "t.py::nonpass": "no_fixture"},
        [],
    )

    assert main(arguments) == 0
    assert capsys.readouterr().out.endswith("Approved 0 regressions from passed outcomes.\n")


@pytest.mark.parametrize("base_outcome", [outcome for outcome in RANK if outcome != "passed"])
def test_removing_a_nonpassing_base_test_needs_no_approval(historical_files, capsys, base_outcome):
    arguments = historical_files({"t.py::gone": base_outcome}, {}, {}, [])

    assert main(arguments) == 0
    assert capsys.readouterr().out.endswith("Approved 0 regressions from passed outcomes.\n")


@pytest.mark.parametrize(
    "flags",
    [
        ("--base",),
        ("--base-sha",),
        ("--approvals",),
        ("--base", "--base-sha"),
        ("--base", "--approvals"),
        ("--base-sha", "--approvals"),
    ],
)
def test_historical_arguments_are_supplied_as_one_complete_set(files, tmp_path, flags):
    arguments = files({"t.py::a": "passed"}, {"t.py::a": "passed"})
    values = {
        "--base": str(tmp_path / "base.json"),
        "--base-sha": BASE_SHA,
        "--approvals": str(tmp_path / "regressions.yaml"),
    }
    historical = [item for flag in flags for item in (flag, values[flag])]

    with pytest.raises(
        SystemExit, match="--base, --base-sha and --approvals must be used together"
    ):
        main([*arguments, *historical])


def test_malformed_historical_inputs_are_refused(historical_files, tmp_path):
    arguments = historical_files(
        {"t.py::a": "passed"}, {"t.py::a": "passed"}, {"t.py::a": "passed"}, []
    )
    (tmp_path / "base.json").write_text("[]", encoding="utf-8")
    with pytest.raises(SystemExit, match="Invalid base expected outcomes"):
        main(arguments)

    (tmp_path / "base.json").write_text('{"t.py::a":"passed"}', encoding="utf-8")
    arguments[arguments.index("--base-sha") + 1] = "not-a-commit"
    with pytest.raises(SystemExit, match="--base-sha must be 40 lowercase hexadecimal"):
        main(arguments)
    with pytest.raises(SystemExit, match="historical arguments apply only to check"):
        main(["update", *arguments[1:]])


@pytest.mark.parametrize("base_sha", [BASE_SHA + "a", BASE_SHA + "\n"])
def test_base_sha_requires_an_exact_full_string(historical_files, base_sha):
    arguments = historical_files(
        {"t.py::a": "passed"}, {"t.py::a": "passed"}, {"t.py::a": "passed"}, []
    )
    arguments[arguments.index("--base-sha") + 1] = base_sha

    with pytest.raises(SystemExit, match="--base-sha must be 40 lowercase hexadecimal"):
        main(arguments)


def test_missing_approval_file_has_an_actionable_diagnostic(historical_files, tmp_path):
    arguments = historical_files(
        {"t.py::a": "passed"}, {"t.py::a": "passed"}, {"t.py::a": "passed"}, []
    )
    (tmp_path / "regressions.yaml").unlink()

    ran = run_expected(arguments)

    assert ran.returncode != 0
    assert ran.stderr.startswith("Invalid regression approvals:")
    assert ran.stdout == ""
    assert "Traceback" not in ran.stderr


def test_missing_base_file_has_an_actionable_diagnostic(historical_files, tmp_path):
    arguments = historical_files(
        {"t.py::a": "passed"}, {"t.py::a": "passed"}, {"t.py::a": "passed"}, []
    )
    (tmp_path / "base.json").unlink()

    ran = run_expected(arguments)

    assert ran.returncode != 0
    assert ran.stderr.startswith("Invalid base expected outcomes:")
    assert ran.stdout == ""
    assert "Traceback" not in ran.stderr


def test_malformed_approval_yaml_has_an_actionable_diagnostic(historical_files, tmp_path):
    arguments = historical_files(
        {"t.py::a": "passed"}, {"t.py::a": "passed"}, {"t.py::a": "passed"}, []
    )
    (tmp_path / "regressions.yaml").write_text("version: 1\napprovals: [\n", encoding="utf-8")

    ran = run_expected(arguments)

    assert ran.returncode != 0
    assert ran.stderr.startswith("Invalid regression approvals:")
    assert ran.stdout == ""
    assert "Traceback" not in ran.stderr


@pytest.mark.parametrize(
    ("name", "contents"),
    [
        ("expected.json", None),
        ("expected.json", "["),
        ("report.json", None),
        ("report.json", "["),
    ],
)
def test_missing_or_malformed_primary_input_has_an_actionable_diagnostic(
    files, tmp_path, name, contents
):
    arguments = files({"t.py::a": "passed"}, {"t.py::a": "passed"})
    path = tmp_path / name
    if contents is None:
        path.unlink()
    else:
        path.write_text(contents, encoding="utf-8")

    assert_invalid_primary_input(run_expected(arguments))


def test_malformed_report_row_has_an_actionable_diagnostic(files, tmp_path):
    arguments = files({"t.py::a": "passed"}, {"t.py::a": "passed"})
    malformed = report({"t.py::a": "passed"}, "fixture")
    del malformed["tests"]["t.py::a"]["outcome"]
    (tmp_path / "report.json").write_text(json.dumps(malformed), encoding="utf-8")

    assert_invalid_primary_input(run_expected(arguments))


def test_nonmapping_report_tests_have_an_actionable_diagnostic(files, tmp_path):
    arguments = files({}, {})
    malformed = report({}, "fixture")
    malformed["tests"] = []
    (tmp_path / "report.json").write_text(json.dumps(malformed), encoding="utf-8")

    assert_invalid_primary_input(run_expected(arguments))


@pytest.mark.parametrize("operation", ["check", "update"])
def test_nonobject_report_result_has_an_actionable_diagnostic(files, tmp_path, operation):
    arguments = files({"t.py::a": "passed"}, {"t.py::a": "passed"})
    malformed = report({"t.py::a": "passed"}, "fixture")
    malformed["tests"]["t.py::a"] = 7
    (tmp_path / "report.json").write_text(json.dumps(malformed), encoding="utf-8")

    assert_invalid_primary_input(run_expected([operation, *arguments[1:]]))
    assert json.loads((tmp_path / "expected.json").read_text(encoding="utf-8")) == {
        "t.py::a": "passed"
    }


def test_unknown_report_outcome_has_an_actionable_diagnostic(files, tmp_path):
    arguments = files({"t.py::a": "passed"}, {"t.py::a": "passed"})
    malformed = report({"t.py::a": "unknown"}, "fixture")
    (tmp_path / "report.json").write_text(json.dumps(malformed), encoding="utf-8")

    assert_invalid_primary_input(run_expected(arguments))


@pytest.mark.parametrize("operation", ["check", "update"])
@pytest.mark.parametrize("outcome", [[], {}])
def test_nonstring_report_outcome_has_the_exact_input_diagnostic(
    files, tmp_path, operation, outcome
):
    arguments = files({"t.py::a": "passed"}, {"t.py::a": "passed"})
    malformed = report({"t.py::a": "passed"}, "fixture")
    malformed["tests"]["t.py::a"]["outcome"] = outcome
    (tmp_path / "report.json").write_text(json.dumps(malformed), encoding="utf-8")

    ran = run_expected([operation, *arguments[1:]])

    assert ran.returncode != 0
    assert ran.stderr == (
        "Invalid expected outcomes or fixture report: fixture report test has an unknown outcome\n"
    )
    assert ran.stdout == ""
    assert json.loads((tmp_path / "expected.json").read_text(encoding="utf-8")) == {
        "t.py::a": "passed"
    }


def test_update_refuses_an_unknown_report_outcome(files, tmp_path):
    arguments = files({"t.py::a": "passed"}, {"t.py::a": "unknown"})

    ran = run_expected(["update", *arguments[1:]])

    assert_invalid_primary_input(ran)
    assert json.loads((tmp_path / "expected.json").read_text(encoding="utf-8")) == {
        "t.py::a": "passed"
    }


def test_update_missing_report_has_an_actionable_diagnostic(files, tmp_path):
    arguments = files({"t.py::a": "passed"}, {"t.py::a": "passed"})
    (tmp_path / "report.json").unlink()

    ran = run_expected(["update", *arguments[1:]])

    assert_invalid_primary_input(ran)
    assert json.loads((tmp_path / "expected.json").read_text(encoding="utf-8")) == {
        "t.py::a": "passed"
    }


def test_the_committed_outcomes_are_in_the_reports_vocabulary_and_in_test_order():
    committed = json.loads(EXPECTED.read_text(encoding="utf-8"))

    assert set(committed.values()) <= set(RANK)
    assert list(committed) == sorted(committed)
