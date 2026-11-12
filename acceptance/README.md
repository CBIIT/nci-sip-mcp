# NCI SI MCP acceptance suite

The behavioural acceptance suite for the NCI SI MCP tools, which tests the requirements of the
[specification](../docs/specification.md), and the upstream fixture server it runs against. It tests the MCP
tool surface of a command or remote fixture server. Managed live execution uses the furnished
CLI and its verified transport integration; assertions remain at the public MCP boundary.

From the repository root:

```bash
pdm run acceptance --report=fixture.json             # fixture mode; writes acceptance/fixture.json
NCI_SI_ACCEPTANCE_MODE=live NCI_SI_ACCEPTANCE_ENVIRONMENT=evs-only pdm run acceptance --report=live.json
NCI_SI_ACCEPTANCE_SERVER="..." pdm run acceptance    # another server (default: nci-si-mcp serve)
NCI_SI_ACCEPTANCE_URL=https://... pdm run acceptance # a remote server over streamable HTTP (below)
NCI_SI_ACCEPTANCE_PROFILE=evs NCI_SI_ACCEPTANCE_SERVER="..." pdm run acceptance  # a server of one profile (default: unified)
pdm run python -m nci_si_acceptance.report acceptance/fixture.json --live acceptance/live.json
pdm run acceptance-index-codes                       # the index set, one code per line (remote server)
pdm run acceptance -n 4 --dist worksteal --report=fixture.json  # four workers, stealing work; the same report
pdm run acceptance-expected check acceptance/fixture.json   # a report against the expected outcomes
pdm run acceptance-expected update acceptance/fixture.json  # rewrite the expected outcomes from a report
pdm run acceptance-status                            # regenerate the functional-status tables
pdm run acceptance-selftest                          # the harness's own tests
SELFTEST_SHARD=1/8 pdm run acceptance-selftest       # one of eight shards, as CI runs them
pdm run acceptance-record                            # re-record fixtures/recorded/ from live
pdm run acceptance-craft                             # record the crafted fixtures' digest
pdm run acceptance-register                          # regenerate request-forms/ from the manifest
pdm run acceptance-stories                           # regenerate the complete domain story catalogue
pdm run acceptance-stories --check                   # verify its mapping and generated content
pdm run spec-render                                  # regenerate docs/specification.md from spec/
```

## Managed live environments

Local live acceptance requires `NCI_SI_ACCEPTANCE_ENVIRONMENT=evs-only`, selecting the reviewed
`fixtures/environments.yaml` policy. Historical manifest origins grant no permission. Only EVS
REST/FHIR are enabled; caDSR (including anonymous/FTP/contracts), SSIS and SPARQL stay blocked.
A caDSR-enabled map needs #448's reviewed bounded reader and separate access approval. These
settings grant no upstream access and do not enable the disabled live workflow.

Managed live execution requires the furnished `.venv`, isolated CLI and unified profile, with
no proxy and same-origin redirects. Custom commands, remote/fixture settings, upstream overrides,
proxy/Python/native-loader injection and blocked-surface credentials fail before launch; later
injection is filtered. This assumes a trusted installation, not an OS sandbox or recorder guard.

Set live preparation to `NCI_SI_ACCEPTANCE_PREPARE='nci-si-mcp index-sample'`; the harness supplies
validated argv without a shell. The shell-expanded fixture command below is rejected live.
Selected origins reach item provenance; blocked surfaces use an owned non-forwarding refusal.
Live caDSR citation comparison fails before transport with
`live caDSR citation comparison is disabled by run policy`; fixture comparison is unchanged.

Native/evidence reports preserve the frozen name, all origins and exact policy-byte SHA-256,
without credentials or local paths. Workers must agree with the controller at report time;
historical omission stays unknown. The hash identifies configuration rather than approval or
authenticity. Combined reports and the [local portal](../docs/local-validation.md) display the
recorded name, origins and hash; null origins are labelled disabled and historical absence adds
no table. Combined CLI input is validated before rendering. Fixture reports/outcomes are unchanged.

Selected-environment stdio benchmarking reuses this managed launcher and refusal lifecycle;
[Benchmark](../docs/benchmark.md) owns its commands, selector boundary and measurement limits.

## Writing a test

### Registry-release selection

`NCI_SI_ACCEPTANCE_REGISTRY_RELEASE=optional|required` is harness-only, defaults to
`optional`, and rejects every other value. Optional suspends exactly five retained cases:
export-date discovery, published-release discovery, published-pin transport/attribution,
and both registry cursor-binding directions. Each reports an ordinary skip with reason
`registry-release requirement suspended pending sponsor decision (NCI_SI_ACCEPTANCE_REGISTRY_RELEASE=optional)`.
They remain ordinary skips in live mode too, never `not_live` or executed passes.
Use `required` to run the existing assertions unchanged:

```bash
NCI_SI_ACCEPTANCE_REGISTRY_RELEASE=required pdm run acceptance --report=required.json
```

The marker `registry_release` selects only those cases. Explicit unpublished/unlisted-pin
refusals, registry-only provenance and date alignment still run, as do NCIt graph, EVS and
GDC checks. The sponsor's contract decision remains pending; see
[the scope, measurements and proposals](../spec/acceptance.md#interim-registry-release-selection).
No live tests were measured as newly unblocked. Deterministic `test_registry_selection.py`
self-tests exercise both modes in the existing CI shards: required detects unpinned content
on an actual behavioral assertion, optional records five skips with the exact reason.

To read the behaviours without starting with Python, use these views:

| Reader's question | Where to look |
|---|---|
| Why does this behaviour matter to a domain user? | [Complete user-story catalogue](../docs/behavioural-tests.md), covering every collected MCP acceptance case |
| What must a caller observe, and which tests check it? | [Requirements and test citations](../docs/specification.md#4-requirements), generated from `spec/` and collected tests |
| What upstream situation does a fixture simulate? | Scenario sections in the [EVS](request-forms/evs.md#scenarios), [caDSR](request-forms/cadsr.md#scenarios) and [Shared SI](request-forms/ssis.md#scenarios) request registers |
| How do tools compose into a user task? | [Workflow tools and prompts](../docs/specification.md#workflow-tools) |
| What passed in a particular run? | [The acceptance report](#the-report), with per-tool counts; its JSON retains individual test outcomes |

The story catalogue groups related tests under a user goal and Given/When/Then narrative.
Each test function has an authored behaviour description in [`stories.yaml`](stories.yaml);
the renderer collects every parameter variant and its requirement citations from pytest.
Expandable evidence lists every exact case. The fixture register separately describes
upstream conditions. Implementation unit tests and harness self-tests are outside this MCP
catalogue.

When adding or renaming a test function, add or update its explicit assignment and description
in `stories.yaml`. Review the narrative when changing an existing test's behaviour. Run
`pdm run acceptance-stories` after changing tests, parameter variants, citations or narratives;
do not edit the generated guide. The harness self-tests check fresh collection against the
mapping and document, rejecting undocumented functions, stale references, duplicate assignments,
missing narrative fields and stale generated content. Collection runs no server or test.

A test calls a required tool by its name, `tools.call("get_concept", {...})`, and names the tool
it is for with `@pytest.mark.tool("get_concept")`; a gate, which fails every tool's verdict (the
P and X-25–X-28 requirements), is marked `gate`.
Every test carries one of the two, or collection refuses the run. A gate that cannot
run leaves the module unaccepted, since acceptance needs every gate to pass. When the
server lacks that exact name, the test is skipped as NOT IMPLEMENTED. Calls and arguments
are sent unchanged; no other tool stands in for a required tool. Each test asserts only
what the specification says.

Each test also cites the requirements it enforces, `@pytest.mark.requirement("X-2")`, by their
ids in [`../spec/requirements.yaml`](../spec/requirements.yaml), the project's statement of the
behaviour the suite tests and the server implements. Every requirement is cited by a test or
planned in an issue; `selftests/test_requirements.py` fails otherwise, and on a test that cites
nothing or an unknown id. A citation may cover part of a requirement: `planned` stays until the
citing tests cover all of it. A test that never runs (skip, a true skipif, xfail) covers nothing.

The cross-cutting tests (`tests/test_crosscutting.py`) run against every tool that has a call
in `tests/calls.yaml`, and find a result's items where the tool's `items` in
[`../spec/tools.yaml`](../spec/tools.yaml) say. A tool joins them with those two entries.

`@pytest.mark.scenario("release/unknown")` serves the scenario's fixtures before the ordinary
ones, to a server process of its own started with the scenario's settings. A test that must
see the server ask upstream is marked `own_server`, for a server process of its own that no
earlier call can have filled a cache of. `tools.process` keeps what a test may need of the
server process: its standard error and data directory (`written()`, everything it wrote), and the
upstream requests it made while it started. A test marked
`live_capable` also runs in live mode, unless it selects a scenario; every other test runs
against fixtures only.

Live coverage has 47 marked cases out of 966; 46 can run live, since the retired-concept
scenario remains fixture-only. Alongside protocol, discovery, argument and cache checks,
content checks discover the current monthly NCIt release for concept identity, includes,
paths, maps, relationship catalogues and cross-domain CDE discovery. They check contracts
and matching release provenance, not historical names, counts or order; fixture mode retains
the exact recorded comparisons. These bounded calls prepare no index. Upstream errors and
mismatched provenance fail, never become `not_live`.

Both CDE identity/version and include families run anonymously. Their six sections come
from the data element API's DataElement response (ValueDomain, DataElementConcept,
AlternateNames, ClassificationSchemes and ReferenceDocuments), not CDE Match or the credentialed
lists-of-values API. The calls are unpinned registry reads; an explicit version is the CDE's own, never an
NCIt release or an export date, so these cases need no registry-release discovery. Operations
that need credentials remain fixture-only. This coverage does not certify all tool modes;
the report retains every fixture-only case as not run in live mode.

The component citation case independently GETs anonymous DataElement 2200604/v4 once using
stdlib HTTP, then calls MCP at that explicit version with DEC, associations and value domain.
It compares each component's own ID/version and derivation code, source, explicit primary
boolean and integer order. Names and dates are not comparison keys; a mismatch shows both
dateModified values because content may change in place. The independent reference GET makes
one attempt; the harness does not repeat the MCP call or comparison after a failure or mismatch.
The server's normal upstream retry policy still applies within that MCP call.
Fixture mode uses the separately recorded explicit-version response; the older
latest and v1 recordings remain intact. A hosted server's rejected caDSR credential remains a
credential failure; this anonymous reference request adds no credential or fallback.

The live scalar check uses each DataElement field's `nullable` flag in `spec/records.yaml`;
nullable metadata is text or null when supplied, and optional omitted metadata stays absent. Identity, version and status
fields remain nonblank text. A null metadata field means the producing answer supplied none;
search and match results are not backfilled from a separate detail lookup.

A test that passes against fixtures and fails live means a fixture is wrong (corrected by
re-recording, under change control) or the live service has changed: both are findings. One
that passes live and fails against fixtures means the server behaves differently against
different upstreams: a defect.

A fixture run may begin with one operator-supplied prepare command, `NCI_SI_ACCEPTANCE_PREPARE`, a
shell command line run once, before the first test that starts a server, in the server's
environment against the ordinary fixtures: the same settings
the server gets, its upstream base URLs and a fresh `NCI_SI_DATA_DIR`. The file that
`NCI_SI_ACCEPTANCE_INDEX_CODES` names holds the index set, one code per line: every concept the
fixture set records at an include that holds its summary. Every server then
starts from a copy of that data directory. A test marked `prepared` needs it and is NOT RUN
without the command; a command that fails, or whose requests find no fixture, fails every
dependent test with its reason. Other tests continue, including on parallel workers.
For this server it builds the interim NCIt index, in under a second and under a MB with the
default hashing embedder:

    NCI_SI_ACCEPTANCE_PREPARE='nci-si-mcp index-sample $(cat "$NCI_SI_ACCEPTANCE_INDEX_CODES")'

Run it only through the suite: the base URLs it is given name each surface of the fixture
server (`NCI_SI_EVS_BASE_URL` ends in `/evs`, to which the server adds `/api/v1/…` as it does to
the production host), and a command pointed at the fixture server's bare address finds no
fixture. Every setting the suite gives a server, and its format, is in the specification's §5
([`spec/acceptance.md`](../spec/acceptance.md)).

In fixture mode a test fails when one of its upstream requests found no fixture, and so does a
server whose requests while it starts found none: the server may treat the refusal as an outage
and still answer plausibly. A test that provokes such requests on purpose is marked
`unmatched_upstream`. Unmatched preparation and startup requests, including remote startup,
remain `no_fixture` outcomes with the missing requests listed, not ordinary `failed` outcomes.

## Remote server

For this repository's server, `pdm run acceptance-http` supplies the local fixture server
settings, prepared index and restart hook automatically; see [transport details](../docs/transport.md).
Session-release cases marked `mcp_session` use handshake mode so they run in an actual MCP
session. Other cases negotiate normally: native list/resource cache fields belong to the
2026 protocol and are intentionally removed by the SDK on older protocols.

The platform's server is remote: `NCI_SI_ACCEPTANCE_URL` names its streamable-HTTP endpoint in
place of `NCI_SI_ACCEPTANCE_SERVER` (naming both stops the run). The harness starts nothing and
cannot set the environment of a process it did not start, so the operator does three things the
harness otherwise does: sets the server's upstream, changes its state for the tests that need a
server of their own, and prepares its index. Run it in one process: xdist workers together with
`NCI_SI_ACCEPTANCE_URL` are a usage error. The report records each run's transport, `stdio` or
`streamable-http`, in the JSON and in the rendered report.

The settings of a run, the probe that precedes the first test, the state hook's contract and the
index of a remote server are specified once, in the specification's §5
([Settings of a run](../spec/acceptance.md#settings-of-a-run) and the text after it); this README
does not repeat them. What the operator needs at a glance: set the fixture server's base URLs and
`NCI_SI_UPSTREAM_MODE` as the harness prints them, give the state hook for the tests that need a
server of their own (without it they count as not run, so their tool is never PASS), and prepare the
index and declare it with `NCI_SI_ACCEPTANCE_PREPARED=1`. A failed initial probe or state change
fails every dependent test; a failed scenario state change fails the test that needed it.
The endpoint wait ends at once on an HTTP 401 or 403. Failures retain their diagnostic reason,
with credentials withheld, rather than aborting the run or counting as `not_live`.

The fixture server's `/_log` endpoint returns recorded request headers, including credential
headers. Keep it local and use test credentials only; treat exported request logs as sensitive.

## The report

`--report` writes one JSON report per run; `nci_si_acceptance.report` renders it as the per-tool
table of the specification's §5. A tool is PASS, FAIL (a failed gate fails every tool), NO
FIXTURE (a request lacked a fixture: a question for the fixture set), INCOMPLETE (the tests that
ran passed but some could not run, or a gate could not run, shown as "gates not run": a
hardening candidate), NOT IMPLEMENTED, NOT RUN or NO TESTS; the module docstring defines each,
and each row counts the tests passed, failed, without a fixture and not run.
The rendered report opens with its title and the identity of the suite: the suite version, the
fixture-set version (the pinned release and the date of the latest recording) and the suite
digest. The title is ACCEPTANCE REPORT only when that digest is in `approved.yaml`; otherwise it
is MODIFIED, which is every report until the furnished tag. The JSON report carries the same
identity. Below the table it names the gates that failed and those that did not run, and gives the size
of the tools/list result in bytes, which every client session reads.
Combined with a live report, a tool that
passes against fixtures but fails live is PASS (fixture only) only when every failing live test
has a documented upstream limitation (`--limitations`, YAML of test id to requirement).

Each fresh report records `run.exit_status`, `run.selected` (the selected test count, agreed
by workers under xdist), `run.finished` (tests whose execution, including teardown, finished),
and `run.worker_crashes`. The shared report loader requires status 0 or 1, no worker crashes,
and equal selected, finished and recorded-outcome counts. Complete failing or skipped runs
are valid evidence; an early stop or recovered worker crash is not. Rendering, ratchet check
and update, live drift checking and the HTTP acceptance runner all use this guard. Reports
without these fields must be rerun. `--check-complete fixture` (or `live`) validates without
rendering; `--live live.json --drift` reports fixture passes that fail live and exits nonzero.
Combined rendering and drift require both reports to select the same test IDs from the same
suite; a complete subset cannot stand in for missing live evidence.

The frozen Phase 5 snapshots are the archival exception: `scripts/upstream_requirements.py`
checks their identical, nonempty 944-test sets and suite digest itself. Their historical JSON
is not given invented completion fields. This exception lasts for those recorded snapshots;
new snapshots must come from guarded runs and carry completion fields.

## CI: the ratchet on expected outcomes

The `acceptance` job of `.github/workflows/ci.yml` runs the suite in fixture mode against the
server built from the checkout (`pdm run acceptance -n 4 --dist worksteal --report=fixture.json` with the prepare
step above), beside the `test` and `selftest` jobs and depending on none of them. Pytest's exit
status 0 and 1 are eligible, but the report must also prove completion; any other status or a
missing or incomplete report fails the job. The verdict
is the comparison with [`expected/fixture.json`](expected/fixture.json), which maps the id of
every test to its outcome (`passed`, `failed`, `no_fixture`, `skipped`, `not_implemented`,
`not_live`: the report's own vocabulary) and holds nothing else, so a reworded failure is not a
change. The job fails on any test with another outcome, any test the report lacks and any test
the expected outcomes lack, and writes the differences to its summary as a table of test,
expected and actual outcome. It stays green while tools are NOT IMPLEMENTED or FAIL, and fails
when a test of a tool that still fails stops passing, or starts to pass without anyone saying so.

A change that moves outcomes on purpose (a tool implemented, a test added or corrected) updates
the expected outcomes in the same pull request: run the suite as the job does, then
`pdm run acceptance-expected update acceptance/fixture.json`, and `pdm run acceptance-status` for
the functional-status tables, which are generated from the expected outcomes and kept current by a
self-test. The diff of `expected/fixture.json` is what the review reads.

On a pull request, the generated report must first equal the expected map in the checkout. CI then
extracts the base expected map from the event's immutable base commit. A test that is `passed` in
that base map but is non-passing in the current map, including a removed test omitted by both the
current map and report, requires an exact `expected/regressions.yaml` record:

```yaml
version: 1
approvals:
  - base_sha: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
    test: acceptance/tests/test_example.py::test_example
    before: passed
    after: failed
    reason: The reviewed reason for accepting this regression.
    reference: https://github.com/CBIIT/nci-si-mcp/issues/123#issuecomment-456
```

The root has exactly `version` and `approvals`; each approval has exactly the six keys above.
`version` is integer `1`, `before` is `passed`, and `after` is a non-passing report outcome or
`absent` for a removed test. Duplicate YAML keys, aliases and merge keys are invalid. A record for
the current base that matches no regression fails as stale; a record for an older base is inert.
The reference records review but does not authenticate it. Manual CI dispatch requires a full
base commit that is an ancestor of the dispatched head; pushes to `main` retain the current
equality check after the pull request has run the base comparison.

The job's summary holds the run's duration (a warning at 7 minutes of the 10 allowed: shard the
run before it grows into the limit) and the per-tool report; the artifact `acceptance-fixture`
keeps `fixture.json` and the rendered report for 30 days. A run on workers writes the report a
serial run writes: the controller process collects each test's tool, gate and outcome from the
reports its workers forward.

## The live workflow

Anonymous live selection includes stable contracts, refusals, bounds, provenance, caching
and cursor checks. Tests comparing recorded content, requiring fixture request observation,
synthetic identity, crafted capabilities or guaranteed truncation triggers stay fixture-only;
scenario parameters remain excluded even when a sibling is live-capable. Credentialed
successful calls retain their scenarios.

For #366, **229 additional cases** passed in the first hosted run
[40255800463](https://github.com/CBIIT/nci-si-mcp/actions/runs/40255800463), whose full result
was **276 passed, 2 failed, 708 not_live, 5 skipped**. Its two no-index cases were
misclassified: local verification had no prepare command, while the hosted live job prepares
an index and does not supply an index-less server. They remain fixture-only (category d).
The first correction was confirmed by green hosted run
[40257975716](https://github.com/CBIIT/nci-si-mcp/actions/runs/40257975716):
**276 passed, 0 failed, 710 not_live, 5 skipped**, including **29 search_concepts passes**.
Follow-up review found two more `none` parameters inheriting a function-level live marker.
Only their `as-prepared` siblings remain live-capable; the index-less variants retain their
fixture assertions. This leaves **227 net additional live-capable cases** and expects
**274 passed, 0 failed, 712 not_live, 5 skipped** (991 total), including
**27 search_concepts passes**. Hosted confirmation
[40263866032](https://github.com/CBIIT/nci-si-mcp/actions/runs/40263866032) subsequently
measured **274 passed, 0 failed, 712 not_live and 5 skipped**, with all 991 cases finished.
The failed caDSR outage run 40261513919 remains retained separately; no assertion or live
classification was changed to hide that failure.
All 991 fixture outcomes remain passing. The stored scheduled-main observation stays separate: its
19 October **46 / 0 / 920** counts describe an older collection.

`Acceptance (live)` (`.github/workflows/acceptance-live.yml`) remains disabled. Its retained
schedule/dispatch configuration and credential forwarding are not a current execution plan.
A reviewed workflow transition and the #448 prerequisites are required before enabling any
live run. Offline fixture verification and stored historical live evidence remain separate;
no scheduled refresh or automatic wake-up is implied.

### Functional-status evidence (local rendering)

`pdm run acceptance-status` renders fixture status and the latest **stored scheduled main**
live observation; `pdm run acceptance-status --check` fails without writing when it is stale.
The default destination is [functional status](../docs/functional-status.md). The existing
`--readme PATH` option selects another Markdown file with the same markers; generated source
links resolve from that destination. Text outside the markers is preserved.
Neither command nor the documentation build contacts upstream services. The live table counts
the native test rows, including a separate unattributed-gates row, and keeps failures visible.
A partial latest run is labelled incomplete; it is never replaced by an older green run.

To refresh, a maintainer reviews the latest `schedule` run on `main` of `Acceptance (live)`
(including failed runs), downloads its `acceptance-live` artifact from GitHub, and copies the
original `live.json` bytes to `docs/evidence/acceptance-live/latest.json`. Update `latest.yaml`
with its SHA-256, run and artifact IDs, event, branch, measured commit SHA and run start date
(UTC ISO timestamp). Then run `pdm run acceptance-status`, review both the evidence and
functional-status diff, and run `pdm run acceptance-status --check`. No workflow publishes this
automatically. This describes retention of an authorized result, not permission to launch a run.
Missing or invalid evidence is an error, not permission to retain a previous green observation.

New native reports optionally carry `not_live_reason` on `not_live` test rows only:
`scenario` for a nonempty crafted scenario (takes precedence), otherwise `not_live_capable`
for a test lacking that marker. The winning test phase owns this evidence. These values do not
change outcomes, the fixture ratchet or drift rules. Historical absence stays absent and is
displayed as **unrecorded**; present null, unknown values and incompatible outcomes are rejected.
The stored 19 October run has 920 such unrecorded reasons, never joined to current markers.

For recorded `not_live_capable` rows, context comes from the existing upstream catalogue and
specification: owners of `cadsr-access` requirements indicate credentials, then other catalogue
requests indicate capability gaps (owners and explicit tool basis names), otherwise **not yet
probed by this live acceptance run**. Scenario precedes all context. These are blocker contexts,
not proof that every skipped case attempted content retrieval or that upstream was never probed.
The older [Phase 5 evidence](../docs/evidence/phase-5/acceptance-live.json) stays dated evidence.

## Layout

`src/nci_si_acceptance/` holds the harness: `client.py` starts the server or connects to it,
`remote.py` probes a remote server and runs the operator's state hook, `tools.py` calls the
required tools, `fixture_server.py` serves the fixtures (its docstring documents the format),
`report.py` writes and renders the per-tool report, `suite_identity.py` digests the suite and
decides whether a report is an acceptance report, `spec.py` reads the specification in
`../spec/` (the required tools among it), `requirements.py` checks the tests' citations of its
requirements, `document.py` renders it as `../docs/specification.md`, and `suite.py` holds the
rules of a run. `concepts.py` composes EVS concept answers from one recording per
concept, `record.py` records the set from live,
`craft.py` crafts, in memory, the scenarios EVS does not produce on demand, `register.py` writes the
register of request forms (`request-forms/`), `expected.py` compares a report with the expected
outcomes (`expected/fixture.json`) and rewrites them, and `status.py` writes the functional-status
table from them and the stored native live evidence. `results.py` holds what the tests read from a tool's result and from the upstream
request log, and `stories.py` renders the domain-readable story of every test from `stories.yaml`.
`fixtures/` holds the fixtures ([fixtures/README.md](fixtures/README.md)), `tests/` the suite,
`selftests/` the tests of the harness itself.

The suite's version is the repository's nearest `vX.Y.Z` tag, derived at install time as the server's
is; the fixture set is versioned apart, by its pinned release and recording dates.

## Change control

The NCI SI MCP project coordinator is the code owner of `spec/` and `acceptance/`. From the
furnished tag, a change to `spec/` or to the suite needs the written approval of the branch chief
or a delegate ([spec/acceptance.md](../spec/acceptance.md)); a report from a changed suite renders
MODIFIED, because only the digests of approved releases, listed in
[approved.yaml](approved.yaml), make an acceptance report; and [CHANGELOG.md](CHANGELOG.md)
records each approved change with the requirement it serves.

The digest covers `tests/`, `fixtures/` with the manifest, `request-forms/`, `src/`, `pyproject.toml`
(the pytest configuration and the dependency pins decide what runs) and `../spec/`; it leaves out
the self-tests, `README.md`, `CHANGELOG.md` (a record that carries release digests), caches, editor
and system litter, `approved.yaml` and the server under test, so one approved suite attests any
server (`src/nci_si_acceptance/suite_identity.py` defines the set). A symbolic link among the
digested files, or a root that lacks any of them, is an error, never a partial digest.

What the check is and is not:

- It is a tripwire, not tamper-proofing. `approved.yaml` changes by review by the NCI SI MCP
  project coordinator, and the report's identity block is self-stated.
- Approval is attested on a clean checkout, since untracked files inside the digested paths count.
- Reports are written outside the digested paths (as `--report=fixture.json` in `acceptance/`
  already is); the digest is taken when the run starts, from the checkout the run is in, and a run
  against a harness installed from another checkout is refused.
- The suite version in the report comes from install metadata (the nearest tag), so run
  `pdm install` after a tag. The digest alone decides approval.

## Secured caller fixtures

The additive X-25–X-28 cases use `NCI_SI_ACCEPTANCE_SECURITY_SERVER`, an operator-supplied
command that starts a loopback HTTP server. This repository's CI and HTTP gate supply `scripts/permissions_fixture.py`. For a
local fixture run set `NCI_SI_ACCEPTANCE_SECURITY_SERVER='python ../scripts/permissions_fixture.py'`
alongside the preparation environment. Successors provide their own adapter;
the suite imports no server code. Without an adapter these cases explicitly report NOT RUN.
They are fixture-only, not evidence of production identity or revocation behavior.

The adapter receives the normal fixture upstream environment plus `NCI_SI_TEST_HTTP_PORT`
and `NCI_SI_TEST_AUTHORITY_FILE`. The JSON file contains generated `tokens` keyed by fixture
actor and `policies` containing each actor's `capabilities` (tool names) and Unix-second
`expires_at`. Missing policy denies. The adapter authenticates tokens, reloads policy on each
request, binds only loopback, and exposes `/mcp`, `/health` and `/ready`. A request to `/mcp`
without a valid token is answered 401 with `Cache-Control: no-store` and a `WWW-Authenticate`
header, and calls to `/mcp` that return content or discovery (X-27) carry `Cache-Control: no-store`.
`/health` and `/ready` answer `{"status": "ok"}` and `{"status": "ready"}` with `no-store`, and a
request whose `Host` is not the adapter's own is answered 421 (X-28). The harness owns and reaps the
process; test tokens are never production credentials and must not appear in logs. This is a
test-adapter contract, not a proposed production policy format or identity-provider choice.
