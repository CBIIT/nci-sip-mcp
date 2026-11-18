"""The Local run workflow walks the documented setup as written, on both platforms."""

import os
import re
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import yaml

ROOT = Path(__file__).parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"
LOCAL_RUN = WORKFLOWS / "local-run.yml"
LOCAL_GUIDE = ROOT / "docs" / "local-guide.md"

MACOS = "runner.os == 'macOS'"
LINUX = "runner.os == 'Linux'"
PDM_ROWS = "matrix.server || matrix.host_docs"

# Every step in order: its name and its condition.
STEPS = [
    ("Resolve prohibited caDSR and Shared SI hosts to loopback (CI only)", None),
    (
        "Require container contexts for Compose rows (CI only)",
        "(matrix.compose_docs || matrix.compose_companion) && "
        "needs.prepare-containers.result != 'success'",
    ),
    ("Remove the runner's python.org Python (macOS)", f"({PDM_ROWS}) && {MACOS}"),
    ("Remove preinstalled Python and PDM (macOS)", f"({PDM_ROWS}) && {MACOS}"),
    ("Install Python and PDM (macOS)", f"({PDM_ROWS}) && {MACOS}"),
    ("Install uv (Linux)", f"({PDM_ROWS}) && {LINUX}"),
    ("Open a new shell (Linux)", f"({PDM_ROWS}) && {LINUX}"),
    ("Install Python and PDM (Linux)", f"({PDM_ROWS}) && {LINUX}"),
    ("Install the project", PDM_ROWS),
    ("Check that the documented Python and PDM are in use (macOS)", f"({PDM_ROWS}) && {MACOS}"),
    ("Check that the documented Python and PDM are in use (Linux)", f"({PDM_ROWS}) && {LINUX}"),
    ("Print the EVS release", "matrix.combination == 'server'"),
    ("Select the released index source (CI only)", "matrix.server || matrix.index"),
    ("Install released server and optional index environments", "matrix.server || matrix.index"),
    ("Start the loopback caDSR and Shared SI recorder (CI only)", "matrix.server"),
    ("Verify the local server lifecycle", "matrix.server"),
    (
        "Stop the loopback recorder and check its requests (CI only)",
        "always() && matrix.server && steps.recorder.outcome != 'skipped'",
    ),
    ("Build and evaluate a small local index", "matrix.index"),
    ("Write the readiness probe (CI only)", None),
    ("Serve over stdio and initialize (CI only)", "matrix.server"),
    ("Verify required-index HTTP and indexed search (CI only)", "matrix.index"),
    ("Verify server-only search without an index (CI only)", "matrix.index"),
    ("Run the acceptance suite against the fixtures", "matrix.combination == 'server'"),
    ("Compare the outcomes with the expected ones", "matrix.combination == 'server'"),
    ("Remove a preinstalled Node.js (macOS)", "matrix.host_docs && runner.os == 'macOS'"),
    ("Install Node.js (macOS)", "matrix.host_docs && runner.os == 'macOS'"),
    (
        "Install nvm and Node.js (Linux)",
        "matrix.host_docs && " + LINUX,
    ),
    (
        "Check that the documented Node.js is in use",
        "matrix.host_docs",
    ),
    (
        "Build the documentation site",
        "matrix.host_docs",
    ),
    ("Serve host documentation until ready (CI only)", "matrix.host_docs"),
    ("Serve the validation dashboard until ready (CI only)", "matrix.host_companion"),
    (
        "Download selected local container contexts (CI only)",
        "matrix.compose_docs || matrix.compose_companion",
    ),
    (
        "Build selected local container images",
        "matrix.compose_docs || matrix.compose_companion",
    ),
    (
        "Verify the selected Compose profiles (CI only)",
        "matrix.compose_docs || matrix.compose_companion",
    ),
    (
        "Verify every selected component together (CI only)",
        "matrix.index && (matrix.host_docs || matrix.compose_docs || matrix.compose_companion)",
    ),
]

# The document that tells a developer to run each step's commands.
DOCUMENTS = {
    "Install Python and PDM (macOS)": "QUICKSTART.md",
    "Install uv (Linux)": "QUICKSTART.md",
    "Install Python and PDM (Linux)": "QUICKSTART.md",
    "Install the project": "QUICKSTART.md",
    "Print the EVS release": "QUICKSTART.md",
    "Install released server and optional index environments": "docs/local-guide.md",
    "Verify the local server lifecycle": "docs/local-guide.md",
    "Build and evaluate a small local index": "docs/local-guide.md",
    "Run the acceptance suite against the fixtures": "AGENTS.md",
    "Compare the outcomes with the expected ones": "acceptance/README.md",
    "Install Node.js (macOS)": "QUICKSTART.md",
    "Install nvm and Node.js (Linux)": "QUICKSTART.md",
    "Build the documentation site": "docs/local-guide.md",
    "Build selected local container images": "docs/local-guide.md",
}

CI_INDEX_INSTALL = (
    '"$INDEX_VENV/bin/python" -m pip install "nci-sip-mcp[server,index] @ '
    'git+https://github.com/CBIIT/nci-sip-mcp@${RELEASE_TAG}"'
)
CI_SERVER_INSTALL = (
    '"$SERVER_VENV/bin/python" -m pip install "nci-sip-mcp[server] @ '
    'git+https://github.com/CBIIT/nci-sip-mcp@${RELEASE_TAG}"'
)

FIXTURE_RUN = """# shellcheck disable=SC2016 # the single quotes are the documented form
NCI_SIP_ACCEPTANCE_REGISTRY_RELEASE=required NCI_SIP_ACCEPTANCE_SECURITY_SERVER='python ../scripts/permissions_fixture.py' NCI_SIP_ACCEPTANCE_PREPARE='nci-sip-mcp index-sample $(cat "$NCI_SIP_ACCEPTANCE_INDEX_CODES")' pdm run acceptance -n 4 --dist worksteal --report=fixture.json
status=$?
[ "$status" -le 1 ] && [ -s acceptance/fixture.json ] || { echo "::error::The fixture run did not finish (exit status $status)."; exit 1; }
"""  # noqa: E501

# What exists only in CI, by step name (the whole step) or by exact line, and why.
CI_ONLY = {
    "Resolve prohibited caDSR and Shared SI hosts to loopback (CI only)": "defense in depth: "
    "the listed caDSR and Shared SI names resolve to loopback for resolvers that read "
    "/etc/hosts; not an egress guarantee",
    "Start the loopback caDSR and Shared SI recorder (CI only)": "the installer's caDSR "
    "reachability check probes this recorder instead of production caDSR",
    "Stop the loopback recorder and check its requests (CI only)": "stops the owned recorder "
    "and proves the check probed it, with only caDSR and Shared SI paths",
    "Remove the runner's python.org Python (macOS)": "the runner image ships python.org builds "
    "in /Library/Frameworks that a clean Mac lacks, and PDM picks them over Homebrew's",
    "Remove preinstalled Python and PDM (macOS)": "brew install exits 0 without installing a "
    "formula the runner image ships, so the documented install must really run",
    "Remove a preinstalled Node.js (macOS)": "the same, for Node.js",
    "Open a new shell (Linux)": "the documents say 'in a new shell'; the next step sees the path",
    "Check that the documented Python and PDM are in use (macOS)": "fails the job when a copy "
    "that the runner image ships takes over from the Homebrew install",
    "Check that the documented Python and PDM are in use (Linux)": "fails the job when a copy "
    "that the runner image ships takes over from the uv install",
    "Write the readiness probe (CI only)": "the helper that starts a documented server, waits "
    "for its readiness endpoint and stops it",
    "Select the released index source (CI only)": "unreleased PR, push and dispatch candidates "
    "use this checkout; scheduled runs select the latest published release",
    "Serve over stdio and initialize (CI only)": "an initialize round-trip that no one types",
    "Verify required-index HTTP and indexed search (CI only)": "starts the documented command "
    "under a readiness probe, then calls MCP and asserts the sampled concept and index provenance",
    "Verify server-only search without an index (CI only)": "calls a separate installed server "
    "through MCP and asserts the no-index error contract",
    "Verify the selected Compose profiles (CI only)": "selects each row's exact profile set, "
    "waits for real health endpoints and removes only selected services",
    "Verify every selected component together (CI only)": "starts the exact process and port set "
    "of each everything row, probes every socket and removes only owned processes and services",
    "Require container contexts for Compose rows (CI only)": "fails only rows that selected "
    "Compose when their shared artifact producer failed; unrelated rows still run",
    "Check that the documented Node.js is in use": "fails the job when a Node.js that the "
    "runner image ships takes over from the documented install",
    "Serve the validation dashboard until ready (CI only)": "the documented portal serve "
    "command, run by the probe, for the same reason",
    "Serve host documentation until ready (CI only)": "the documented static-server command, "
    "run by the probe so its foreground process can be stopped",
    '. "$HOME/.nvm/nvm.sh"': "the documents say 'in a new shell', where nvm is loaded",
    'dirname "$(command -v node)" >> "$GITHUB_PATH"': "puts the nvm Node.js on the "
    "path of the job's next steps, as a new shell would",
    'INDEX_VENV="$RUNNER_TEMP/index-venv"': "names the isolated release-style index environment",
    'SERVER_VENV="$RUNNER_TEMP/server-venv"': "names the isolated server-only control environment",
    'python3.14 -m venv "$INDEX_VENV"': "creates the release-style index environment",
    'python3.14 -m venv "$SERVER_VENV"': "creates the server-only control environment",
    CI_INDEX_INSTALL: "substitutes the selected concrete release tag or pull-request checkout for "
    "the document's vX.Y.Z placeholder",
    CI_SERVER_INSTALL: "installs the server-only control without the index extra",
    'echo "INDEX_VENV=$INDEX_VENV" >> "$GITHUB_ENV"': "passes the index environment to later steps",
    'echo "SERVER_VENV=$SERVER_VENV" >> "$GITHUB_ENV"': "passes the control environment to "
    "later steps",
    'echo "INDEX_DATA=$RUNNER_TEMP/index-data" >> "$GITHUB_ENV"': "passes the isolated index "
    "data directory to later steps",
    'if [ "$WITH_INDEX" = "true" ]; then': "installs the optional index only in index rows",
    'if [ "$COMPOSE_COMPANION" = "true" ]; then': "builds the admin image only for "
    "rows that select the companion",
    "fi": "closes a matrix-selected workflow branch",
    "set -euo pipefail": "makes a multi-command CI verification fail at its first failed command",
    'STDIO_ROOT="$RUNNER_TEMP/guide-stdio"': "selects an isolated CI installer data directory",
    'HTTP_ROOT="$RUNNER_TEMP/guide-http"': "selects an isolated CI installer data directory",
    'mkdir -p "$STDIO_ROOT" "$HTTP_ROOT"': "creates the installer inputs the guide represents "
    "as operator-selected paths",
    "curl --fail --silent http://127.0.0.1:8000/ready": "verifies the guide's documented HTTP "
    "readiness endpoint",
    "status=$?": "keeps the fixture run's exit status, which the next line judges",
    'export NCI_SIP_DATA_DIR="$INDEX_DATA"': "uses the workflow's temporary index directory in "
    "place of the document's /path/to/data placeholder",
    '"$INDEX_VENV/bin/nci-sip-mcp" index-sample C3262 C2991 C40704 C153397 C116938': "runs the "
    "documented sample through the isolated tagged-release environment",
    '"$INDEX_VENV/bin/nci-sip-mcp" evaluate': "runs the documented evaluation through the same "
    "isolated tagged-release environment",
    '[ "$status" -le 1 ] && [ -s acceptance/fixture.json ] || { echo "::error::The fixture run '
    'did not finish (exit status $status)."; exit 1; }': "exit status 1 means some tests did "
    "not pass, which the comparison judges; any other status, or no report, is no run",
}

# The job points caDSR and Shared SI at loopback: step shells get loopback URLs and no_proxy for
# loopback and the listed hosts, which resolve to loopback for resolvers that read /etc/hosts.
# local-run.yml says what this does not cover; tests/test_workflow_containment.py ties both
# installer jobs to the same design.
LOOPBACK_ENV = {
    "NCI_SIP_CADSR_BASE_URL": "http://127.0.0.1:8767/cadsr",
    "NCI_SIP_CADSR_FTP_URL": "http://127.0.0.1:8767/cadsr-ftp",
    "NCI_SIP_SSIS_SPARQL_URL": "http://127.0.0.1:8767/ssis-sparql",
    "no_proxy": "127.0.0.1,localhost,::1,cadsrapi.cancer.gov,cadsrapi-stage.cancer.gov,"
    "cadsr.nci.nih.gov,shared.semantics.cancer.gov",
}

HOSTS_BACKSTOP_RUN = r"""set -euo pipefail
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
"""  # noqa: E501

RECORDER_START_RUN = r"""set -euo pipefail
mkdir -p "$RUNNER_TEMP/upstream-recorder"
python3 -m http.server 8767 --bind 127.0.0.1 --directory "$RUNNER_TEMP/upstream-recorder" < /dev/null > "$RUNNER_TEMP/upstream-recorder.out" 2> "$RUNNER_TEMP/upstream-requests.log" &
recorder=$!
echo "$recorder" > "$RUNNER_TEMP/upstream-recorder.pid"
for attempt in $(seq 1 100); do
  kill -0 "$recorder"
  if (: <> /dev/tcp/127.0.0.1/8767) 2>/dev/null; then break; fi
  if [ "$attempt" -eq 100 ]; then echo "::error::The loopback recorder did not listen."; exit 1; fi
  sleep 0.1
done
"""  # noqa: E501

RECORDER_STOP_RUN = r"""set -euo pipefail
if [ ! -f "$RUNNER_TEMP/upstream-recorder.pid" ]; then echo "::error::No loopback recorder was started."; exit 1; fi
recorder="$(cat "$RUNNER_TEMP/upstream-recorder.pid")"
case "$(ps -p "$recorder" -o command= || true)" in
  *"-m http.server 8767 --bind 127.0.0.1 "*) ;;
  *) echo "::error::Process $recorder is not the owned loopback recorder."; exit 1 ;;
esac
kill "$recorder"
for attempt in $(seq 1 100); do
  case "$(ps -p "$recorder" -o stat= || true)" in ""|Z*) break ;; esac
  if [ "$attempt" -eq 100 ]; then echo "::error::The loopback recorder did not stop."; exit 1; fi
  sleep 0.1
done
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
"""  # noqa: E501

# The CI-only probe steps run a documented server command; the document that gives the command,
# the command, and the readiness URL the probe waits for (None: an initialize over stdio).
PROBES = {
    "Serve over stdio and initialize (CI only)": (
        "QUICKSTART.md",
        "pdm run nci-sip-mcp serve",
        None,
    ),
    "Serve the validation dashboard until ready (CI only)": (
        "docs/local-guide.md",
        "pdm run portal serve",
        "http://127.0.0.1:8081/health",
    ),
    "Serve host documentation until ready (CI only)": (
        "docs/local-guide.md",
        "pdm run python -m scripts.static_server --directory tmp/docs-site --port 8080",
        "http://127.0.0.1:8080/health",
    ),
}

DOCUMENTED_INDEX_INSTALL = (
    '"$INDEX_VENV/bin/python" -m pip install "nci-sip-mcp[server,index] @ '
    'git+https://github.com/CBIIT/nci-sip-mcp@vX.Y.Z"'
)
REQUIRED_INDEX_RUN = r"""set -euo pipefail
server=""
cleanup() {
  status=$?
  cleanup_status=0
  signalled=false
  if [ -n "$server" ]; then
    if kill -0 "$server" 2>/dev/null; then
      kill "$server" || cleanup_status=$?
      signalled=true
    fi
    if wait "$server"; then child_status=0; else child_status=$?; fi
    printf 'cleanup required-index pid=%s status=%s signalled=%s\n' \
      "$server" "$child_status" "$signalled" >&2
    if [ "$signalled" = true ]; then
      case "$child_status" in 0|143|241) ;; *) cleanup_status="$child_status" ;; esac
    elif [ "$child_status" -ne 0 ]; then
      cleanup_status="$child_status"
    fi
  fi
  if [ "$status" -eq 0 ]; then status=$cleanup_status; fi
  exit "$status"
}
trap cleanup EXIT
export NCI_SIP_DATA_DIR="$INDEX_DATA"
export NCI_SIP_HTTP_REQUIRE_INDEX=1
"$INDEX_VENV/bin/nci-sip-mcp" serve --transport streamable-http &
server=$!
python3 "$RUNNER_TEMP/ready.py" http://127.0.0.1:8000/ready --pid "$server"
"$INDEX_VENV/bin/python" - <<'PY'
import asyncio
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client

async def check():
    async with Client(streamable_http_client("http://127.0.0.1:8000/mcp")) as client:
        arguments = {"terminology": "ncit", "query": "Neoplasm", "mode": "semantic"}
        reply = await client.call_tool("search_concepts", arguments)
        result = reply.structured_content
        concept = result["results"][0]["concept"]
        assert concept["code"] == "C3262", result
        assert concept["provenance"]["servedBy"] == "index", result
        assert concept["provenance"]["source"] == "evs_index", result

asyncio.run(check())
PY
"""

SERVER_ONLY_RUN = r"""export NCI_SIP_DATA_DIR="$RUNNER_TEMP/server-only-data"
"$SERVER_VENV/bin/python" - <<'PY'
import asyncio
import os
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters, stdio_client

async def check():
    server = StdioServerParameters(
        command=os.environ["SERVER_VENV"] + "/bin/nci-sip-mcp",
        args=["serve"],
        env={"NCI_SIP_DATA_DIR": os.environ["RUNNER_TEMP"] + "/server-only-data"},
    )
    async with Client(stdio_client(server)) as client:
        arguments = {"terminology": "ncit", "query": "Neoplasm", "mode": "semantic"}
        reply = await client.call_tool("search_concepts", arguments)
        result = reply.structured_content
        assert result["error"]["code"] == "capability_unavailable", result
        assert "results" not in result, result

asyncio.run(check())
PY
"""

# The exact run text of the step that writes the readiness probe. It answers ready only while the
# server it started is running, stops that server, and fails unless it ended by its own exit or
# the SIGTERM sent to it (directly, or as `pdm run` reports it).
READY_PROBE_RUN = r"""cat > "$RUNNER_TEMP/ready.py" <<'PY'
import os, signal, subprocess, sys, time, urllib.request

url, command = sys.argv[1], sys.argv[2:]
owned = command[:1] != ["--pid"]
server = (subprocess.Popen(command, start_new_session=True) if owned else None)
pid = server.pid if server else int(command[1])
def running():
    if server is not None:
        return server.poll() is None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True
try:
    deadline = time.monotonic() + 90
    while True:
        try:
            with urllib.request.urlopen(url, timeout=5) as reply:
                if running():
                    print(url, reply.status, reply.read().decode())
                    break
        except OSError:
            pass
        if not running() or time.monotonic() > deadline:
            sys.exit("not ready: " + url)
        time.sleep(1)
finally:
    if server is not None:
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        status = server.wait(timeout=30)
# pdm run reports a child that the SIGTERM ended as 256 - 15.
if server is not None and status not in (0, -signal.SIGTERM, 256 - signal.SIGTERM):
    sys.exit(f"the server stopped with status {status}")
PY
"""

# The CI-only steps that remove a copy the runner image ships, and the formulae each removes.
REMOVALS = {
    "Remove preinstalled Python and PDM (macOS)": ["python@3.14", "pdm"],
    "Remove a preinstalled Node.js (macOS)": ["node"],
}

# The exact run text of each in-use check: a check that cannot fail would let a copy the runner
# image ships take over unnoticed.
IN_USE_PYTHON_MACOS = r"""fail() { echo "::error::$1"; exit 1; }
[ "$(command -v pdm)" = "$(brew --prefix)/bin/pdm" ] || fail "pdm is $(command -v pdm), not the Homebrew install"
tool_python="$(brew --prefix pdm)/libexec/bin/python"
"$tool_python" -c 'import json; from importlib.metadata import distributions; print(json.dumps(sorted((item.metadata["Name"], item.version) for item in distributions())))'
version="$(pdm run python -c "import sys; print('%d.%d' % sys.version_info[:2])" | tail -n 1)"
[ "$version" = "3.14" ] || fail "the project runs Python $version, not 3.14"
base="$(pdm run python -c "import sys; print(sys.base_prefix)" | tail -n 1)"
formula="$(brew --prefix python@3.14)"
case "$base" in "$formula"/*) ;; *) fail "the project's Python is $base, not Homebrew's python@3.14 ($formula)" ;; esac
"""  # noqa: E501

IN_USE_PYTHON_LINUX = r"""fail() { echo "::error::$1"; exit 1; }
[ "$(command -v pdm)" = "$HOME/.local/bin/pdm" ] || fail "pdm is $(command -v pdm), not the uv tool install"
tool_python="$(uv tool dir)/pdm/bin/python"
"$tool_python" -c 'import json; from importlib.metadata import distributions; print(json.dumps(sorted((item.metadata["Name"], item.version) for item in distributions())))'
version="$(pdm run python -c "import sys; print('%d.%d' % sys.version_info[:2])" | tail -n 1)"
[ "$version" = "3.14" ] || fail "the project runs Python $version, not 3.14"
base="$(pdm run python -c "import sys; print(sys.base_prefix)" | tail -n 1)"
managed="$(uv python dir)"
case "$base" in "$managed"/*) ;; *) fail "the project's Python is $base, not a uv-managed one under $managed" ;; esac
"""  # noqa: E501

IN_USE_NODE = r"""fail() { echo "::error::$1"; exit 1; }
case "$RUNNER_OS" in
  macOS) expected="$(brew --prefix)/bin/node" ;;
  *) expected="$HOME/.nvm/versions/node/*/bin/node" ;;
esac
# shellcheck disable=SC2254 # the pattern is meant to expand
case "$(command -v node)" in $expected) ;; *) fail "node is $(command -v node), not the documented install ($expected)" ;; esac
node -e 'process.exit(Number(process.versions.node.split(".")[0]) >= 24 ? 0 : 1)' || fail "Node.js $(node --version) is older than 24"
"""  # noqa: E501

IN_USE_CHECKS = {
    "Check that the documented Python and PDM are in use (macOS)": IN_USE_PYTHON_MACOS,
    "Check that the documented Python and PDM are in use (Linux)": IN_USE_PYTHON_LINUX,
    "Check that the documented Node.js is in use": IN_USE_NODE,
}

# The CI-only probe steps each end after this long, however the server behaves.
PROBE_TIMEOUT_MINUTES = 3

TRIGGERS = {
    "push": {"branches": ["main"]},
    "schedule": [{"cron": "17 7 * * 1"}],
    "workflow_dispatch": None,
    "pull_request": {
        "paths": [
            "src/**",
            "acceptance/**",
            "scripts/**",
            "docs/site-assets/**",
            ".github/actions/**",
            "tests/test_local_run_workflow.py",
            "tests/test_workflow_twins.py",
            "LICENSE",
            "pyproject.toml",
            "pdm.lock",
            "docs/site-assets/package.json",
            "docs/site-assets/package-lock.json",
            "README.md",
            "QUICKSTART.md",
            "CONTRIBUTING.md",
            "acceptance/README.md",
            "docs/documentation-site.md",
            "docs/local-guide.md",
            "docs/local-validation.md",
            "docs/installer-targets.md",
            "container/compose.local.yaml",
            "container/Docs.Dockerfile",
            "container/Admin.Dockerfile",
            "scripts/companion_context.py",
            "scripts/companion_relay.py",
            "scripts/companion_smoke.py",
            "scripts/static_server.py",
            "scripts/portal*.py",
            "scripts/operator_*.py",
            "scripts/docs_site.py",
            ".github/workflows/local-run.yml",
        ]
    },
}

COMBINATIONS = [
    {
        "os": "ubuntu-24.04",
        "combination": "server",
        "server": True,
        "index": False,
        "host_docs": False,
        "host_companion": False,
        "compose_docs": False,
        "compose_companion": False,
        "processes": "installer stdio, then installer HTTP",
        "ports": "8000",
    },
    {
        "os": "ubuntu-24.04",
        "combination": "server-index",
        "server": True,
        "index": True,
        "host_docs": False,
        "host_companion": False,
        "compose_docs": False,
        "compose_companion": False,
        "processes": "installer stopped, then required-index HTTP",
        "ports": "8000",
    },
    {
        "os": "ubuntu-24.04",
        "combination": "docs",
        "server": False,
        "index": False,
        "host_docs": False,
        "host_companion": False,
        "compose_docs": True,
        "compose_companion": False,
        "processes": "Compose documentation",
        "ports": "8080",
    },
    {
        "os": "ubuntu-24.04",
        "combination": "companion",
        "server": False,
        "index": False,
        "host_docs": False,
        "host_companion": False,
        "compose_docs": False,
        "compose_companion": True,
        "processes": "Compose administration and ingress",
        "ports": "8081",
    },
    {
        "os": "ubuntu-24.04",
        "combination": "everything",
        "server": True,
        "index": True,
        "host_docs": False,
        "host_companion": False,
        "compose_docs": True,
        "compose_companion": True,
        "processes": "required-index HTTP, Compose documentation, administration and ingress",
        "ports": "8000,8080,8081",
    },
    {
        "os": "macos-latest",
        "combination": "server",
        "server": True,
        "index": False,
        "host_docs": False,
        "host_companion": False,
        "compose_docs": False,
        "compose_companion": False,
        "processes": "installer stdio, then installer HTTP",
        "ports": "8000",
    },
    {
        "os": "macos-latest",
        "combination": "everything-without-docker",
        "server": True,
        "index": True,
        "host_docs": True,
        "host_companion": True,
        "compose_docs": False,
        "compose_companion": False,
        "processes": "required-index HTTP, host documentation and portal",
        "ports": "8000,8080,8081",
    },
]

GUIDE_COMBINATIONS = [
    ("Server", "macOS/Linux", "Installer stdio, then installer HTTP", "8000"),
    ("Server and index", "Linux", "Installer stopped, then required-index HTTP", "8000"),
    ("Documentation", "Linux with Compose", "Compose documentation", "8080"),
    ("Companion", "Linux with Compose", "Compose administration and ingress", "8081"),
    (
        "Everything",
        "Linux with Compose",
        "Required-index HTTP, Compose documentation, administration and ingress",
        "8000, 8080, 8081",
    ),
    (
        "Everything without Docker",
        "macOS",
        "Required-index HTTP, host documentation and portal",
        "8000, 8080, 8081",
    ),
]


STDIO_INITIALIZE_RUN = r"""python3 - <<'PY'
import json, subprocess

request = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
    "protocolVersion": "2025-06-18", "capabilities": {},
    "clientInfo": {"name": "local-run", "version": "0"}}}
server = subprocess.Popen(["pdm", "run", "nci-sip-mcp", "serve"], stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, text=True)
try:
    server.stdin.write(json.dumps(request) + "\n")
    server.stdin.flush()
    reply = json.loads(server.stdout.readline())
    assert reply["id"] == 1 and "serverInfo" in reply["result"], reply
    print("initialize answered by", reply["result"]["serverInfo"])
finally:
    server.kill()
    server.wait(timeout=30)
PY
"""


def probe_run(command, url):
    """The run text a probe step must have: the documented command as the probe starts it."""

    if url is None:
        return '["' + '", "'.join(command.split()) + '"]'
    return f'python3 "$RUNNER_TEMP/ready.py" {url} {command}'


def load(path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def job():
    return load(LOCAL_RUN)["jobs"]["local-run"]


def steps():
    return [step for step in job()["steps"] if "name" in step]


def run_by_name():
    return {step["name"]: step["run"] for step in steps() if "run" in step}


def guide_combinations():
    rows = re.findall(
        r"^\| ([^|]+) \| ([^|]+) \| ([^|]+) \| ([^|]+) \|",
        LOCAL_GUIDE.read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    return [tuple(value.strip() for value in row) for row in rows if row[0] != "Combination"]


def workflow_guide_combinations():
    names = {
        "server-index": ("Server and index", "Linux"),
        "docs": ("Documentation", "Linux with Compose"),
        "companion": ("Companion", "Linux with Compose"),
        "everything": ("Everything", "Linux with Compose"),
        "everything-without-docker": ("Everything without Docker", "macOS"),
    }
    return [
        (
            *names[row["combination"]],
            row["processes"].casefold(),
            row["ports"].replace(",", ", "),
        )
        for row in COMBINATIONS
        if row["combination"] != "server"
    ]


def documented_commands(document):
    """Commands a document gives: fenced block lines and table-row code spans, comments removed."""

    text = (ROOT / document).read_text(encoding="utf-8")
    commands = {}  # ordered: a step's lines must keep the document's order
    for block in re.findall(r"^```(?:bash|sh|shell)\n(.*?)^```", text, re.DOTALL | re.MULTILINE):
        for line in block.splitlines():
            commands.setdefault(re.sub(r"\s+#.*$", "", line).strip())
    # Inline spans count only in table rows (QUICKSTART's prerequisites table), never in prose.
    for row in re.findall(r"^\|.*$", text, re.MULTILINE):
        for span in re.findall(r"`([^`\n]+)`", row):
            commands.setdefault(span.replace("\\|", "|").strip())
    return list(commands)


def command_lines(run):
    lines = (line.strip() for line in run.splitlines())
    return [line for line in lines if line and not line.startswith("#")]


def undocumented_lines(step_name, run, documents):
    """The step's command lines that neither a document gives nor the CI-only list explains."""

    if step_name in CI_ONLY:
        return []
    commands = documented_commands(documents[step_name])
    return [line for line in command_lines(run) if line not in commands and line not in CI_ONLY]


class LocalRunWorkflowTest(unittest.TestCase):
    def test_linux_bootstrap_uses_secure_compatible_pdm_at_the_independently_pinned_ci_version(
        self,
    ):
        steps = {step.get("name"): step for step in job()["steps"]}
        self.assertEqual(
            steps["Install Python and PDM (Linux)"]["run"],
            "uv python install 3.14\n"
            "uv tool install --exclude-newer 5d --with 'hishel==1.2.1' "
            "--with 'unearth==0.18.2' --with 'virtualenv==21.15.2' 'pdm==2.29.2'\n",
        )
        setup = load(WORKFLOWS.parent / "actions" / "setup" / "action.yml")
        self.assertEqual(setup["runs"]["steps"][0]["with"]["version"], "2.29.2")

    def test_the_workflow_exists_beside_the_others(self):
        self.assertEqual(
            sorted(path.name for path in WORKFLOWS.glob("*.yml")),
            [
                "acceptance-live.yml",
                "audit.yml",
                "branch-cleanup.yml",
                "ci.yml",
                "local-installer.yml",
                "local-run.yml",
                "pr-title.yml",
                "release.yml",
                "scorecard.yml",
            ],
        )

    def test_it_runs_only_when_intended(self):
        self.assertEqual(load(LOCAL_RUN)[True], TRIGGERS)

    def test_it_has_least_privilege_and_no_secret(self):
        self.assertEqual(load(LOCAL_RUN)["permissions"], {"contents": "read"})
        self.assertNotIn("secrets.", LOCAL_RUN.read_text(encoding="utf-8"))

    def test_it_runs_on_exactly_the_two_platforms_with_a_bound(self):
        self.assertEqual(job()["name"], "local run (${{ matrix.os }}, ${{ matrix.combination }})")
        self.assertEqual(job()["needs"], "prepare-containers")
        self.assertEqual(job()["if"], "${{ !cancelled() }}")
        self.assertEqual(job()["runs-on"], "${{ matrix.os }}")
        self.assertEqual(
            job()["strategy"],
            {"fail-fast": False, "matrix": {"include": COMBINATIONS}},
        )
        self.assertEqual(job()["timeout-minutes"], 60)

    def test_every_combination_has_exact_processes_ports_and_component_selection(self):
        self.assertEqual(guide_combinations(), GUIDE_COMBINATIONS)
        expected = [
            (name, platform, process.casefold(), ports)
            for name, platform, process, ports in GUIDE_COMBINATIONS[1:]
        ]
        self.assertEqual(workflow_guide_combinations(), expected)
        self.assertEqual(
            [row["os"] for row in COMBINATIONS if row["combination"] == "server"],
            ["ubuntu-24.04", "macos-latest"],
        )

    def test_checkout_keeps_tags_and_no_credentials(self):
        checkouts = [s for s in job()["steps"] if s.get("uses", "").startswith("actions/checkout@")]
        self.assertEqual(len(checkouts), 1)
        self.assertEqual(checkouts[0]["with"], {"fetch-depth": 0, "persist-credentials": False})

    def test_it_installs_by_the_documented_route_not_a_setup_action(self):
        self.assertEqual(
            [s["uses"] for s in job()["steps"] if "uses" in s],
            [
                "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
                "actions/download-artifact@9000827ccba6bdab643e8b6fd33ac0654aef8333",
            ],
        )

    def test_container_context_is_prepared_once_and_downloaded_only_by_compose_rows(self):
        prepare = load(LOCAL_RUN)["jobs"]["prepare-containers"]
        self.assertEqual(
            {key: prepare[key] for key in ("name", "runs-on", "timeout-minutes")},
            {
                "name": "prepare local container contexts",
                "runs-on": "ubuntu-24.04",
                "timeout-minutes": 10,
            },
        )
        self.assertEqual(prepare["steps"][1]["uses"], "$/.github/actions/setup")
        self.assertEqual(
            prepare["steps"][0],
            {
                "uses": "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
                "with": {"fetch-depth": 0, "persist-credentials": False},
            },
        )
        self.assertEqual(
            [step.get("run") for step in prepare["steps"][2:5]],
            [
                "npm ci --prefix docs/site-assets --ignore-scripts",
                "npm run build --prefix docs/site-assets",
                "pdm run python -m scripts.companion_context",
            ],
        )
        self.assertEqual(
            prepare["steps"][5]["with"],
            {
                "name": "local-companion-context-${{ github.sha }}",
                "path": "tmp/companion-context/",
                "if-no-files-found": "error",
                "retention-days": 1,
            },
        )
        download = next(
            step
            for step in steps()
            if step["name"] == "Download selected local container contexts (CI only)"
        )
        self.assertEqual(download["if"], "matrix.compose_docs || matrix.compose_companion")
        self.assertEqual(
            download["with"],
            {
                "name": "local-companion-context-${{ github.sha }}",
                "path": "tmp/companion-context",
            },
        )

    def test_the_steps_are_exactly_these_in_order_each_on_its_platform(self):
        self.assertEqual([(s["name"], s.get("if")) for s in steps()], STEPS)

    def test_every_command_is_copied_from_the_document_it_walks(self):
        for step in steps():
            if "run" not in step:
                continue
            with self.subTest(step=step["name"]):
                self.assertEqual(undocumented_lines(step["name"], step["run"], DOCUMENTS), [])

    def test_every_step_keeps_the_order_of_its_document(self):
        for step in steps():
            if "run" in step and step["name"] in DOCUMENTS and step["name"] not in CI_ONLY:
                commands = documented_commands(DOCUMENTS[step["name"]])
                found = [
                    commands.index(line) for line in command_lines(step["run"]) if line in commands
                ]
                with self.subTest(step=step["name"]):
                    self.assertEqual(found, sorted(found))

    def test_the_stdio_initialize_round_trip_is_exactly_this(self):
        by_name = run_by_name()
        self.assertEqual(by_name["Serve over stdio and initialize (CI only)"], STDIO_INITIALIZE_RUN)

    def test_the_optional_index_uses_the_documented_release_extra_or_the_pr_checkout(self):
        by_name = run_by_name()
        self.assertIn(DOCUMENTED_INDEX_INSTALL, documented_commands("docs/local-guide.md"))
        install = by_name["Install released server and optional index environments"]
        self.assertEqual(
            install,
            "\n".join(
                (
                    'INDEX_VENV="$RUNNER_TEMP/index-venv"',
                    'SERVER_VENV="$RUNNER_TEMP/server-venv"',
                    'python3.14 -m venv "$SERVER_VENV"',
                    CI_SERVER_INSTALL,
                    'echo "SERVER_VENV=$SERVER_VENV" >> "$GITHUB_ENV"',
                    'if [ "$WITH_INDEX" = "true" ]; then',
                    '  python3.14 -m venv "$INDEX_VENV"',
                    "  " + CI_INDEX_INSTALL,
                    '  echo "INDEX_VENV=$INDEX_VENV" >> "$GITHUB_ENV"',
                    "fi",
                    'echo "INDEX_DATA=$RUNNER_TEMP/index-data" >> "$GITHUB_ENV"',
                    "",
                )
            ),
        )
        self.assertEqual(
            by_name["Select the released index source (CI only)"],
            "\n".join(
                (
                    "set -euo pipefail",
                    'if [ "$EVENT_NAME" = "schedule" ]; then',
                    '  RELEASE_TAG="$(git describe --tags --abbrev=0)"',
                    "else",
                    "  RELEASE_TAG=v0.0.0",
                    '  git tag "$RELEASE_TAG" HEAD',
                    '  git config --global --add url."file://$GITHUB_WORKSPACE/".insteadOf https://github.com/CBIIT/nci-si-mcp',
                    '  git config --global --add url."file://$GITHUB_WORKSPACE/".insteadOf https://github.com/CBIIT/nci-sip-mcp',
                    "fi",
                    'echo "RELEASE_TAG=$RELEASE_TAG" >> "$GITHUB_ENV"',
                    "",
                )
            ),
        )

    def test_source_selection_uses_candidate_for_unreleased_events_and_tag_for_schedule(self):
        script = run_by_name()["Select the released index source (CI only)"]
        scenarios = (
            ("pull_request", "refs/pull/42/merge", "v0.0.0", True),
            ("push", "refs/heads/main", "v0.0.0", True),
            ("workflow_dispatch", "refs/heads/issue/511-policy", "v0.0.0", True),
            ("workflow_dispatch", "refs/heads/main", "v0.0.0", True),
            ("schedule", "refs/heads/main", "v0.39.2", False),
        )
        for event, ref, expected_tag, candidate in scenarios:
            with self.subTest(event=event, ref=ref), TemporaryDirectory() as directory:
                root = Path(directory)
                git_dir = root / "bin"
                git_dir.mkdir()
                fake_git = git_dir / "git"
                calls = root / "git-calls"
                fake_git.write_text(
                    f"#!{sys.executable}\n"
                    "import os, sys\n"
                    "with open(os.environ['GIT_CALLS'], 'a', encoding='utf-8') as stream:\n"
                    "    stream.write(' '.join(sys.argv[1:]) + '\\n')\n"
                    "if sys.argv[1:] == ['describe', '--tags', '--abbrev=0']:\n"
                    "    print(os.environ['FAKE_TAG'])\n",
                    encoding="utf-8",
                )
                fake_git.chmod(0o755)
                environment = {
                    "EVENT_NAME": event,
                    "GITHUB_REF": ref,
                    "GITHUB_WORKSPACE": str(root),
                    "GITHUB_ENV": str(root / "github-env"),
                    "GIT_CONFIG_GLOBAL": str(root / "global.gitconfig"),
                    "GIT_CALLS": str(calls),
                    "FAKE_TAG": "v0.39.2",
                    "HOME": str(root),
                    "PATH": str(git_dir) + os.pathsep + os.environ["PATH"],
                }
                result = subprocess.run(  # noqa: S603 - executes a repository-owned workflow step in a temp dir
                    ["bash", "-e", "-o", "pipefail", "-c", script],  # noqa: S607
                    cwd=root,
                    env=environment,
                    capture_output=True,
                    text=True,
                    check=False,
                )

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(
                    (root / "github-env").read_text(encoding="utf-8"),
                    f"RELEASE_TAG={expected_tag}\n",
                )
                commands = calls.read_text(encoding="utf-8").splitlines()
                if candidate:
                    self.assertEqual(len(commands), 3)
                    self.assertEqual(commands[0], "tag v0.0.0 HEAD")
                    self.assertTrue(all("file:///" in command for command in commands[1:]))
                else:
                    self.assertEqual(commands, ["describe --tags --abbrev=0"])

    def test_required_index_readiness_and_search_are_exact(self):
        by_name = run_by_name()
        self.assertEqual(
            by_name["Verify required-index HTTP and indexed search (CI only)"],
            REQUIRED_INDEX_RUN,
        )

    def test_server_only_search_has_the_exact_no_index_error(self):
        by_name = run_by_name()
        self.assertEqual(
            by_name["Verify server-only search without an index (CI only)"], SERVER_ONLY_RUN
        )

    def test_combination_cleanup_classifies_every_process_exit_exactly(self):
        run = run_by_name()["Verify every selected component together (CI only)"]
        classification = """if [ "$signalled" = true ]; then
      case "$child_status" in 0|143|241) ;; *) cleanup_status="$child_status" ;; esac
    elif [ "$child_status" -ne 0 ]; then
      cleanup_status="$child_status"
    fi"""
        self.assertIn(classification, run)
        diagnostic = (
            "printf 'cleanup %s pid=%s status=%s signalled=%s\\n' \\\n"
            '      "$name" "$pid" "$child_status" "$signalled" >&2'
        )
        self.assertIn(diagnostic, run)

    def test_compose_verification_compares_exactly_the_running_selected_services(self):
        checks = (
            "docker compose -f container/compose.local.yaml ps --status running --services | "
            'sort > "$RUNNER_TEMP/running-services"',
            'cmp "$RUNNER_TEMP/running-services" "$RUNNER_TEMP/expected-services"',
        )
        runs = run_by_name()
        profile = runs["Verify the selected Compose profiles (CI only)"]
        combined = runs["Verify every selected component together (CI only)"]
        for check in checks:
            with self.subTest(step="profile", check=check):
                self.assertIn(check, profile)
            with self.subTest(step="combined", check=check):
                self.assertIn(check, combined)
        self.assertIn(
            'printf \'%s\\n\' "${expected[@]}" | sort > "$RUNNER_TEMP/expected-services"',
            profile,
        )
        self.assertIn(
            "printf '%s\\n' admin-ingress administration documentation | sort > "
            '"$RUNNER_TEMP/expected-services"',
            combined,
        )

    def test_the_ci_only_list_has_no_stale_entry(self):
        lines = {line.strip() for run in run_by_name().values() for line in run.splitlines()}
        names = {step["name"] for step in steps()}
        self.assertEqual([key for key in CI_ONLY if key not in names | lines], [])

    def test_the_probe_steps_run_the_documented_server_commands(self):
        by_name = run_by_name()
        for name, (document, command, url) in PROBES.items():
            with self.subTest(step=name):
                self.assertIn(command, documented_commands(document))
                self.assertIn(probe_run(command, url), by_name[name])
                if url is not None:
                    self.assertEqual(by_name[name].strip(), probe_run(command, url))

    def test_the_in_use_checks_are_exactly_these(self):
        by_name = run_by_name()
        for name, run in IN_USE_CHECKS.items():
            with self.subTest(step=name):
                self.assertEqual(by_name[name], run)

    def test_every_probe_step_has_the_same_short_bound(self):
        by_name = {step["name"]: step for step in steps()}
        for name in PROBES:
            with self.subTest(step=name):
                self.assertEqual(by_name[name]["timeout-minutes"], PROBE_TIMEOUT_MINUTES)

    def test_the_readiness_probe_is_exactly_this(self):
        by_name = run_by_name()
        self.assertEqual(by_name["Write the readiness probe (CI only)"], READY_PROBE_RUN)

    def test_the_runner_loses_its_python_org_builds_before_the_documented_install(self):
        by_name = run_by_name()
        self.assertEqual(
            by_name.get("Remove the runner's python.org Python (macOS)", "").splitlines(),
            [
                "sudo find /usr/local/bin -lname '*Library/Frameworks/Python.framework/*' -delete",
                "sudo rm -rf /Library/Frameworks/Python.framework",
            ],
        )

    def test_a_failed_removal_of_a_preinstalled_formula_fails_the_job(self):
        by_name = run_by_name()
        for name, formulae in REMOVALS.items():
            with self.subTest(step=name):
                self.assertEqual(
                    by_name.get(name, "").splitlines(),
                    [
                        f"if brew list --versions {formula} >/dev/null; then "
                        f"brew uninstall --ignore-dependencies {formula}; fi"
                        for formula in formulae
                    ],
                )

    def test_nothing_changes_a_step_beside_its_pinned_keys(self):
        # An env, defaults, working-directory or continue-on-error would change what a documented
        # line does, or keep a failed check from failing the job, with every run line unchanged.
        # The one job env is deliberate: it keeps caDSR and Shared SI on loopback (tested below).
        workflow = load(LOCAL_RUN)
        self.assertEqual(set(workflow), {"name", True, "permissions", "concurrency", "jobs"})
        self.assertEqual(
            set(job()),
            {"name", "needs", "if", "runs-on", "strategy", "timeout-minutes", "env", "steps"},
        )
        allowed = {"name", "if", "uses", "with", "shell", "timeout-minutes", "run"}
        extra = {
            step.get("name", step.get("uses")): sorted(set(step) - allowed)
            for step in job()["steps"]
            if set(step) - allowed
        }
        # Exact step env values are owned by test_ci_required and the job env by the loopback
        # tests; no other step gains env, and only the recorder step gains an id.
        self.assertEqual(
            extra,
            {
                "Select the released index source (CI only)": ["env"],
                "Install released server and optional index environments": ["env"],
                "Start the loopback caDSR and Shared SI recorder (CI only)": ["id"],
                "Build selected local container images": ["env"],
                "Verify the selected Compose profiles (CI only)": ["env"],
                "Verify every selected component together (CI only)": ["env"],
            },
        )

    def test_the_job_points_cadsr_and_shared_si_at_loopback_with_a_hosts_backstop(self):
        self.assertEqual(job().get("env"), LOOPBACK_ENV)
        by_name = {step["name"]: step for step in steps()}
        self.assertEqual(
            by_name.get("Resolve prohibited caDSR and Shared SI hosts to loopback (CI only)"),
            {
                "name": "Resolve prohibited caDSR and Shared SI hosts to loopback (CI only)",
                "run": HOSTS_BACKSTOP_RUN,
            },
        )

    def test_the_recorder_is_owned_ready_and_judged_exactly(self):
        by_name = {step["name"]: step for step in steps()}
        start = by_name.get("Start the loopback caDSR and Shared SI recorder (CI only)", {})
        self.assertEqual((start.get("id"), start.get("run")), ("recorder", RECORDER_START_RUN))
        stop = by_name.get("Stop the loopback recorder and check its requests (CI only)", {})
        self.assertEqual(stop.get("run"), RECORDER_STOP_RUN)

    def test_only_the_fixture_run_survives_a_failing_command_to_judge_its_status(self):
        # Under the default `bash -e`, exit status 1 would end the step before the guard and the
        # comparison that judge it.
        shells = {step["name"]: step["shell"] for step in steps() if "shell" in step}
        self.assertEqual(shells, {"Run the acceptance suite against the fixtures": "bash {0}"})
        self.assertEqual(
            run_by_name()["Run the acceptance suite against the fixtures"], FIXTURE_RUN
        )

    def test_a_newer_push_replaces_only_a_pull_request_run(self):
        self.assertEqual(
            load(LOCAL_RUN)["concurrency"],
            {
                "group": "local-run-"
                "${{ github.event_name == 'pull_request' && github.ref || github.sha }}",
                "cancel-in-progress": "${{ github.event_name == 'pull_request' }}",
            },
        )

    def test_a_command_in_prose_is_not_a_documented_command(self):
        with TemporaryDirectory() as directory:
            document = Path(directory) / "document.md"
            document.write_text(
                "Run `in prose` first.\n\n| Tool | Check |\n|---|---|\n| Node | `in a table` |\n",
                encoding="utf-8",
            )
            self.assertEqual(documented_commands(str(document)), ["in a table"])

    def test_a_one_sided_edit_is_caught(self):
        step = next(s for s in steps() if s["name"] == "Install the project")
        self.assertEqual(undocumented_lines(step["name"], step["run"], DOCUMENTS), [])
        self.assertEqual(
            undocumented_lines(step["name"], "pdm install --no-lock", DOCUMENTS),
            ["pdm install --no-lock"],
        )
