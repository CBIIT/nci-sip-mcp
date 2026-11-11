"""A live selection must fail safely before any process or request can start."""

import hashlib
import json
import os
import runpy
import subprocess
import sys
from http import HTTPStatus
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, build_opener

import pytest
import yaml

from conftest import FIXTURES, furnished_child
from nci_si_acceptance import environments
from nci_si_acceptance.client import Target, server_environment
from nci_si_acceptance.fixture_server import (
    UPSTREAM_VARIABLES,
    FixtureServer,
    FixtureSet,
    load_fixtures,
)

ENVIRONMENT = "NCI_SI_ACCEPTANCE_ENVIRONMENT"
pytest_plugins = ["pytester"]


@pytest.fixture(autouse=True)
def clean_settings(monkeypatch):
    for key in os.environ:
        if key.startswith("NCI_SI_") or environments.external_runtime_setting(key):
            monkeypatch.delenv(key)
    monkeypatch.setenv("NCI_SI_ACCEPTANCE_MODE", "live")
    popen = subprocess.Popen

    def local_process(command, *args, **kwargs):
        if "nci_si_acceptance.live_launcher" in command:
            env = kwargs.get("env") or {}
            assert all(
                urlsplit(env.get(key, "")).hostname == "127.0.0.1" for key in UPSTREAM_VARIABLES
            )
        return popen(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", local_process)


@pytest.mark.parametrize("name", [None, "", "stage", "production", "unknown"])
def test_selection_precedes_process_or_request(monkeypatch, name):
    if name is not None:
        monkeypatch.setenv(ENVIRONMENT, name)
    with pytest.raises(ValueError) as error:
        Target.from_env()
    reason = "is required for live mode" if not name else "does not name a reviewed environment"
    assert str(error.value) == f"{ENVIRONMENT} {reason}"


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("NCI_SI_EVS_BASE_URL", "http://127.0.0.1:1"),
        ("NCI_SI_CADSR_FTP_URL", "http://127.0.0.1:1"),
        ("NCI_SI_CADSR_CREDENTIAL", "private-marker"),
        ("HTTP_PROXY", "http://127.0.0.1:1"),
        ("hTtPs_PrOxY", "http://127.0.0.1:1"),
        ("NO_PROXY", "*"),
        ("PYTHONPATH", "/external/module-substitution"),
        ("Python_ROOT_DIR", "/external/runner-python"),
        ("LD_PRELOAD", "private-marker"),
        ("LD_LIBRARY_PATH", "private-marker"),
        ("DYLD_INSERT_LIBRARIES", "private-marker"),
        ("DYLD_LIBRARY_PATH", "private-marker"),
        ("NCI_SI_ACCEPTANCE_SERVER", "python alternate-server.py"),
        ("NCI_SI_ACCEPTANCE_SERVER", "nci-si-mcp serve; alternate"),
        (
            "NCI_SI_ACCEPTANCE_PREPARE",
            'nci-si-mcp index-sample $(cat "$NCI_SI_ACCEPTANCE_INDEX_CODES")',
        ),
        ("NCI_SI_ACCEPTANCE_PREPARE", "nci-si-mcp index-sample C2991"),
        ("NCI_SI_ACCEPTANCE_PROFILE", "evs"),
        ("NCI_SI_ACCEPTANCE_SECURITY_SERVER", "python alternate.py"),
        ("NCI_SI_ACCEPTANCE_FIXTURE_BIND", "127.0.0.1:1"),
        ("NCI_SI_ACCEPTANCE_FIXTURE_URL", "http://127.0.0.1:1"),
        ("NCI_SI_ACCEPTANCE_URL", "http://127.0.0.1:1/mcp"),
    ],
)
def test_unsupported_live_commands_and_overrides_are_refused(monkeypatch, key, value):
    monkeypatch.setenv(ENVIRONMENT, "evs-only")
    monkeypatch.setenv(key, value)
    with pytest.raises(ValueError) as error:
        Target.from_env()
    assert key in str(error.value)
    assert value not in str(error.value)


def test_fixture_mode_cannot_claim_a_live_selection(monkeypatch):
    monkeypatch.setenv("NCI_SI_ACCEPTANCE_MODE", "fixture")
    monkeypatch.setenv(ENVIRONMENT, "evs-only")
    with pytest.raises(ValueError, match=ENVIRONMENT):
        Target.from_env()


def test_selected_live_server_uses_only_the_furnished_isolated_launcher(monkeypatch):
    monkeypatch.setenv(ENVIRONMENT, "evs-only")
    assert Target.from_env().command == [
        sys.executable,
        "-I",
        "-m",
        "nci_si_acceptance.live_launcher",
        "serve",
    ]


def test_ci_host_settings_preserve_the_positive_owner(pytester, monkeypatch):
    monkeypatch.delenv("SELFTEST_SHARD", raising=False)
    pytester.makepyfile(ci_host="import os\nos.environ['Python_ROOT_DIR'] = '/ci-python'\n")
    node = f"{__file__}::test_selected_live_server_uses_only_the_furnished_isolated_launcher"
    result = pytester.runpytest_subprocess("-p", "ci_host", node, "-q")
    assert result.ret == 0, result.stdout.str() + result.stderr.str()


@pytest.fixture
def selected_policy(monkeypatch, tmp_path):
    with FixtureServer(load_fixtures(FIXTURES)) as upstream:
        policy = yaml.safe_load(environments.POLICY.read_bytes())
        for surface in ("evs", "evs-fhir"):
            policy["allowed"][surface] = upstream.base_url(surface)
            policy["environments"]["evs-only"][surface] = upstream.base_url(surface)
        path = tmp_path / "policy.yaml"
        path.write_text(yaml.safe_dump(policy))
        monkeypatch.setattr(environments, "POLICY", path)
        monkeypatch.setenv(ENVIRONMENT, "evs-only")
        monkeypatch.setenv("NCI_SI_EVS_LICENSE_KEY", "selected-private-marker")
        yield upstream, path


def test_selected_destinations_and_credentials_are_one_private_snapshot(
    monkeypatch, tmp_path, selected_policy
):
    upstream, path = selected_policy
    monkeypatch.setenv("OTHER_BASE_URL", "unrelated-private-marker")
    target = Target.from_env()
    selected = target.environment
    expected = json.loads(json.dumps(selected.to_record()))
    assert expected["policy_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    path.write_text("invalid replacement policy")
    assert selected.to_record() == expected
    assert "selected-private-marker" not in json.dumps(expected)
    with pytest.raises(TypeError):
        selected.origins["evs"] = "http://127.0.0.1:1"
    monkeypatch.setenv("NCI_SI_EVS_LICENSE_KEY", "later-private-marker")
    with target.managed() as managed:
        env = server_environment(
            "live", tmp_path / "data", None, environment=selected, denied_url=managed.denied_url
        )
        assert env["NCI_SI_EVS_LICENSE_KEY"] == "selected-private-marker"
        assert "selected-private-marker" not in repr(target)
        # This assertion guards the old/red and mutated implementation from nonlocal sends.
        for key in UPSTREAM_VARIABLES:
            assert env[key].startswith("http://127.0.0.1:")
        ran = furnished_child([*environments.LAUNCHER, "index-sample", "C4817"], env)
        assert ran.returncode == 0, ran.stderr
        received = upstream.log()
        assert received and all(row["surface"] == "evs" for row in received)
        for row in received:
            headers = {key.lower(): value for key, value in row["headers"].items()}
            assert headers["x-evsrestapi-license-key"] == "selected-private-marker"
        with (
            pytest.raises(HTTPError) as error,
            build_opener(ProxyHandler({})).open(
                env["NCI_SI_CADSR_BASE_URL"] + "/blocked", timeout=2
            ),
        ):
            pass
        with error.value:
            assert error.value.code == HTTPStatus.FORBIDDEN
            assert upstream.log() == received


@pytest.mark.parametrize(
    "key", ["LD_PRELOAD", "DYLD_INSERT_LIBRARIES", "PYTHONPATH", "HTTP_PROXY", "Python_ROOT_DIR"]
)
def test_live_child_environment_discards_later_ambient_injection(monkeypatch, tmp_path, key):
    monkeypatch.setenv(ENVIRONMENT, "evs-only")
    target = Target.from_env()
    # Set only in this process; never execute a loader with an injected variable.
    monkeypatch.setenv(key, "private-marker")
    child = server_environment(
        "live", tmp_path, None, environment=target.environment, denied_url="http://127.0.0.1:1"
    )
    assert key not in child


@pytest.mark.parametrize("codes", [[], ["C0"], ["C4817; touch marker"], ["$(touch marker)"]])
def test_preparation_accepts_only_harness_concept_arguments(monkeypatch, codes):
    monkeypatch.setenv(ENVIRONMENT, "evs-only")
    target = Target.from_env()
    assert target.prepare_command(["C4817", "C17049"]) == [
        *environments.LAUNCHER,
        "index-sample",
        "C4817",
        "C17049",
    ]
    with pytest.raises(ValueError, match="validated harness concept list"):
        target.prepare_command(codes)


def test_actual_managed_preparation_and_mcp_keep_the_selected_item_origin(
    monkeypatch, tmp_path, tmp_path_factory, selected_policy
):
    upstream, _path = selected_policy
    monkeypatch.setenv("NCI_SI_ACCEPTANCE_PREPARE", "nci-si-mcp index-sample")
    target = Target.from_env()
    suite = runpy.run_path(str(FIXTURES.parent / "tests/conftest.py"))
    prepare = suite["prepared"].__wrapped__
    (tmp_path / "manifest.yaml").write_text(
        yaml.safe_dump({"record": {"concepts": {"full": ["C4817"]}}})
    )
    prepare.__globals__["FIXTURES"] = tmp_path
    with target.managed() as managed:
        data = prepare(managed, None, tmp_path_factory)
        assert data.is_dir()
        prepared = upstream.log()
        assert any(row["params"].get("list") == ["C4817"] for row in prepared), prepared
        with suite["_tools"](managed, None, tmp_path_factory, data) as tools:
            result = tools.call(
                "get_concept", {"terminology": "ncit", "release": "26.09d", "code": "C4817"}
            )
            assert not result.is_error, result.content
            uri = result.content["provenance"]["sourceUri"]
            assert uri == upstream.base_url("evs") + "/api/v1/concept/ncit_26.09d/C4817"
            assert not uri.startswith(managed.denied_url)
        received = upstream.log()
        assert all(row["surface"] == "evs" for row in received)
        assert all(row["fixture"] is not None for row in received)


@pytest.mark.parametrize("variable", ["NCI_SI_CADSR_BASE_URL", "NCI_SI_SSIS_SPARQL_URL"])
@pytest.mark.parametrize("credential", [None, "private-marker"])
def test_prohibited_destination_attempts_reach_no_receiver(monkeypatch, variable, credential):
    with FixtureServer(FixtureSet({}, {})) as forbidden:
        monkeypatch.setenv(ENVIRONMENT, "evs-only")
        monkeypatch.setenv(variable, forbidden.url)
        if credential:
            monkeypatch.setenv("NCI_SI_CADSR_CREDENTIAL", credential)
        with pytest.raises(ValueError) as error:
            Target.from_env()
        assert forbidden.url not in str(error.value)
        assert "private-marker" not in str(error.value)
        assert forbidden.log() == []
