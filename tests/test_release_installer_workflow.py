import importlib.metadata
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

# ruff: noqa: E501 - exact workflow command snapshot intentionally preserves literal lines

ROOT = Path(__file__).parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "local-installer.yml"
RELEASE_WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
LOCAL_RUN_WORKFLOW = ROOT / ".github" / "workflows" / "local-run.yml"
DOCUMENT = ROOT / "docs" / "installer-targets.md"
BASE = "f49b766bf33c67ae42afd9bbda18be8ce4f65b06"
OLD_RELEASE_TAG = "v0.39.2"
STDIO_CAPTURE = (
    'STDIO_CLIENT="$("$INSTALLER_VENV/bin/nci-sip-install" start '
    '--target local --transport stdio --data-dir "$STDIO_ROOT")"'
)
CI_BOOTSTRAP_NOTE = (
    "CI selects the source before installation and checks scheduled releases against protected "
    "`main`. Both CI installer environments bootstrap from that selected full commit "
    "(`INSTALL_REF`), deliberately differing from the public released-tag example above. "
    "Moving a tag after selection cannot select different bootstrap source. The selected tag "
    "still identifies the release and drives the controlled moved-tag negative check."
)
EXPECTED_JOB = yaml.safe_load(r"""
name: local installer (macOS/Linux) (${{ matrix.os }})
needs: prepare
runs-on: ${{ matrix.os }}
timeout-minutes: 20
permissions: {contents: read}
strategy:
  fail-fast: false
  matrix: {os: [macos-latest, ubuntu-24.04]}
env:
  NCI_SIP_CADSR_BASE_URL: http://127.0.0.1:8767/cadsr
  NCI_SIP_CADSR_FTP_URL: http://127.0.0.1:8767/cadsr-ftp
  NCI_SIP_SSIS_SPARQL_URL: http://127.0.0.1:8767/ssis-sparql
  no_proxy: 127.0.0.1,localhost,::1,cadsrapi.cancer.gov,cadsrapi-stage.cancer.gov,cadsr.nci.nih.gov,shared.semantics.cancer.gov
steps:
  - name: Resolve prohibited caDSR and Shared SI hosts to loopback (CI only)
    run: |
      set -euo pipefail
      hosts="cadsrapi.cancer.gov cadsrapi-stage.cancer.gov cadsr.nci.nih.gov shared.semantics.cancer.gov"
      printf '127.0.0.1 %s\n::1 %s\n' "$hosts" "$hosts" | sudo tee -a /etc/hosts
      if [ "$RUNNER_OS" = "macOS" ]; then sudo dscacheutil -flushcache; sudo killall -HUP mDNSResponder; fi
      HOSTS="$hosts" python3 - <<'PY'
      import os, socket
      for host in os.environ["HOSTS"].split():
          addresses = {info[4][0] for info in socket.getaddrinfo(host, 443)}
          assert addresses and addresses <= {"127.0.0.1", "::1"}, (host, addresses)
          print(host, sorted(addresses))
      PY
  - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1
    with: {ref: '${{ needs.prepare.outputs.ref }}', fetch-depth: 0, persist-credentials: false}
  - name: Install Python
    id: python
    uses: actions/setup-python@6d4fd22b20465ccc0f7354476588e8df7c9ca0e0
    with: {python-version: '3.14'}
  - name: Prepare unreleased source (CI only)
    if: github.event_name == 'pull_request' || github.event_name == 'workflow_dispatch'
    env:
      INSTALL_REF: ${{ needs.prepare.outputs.ref }}
      INSTALL_TAG: ${{ needs.prepare.outputs.tag }}
    run: |
      git tag "$INSTALL_TAG" "$INSTALL_REF"
      git config --global --add url."file://$GITHUB_WORKSPACE/".insteadOf https://github.com/CBIIT/nci-si-mcp
      git config --global --add url."file://$GITHUB_WORKSPACE/".insteadOf https://github.com/CBIIT/nci-sip-mcp
  - name: Install the selected installer source
    working-directory: ${{ runner.temp }}
    env:
      INSTALL_REF: ${{ needs.prepare.outputs.ref }}
      INSTALLER_VENV: ${{ runner.temp }}/installer-venv
      PYTHON: ${{ steps.python.outputs.python-path }}
    run: |
      "$PYTHON" -m venv "$INSTALLER_VENV"
      "$INSTALLER_VENV/bin/python" -m pip install "nci-sip-mcp @ git+https://github.com/CBIIT/nci-sip-mcp@${INSTALL_REF}"
  - name: Install the acceptance suite (CI only)
    env: {PYTHON: '${{ steps.python.outputs.python-path }}'}
    run: |
      "$PYTHON" -m venv "$RUNNER_TEMP/acceptance-venv"
      "$RUNNER_TEMP/acceptance-venv/bin/python" -m pip install --editable ./acceptance
  - name: Verify stdio and HTTP installer lifecycles
    working-directory: ${{ runner.temp }}
    env:
      ACCEPTANCE_VENV: ${{ runner.temp }}/acceptance-venv
      INSTALLER_VENV: ${{ runner.temp }}/installer-venv
      STDIO_ROOT: ${{ runner.temp }}/stdio-install
      HTTP_ROOT: ${{ runner.temp }}/http-install
      NEGATIVE_INSTALLER_VENV: ${{ runner.temp }}/negative-installer-venv
      MOVED_TAG_ROOT: ${{ runner.temp }}/moved-tag-install
      TAMPERED_HASH_ROOT: ${{ runner.temp }}/tampered-hash-install
      PYTHON: ${{ steps.python.outputs.python-path }}
      INSTALL_REF: ${{ needs.prepare.outputs.ref }}
      RELEASE_TAG: ${{ needs.prepare.outputs.tag }}
    run: |
      set -euo pipefail
      recorder=""
      original_tag=""
      expected_commit=""
      lock=""
      cleanup() {
        status=$?
        cleanup_status=0
        if [ "$status" -ne 0 ] && [ "$RUNNER_OS" = "macOS" ]; then
          sudo lsof -nP -i :8000 || true
          netstat -anv -p tcp | grep '\.8000 ' || true
        fi
        if [ -f "$HTTP_ROOT/installer-state.json" ]; then
          "$INSTALLER_VENV/bin/nci-sip-install" stop --target local --transport http --data-dir "$HTTP_ROOT" || cleanup_status=$?
          "$INSTALLER_VENV/bin/nci-sip-install" uninstall --target local --transport http --data-dir "$HTTP_ROOT" || cleanup_status=$?
        fi
        if [ -n "$original_tag" ]; then
          git -C "$GITHUB_WORKSPACE" tag -f "$RELEASE_TAG" "$original_tag" || cleanup_status=$?
        fi
        if [ -n "$lock" ] && [ -f "$RUNNER_TEMP/server-requirements.txt" ]; then
          cp "$RUNNER_TEMP/server-requirements.txt" "$lock" || cleanup_status=$?
        fi
        if [ -n "$recorder" ]; then
          kill "$recorder" 2>/dev/null || true
          wait "$recorder" || true
        fi
        if [ "$status" -eq 0 ]; then status=$cleanup_status; fi
        exit "$status"
      }
      trap cleanup EXIT
      mkdir -p "$RUNNER_TEMP/upstream-recorder"
      python3 -m http.server 8767 --bind 127.0.0.1 --directory "$RUNNER_TEMP/upstream-recorder" < /dev/null > "$RUNNER_TEMP/upstream-recorder.out" 2> "$RUNNER_TEMP/upstream-requests.log" &
      recorder=$!
      for attempt in $(seq 1 100); do
        kill -0 "$recorder"
        if (: <> /dev/tcp/127.0.0.1/8767) 2>/dev/null; then break; fi
        if [ "$attempt" -eq 100 ]; then echo "::error::The loopback recorder did not listen."; exit 1; fi
        sleep 0.1
      done
      export STDIO_REQUIRED_TESTS="test_a_resolved_release_is_never_cached test_an_index_mode_without_an_index_is_unavailable"
      export HTTP_REQUIRED_TESTS="test_a_resolved_release_is_never_cached test_a_discovered_release_identifies_the_concept_and_its_provenance"
      mkdir -p "$STDIO_ROOT"
      touch "$STDIO_ROOT/unowned"
      "$INSTALLER_VENV/bin/nci-sip-install" check --target local
      "$INSTALLER_VENV/bin/nci-sip-install" run --target local --transport stdio --data-dir "$STDIO_ROOT" --non-interactive
      "$INSTALLER_VENV/bin/nci-sip-install" status --target local --transport stdio --data-dir "$STDIO_ROOT"
      STDIO_CLIENT="$("$INSTALLER_VENV/bin/nci-sip-install" start --target local --transport stdio --data-dir "$STDIO_ROOT")"
      STDIO_SERVER="$(printf '%s' "$STDIO_CLIENT" | "$ACCEPTANCE_VENV/bin/python" -c 'import json,shlex,sys; r=json.load(sys.stdin); print(shlex.join(["env", *(f"{k}={v}" for k,v in r["env"].items()), r["command"], *r["args"]]))')"
      NCI_SIP_ACCEPTANCE_MODE=fixture NCI_SIP_ACCEPTANCE_SERVER="$STDIO_SERVER" "$ACCEPTANCE_VENV/bin/pytest" "$GITHUB_WORKSPACE/acceptance/tests/test_evs.py::test_a_resolved_release_is_never_cached" "$GITHUB_WORKSPACE/acceptance/tests/test_evs.py::test_an_index_mode_without_an_index_is_unavailable" --report="$RUNNER_TEMP/stdio.json"
      "$INSTALLER_VENV/bin/nci-sip-install" uninstall --target local --transport stdio --data-dir "$STDIO_ROOT"
      test -f "$STDIO_ROOT/unowned"
      test ! -e "$STDIO_ROOT/venv"
      test ! -e "$STDIO_ROOT/installer-state.json"
      mkdir -p "$HTTP_ROOT"
      touch "$HTTP_ROOT/unowned"
      "$INSTALLER_VENV/bin/nci-sip-install" check --target local
      "$INSTALLER_VENV/bin/nci-sip-install" run --target local --transport http --data-dir "$HTTP_ROOT" --non-interactive
      "$INSTALLER_VENV/bin/nci-sip-install" status --target local --transport http --data-dir "$HTTP_ROOT"
      kill "$recorder"
      wait "$recorder" || [ $? -eq 143 ]
      recorder=""
      python3 - "$RUNNER_TEMP/upstream-requests.log" <<'PY'
      import re, sys
      request = re.compile(r'"(?P<method>[A-Z]+) (?P<path>\S+) HTTP/1\.[01]" (?P<status>\d{3}) ')
      notice = re.compile(r"code 404, message File not found$")
      allowed = re.compile(r"/(cadsr|cadsr-ftp|ssis-sparql)(/\S*)?")
      records = []
      for line in open(sys.argv[1], encoding="utf-8").read().splitlines():
          found = request.search(line)
          if found:
              records.append((found["method"], found["path"], found["status"]))
          else:
              assert notice.search(line), line
      assert all(method in ("GET", "HEAD") and allowed.fullmatch(path) and status == "404" for method, path, status in records), records
      assert ("GET", "/cadsr", "404") in records, records
      print("loopback requests:", records)
      PY
      if "$INSTALLER_VENV/bin/nci-sip-install" check --target local > "$RUNNER_TEMP/unreachable.json"; then unreachable=0; else unreachable=$?; fi
      [ "$unreachable" -eq 1 ] || { echo "::error::check against the stopped recorder exited $unreachable, not 1."; exit 1; }
      python3 - "$RUNNER_TEMP/unreachable.json" <<'PY'
      import json, sys
      record = json.load(open(sys.argv[1], encoding="utf-8"))
      assert record == {"target": "local", "components": ["server"], "status": "failed", "results": [{"name": "supported_os", "status": "passed"}, {"name": "git", "status": "passed"}, {"name": "python", "status": "passed"}, {"name": "evs_reachable", "status": "passed"}, {"name": "cadsr_reachable", "status": "failed", "fix": "Restore network access to NCI_SIP_CADSR_BASE_URL."}]}, record
      PY
      export NCI_SIP_UPSTREAM_MODE=fixture
      export NCI_SIP_EVS_BASE_URL=http://127.0.0.1:8766/evs
      export NCI_SIP_EVS_FHIR_BASE_URL=http://127.0.0.1:8766/evs-fhir
      export NCI_SIP_CADSR_BASE_URL=http://127.0.0.1:8766/cadsr
      export NCI_SIP_CADSR_FTP_URL=http://127.0.0.1:8766/cadsr-ftp
      export NCI_SIP_SSIS_SPARQL_URL=http://127.0.0.1:8766/ssis-sparql
      "$INSTALLER_VENV/bin/nci-sip-install" start --target local --transport http --data-dir "$HTTP_ROOT"
      NCI_SIP_ACCEPTANCE_URL=http://127.0.0.1:8000/mcp NCI_SIP_ACCEPTANCE_FIXTURE_BIND=127.0.0.1:8766 NCI_SIP_ACCEPTANCE_FIXTURE_URL=http://127.0.0.1:8766 "$ACCEPTANCE_VENV/bin/pytest" "$GITHUB_WORKSPACE/acceptance/tests/test_evs.py::test_a_resolved_release_is_never_cached" "$GITHUB_WORKSPACE/acceptance/tests/test_evs.py::test_a_discovered_release_identifies_the_concept_and_its_provenance" --report="$RUNNER_TEMP/http.json"
      "$INSTALLER_VENV/bin/nci-sip-install" stop --target local --transport http --data-dir "$HTTP_ROOT"
      "$INSTALLER_VENV/bin/nci-sip-install" uninstall --target local --transport http --data-dir "$HTTP_ROOT"
      test -f "$HTTP_ROOT/unowned"
      test ! -e "$HTTP_ROOT/venv"
      test ! -e "$HTTP_ROOT/installer-state.json"
      "$ACCEPTANCE_VENV/bin/python" - <<'PY'
      import json, os
      from pathlib import Path
      expected = json.loads(Path(os.environ["GITHUB_WORKSPACE"] + "/acceptance/expected/fixture.json").read_text())
      for name in ("stdio", "http"):
          names = os.environ[f"{name.upper()}_REQUIRED_TESTS"].split()
          required = {key for key in expected if any(test in key for test in names)}
          report = json.loads(Path(os.environ["RUNNER_TEMP"] + f"/{name}.json").read_text())
          actual = {key: value["outcome"] for key, value in report["tests"].items()}
          assert set(actual) == required, (name, set(actual), required)
          assert set(actual.values()) == {"passed"}, (name, actual)
          assert actual == {key: expected[key] for key in actual}, (name, actual)
      PY
      export NCI_SIP_UPSTREAM_MODE=fixture
      export NCI_SIP_EVS_BASE_URL=http://127.0.0.1:8767/evs
      export NCI_SIP_EVS_FHIR_BASE_URL=http://127.0.0.1:8767/evs-fhir
      export NCI_SIP_CADSR_BASE_URL=http://127.0.0.1:8767/cadsr
      export NCI_SIP_CADSR_FTP_URL=http://127.0.0.1:8767/cadsr-ftp
      export NCI_SIP_SSIS_SPARQL_URL=http://127.0.0.1:8767/ssis-sparql
      python3 -m http.server 8767 --bind 127.0.0.1 --directory "$RUNNER_TEMP/upstream-recorder" < /dev/null > "$RUNNER_TEMP/negative-upstream.out" 2> "$RUNNER_TEMP/negative-upstream.log" &
      recorder=$!
      for attempt in $(seq 1 100); do
        kill -0 "$recorder"
        if (: <> /dev/tcp/127.0.0.1/8767) 2>/dev/null; then break; fi
        if [ "$attempt" -eq 100 ]; then echo "::error::The negative-scenario recorder did not listen."; exit 1; fi
        sleep 0.1
      done
      "$PYTHON" -m venv "$NEGATIVE_INSTALLER_VENV"
      "$NEGATIVE_INSTALLER_VENV/bin/python" -m pip install "nci-sip-mcp @ git+https://github.com/CBIIT/nci-sip-mcp@${INSTALL_REF}"
      git config --file "$RUNNER_TEMP/moved-tag.gitconfig" --add url."file://$GITHUB_WORKSPACE/".insteadOf https://github.com/CBIIT/nci-si-mcp
      git config --file "$RUNNER_TEMP/moved-tag.gitconfig" --add url."file://$GITHUB_WORKSPACE/".insteadOf https://github.com/CBIIT/nci-sip-mcp
      original_tag="$(git -C "$GITHUB_WORKSPACE" rev-parse "$RELEASE_TAG^{commit}")"
      expected_commit="$original_tag"
      git -C "$GITHUB_WORKSPACE" tag -f "$RELEASE_TAG" "$original_tag^"
      if GIT_CONFIG_GLOBAL="$RUNNER_TEMP/moved-tag.gitconfig" "$NEGATIVE_INSTALLER_VENV/bin/nci-sip-install" run --target local --transport stdio --data-dir "$MOVED_TAG_ROOT" --non-interactive > "$RUNNER_TEMP/moved-tag.json" 2> "$RUNNER_TEMP/moved-tag.err"; then moved_status=0; else moved_status=$?; fi
      git -C "$GITHUB_WORKSPACE" tag -f "$RELEASE_TAG" "$original_tag"
      original_tag=""
      [ "$moved_status" -eq 1 ] || { echo "::error::moved-tag run exited $moved_status, not 1."; exit 1; }
      python3 - "$RUNNER_TEMP/moved-tag.json" "$RUNNER_TEMP/moved-tag.err" "$expected_commit" <<'PY'
      import json, re, sys
      record = json.load(open(sys.argv[1], encoding="utf-8"))
      error = open(sys.argv[2], encoding="utf-8").read()
      assert record["completed"] == ["check_prerequisites"], record
      assert record["failed"] == ["install"], record
      assert re.fullmatch(r"[0-9a-f]{40}", sys.argv[3]), sys.argv[3]
      assert "release tag may have moved" in error and sys.argv[3] in error, error
      PY
      lock="$NEGATIVE_INSTALLER_VENV/lib/python3.14/site-packages/nci_sip_mcp/server-requirements.txt"
      cp "$lock" "$RUNNER_TEMP/server-requirements.txt"
      python3 - "$lock" <<'PY'
      import re, sys
      path = sys.argv[1]
      text = open(path, encoding="utf-8").read()
      text = re.sub(r"--hash=sha256:[0-9a-f]{64}", "--hash=sha256:" + "0" * 64, text)
      open(path, "w", encoding="utf-8").write(text)
      PY
      if "$NEGATIVE_INSTALLER_VENV/bin/nci-sip-install" run --target local --transport stdio --data-dir "$TAMPERED_HASH_ROOT" --non-interactive > "$RUNNER_TEMP/tampered-hash.json"; then hash_status=0; else hash_status=$?; fi
      cp "$RUNNER_TEMP/server-requirements.txt" "$lock"
      lock=""
      [ "$hash_status" -eq 1 ] || { echo "::error::tampered-hash run exited $hash_status, not 1."; exit 1; }
      python3 - "$RUNNER_TEMP/tampered-hash.json" "$TAMPERED_HASH_ROOT/venv/bin/python" <<'PY'
      import json, subprocess, sys
      record = json.load(open(sys.argv[1], encoding="utf-8"))
      assert record["completed"] == ["check_prerequisites"], record
      assert record["failed"] == ["install"], record
      packages = subprocess.check_output([sys.argv[2], "-m", "pip", "list", "--format=json"], text=True)
      assert [item["name"].lower() for item in json.loads(packages)] == ["pip"], packages
      PY
      kill "$recorder"
      wait "$recorder" || [ $? -eq 143 ]
      recorder=""
""")

EXPECTED_TRIGGERS = yaml.safe_load(r"""
workflow_dispatch:
workflow_call:
  inputs:
    ref: {required: true, type: string}
    tag: {required: true, type: string}
pull_request:
  paths:
    - src/**
    - scripts/**
    - README.md
    - LICENSE
    - docs/installer-targets.md
    - .github/actions/**
    - tests/test_installer*.py
    - tests/test_release_installer_workflow.py
    - acceptance/**
    - pyproject.toml
    - pdm.lock
    - .github/workflows/local-installer.yml
    - .github/workflows/release.yml
schedule:
  - cron: '23 7 * * 1'
""")
EXPECTED_PREPARE = yaml.safe_load(r"""
name: select source
runs-on: ubuntu-24.04
timeout-minutes: 5
outputs:
  ref: ${{ steps.source.outputs.ref }}
  tag: ${{ steps.source.outputs.tag }}
steps:
  - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1
    with: {fetch-depth: 0, persist-credentials: false}
  - name: Select exact source
    id: source
    env:
      EVENT_NAME: ${{ github.event_name }}
      INPUT_REF: ${{ inputs.ref }}
      INPUT_TAG: ${{ inputs.tag }}
      PR_REF: ${{ github.event.pull_request.head.sha }}
      DISPATCH_REF: ${{ github.sha }}
      GH_TOKEN: ${{ github.token }}
    run: |
      set -euo pipefail
      if [ -n "$INPUT_REF" ]; then
        tag="$INPUT_TAG"
        if [[ ! "$tag" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then exit 1; fi
        ref="$(git rev-parse "$INPUT_REF^{commit}")"
        test "$(git rev-parse "$tag^{commit}")" = "$ref"
      elif [ "$EVENT_NAME" = "pull_request" ] || [ "$EVENT_NAME" = "workflow_dispatch" ]; then
        ref="${PR_REF:-$DISPATCH_REF}"
        tag="v0.0.0"
      else
        tag="$(gh release view --json tagName --jq .tagName)"
        if [[ ! "$tag" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then exit 1; fi
        ref="$(git rev-parse --verify "refs/tags/$tag^{commit}")"
        git fetch --no-tags origin refs/heads/main:refs/remotes/origin/main
        git merge-base --is-ancestor "$ref" refs/remotes/origin/main
      fi
      if [ "$EVENT_NAME" = "schedule" ] || [ -n "$INPUT_REF" ]; then
        distribution="$(git show "$ref:pyproject.toml" | python3 -c 'import sys, tomllib; print(tomllib.loads(sys.stdin.read())["project"]["name"])')"
        if [ "$distribution" != "nci-sip-mcp" ]; then
          echo "::error::Selected release $tag provides $distribution; no compatible NCI SIP release is available, so released SIP verification was not performed."
          exit 1
        fi
      fi
      echo "ref=$ref" >> "$GITHUB_OUTPUT"
      echo "tag=$tag" >> "$GITHUB_OUTPUT"
""")


def workflow():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def job():
    return workflow()["jobs"]["local-installer"]


def named_steps():
    return [step for step in job()["steps"] if "name" in step]


def documented_procedures():
    text = DOCUMENT.read_text(encoding="utf-8")
    section = text.split("\n## Verify a released local installation\n", 1)[1]
    section = section.split("\n## ", 1)[0]
    blocks = re.findall(r"^```(?:bash|sh|shell)\n(.*?)^```", section, re.DOTALL | re.MULTILINE)
    return [block.splitlines() for block in blocks]


def workflow_operator_lines(run):
    lines = [line.strip() for line in run.splitlines()]
    selected = [
        line
        for line in lines
        if line.startswith('"$INSTALLER_VENV/bin/nci-sip-install"') or line == STDIO_CAPTURE
    ]
    return [line for line in selected if "|| cleanup_status=$?" not in line]


def moved_tag_verdict():
    run = next(
        step["run"]
        for step in named_steps()
        if step["name"] == "Verify stdio and HTTP installer lifecycles"
    )
    command = 'python3 - "$RUNNER_TEMP/moved-tag.json"'
    command_start = run.index(command)
    start = run.index("<<'PY'\n", command_start) + len("<<'PY'\n")
    return run[start:].split("\nPY\n", 1)[0]


def local_git(repository, *arguments):
    result = subprocess.run(  # noqa: S603 - fixed Git commands in a temporary local clone
        ["git", "-C", str(repository), *arguments],  # noqa: S607
        capture_output=True,
        check=True,
        text=True,
    )
    return result.stdout.strip()


def clone_release_fixture(parent):
    repository = parent / "release-era-repo"
    subprocess.run(  # noqa: S603 - local clone of this repository into a temporary directory
        ["git", "clone", "--quiet", "--shared", str(ROOT), str(repository)],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    )
    local_git(repository, "update-ref", "refs/heads/main", BASE)
    local_git(repository, "remote", "set-url", "origin", str(repository))
    return repository


def run_local_run_source_selection(repository, scratch, event):
    workflow = yaml.safe_load(LOCAL_RUN_WORKFLOW.read_text(encoding="utf-8"))
    step = next(
        item
        for item in workflow["jobs"]["local-run"]["steps"]
        if item.get("name") == "Select release or candidate index source (CI only)"
    )
    environment = os.environ | {
        "EVENT_NAME": event,
        "GITHUB_WORKSPACE": str(repository),
        "GITHUB_ENV": str(scratch / f"local-run-{event}.env"),
        "GIT_CONFIG_GLOBAL": str(scratch / "local-run.gitconfig"),
        "HOME": str(scratch),
        "PATH": os.environ["PATH"],
    }
    result = subprocess.run(  # noqa: S603 - runs a repository-owned workflow step in a temp clone
        ["bash", "-e", "-o", "pipefail", "-c", step["run"]],  # noqa: S607
        cwd=repository,
        env=environment,
        capture_output=True,
        check=False,
        text=True,
    )
    return result


def run_installer_source_selection(
    repository, scratch, event, release_tag=None, input_ref="", input_tag=""
):
    workflow_data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    step = next(
        item
        for item in workflow_data["jobs"]["prepare"]["steps"]
        if item.get("name") == "Select exact source"
    )
    binary = scratch / "bin"
    binary.mkdir(exist_ok=True)
    environment = os.environ | {
        "DISPATCH_REF": local_git(repository, "rev-parse", "HEAD"),
        "EVENT_NAME": event,
        "GH_TOKEN": "fixture",
        "GITHUB_OUTPUT": str(scratch / f"installer-{event}.output"),
        "GIT_CONFIG_GLOBAL": str(scratch / "installer.gitconfig"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_ALLOW_PROTOCOL": "file",
        "HOME": str(scratch),
        "INPUT_REF": input_ref,
        "INPUT_TAG": input_tag,
        "PATH": os.environ["PATH"],
        "PR_REF": "",
    }
    if release_tag is not None:
        gh = binary / "gh"
        gh.write_text(
            "#!/bin/sh\n"
            '[ "$*" = "release view --json tagName --jq .tagName" ] || exit 2\n'
            'printf "%s\\n" "$RELEASE_TAG_FIXTURE"\n',
            encoding="utf-8",
        )
        gh.chmod(0o755)
        environment["PATH"] = str(binary) + os.pathsep + environment["PATH"]
        environment["RELEASE_TAG_FIXTURE"] = release_tag
    result = subprocess.run(  # noqa: S603 - runs a repository-owned workflow step in a temp clone
        ["bash", "-e", "-o", "pipefail", "-c", step["run"]],  # noqa: S607
        cwd=repository,
        env=environment,
        capture_output=True,
        check=False,
        text=True,
    )
    return result, Path(environment["GITHUB_OUTPUT"])


def install_local_git_requirement(repository, ref, distribution, target):
    environment = os.environ | {
        "GIT_ALLOW_PROTOCOL": "file",
        "GIT_CONFIG_GLOBAL": str(target.parent / "gitconfig"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "HOME": str(target.parent),
        "PIP_CACHE_DIR": str(target.parent / "pip-cache"),
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PIP_NO_INDEX": "1",
    }
    requirement = f"{distribution} @ git+{repository.as_uri()}@{ref}"
    return subprocess.run(  # noqa: S603 - installs this repository's local Git source only
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-deps",
            "--no-build-isolation",
            "--target",
            str(target),
            requirement,
        ],
        check=False,
        capture_output=True,
        env=environment,
        text=True,
    )


def installed_distribution_and_help(target, command):
    distributions = {
        item.metadata["Name"] for item in importlib.metadata.distributions(path=[str(target)])
    }
    result = subprocess.run(  # noqa: S603 - executes the locally installed release command
        [str(target / "bin" / command), "--help"],
        env=os.environ | {"PYTHONPATH": str(target)},
        capture_output=True,
        check=False,
        text=True,
    )
    return distributions, result


class ReleaseInstallerWorkflowTest(unittest.TestCase):
    def test_scheduled_release_preflight_rejects_old_distribution_and_installs_both_eras(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = clone_release_fixture(root)
            candidate_sha = local_git(repository, "rev-parse", "HEAD")
            local_git(repository, "checkout", "--quiet", "--detach", OLD_RELEASE_TAG)
            old_run = run_local_run_source_selection(repository, root, "schedule")
            old_prepare, old_prepare_output = run_installer_source_selection(
                repository, root, "schedule", OLD_RELEASE_TAG
            )
            old_install = install_local_git_requirement(
                repository, OLD_RELEASE_TAG, "nci-si-mcp", root / "old-installed"
            )
            self.assertEqual(old_install.returncode, 0, old_install.stderr)
            old_distributions, old_help = installed_distribution_and_help(
                root / "old-installed", "nci-si-install"
            )
            old_prepare_output_contents = (
                old_prepare_output.read_text(encoding="utf-8")
                if old_prepare_output.exists()
                else None
            )

            local_git(repository, "checkout", "--quiet", "--detach", candidate_sha)
            candidate_run = run_local_run_source_selection(repository, root, "workflow_dispatch")
            candidate_prepare, candidate_output = run_installer_source_selection(
                repository,
                root,
                "workflow_call",
                input_ref=candidate_sha,
                input_tag="v0.0.0",
            )
            candidate_install = install_local_git_requirement(
                repository, "v0.0.0", "nci-sip-mcp", root / "candidate-installed"
            )
            self.assertEqual(candidate_install.returncode, 0, candidate_install.stderr)
            candidate_distributions, candidate_help = installed_distribution_and_help(
                root / "candidate-installed", "nci-sip-install"
            )
            candidate_output_contents = candidate_output.read_text(encoding="utf-8")

        old_diagnostic = old_run.stdout + old_run.stderr + old_prepare.stdout + old_prepare.stderr
        self.assertEqual(old_run.returncode, 1, old_diagnostic)
        self.assertEqual(old_prepare.returncode, 1, old_diagnostic)
        self.assertIn(f"{OLD_RELEASE_TAG} provides nci-si-mcp", old_diagnostic)
        self.assertIn("released SIP verification was not performed", old_diagnostic)
        self.assertIsNone(old_prepare_output_contents)
        self.assertEqual(old_distributions, {"nci-si-mcp"})
        self.assertEqual(old_help.returncode, 0, old_help.stderr)
        self.assertEqual(candidate_run.returncode, 0, candidate_run.stderr)
        self.assertEqual(candidate_prepare.returncode, 0, candidate_prepare.stderr)
        self.assertIn(f"ref={candidate_sha}\ntag=v0.0.0\n", candidate_output_contents)
        self.assertEqual(candidate_distributions, {"nci-sip-mcp"})
        self.assertEqual(candidate_help.returncode, 0, candidate_help.stderr)

    def test_both_origin_rewrites_resolve_the_selected_local_commit(self):
        local_run = yaml.safe_load((WORKFLOW.parent / "local-run.yml").read_text())
        scripts = [step["run"] for step in named_steps() if "run" in step]
        scripts += [step.get("run", "") for step in local_run["jobs"]["local-run"]["steps"]]
        mappings = [script for script in scripts if "insteadOf" in script]
        self.assertEqual(len(mappings), 3)
        for script in mappings:
            with self.subTest(script=script), tempfile.TemporaryDirectory() as directory:
                self.check_origin_rewrites(Path(directory), script)

    def check_origin_rewrites(self, root, script):
        repository = root / "repo"
        repository.mkdir()
        environment = os.environ | {
            "GITHUB_WORKSPACE": str(repository),
            "RUNNER_TEMP": str(root),
            "GIT_CONFIG_GLOBAL": str(root / "global.gitconfig"),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_ALLOW_PROTOCOL": "file",
        }

        def git(*arguments):
            return subprocess.check_output(  # noqa: S603 - isolated local Git fixture
                ["git", "-C", str(repository), *arguments],  # noqa: S607
                env=environment,
                text=True,
            ).strip()

        git("init", "--quiet")
        git(
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "--quiet",
            "--allow-empty",
            "-m",
            "selected",
        )
        commands = "\n".join(line for line in script.splitlines() if "insteadOf" in line)
        subprocess.run(  # noqa: S603 - only workflow Git config lines, isolated fixture paths
            ["bash", "-euc", commands],  # noqa: S607 - Bash interprets the workflow fixture
            env=environment,
            check=True,
        )
        config = root / ("moved-tag.gitconfig" if "--file" in commands else "global.gitconfig")
        values = git(
            "config", "--file", str(config), "--get-all", f"url.file://{repository}/.insteadOf"
        )
        self.assertEqual(
            values.splitlines(),
            ["https://github.com/CBIIT/nci-si-mcp", "https://github.com/CBIIT/nci-sip-mcp"],
        )
        environment["GIT_CONFIG_GLOBAL"] = str(config)
        expected = git("rev-parse", "HEAD") + "\tHEAD"
        for origin in values.splitlines():
            self.assertEqual(git("ls-remote", origin, "HEAD"), expected)

    def test_the_whole_release_installer_job_is_exact(self):
        self.assertEqual(job(), EXPECTED_JOB)

    def test_source_selection_is_exact_for_call_pr_and_schedule(self):
        self.assertEqual(workflow()["jobs"]["prepare"], EXPECTED_PREPARE)

    def test_workflow_has_exact_entry_paths_concurrency_and_permissions(self):
        value = workflow()
        self.assertEqual(value[True], EXPECTED_TRIGGERS)
        self.assertEqual(value["permissions"], {"contents": "read"})
        self.assertEqual(
            value["concurrency"],
            {
                "group": "local-installer-${{ inputs.ref || github.event.pull_request.head.sha || github.ref }}",
                "cancel-in-progress": True,
            },
        )

    def test_release_workflow_calls_the_reusable_workflow_without_a_pr_trigger(self):
        release = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
        self.assertNotIn("pull_request", release[True])
        self.assertEqual(
            release["jobs"]["local-installer"],
            {
                "name": "local installer (macOS/Linux)",
                "needs": "release",
                "if": "needs.release.outputs.image_tag != ''",
                "permissions": {"contents": "read"},
                "uses": "$/.github/workflows/local-installer.yml",
                "with": {
                    "ref": "${{ needs.release.outputs.image_sha }}",
                    "tag": "${{ needs.release.outputs.image_tag }}",
                },
            },
        )

    def test_installer_commands_run_outside_checkout_with_the_release_executable(self):
        steps = {step["name"]: step for step in named_steps()}
        install = steps["Install the selected installer source"]
        self.assertEqual(install["working-directory"], "${{ runner.temp }}")
        procedures = documented_procedures()
        self.assertEqual(len(procedures), 3)
        public = procedures[0]
        self.assertEqual(
            public,
            [
                '"$PYTHON" -m venv "$INSTALLER_VENV"',
                '"$INSTALLER_VENV/bin/python" -m pip install "nci-sip-mcp @ '
                'git+https://github.com/CBIIT/nci-sip-mcp@${RELEASE_TAG}"',
                '"$INSTALLER_VENV/bin/nci-sip-install" check --target local',
                '"$INSTALLER_VENV/bin/nci-sip-install" run --target local --transport stdio --data-dir "$STDIO_ROOT" --non-interactive',
                '"$INSTALLER_VENV/bin/nci-sip-install" status --target local --transport stdio --data-dir "$STDIO_ROOT"',
                '"$INSTALLER_VENV/bin/nci-sip-install" start --target local --transport stdio --data-dir "$STDIO_ROOT"',
                '"$INSTALLER_VENV/bin/nci-sip-install" uninstall --target local --transport stdio --data-dir "$STDIO_ROOT"',
                '"$INSTALLER_VENV/bin/nci-sip-install" check --target local',
                '"$INSTALLER_VENV/bin/nci-sip-install" run --target local --transport http --data-dir "$HTTP_ROOT" --non-interactive',
                '"$INSTALLER_VENV/bin/nci-sip-install" status --target local --transport http --data-dir "$HTTP_ROOT"',
                '"$INSTALLER_VENV/bin/nci-sip-install" start --target local --transport http --data-dir "$HTTP_ROOT"',
                '"$INSTALLER_VENV/bin/nci-sip-install" stop --target local --transport http --data-dir "$HTTP_ROOT"',
                '"$INSTALLER_VENV/bin/nci-sip-install" uninstall --target local --transport http --data-dir "$HTTP_ROOT"',
            ],
        )
        self.assertEqual(
            procedures[1],
            [
                "PRE_SIP_TAG=v0.39.2",
                '"$PYTHON" -m venv "$INSTALLER_VENV"',
                '"$INSTALLER_VENV/bin/python" -m pip install "nci-si-mcp @ '
                'git+https://github.com/CBIIT/nci-sip-mcp@${PRE_SIP_TAG}"',
                '"$INSTALLER_VENV/bin/nci-si-install" check --target local',
            ],
        )
        self.assertEqual(
            install["run"].splitlines(),
            [public[0], public[1].replace("${RELEASE_TAG}", "${INSTALL_REF}")],
        )
        self.assertIn(CI_BOOTSTRAP_NOTE, DOCUMENT.read_text(encoding="utf-8"))
        lifecycle = steps["Verify stdio and HTTP installer lifecycles"]
        self.assertEqual(lifecycle["working-directory"], "${{ runner.temp }}")
        stdio_start = (
            '"$INSTALLER_VENV/bin/nci-sip-install" start --target local --transport stdio '
            '--data-dir "$STDIO_ROOT"'
        )
        expected = [STDIO_CAPTURE if line == stdio_start else line for line in public[2:]]
        self.assertEqual(
            workflow_operator_lines(lifecycle["run"]),
            expected,
        )

    def test_legacy_installer_keeps_its_separate_complete_old_origin_procedure(self):
        procedures = documented_procedures()
        self.assertEqual(len(procedures), 3)
        self.assertEqual(
            procedures[2],
            [
                '"$PYTHON" -m venv "$INSTALLER_VENV"',
                '"$INSTALLER_VENV/bin/python" -m pip install "nci-si-mcp @ '
                'git+https://github.com/CBIIT/nci-si-mcp@${LEGACY_RELEASE_TAG}"',
            ],
        )

    def test_moved_tag_verdict_requires_the_full_expected_commit_in_the_diagnostic(self):
        commit = "a" * 40
        record = {"completed": ["check_prerequisites"], "failed": ["install"]}
        with tempfile.TemporaryDirectory() as directory:
            status = Path(directory) / "status.json"
            error = Path(directory) / "error.txt"
            status.write_text(json.dumps(record), encoding="utf-8")
            command = [sys.executable, "-c", moved_tag_verdict(), str(status), str(error), commit]
            error.write_text(f"{commit}; the release tag may have moved", encoding="utf-8")
            accepted = subprocess.run(  # noqa: S603 - test interpreter and temporary paths
                command, check=False
            ).returncode
            error.write_text("the release tag may have moved", encoding="utf-8")
            rejected = subprocess.run(  # noqa: S603 - test interpreter and temporary paths
                command, check=False
            ).returncode

        self.assertEqual((accepted, rejected), (0, 1))
