## 5. Acceptance

The acceptance suite (`acceptance/`, [README](../acceptance/README.md)) tests the requirements
against a server: in fixture mode against the recorded and crafted upstream answers of
`acceptance/fixtures/`, and in live mode for the tests marked live-capable. A run gives each
required tool one outcome:

| Outcome | Meaning |
|---|---|
| PASS | Every gate, every cross-cutting test and every test of the tool passes, in fixture mode and, for the live-capable tests, in live mode |
| PASS (fixture only) | Passes in fixture mode; each live failure is a test with a documented upstream limitation, named per test |
| FAIL | A test fails in fixture mode, or a live-capable test fails live without a documented upstream limitation |
| INCOMPLETE | The tests that ran passed, but others could not run for want of a capability, or a gate could not run: a hardening candidate, never PASS |
| NO FIXTURE | An upstream request found no fixture; the report names it |
| NOT RUN | No test of the tool ran, for example in a live run or without the operator's prepare step |
| NO TESTS | The suite has no test for the tool: a defect of the suite |
| NOT IMPLEMENTED | The server does not list the required name |

An upstream limitation excuses a failing live test only test by test, each with its
requirement named; one known limitation does not excuse another live failure of the same tool,
and each such limitation is an entry in the upstream requirements package. A
gate (a test of a P or X-25–X-28 requirement) that fails live fails every tool, as a failing live test does. A module is accepted when
every one of its tools is PASS or PASS (fixture only) and the gates pass; INCOMPLETE, NOT RUN, NO FIXTURE, NO TESTS and NOT IMPLEMENTED are not accepted.
Every report names the suite version, the fixture-set version,
a digest over the suite (its tests, fixtures, request forms, harness code and configuration, and the specification data, but not its self-tests, documentation or the server under test), and the tools whose tests have never run against an implementation.

### Settings the suite gives a server

The suite starts each server under test with these settings, and with no other `NCI_SIP_*`
setting of the operator's environment. A server is configured by them: in fixture mode every
upstream base URL names the fixture server, and a scenario that needs a credential, a short
timeout or a log level sets it.

| Setting | When the suite sets it | Format |
|---|---|---|
| `NCI_SIP_UPSTREAM_MODE` | Always | `fixture` or `live` |
| `NCI_SIP_DATA_DIR` | Always | A directory of the server's own, fresh for each server; a copy of the prepare command's where one is given |
| `NCI_SIP_EVS_BASE_URL`, `NCI_SIP_EVS_FHIR_BASE_URL`, `NCI_SIP_CADSR_BASE_URL`, `NCI_SIP_CADSR_FTP_URL`, `NCI_SIP_SSIS_SPARQL_URL` | Always | Fixture bases or selected live origins; blocked live surfaces use an owned refusal URL. A base URL, to which the server adds the platform's own paths as it does to the production one (`…/evs` + `/api/v1/…`; `…/cadsr` + `/NCIAPI/1.0/api/…`; `…/ssis-sparql` + `/sparql`) |
| `NCI_SIP_EVS_LICENSE_KEY` | Live mode, from the operator; the `license/restricted` and `license/attributed` scenarios | The key, sent as the `X-EVSRESTAPI-License-Key` header on EVS requests |
| `NCI_SIP_CADSR_CREDENTIAL` | Fixture `cadsr/credentialed` and `cadsr/match-timeout` scenarios only; refused live | `user:password`, sent as HTTP Basic authentication (`Authorization: Basic` and its base64), as every caDSR contract declares |
| `NCI_SIP_TIMEOUT_SECONDS` | The `upstream/unavailable` scenario | Seconds an upstream request may take |
| `NCI_SIP_MATCH_TIMEOUT_SECONDS` | The `cadsr/match-timeout` scenario | Seconds a caDSR match request may take |
| `NCI_SIP_LOG_LEVEL` | The `license/restricted` scenario | `DEBUG`, so that a secret logged as a detail shows |
| `NCI_SIP_ACCEPTANCE_INDEX_CODES` | The prepare command only | A file of the concept codes to index, one per line |
| `NCI_SIP_TEST_HTTP_PORT` | The secured fixture adapter only | The loopback port the adapter listens on, chosen by the suite |
| `NCI_SIP_TEST_AUTHORITY_FILE` | The secured fixture adapter only | The path of the JSON file of generated test tokens and each actor's policy, which the adapter rereads on every request |

### Settings of a run

The operator selects the server under test, and the run, with these settings of the harness's own
environment. A server the harness starts is given none of them. The state-change hook (below) is
given all of them but the credential, besides the fixture settings and a scenario set's.

| Setting | Format |
|---|---|
| `NCI_SIP_ACCEPTANCE_MODE` | `fixture` (default) or local managed `live` |
| `NCI_SIP_ACCEPTANCE_ENVIRONMENT` | Required live: `evs-only`; incompatible with fixture/remote mode. Policy and provenance: [managed live environments](../acceptance/README.md#managed-live-environments) |
| `NCI_SIP_ACCEPTANCE_REGISTRY_RELEASE` | `optional` (default) or `required`; other values are rejected |
| `NCI_SIP_ACCEPTANCE_SERVER` | Fixture command; live accepts only `nci-sip-mcp serve` and uses the furnished isolated launcher |
| `NCI_SIP_ACCEPTANCE_PROFILE` | Fixture: `evs`, `cadsr` or `unified` (default); live: `unified` only |
| `NCI_SIP_ACCEPTANCE_PREPARE` | Fixture shell command; live only `nci-sip-mcp index-sample`, with harness-owned validated argv |
| `NCI_SIP_ACCEPTANCE_URL` | The streamable-HTTP endpoint of a remote server, in place of `NCI_SIP_ACCEPTANCE_SERVER`; naming both is a usage error |
| `NCI_SIP_ACCEPTANCE_AUTHORIZATION` | The `Authorization` header sent on every request to a remote server: a credential, in no output of the harness |
| `NCI_SIP_ACCEPTANCE_FIXTURE_BIND` | `HOST` or `HOST:PORT` the fixture server listens on (default `127.0.0.1`, any port) |
| `NCI_SIP_ACCEPTANCE_FIXTURE_URL` | The base URL a remote server reaches the fixture server by |
| `NCI_SIP_ACCEPTANCE_STATE_HOOK` | The operator's command that changes the state of a remote server: it applies the settings in its environment and forgets every upstream answer cached so far |
| `NCI_SIP_ACCEPTANCE_STATE_HOOK_TIMEOUT` | Seconds the hook may take to return, and again the endpoint to answer after it (default 60) |
| `NCI_SIP_ACCEPTANCE_PREPARED` | `1`: the operator has prepared the index of a remote server |
| `NCI_SIP_ACCEPTANCE_SECURITY_SERVER` | The command that starts the secured fixture adapter, a loopback HTTP server, for the X-25 to X-28 cases; without it they report NOT RUN |

### Interim registry-release selection

The sponsor's decision on `registryRelease`, `resolve_registry_release` and
`get_release_alignment` is pending. The harness-only setting above changes no server setting.
`optional` suspends exactly five retained checks in `test_cadsr.py`: export-date discovery,
published-release discovery, published-pin transport/attribution, and the two registry cursor
binding cases. Each is an ordinary skip, never `not_live` or a pass, with this reason:
`registry-release requirement suspended pending sponsor decision (NCI_SIP_ACCEPTANCE_REGISTRY_RELEASE=optional)`.
`required` runs their existing assertions. Both modes are exercised by deterministic harness
self-tests in the existing CI selftest jobs; required detects an unpinned response to a pin.

Unpublished/unlisted explicit-pin refusals, registry-only provenance, alignment, Shared SI
NCIt graph verification and EVS/GDC pinning remain active in both modes. Unsupported explicit
pins still return `release_not_available`; a listed pin without transport is
`capability_unavailable`. No identifier is invented or export date attached to newer API content.
Unpinned caDSR content already works without registry discovery. The 19 October scheduled run
39214306172 and 25 October run 40178930466 show all 26 inventoried registry/alignment candidates
as `not_live`, not server release rejections. Measured newly unblocked live tests: none.
These historical results are retained; no new live probe is part of this change.

**Delivered (#360): cited-item metadata.** CDEs, DEC/object-class/property identities,
value meanings, permissible values, classification schemes and scheme items retain their
own supplied `dateModified`, `beginDate` and `endDate`. PVs have no own version; included
VD and representation metadata remain upstream pass-throughs. Optional
`include: ["dataElementConcept"]` exposes DEC/object-class/property identities and dates,
without concepts. API concept associations also include representation terms and retain
source order and duplicates, explicit Yes/No primary flags and supplied integer order;
order never determines primary. Omitted scalar fields, including CRDC names, stay absent;
explicit null stays null. Recording `data-element-2200604.json` supplies the citation
evidence; exact reproduction of in-place edits still requires that day's export.

**Pending sponsor proposal, not implemented:** per-reference NCIt alignment would check
every object-class, property, representation and value-meaning concept code for existence
and active status in the pinned NCIt release, and report copied-name differences. Current
alignment compares content-state dates.

The upstream request is twelve months of retained dated caDSR exports, not a server archive
or synthetic release. Exact reproduction of set-level answers or items edited in place still
needs that day's export. See [the caDSR package](../docs/upstream/cadsr.md#cadsr-export-retention).

A remote server is tested over streamable HTTP, and the report records the transport of each run
(`stdio` or `streamable-http`). The harness cannot set a remote server's environment: the
operator sets the fixture server's base URLs and `NCI_SIP_UPSTREAM_MODE`, which the harness prints
at the start of a fixture-mode run. Before the first test the harness requires that the server
answers and, against fixtures, that its `resolve_release` call reaches the fixture server;
otherwise every dependent test fails with the probe's reason. For a profile without `resolve_release` (caDSR) the probe only calls
`tools/list`. The state-change hook is called with the name of a scenario set (in `NCI_SIP_ACCEPTANCE_SCENARIOS`,
the scenarios separated by commas, empty for none) and its settings, and its contract is: apply these settings and forget every upstream answer cached so far. A
restart is the simplest implementation and satisfies it; an operator whose server can flush its
cache and reread its settings may do that instead. It is called once for each distinct scenario
set, before each test that needs a server of its own (one that no earlier call can have filled a
cache of), before the probe, and at the end without settings. A test that needs such a state runs
when the hook gives it; a failed state change fails that test with its reason. Without a hook,
it is not run, and its tool is never PASS. A test that needs a
server without the index (`unprepared`) is always skipped against a remote server, which cannot
be made one, and counts as not run. The harness never prepares a remote server: the operator
indexes the concepts that `pdm run acceptance-index-codes` prints, one per line, and declares the
server prepared. A server declared prepared holds exactly that set: the semantic tests assert
`totalKnown` equal to its size.

Details of a remote run. Against fixtures the probe stops with "the server under test does not
reach the fixture server" when the fixture server's log shows no request, as it does for a server
that answers from a cache filled before the run (change its state first, or give the hook, which
then runs before the probe; what the server asks while the hook runs counts); in live mode it stops
with "the server under test does not answer". When the fixture server listens on `0.0.0.0` and
`NCI_SIP_ACCEPTANCE_FIXTURE_URL` is not set, the harness warns that the URLs it announces may not
reach it from where the server runs. Set `NCI_SIP_ACCEPTANCE_FIXTURE_BIND` on a fixed port so that
the URLs it prints, which the operator sets on the server, do not change between runs. The hook runs with the harness's environment less the
credential and less every `NCI_SIP_*` setting that is not an `NCI_SIP_ACCEPTANCE_*` one, plus the
fixture settings, `NCI_SIP_ACCEPTANCE_SCENARIOS` and the set's settings (none for an `own_server`
test). The tests on the server as the operator started it run first, then the `own_server` tests,
then each scenario set's. Without the hook the tests that need a server of their own are skipped as
"needs a server of its own (NCI_SIP_ACCEPTANCE_STATE_HOOK)"; they count as not run, so a tool whose
scenario tests did not run is never PASS: it is NOT RUN, or INCOMPLETE where its other tests
passed. Tests marked `prepared` are NOT RUN until the operator declares the index with
`NCI_SIP_ACCEPTANCE_PREPARED=1`; naming `NCI_SIP_ACCEPTANCE_PREPARE` against a remote server is a
usage error. The hook must return once the old state is gone (for a restart: once the
old instance has stopped), with the output of any process it leaves running redirected; the harness
then waits for the endpoint, up to the timeout, and stops at once on an HTTP 401 or 403. The
`Authorization` credential is withheld from the output of a failing test, with the token after its
scheme, and is not given to the hook. The harness cannot read a remote server's standard error or
data directory, so the checks that a secret is in neither (X-12) cover what the server returns.

The baseline assessment reads the outcomes of a run against the furnished server:
a tool that passes is a reuse candidate, one that fails or is INCOMPLETE a hardening candidate,
and one NOT IMPLEMENTED new development.

The suite does not test response time and throughput (the benchmark), the ranking quality of
semantic search (the retrieval evaluation set), security controls (the implementing team's
security tests), the platform APIs themselves (the conformance suite), or operator procedures
such as building, activating and rolling back the index (the implementing team's integration
tests and the deployment guide). It tests what each of these must show at the tool surface: a structured
timeout error, the index release in provenance, correlation, and no secret in a result or log.

Changes to this specification, the suite, the fixture set and the request forms follow a
versioned change request, an impact assessment and the written approval of the branch chief or
a delegate, from the furnished tag. The request forms, prompt templates and resource definitions
are initial versions for the EVS, caDSR and Shared SI teams to refine through that
record. For the Shared SI Service's SPARQL endpoint the request form prescribes the query text,
matched with runs of whitespace collapsed; a team may propose another form through the register,
as for every form.
The suite and the fixture set are versioned independently. A team implementing the server may
propose a test, but may not substitute its own tests for the suite as the basis of acceptance.
Every suite test cites the requirements it enforces, and at the furnished tag every requirement
is cited by one; a report whose digest is not that of an approved release reads MODIFIED;
changes under `spec/` and `acceptance/` need a code owner's approval; and the change log records
the approval and the requirement each change serves.
