"""The suite's identity: a digest over its own files, which a report states and which makes it an
acceptance report only when an approved release of the suite carries it."""

import json
import re
import shutil
import tomllib
from types import SimpleNamespace

import pytest
import yaml

from nci_sip_acceptance import report as report_module
from nci_sip_acceptance.report import COLLECTOR, SUITE, Collector, render
from nci_sip_acceptance.suite_identity import (
    REPOSITORY,
    SuiteError,
    approved_digests,
    fixture_set_version,
    identity,
    suite_digest,
)


def recording(day):
    return json.dumps({"kind": "recorded", "recorded_on": day}).encode()


FILES = {
    "acceptance/tests/test_a.py": b"def test_a():\n    pass\n",
    "acceptance/fixtures/manifest.yaml": b"evs:\n  release: ncit_26.09d\n",
    "acceptance/fixtures/recorded/evs/old.json": recording("2026-10-01"),
    "acceptance/fixtures/recorded/evs/new.json": recording("2026-10-03"),
    "acceptance/fixtures/crafted/version.json": b'{"kind": "crafted"}',
    "acceptance/pyproject.toml": b'[project]\ndynamic = ["version"]\n',
    "acceptance/request-forms/evs.md": b"# EVS\n",
    "acceptance/src/nci_sip_acceptance/client.py": b"VALUE = 1\n",
    "spec/tools.yaml": b"tools: []\n",
}
# The digest of FILES as the format defines it: a length before each name and each content.
GOLDEN = "adc4270dea06c0e385b8ff5508582e69f95d7683b27eb1181ff105c32a55c00b"
# Each is one of the kinds of file the digest covers.
COVERED = [
    "acceptance/tests/test_a.py",
    "acceptance/fixtures/crafted/version.json",
    "acceptance/fixtures/recorded/evs/old.json",
    "acceptance/pyproject.toml",
    "acceptance/fixtures/manifest.yaml",
    "spec/tools.yaml",
    "acceptance/request-forms/evs.md",
    "acceptance/src/nci_sip_acceptance/client.py",
]
# What a change leaves the digest as it was.
NOT_COVERED = [
    "acceptance/selftests/test_a.py",
    "acceptance/approved.yaml",
    "acceptance/tests/__pycache__/test_a.cpython-314.pyc",
    "acceptance/tests/.pytest_cache/state",
    "acceptance/.coverage",
    "acceptance/tests/.coverage.host.123",
    "spec/.DS_Store",
    "spec/Thumbs.db",
    "spec/.tools.yaml.swp",
    "spec/.tools.yaml.swo",
    "spec/tools.yaml~",
    "spec/.#tools.yaml",
    "spec/._tools.yaml",
    "acceptance/README.md",
    "acceptance/CHANGELOG.md",
    "src/nci_sip_mcp/server.py",
]


def build(root, files=FILES):
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return root


def approving(tmp_path, *digests):
    path = tmp_path / "approved.yaml"
    entries = [
        {"version": "v1.0.0", "digest": d, "approval": "ref", "date": "2026-12-01"} for d in digests
    ]
    path.write_text(yaml.safe_dump(entries), encoding="utf-8")
    return path


def rendered(root, approved):
    report = Collector().report("fixture") | {"suite": identity(root)}
    combined = {name: (row["outcome"], []) for name, row in report["tools"].items()}
    return render(report, combined, "fixture only", approved)


def test_the_digest_does_not_depend_on_file_order_or_on_where_the_tree_lies(tmp_path):
    forward = build(tmp_path / "one" / "deep")
    backward = build(tmp_path / "two", dict(reversed(FILES.items())))
    copied = shutil.copytree(forward, tmp_path / "three")

    assert suite_digest(forward) == suite_digest(backward) == suite_digest(copied)


def test_the_digest_of_a_fixed_tree_is_the_one_the_format_defines(tmp_path):
    # Dropping a length, putting it on the name only, or ordering by content moves this value;
    # a change of the format is a change of every approved digest, so it is made on purpose.
    assert suite_digest(build(tmp_path / "suite")) == GOLDEN


def test_a_name_cannot_be_absorbed_into_a_content_to_make_the_same_digest(tmp_path):
    # Joined without lengths, both trees give "spec/xyz".
    one = build(tmp_path / "one", FILES | {"spec/x": b"yz"})
    two = build(tmp_path / "two", FILES | {"spec/xy": b"z"})

    assert suite_digest(one) != suite_digest(two)


def test_line_ends_do_not_change_the_digest_of_a_text_file_but_a_binary_file_is_hashed_as_is(
    tmp_path,
):
    text = {"acceptance/tests/test_a.py": b"a\nb\n"}
    crlf = {"acceptance/tests/test_a.py": b"a\r\nb\r\n"}
    binary = {"acceptance/fixtures/blob.bin": b"\0a\nb\n"}
    binary_crlf = {"acceptance/fixtures/blob.bin": b"\0a\r\nb\r\n"}

    digests = [
        suite_digest(build(tmp_path / str(n), FILES | files))
        for n, files in enumerate([text, crlf, binary, binary_crlf])
    ]

    assert digests[0] == digests[1]
    assert digests[2] != digests[3]


def test_the_boundary_between_two_files_is_part_of_the_digest(tmp_path):
    one = build(tmp_path / "one", FILES | {"spec/a.yaml": b"x", "spec/b.yaml": b"yz"})
    two = build(tmp_path / "two", FILES | {"spec/a.yaml": b"xy", "spec/b.yaml": b"z"})
    renamed = build(tmp_path / "three", FILES | {"spec/a.yaml": b"x", "spec/c.yaml": b"yz"})

    digests = {suite_digest(one), suite_digest(two), suite_digest(renamed)}
    assert len(digests) == len([one, two, renamed])


@pytest.mark.parametrize("name", COVERED)
def test_changing_any_suite_file_turns_the_report_modified(tmp_path, name):
    root = build(tmp_path / "suite")
    approved = approving(tmp_path, suite_digest(root))
    assert rendered(root, approved).startswith("# ACCEPTANCE REPORT")

    (root / name).write_bytes(FILES[name] + b"\n")

    text = rendered(root, approved)
    assert text.startswith("# MODIFIED")
    assert "not an approved release of the suite (digest " in text.lower()


@pytest.mark.parametrize("name", NOT_COVERED)
def test_changing_what_is_not_the_suite_leaves_the_digest_alone(tmp_path, name):
    root = build(tmp_path / "suite")
    before = suite_digest(root)

    build(root, {name: b"changed"})

    assert suite_digest(root) == before


def test_a_digest_listed_as_approved_renders_an_acceptance_report_that_states_the_identity(
    tmp_path,
):
    root = build(tmp_path / "suite")
    digest = suite_digest(root)

    text = rendered(root, approving(tmp_path, "0" * 64, digest))

    assert text.startswith("# ACCEPTANCE REPORT\n")
    assert "MODIFIED" not in text
    assert f"Suite digest: {digest}." in text
    assert "Fixture set: ncit_26.09d, recorded 2026-10-03." in text
    assert re.search(r"Suite version: \S+\.", text)


def test_a_suite_not_listed_renders_modified_with_the_start_of_its_digest(tmp_path):
    root = build(tmp_path / "suite")
    digest = suite_digest(root)

    text = rendered(root, approving(tmp_path))

    assert text.startswith("# MODIFIED\n")
    assert f"Not an approved release of the suite (digest {digest[:12]}…)." in text
    assert f"Suite digest: {digest}." in text


def test_an_empty_approval_record_approves_nothing(tmp_path):
    path = tmp_path / "approved.yaml"
    path.write_text("# empty until the furnished tag\n[]\n", encoding="utf-8")

    assert approved_digests(path) == set()


def test_the_fixture_set_is_the_pin_and_the_latest_recording_date(tmp_path):
    root = build(tmp_path / "suite")

    assert fixture_set_version(root) == "ncit_26.09d, recorded 2026-10-03"
    shutil.rmtree(root / "acceptance/fixtures/recorded")
    assert fixture_set_version(root) == "ncit_26.09d, no recording"


def test_the_repository_itself_has_a_digest_and_an_approval_record():
    suite = identity()

    assert re.fullmatch(r"[0-9a-f]{64}", suite["digest"])
    assert re.fullmatch(r"ncit_\S+, recorded \d{4}-\d\d-\d\d", suite["fixture_set"])
    assert isinstance(approved_digests(), set)


def test_a_digest_equal_to_an_approved_one_but_for_its_last_character_is_not_approved(tmp_path):
    root = build(tmp_path / "suite")
    digest = suite_digest(root)
    near = digest[:-1] + ("0" if digest[-1] != "0" else "1")

    assert rendered(root, approving(tmp_path, near)).startswith("# MODIFIED")


@pytest.mark.parametrize(
    "content",
    [None, "{unclosed", "digest: abc\n", "- version: v1\n", "- digest: 12\n", "- abc\n", ""],
    ids=["missing", "malformed", "mapping", "no digest", "digest not text", "bare text", "empty"],
)
def test_an_approval_record_that_is_not_a_list_of_entries_with_digests_is_refused(
    tmp_path, content
):
    path = tmp_path / "approved.yaml"
    if content is not None:
        path.write_text(content, encoding="utf-8")

    with pytest.raises(SuiteError, match="must be a list of entries, each with a digest"):
        approved_digests(path)


def test_the_suite_of_an_empty_root_is_refused_with_the_root_named(tmp_path):
    with pytest.raises(SuiteError, match=re.escape(str(tmp_path))):
        suite_digest(tmp_path)


@pytest.mark.parametrize("missing", ["spec", "acceptance/fixtures/manifest.yaml"])
def test_a_root_lacking_a_part_of_the_suite_is_refused_not_digested_in_part(tmp_path, missing):
    root = build(tmp_path / "suite")
    if missing == "spec":
        shutil.rmtree(root / missing)
    else:
        (root / missing).unlink()

    with pytest.raises(SuiteError, match=re.escape(str(root))):
        suite_digest(root)


@pytest.mark.parametrize("link", ["spec/link.yaml", "spec/linked-directory"])
def test_a_symbolic_link_among_the_suite_files_is_refused_with_its_name(tmp_path, link):
    root = build(tmp_path / "suite")
    outside = build(tmp_path / "outside", {"leak.yaml": b"x"})
    (root / link).symlink_to(outside / "leak.yaml" if link.endswith("yaml") else outside)

    with pytest.raises(SuiteError, match=re.escape(link)):
        suite_digest(root)


def test_a_symbolic_link_in_place_of_a_whole_directory_of_the_suite_is_refused(tmp_path):
    root = build(tmp_path / "suite")
    other = build(tmp_path / "other", {"tools.yaml": b"x"})
    shutil.rmtree(root / "spec")
    (root / "spec").symlink_to(other)

    with pytest.raises(SuiteError, match=re.escape(str(root / "spec"))):
        suite_digest(root)


def test_the_suite_version_comes_from_the_tag_not_from_a_file():
    pyproject = tomllib.loads((REPOSITORY / "acceptance" / "pyproject.toml").read_text("utf-8"))

    assert "version" in pyproject["project"]["dynamic"]
    assert "version" not in pyproject["project"]
    assert pyproject["tool"]["pdm"]["version"]["source"] == "scm"


def stub_config(rootpath, report):
    stash = pytest.Stash()
    stash[COLLECTOR] = Collector()
    return SimpleNamespace(
        rootpath=rootpath, stash=stash, getoption=lambda name: report if name == "report" else None
    )


def test_a_report_states_the_identity_taken_when_the_run_started(tmp_path):
    written = tmp_path / "report.json"
    config = stub_config(REPOSITORY / "acceptance", str(written))

    report_module.pytest_sessionstart(SimpleNamespace(config=config))
    report_module.write_report(config, "fixture")

    suite = json.loads(written.read_text(encoding="utf-8"))["suite"]
    assert suite == identity() == config.stash[SUITE]
    assert set(suite) == {"version", "fixture_set", "digest"}


def test_a_run_in_another_checkout_than_the_installed_harness_is_refused_naming_both(tmp_path):
    config = stub_config(tmp_path / "other" / "acceptance", str(tmp_path / "report.json"))

    with pytest.raises(pytest.UsageError) as refused:
        report_module.pytest_sessionstart(SimpleNamespace(config=config))

    assert str(tmp_path / "other") in str(refused.value)
    assert str(REPOSITORY) in str(refused.value)


def test_a_run_in_a_root_that_is_not_a_suite_is_a_usage_error_naming_it(monkeypatch, tmp_path):
    monkeypatch.setattr(report_module, "REPOSITORY", tmp_path)
    config = stub_config(tmp_path / "acceptance", str(tmp_path / "report.json"))

    with pytest.raises(pytest.UsageError, match=re.escape(str(tmp_path))):
        report_module.pytest_sessionstart(SimpleNamespace(config=config))


def test_a_run_without_a_report_takes_no_identity(tmp_path):
    config = stub_config(tmp_path / "other" / "acceptance", None)

    report_module.pytest_sessionstart(SimpleNamespace(config=config))
    report_module.write_report(config, "fixture")

    assert SUITE not in config.stash
