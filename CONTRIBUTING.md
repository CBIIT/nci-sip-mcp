# Contributing

How to work on this repository. What it is, and its status, is in [README.md](README.md); how to
install and run the server is in [QUICKSTART.md](QUICKSTART.md); how it is built is in
[ARCHITECTURE.md](ARCHITECTURE.md). Update the documents and diagrams with each behaviour change:
the [documentation website](docs/documentation-site.md) is built from these sources (pinned build,
local preview, reviewed page allowlist, source and version labels).
Everyone taking part follows the [code of conduct](CODE_OF_CONDUCT.md).

## Set up

The [platforms and prerequisites](QUICKSTART.md#platforms-and-prerequisites) come first: Python 3.14 or newer and PDM.

```bash
pdm install                    # .venv from pdm.lock, with the test and lint tools and the server and index extras
pdm run pre-commit install     # install the commit gates and the full-suite push gate
```

Run the install command again in a clone whose hooks were installed before the push gate was
added. A commit runs the file and project gates. A push runs `pdm run test` and therefore takes
about as long as that command.

The Gitleaks developer hook needs a Go compiler: pre-commit uses Go on `PATH` or downloads
a toolchain. On managed/VPN macOS, an approved local installation (for example `brew install go`)
avoids the Python downloader. Verify that the following prints `system`, then rerun all hooks:

```bash
pdm run python -c "from pre_commit.languages.golang import get_default_version; print(get_default_version())"
pdm run pre-commit run --all-files
```

If certificate verification fails, diagnose the trust chain with IT; never disable verification
or skip the hook. This is a developer bootstrap option, not a server-installer prerequisite or
a diagnosis of the underlying TLS failure.

## Everyday commands

| Command | What it does |
| --- | --- |
| `pdm run test` | The whole suite, with the coverage minimum |
| `pdm run pytest tests/test_index.py -k name` | A selection of tests, without the minimum |
| `pdm run lint` | Ruff and basedpyright: the fast check while you work |
| `pdm run mutation-gate` | Kill every changed-site Python mutant before a PR; `--paths src/package/module.py` checks a whole module; review survivors with the printed AST keys in `scripts/validation/mutation-allowlist.yaml` |
| `pdm run fmt` | Format with Ruff |
| `pdm run lock` | Refresh every Python group to the newest version admitted by the five-day release-age policy |
| `pdm run github-setup` | Create missing workflow credential names with safe placeholders; never overwrite existing names |
| `pdm run pre-commit run --all-files` | Every commit-stage hook, as the CI `quality` job runs them |
| `pdm run acceptance` | The acceptance suite against the server ([acceptance/README.md](acceptance/README.md)) |
| `pdm run acceptance-http` | Real HTTP fixture suite, managed restarts and explicit remote-contract skips ([transport details](docs/transport.md)) |
| `pdm run python -m scripts.benchmark --output tmp/benchmark.json` | Representative MCP measurements with actual outbound counts ([method and evidence](docs/benchmark.md)) |
| `pdm run python scripts/upstream_requirements.py` | Regenerate the [upstream requirements packages](docs/upstream/README.md) from the catalogue and measured reports; `--check` verifies them without writing |
| `pdm run acceptance -n 4 --dist worksteal --report=fixture.json` | The acceptance suite on four workers, with the report CI compares ([the ratchet](acceptance/README.md#ci-the-ratchet-on-expected-outcomes)) |
| `pdm run acceptance-expected check acceptance/fixture.json` | The report against `acceptance/expected/fixture.json`, test by test |
| `pdm run acceptance-status` | Regenerate the README status from fixture expected outcomes and stored scheduled-live evidence |
| `pdm run acceptance-selftest` | The acceptance harness's own tests, on four workers |
| `pdm run acceptance-index-codes` | Print the sample concept codes the suite indexes before a run |
| `pdm run acceptance-record` | Re-record `acceptance/fixtures/recorded/` from the live services, under change control |
| `pdm run acceptance-craft` | Record the digest of the crafted fixtures in the manifest; `--out DIR` writes them to read |
| `pdm run acceptance-register` | Regenerate the request-forms register (`acceptance/request-forms/`) |
| `pdm run acceptance-stories` | Regenerate [docs/behavioural-tests.md](docs/behavioural-tests.md) from the suite; `--check` verifies it |
| `pdm run spec-render` | Regenerate [docs/specification.md](docs/specification.md) from `spec/`, after any change there |
| `pdm run quickstart-tools` | Regenerate the table of tools in [QUICKSTART.md](QUICKSTART.md) from `spec/tools.yaml`; `--check` verifies it |
| `pandoc -f gfm docs/specification.md -o specification.docx` | The Word copy of the specification, a build product: never committed or edited |

CI runs the complete mutation catalogue as three stable-key hash partitions, each with two
workers and a 300-minute timeout: `--shard 1/3 -n 2`, `--shard 2/3 -n 2` and
`--shard 3/3 -n 2`. All three must pass; a job timeout fails. Shard JSON lines report
full/selected counts and a final outcome/kill-kind summary with elapsed seconds. An empty
partition reports `EMPTY`; allowlist freshness is checked before partitioning. CI mutates the
default PR merge checkout relative to the event's base SHA, just as the suite producers test it.
The gate's nested self-tests require the larger timeout; current timing evidence belongs in
the issue and pull request, not a standing runtime promise.

CI's existing unit, harness self-test and prepared-fixture jobs collect per-test and child-phase
coverage once. Each job's generated config (`mutation_coverage configure`) names that job's own
absolute data file (`.coverage` at the root for unit, `acceptance/.coverage` for the harness
domains), so a child started from another working directory writes beside it and is combined.
They export one-day fragments bound to the checked-out merge commit and tracked
input bytes; mutation shards wait for those producers and the coverage floors, download their
same-run fragments and pass `--coverage-dir`. A missing, incomplete, malformed or mismatched
import fails as `INFRA`, even for an empty catalogue; it never silently reruns a baseline.
A fixture execution must exit zero to export a baseline, even if its ratchet expects failures.
Collection/setup/teardown attribution retains the existing conservative selection rules.

Without `--coverage-dir`, local `pdm run mutation-gate` still computes clean unit, harness
self-test and prepared-fixture baselines itself; unit remains serial. Any baseline failure
names its domain and fails the gate. The shard summary's `baseline_domains` records freshly
computed domain outcomes and seconds; imported runs leave that list empty.

Beyond the server and the suite, the repository holds the documentation site, the validation
companion and the benchmark tooling; these are their entry points.

| Command | What it does |
| --- | --- |
| `pdm run docs-build` | Build the documentation website into `tmp/docs-site` ([documentation site](docs/documentation-site.md)) |
| `pdm run portal` | Serve the local validation dashboard on loopback ([local validation](docs/local-validation.md)) |
| `pdm run operator-worker` | Run one validation job (acceptance or benchmark) the way the dashboard does, from the command line |
| `pdm run benchmark-http` | HTTP transport measurements of the served tools ([method and evidence](docs/benchmark.md)) |

The server tests are `unittest.TestCase` classes, run by pytest. They are offline:
`tests/fakes.py` supplies shared doubles, and client integration tests use local HTTP fixtures.
The acceptance suite and its self-tests use pytest directly.
The unit tests ignore the developer's `NCI_SI_*` variables (`tests/conftest.py` removes them); a
test that needs a setting sets it with `monkeypatch` or `patch.dict`.

### Explicit controlled request integration

`HttpClient(request_policy=...)` selects an explicit provider. Platform-client constructors
can instead run inside `nci_si_mcp.request_policy.using_request_policy`; each client captures
that default, and nested scopes restore their parent. Without either opt-in, transport behavior
is unchanged. The core classifies the provider's raw `Exchange` through the existing parser and
error path; terminal refusal is `upstream_unavailable`, without cache fallback or public policy
counters. I/O errors before transport entry are terminal refusals, not fictitious attempts.

A provider owns exact request validation and immutable snapshots, pre-send budget reservation,
proxy/redirect controls, bounded success/error reads and sanitization of admission/cleanup errors.
It calls `on_start` once immediately at transport entry, after other admission gates, before I/O;
the callback checks the call's existing budget/deadline. Returned exchanges hide private bytes
and headers in their representation. Core classification alone proves none of the provider's
I/O guarantees. The `on_request` observer is not an enforcement boundary.

Explicit authorization, shared accounting and owned-run supervision are described below;
the production provider remains #465. Furnished harness,
prepare, selected stdio benchmark and direct-citation adoption remains #448, recorder adoption #449. This extension
activates no caller protection or live access and changes no native/benchmark report schema.

### Explicit request authorization

`nci_si_acceptance.request_authorization` supplies frozen `RequestGrant`, `Limits`,
`Authorization` and private `RunDescriptor` values for explicitly cooperating diagnostic callers.
Grants describe one exact GET/POST method, URL, headers, body and credential destination.
`Authorization` admits only exact `RequestGrant` and `Limits` records; binding requires an exact
`Authorization`. Subclass extensions are refused locally. Binding serializes the declared
request, limit and policy fields; attachments on scalar subclasses are excluded.
Constructors snapshot tuple/list inputs into immutable values; unsupported outer containers or
header-row shapes refuse before parsing or iteration. `Authorization.binding` binds the complete
private authorization with a caller-supplied private byte key. Callers supply a reviewed nonsecret
public policy identity separately; this API validates its format and does not derive it from
credentials or the binding. Descriptor records are private IPC, including their local path;
keep them and the binding out of public reports.

These values alone enforce no budget; explicit shared accounting is described below.
Supervision/summaries are explicit opt-ins below; production transport #465 and actual caller adoption
#448/#449. No furnished caller or live access is activated.

### Explicit shared request accounting

`nci_si_acceptance.request_budget.ControlledRun` owns a private SQLite ledger for explicitly
cooperating callers. `create` starts with admission closed; the controller opens once,
authorizes participant identities and terminally closes. `attach` opens existing state only.
Existing ledger operations refuse missing or ambiguous run records before using their fields.
Creation requires a `Path`; create/attach require exact authorization/descriptor records and
refuse unsupported shapes before reading their fields.
`logical` and `attempt` reserve shared call/attempt/concurrency and response-plus-sentinel
capacity before the caller declares actual entry and reads through its lease. A reservation,
local transport entry, committed byte receipt and independently observed receiver contact
are different observations. Known bytes remain recorded after a later admission refusal;
unknown read/receipt progress retains capacity. Only known unused body allowance is refunded.
Invalid clocks or snapshot counters close shared admission. Known receipts may still commit
without waiting on an invalid clock; counters are never clamped to manufacture a valid snapshot.
Retired lease identities remain bounded by admitted attempts. Repeat release validates authority
before returning without a second refund/decrement; unknown or foreign leases still refuse.
Snapshots are private immutable typed accounting records, not public summaries or proof of upstream work.
Callers own, join and reap their children and retain needed private ledger evidence outside
managed scratch before pruning. This API supplies no automatic supervisor, owner-death
observation, production transport or furnished-caller protection; those successors remain above.

### Explicit owned-run supervision

`request_supervision.SupervisedRun.create(authorization, owned_directory)` wraps the ledger
for an explicitly cooperating diagnostic caller. Enter its context before using `.run`:
initial strict `summary.json` publication succeeds before controller admission opens.
Supervised participants refuse a heartbeat older than two seconds; the controller observes
about once per second. This is cooperative observation, not instantaneous death detection.
Existing explicitly unmanaged accounting callers retain their current behavior.

`register_child(process, participant=..., role="worker" | "service")` transfers custody of
an actual owned direct-child handle. Keep the child behind its start barrier until registration
succeeds; rejection leaves custody with the caller. Never register an arbitrary PID.
Zero-exit workers need resolved leases; failed workers and unexpected service exits fail the run.
Shutdown closes admission first, joins the observer within two seconds, then owns child cleanup
and stream closure. Each worker gets up to ten seconds to exit, followed if necessary by the
existing ten-second TERM/wait/KILL/wait leaf; service cleanup uses that leaf directly.
Resource waits are bounded by `2 + 30N` seconds for N registered children, plus ledger operations
and filesystem/OS scheduling, not a hard real-time guarantee. Intentional termination is not
reported as successful worker completion. Initiating and cleanup failures remain distinct.

The controller alone publishes initial/final summaries; the observer never writes them.
`request_summary.read_summary(path, run_id=..., policy_sha256=...)` validates the separate
exact diagnostic schema and identity. `denials: null` means counts were not measured.
Known bytes remain known after failure; unresolved receipts retain capacity and prevent
completion. Private descriptors, ledger files and raw synthetic evidence are separate from
these sanitized summaries. This API activates no production transport or furnished caller.

## Test levels

The [behavioural story catalogue](docs/behavioural-tests.md) documents every MCP acceptance
case for domain reviewers. Keep its authored narratives and function assignments in
`acceptance/stories.yaml` aligned with test changes, then run `pdm run acceptance-stories`.
`pdm run acceptance-stories --check` verifies completeness and freshness; the harness
self-tests enforce the same check in CI.

Give each behavior an owning test at the interface its caller uses. Keep component tests
for independent contracts and broader tests for distinct integration failures; do not repeat
the same claim at every layer. Describe Given/When/Then before implementation, observe the
intended failure, and remove redundant development scaffolding with evidence before landing.
The outside-in review rules are in [AGENTS.md](AGENTS.md#outside-in-test-design-and-review).
This preserves the
[test pyramid](https://testing.googleblog.com/2015/04/just-say-no-to-more-end-to-end-tests.html);
parameterized contract cases do not need an arbitrary ratio of test counts.

| Level | What exercises it here |
| --- | --- |
| Unit | Parsers, validation, ranking, truncation and report verdicts in `tests/` and `acceptance/selftests/` |
| Integration | Real SQLite migrations/builds/rollback, HTTP retries against local servers, and owned subprocess cleanup |
| API | The real MCP client in `tests/test_server.py`; HTTP authentication, sessions and health in `tests/test_transport.py` |
| End to end | Prepared fixture acceptance through the real stdio server; `pdm run acceptance-http` through a listening HTTP server; the CI container smoke through the built image, external model/index, readiness and graceful shutdown |

The companion follows the same pyramid: unit tests verify evidence projection, historical
story mapping, proposals and rendering; real SQLite/filesystem/subprocess tests verify import,
atomic history and bounded worker lifecycle; loopback HTTP tests verify controls and error
responses. The `companion containers (amd64)` CI job builds both images and exercises a real
fixture benchmark, result display, network/mount isolation and shutdown independence.
Automated Chromium journeys cover responsive search, requirement links, result filtering and
native run/cancel forms. The worker is controlled in the browser tests; separate integration
tests exercise real worker termination. See the
[assurance record](docs/government-site-assurance.md#automated-browser-regression-journeys)
for setup commands, manual checks and limitations.

Coverage includes lines, branches and Python subprocesses for the server and harness suites.
It identifies execution gaps, not missing assertions: use a targeted mutation when an
assertion's sensitivity is uncertain. Image-only checks run in the container CI job. The
fixtures verify contracts, including crafted caDSR responses; they do not establish live
upstream support. Some caDSR endpoints permit anonymous access; protected endpoints require
issued credentials. The separate live workflow checks only cases marked `live_capable` and
does not treat skipped cases as drift. It covers protocol/discovery, selected local validation
and unavailable-capability contracts, and bounded EVS, anonymous CDE and cross-domain content
checks; [the suite README](acceptance/README.md#writing-a-test) maintains the exact live scope.
Validation refusals are not evidence of working upstream retrieval; credentials alone do not
enable the fixture-only cases. This remains limited live coverage, not full content conformance.

On pull requests, `scripts/ci_images.py` skips an image job only when every changed path
is a known noninput for that image. Unknown paths, an empty or unavailable diff, and all
pushes to `main` select both image jobs. The event's full base/head change set includes
deleted paths and both sides of renames; changes confined to `tests/` can avoid both image
builds. Acceptance self-tests are packaged in Admin, and public documentation and checked
source links feed the companion site. The root README also feeds server wheel metadata.
Other CI gates are unchanged.

### Mutation gate

A single selected owning call that passed the baseline is `KILLED` when it fails,
with `kind: assertion` or `kind: error`. Multiple unittest subtest reports become one owning
call record; mixed assertion and error subtest failures are `KILLED` with `kind: error`.
A collection exception whose traceback reaches the mutated file is a `kind: import`
kill after a clean baseline, including pytest's exit 4 when precise node selection cannot
collect that node because of the mutated import. When the owners span several files, pytest
`-x` then stops collecting and reports that stop as a further failed collection of the session;
the probe marks only that exception (`Session.Failed` carrying the session's own stop reason,
read in `pytest_make_collect_report` before pytest discards `report.call`), and the kill keeps
such notices as `stop_notices`. Exit 4 without that collection evidence
stays infrastructure failure. Unrelated collection failures, `SystemExit`, setup,
teardown, internal and baseline failures stay infrastructure errors.
Evidence collection loads an immutable controller copy, even when testing the probe.
Baseline Python children inherit their parent's test-phase coverage context. Call contexts
select their exact owning tests. A span with the empty import/collection context selects all
call owners in that domain that execute any line of the same file, so consumers of constants
and decorators need not repeat imports. With no identifiable call owner, the whole domain runs.
Unrecognized contexts fail mapping. Each result names its `fallback_domains`; shard
summaries count fallback mutants overall and per domain, alongside outcomes and kill kinds.
Domains run in unit, self-test, then fixture order, stopping when a mutant is detected.
Fixture confirmation runs only tests whose baseline contexts reach the mutated lines;
`confirmation: unreached` records a survivor with no fixture reach and no fixture run.
`NO_TESTS` means no domain covered the mutated span, or a selected domain run produced no
passed owning call. It fails the gate and cannot be allowlisted; inspect selection and call
evidence before concluding the code is unreachable.
Long-lived fixture servers retain the setup context present when they start; later RPCs
cannot inherit changes to the parent's environment. Their setup-only lines therefore use
the whole-fixture fallback, counted like the other domains, rather than claiming per-RPC ownership.
`runner_seconds_by_domain`, `runner_seconds` and `runner_hours` sum measured mutant test-process
durations, excluding baseline and scratch setup; they are distinct from shard wall time.
If their aggregate exceeds reporting range, the shard SUMMARY retains counts and fallback
accounting but omits those three numeric fields and states `cost_error`; structured `INFRA`
follows and the command exits 2. Candidate evidence stays unchanged: this reporting failure
is not fabricated as a candidate-level result, and missing costs are never reported as zero.

Mutation JSON rows record monotonic `elapsed_seconds` for the whole mutant (including scratch
setup) and a `domains` list of measured test-run durations and outcomes. The scheduler stores
each source once, span/replacement edits and preselected test routes; it does not duplicate
the baseline coverage map per worker. The probe consumes its launch-only evidence destination
at configuration; only declared xdist workers receive it, so nested pytest programs cannot
append to the parent's records.

The mutation runner supports Darwin and Linux. `mutation_run.program()` verifies that its
direct child leads a new session and process group, observes exit with `waitid(WNOWAIT)` without
reaping, then owns TERM, grace, final KILL if needed and actual-parent reap. The unreaped child
reserves the original PID/PGID until cleanup finishes; a consumed or missing anchor fails the
ownership contract. A broken launch contract uses direct-child cleanup only and fails.

While the anchor is live, exit observation needs no `ps`. After early leader exit, and when
diagnosing denied group signals, inspection uses `/bin/ps -g PID` on Darwin or `/bin/ps -s PID`
on Linux, filtering the latter's session rows to the original PGID. Both request
`pid,ppid,pgid,uid,ruid,state` fields with numeric identities; Linux also requests `sid`.
A complete, nonempty snapshot must contain the actual-parent anchor, have unique PIDs,
and show only zombies with the expected
PGID and effective/real UID (and Linux SID) before further signals can stop. Darwin zombie SID
is unavailable: the retained session/group leader anchors membership because POSIX groups
cannot cross sessions. `waitid`'s `si_uid` is not ownership evidence.
Snapshots are non-atomic and establish no durable ownership or containment. Signals target
only the original anchored group, never PIDs discovered by inspection. Live, mixed, foreign or
unknown state cannot suppress a permission denial; failed inspection retains that denial.
If inspection fails after successful TERM, cleanup still attempts final KILL through the
verified anchor and retains the inspection error. Only the direct child is reaped here;
descendants require their actual parents, including workers owning separate-session runners.

`--timeout` defaults to 600 seconds per mutant domain run; `--baseline-timeout` defaults to
1,800 seconds per baseline domain. The scheduler execution limit is
`timeout × (candidate count + 2) × 3`. Cleanup has a separate single monotonic deadline:
ordinary runners allow 10 seconds of TERM grace within 20 seconds total; the scheduler's
`worker_cleanup=True` allows 21 seconds of grace within 31 seconds total so workers can finish
nested cleanup. Inspection takes at most one second or the remaining cleanup time, whichever
is smaller; polls and inspections never reset the deadline. `TIMEOUT` counts as killed by
policy, not assertion-strength evidence; cleanup failures remain diagnostic failures.

Each scheduler run prints its unique `tmp/mutation-runs/run-*` directory under the selected
root to stderr as soon as it exists. Inspect `mutations.json`, `scheduler.log` and
`result-N.json` there; worker results are published atomically. Scheduler failures and
cooperative cancellation retain `traceback.txt`, including combined initiating/cleanup errors,
and exit nonzero even if all candidates finished. Completed result bytes remain intact.
Missing results receive explicit `INFRA` rows; corrupt JSON remains byte-for-byte in the
original file, with its synthesized row in `result-N.infra.json`. Corruption fails the command
even after scheduler exit zero; file permission failures remain diagnostic. An incomplete row
never counts as a kill or verified scratch restoration. Retained runs are never resumed automatically.

## Coverage badges

CI runs the unit suite in four sorted round-robin partitions, each serial with a 10-minute
budget. `--unit-shard K/N` selects a partition without affecting nested pytest child runs.
Each partition retains JUnit evidence and one-day coverage data. The `unit-coverage` job
requires all four files, combines them, enforces 95% floors over the core-and-scripts measurement
and separately over `src/nci_si_mcp` plus `src/ncit_index`, and reports the above-98% aim over
the combined measurement; individual partitions do not enforce a partial coverage floor. Local
`pdm run test` checks the complete core-and-scripts suite. The mutation gate's local unit baseline
also remains serial and unsharded.

CI exports separate JSON coverage reports for the combined unit suite and the combined harness
self-tests. The harness runs in eight sorted round-robin shards, each with four workers;
shared subprocess mutation coverage needs more shards than ordinary local self-tests. Each job
keeps its ten-minute bound; add shards when measured jobs exceed about four minutes.
A read-only job renders the three badge JSON files; a separate writer downloads
that run's data without checking out or executing repository code. After every CI gate
passes on a push to `main`, the writer publishes measured
line-plus-branch percentages and their source counts, commit and run URL to the dedicated
`coverage-badges` branch. It never commits to `main`, publishes from a PR, or replaces data
from a newer main commit with an older run. Failed runs leave the previous measurements
visible; use the adjacent CI badge to see main's current status.

The README uses [Shields endpoint badges](https://shields.io/badges/endpoint-badge), so no
coverage-service account, extra credential or manual percentage update is needed. GitHub and
Shields cache images, so a successful publication may take a few minutes to appear. Before
the first successful publication, the endpoints do not exist. Do not delete `coverage-badges`
during feature-branch cleanup; it is generated publication data.

## Standards

1. **Stay on the goal.** A change does what its task asks.
2. **Tests are written for their value.** Each one checks behaviour that a user or a caller can
   observe, and can fail when that behaviour breaks.
3. **Coverage: 95% is the checked minimum, above 98% is the aim.** The gap is deliberate: it
   keeps anyone from writing tests for the number. Cover untested behaviour whenever you can.
   Never add a test that asserts nothing, or only that a mock was called. Local `pdm run test`
   checks the minimum over `src/nci_si_mcp`, `src/ncit_index` and every script in `scripts/`
   except those `omit`ted in `pyproject.toml`. CI checks both the combined core-and-scripts and
   core-only minimums, and reports the aim over the combined measurement. Harness self-tests
   check their own 95% minimum and above-98% aim. Omit nothing to raise a number.
4. **Keep it simple, and say each thing once.** Follow the structure in ARCHITECTURE.md: thin
   adapters over the service, one error path, closed value sets in `validation.py`. No
   abstraction for a single use.
5. **Write code to be read.** Small functions, names that say what a thing is, comments that
   give the reason.
6. **Remove dead code** in the change that makes it dead.
7. **Keep the documentation in step, and short.** Update it in the same change. The README
   answers the first questions and links onward; details belong in the deeper documents.

### Adding a tool

1. Specify it in `spec/tools.yaml` first; run `pdm run spec-render` and `pdm run quickstart-tools`.
2. Write the handler with the `Context` as its first positional parameter, public inputs typed
   with `parameters.py`, and a docstring that is the client contract.
3. Add its result `TypedDict` to `results.py` and declare a `ToolSpec` in `registry.SPECS`: its
   `group`, `name` and `title` (without a `name` it is not served as a tool), the output union with
   `ErrorResult`, cache `resolution` or a handler-owned cache hint, an `audit` class per
   parameter, and a `_CLI_FLAGS` spelling for any `True`-default boolean.
4. Rely on the scopes `registry.invoke` and the server open for every call (permission, input
   limits, cache decision, release selection, deadline, audited correlation); never reopen them.
5. Add its scenario to `tests/test_registry_contracts.py` and update QUICKSTART.md; if outcomes
   move, commit `acceptance/expected/fixture.json` from a fresh fixture run and run
   `pdm run acceptance-status`.

### Typing conventions

These are the target conventions for new and changed code, not a claim that legacy upstream
dictionaries have already been migrated. Validate at the owning boundary; keep the core free
of dependencies (`dependencies = []`). Existing typing escapes need a reason; the exact-count
ratchet in `tests/test_typing_conventions.py` prevents growth and requires reductions to lower
their recorded counts in the same commit.

| Status | Situation | Use |
| --- | --- | --- |
| Standard | MCP inputs | `Annotated[T, metadata]`; the optional MCP SDK builds and validates parameter schemas. |
| Project convention | MCP output | `TypedDict` records in `results.py`; the SDK's `RootModel` builds structured-output schemas. |
| Standard | Trust boundaries | Validate once into the fields' typed shape: SDK inputs, frozen `Settings`, index schema checks, and upstream `parse_upstream_json` plus field checks. Upstream parsing may use `dict[str, Any]`; do not add Pydantic to the core. |
| Project convention | Internal data | `dataclass`, frozen and slotted where immutable. |
| Standard | JSON records | `TypedDict` for plain dictionaries leaving as JSON; `NotRequired` preserves upstream omissions, never invented defaults. |
| Standard | Interfaces | Structural `Protocol`, without `@abstractmethod`; use an ABC only when its base supplies shared behavior. |
| Project convention | Closed sets | `Literal`, once in `validation.py` (error codes in `errors.py`), checked through `get_args`; `StrEnum` only for named runtime constants. |
| Standard | Distinct identifiers | `NewType` where mixing identifiers is a bug; migration is separate work. |
| Project convention | Storage | stdlib `sqlite3` through `LocalIndex._connect`, parameterized SQL, and index dataclasses at the storage edge. |
| Project convention | Escapes | No new `cast`, `pyright: ignore`, `type: ignore`, or `dict[str, Any]` outside upstream parsing. Remove an escape rather than replace it with an alias; existing counts only go down. |

## The gates

The commit hook runs every gate except the last three on the files of a commit, and CI runs the
same hooks on all files. The push hook runs the whole suite before commits leave the checkout.
The tests also run in CI and with `pdm run test`. A gate that fails is fixed, not skipped
(`--no-verify` and `SKIP=` are not used).

Quality, test and dependency-audit jobs run independently for faster feedback. Within the
quality job, pre-commit runs its hooks sequentially. Each gate must pass before merge; scheduling does not weaken
that requirement. Unit tests and each harness shard retain JUnit XML artifacts and publish
counts plus failed/error test names in the run summary, even after test failure. Cancelled runs
do not add report errors. Diagnostic bodies are excluded from the summary; XML artifacts retain
pytest's original report. Local report files live under ignored `tmp/` directories.

| Gate | What it enforces |
| --- | --- |
| Ruff format and lint | Style, imports, likely bugs, security patterns, a bare `print` outside the gate scripts, a broad `except` that neither passes the exception on nor logs the traceback |
| basedpyright | Types, over `src`, `scripts` and `acceptance/src` |
| Complexity | Every function below cyclomatic complexity 8, nested ones and the tests included (`scripts/validation/check_complexity.py`) |
| Test quality | No test without an assertion, or with only mock or `callable` assertions (`scripts/validation/check_test_quality.py`) |
| Dead code | No unused functions, classes or variables (vulture) |
| gitleaks, zizmor | No secrets; safe GitHub Actions workflows |
| actionlint, shellcheck | Workflow syntax, expressions and shell scripts; both binaries are pinned in the PDM lint group. Only the unsupported `$/` self-repository diagnostics are ignored in `.github/actionlint.yaml`; local target existence is tested separately. |
| Upstream requirement packages | `docs/upstream/*.md` are the current render of `docs/upstream/catalogue.yaml` (`scripts/upstream_requirements.py --check`) |
| Installer requirements | The package-shipped server lock is the current PDM export of the `server` and `build` groups: `pdm export -G server -G build --no-default -o src/nci_si_mcp/server-requirements.txt` |
| Acceptance ratchet (CI) | Every fixture-mode test has the outcome in `acceptance/expected/fixture.json`. An intentional move updates that file in the same pull request; a base `passed` outcome that regresses or is removed also needs its exact base-bound transition in `acceptance/expected/regressions.yaml`. `pdm run acceptance-expected update acceptance/fixture.json` writes current outcomes from a fresh report, and `pdm run acceptance-status` updates the README table. |
| Tests and coverage | The suite passes before each push and in CI; CI fails below the coverage minimum of standard 3 and warns when the aim is missed |
| Local run (CI only) | The documented setup, walked literally on `macos-latest` and `ubuntu-24.04` (`.github/workflows/local-run.yml`, on pushes to `main`, weekly, by hand, and on pull requests that touch the install paths). Every command is copied from the document that gives it; the test of the workflow compares the two, so change a documented command and its workflow line together. Not a required check: it uses live EVS |
| Dependency audit (CI only) | Every Python and npm package in the committed locks is at least five days old; no external Python package in any PDM-locked group has a known vulnerability (pip-audit across all platform markers, on every change and weekly; only the local acceptance project is excluded); no pull request brings one in at high severity (dependency review). Dependabot groups weekly updates for Actions, both Dockerfile directories and the documentation npm tree after a seven-day cooldown. Dependabot cannot update `pdm.lock`, so `pdm run lock` is the only Python lock command. [SECURITY.md](SECURITY.md) says how to report a vulnerability |

The [`$/` self-repository syntax](https://github.blog/changelog/2026-07-30-reference-same-repository-actions-with-self-repository-syntax/)
resolves local actions and reusable workflows at the running commit without a checkout.

A dependency security fix inside the five-day window is an exception only when the owner records
its exact version and GHSA, CVE or PYSEC advisory in `[tool.nci-si.dependency-age.exceptions]`.
The lock and audit read that one list. The audit fails with “remove this exception” once the fixed
release is five days old; remove the entry then. An exception never admits another version.
The lock entry point reads the excepted version's upload time from the index at run time and
uses it as that package's cutoff; the timestamp is never committed. A lock run refreshes every
package to its newest admissible version. Treat a re-lock as a separate reviewed dependency
refresh with an old → new version table in its PR, never as a side effect of feature work.

Direct-URL wheels are checked by their index's upload time, matching the filename and locked
SHA-256; a missing or unverifiable index record fails the audit.

A finding is fixed in the code. Where a rule does not fit, it is suppressed as narrowly as
possible (a line, then a file or a directory, then the project, in `pyproject.toml`), with the
reason beside the suppression, or once in the module when it repeats.

### Required checks

The owner-approved required contexts below replace the individual CI job contexts after
`required` passes on the Phase 33p milestone PR, before merge. The reviewer applies that ruleset
change with the owner's approval; implementation does not change repository settings.

| Exact context | Producer |
| --- | --- |
| `required` | CI |
| `conventional commit subject` | PR title |
| `dependency review` | Audit |
| `CodeQL` | GitHub default setup (integration 57789) |
| `pip-audit` | Audit |

`required` waits for every other CI job and rejects failure, cancellation, missing or unknown
results. Only `mutation`, `image`, `companions` and `coverage-badges` may be skipped by their
declared conditions. Their dependencies are also gate inputs, so a failed producer still fails
the gate. Local run and Local installer remain unrequired because their PR triggers are
path-filtered. CodeQL is GitHub-managed default setup, not a workflow file in this checkout.

### Verification before merge

Every verification has a pull-request run or a bounded exemption below. The test derives
postmerge-only jobs from workflow events, exact conditions and dependencies; an unknown
condition requires classification. Path-filtered twins run for their complete input families.
Fixture checks never establish live-service assurance.

| Postmerge-only job | Pull-request verifier or exemption |
| --- | --- |
| `ci/coverage-badges` | ci/coverage-badges-render; privileged mutation: publish rendered badge data only on main. |
| `branch-cleanup/cleanup` | ci/test; privileged mutation: delete merged branches only after merge. |
| `release/release` | ci/test and pr-title/conventional; privileged mutation: create real tags and releases only after validated main. |
| `release/image` | ci/image uses the same image action; privileged mutation: registry and release publication; post-publication digest availability requires the published digest. |
| `release/local-installer` | local-installer/prepare and local-installer/local-installer use the same reusable verification; actual release tag identity is checked after tagging. |
| `acceptance-live/acceptance-live` | Live drift exemption: measures real services, not fixture behavior; operationally disabled, no PR live run. |
| `scorecard/analysis` | ci/test exact offline workflow contract; first run after the milestone merge, reported in step 9. |

## Workflow credentials

Only protected live checks need issued credentials. Anonymous live checks and fixture CI need
none. A maintainer may run `pdm run github-setup` after authenticating `gh`; it creates missing
names with `NCI_SI_CONFIGURE_ME`, never overwrites an existing name, and never reads secret values.
The live workflow treats the placeholder as absent and lists absent names in its summary.
Existing names may still contain dummy values: setup cannot inspect them. No caDSR credential
has been issued. The owner deleted the dummy on 24 October; setup can create its placeholder.

| Secret | Purpose | Workflow |
| --- | --- | --- |
| `NCI_SI_CADSR_CREDENTIAL` | Issued caDSR user:password for protected live checks | Acceptance (live) |
| `NCI_SI_EVS_LICENSE_KEY` | Issued EVS licence key for restricted live checks | Acceptance (live) |

Maintainer: run gh secret set NCI_SI_CADSR_CREDENTIAL --repo CBIIT/nci-sip-mcp in your own terminal; it prompts for the value.

Maintainer: run gh secret set NCI_SI_EVS_LICENSE_KEY --repo CBIIT/nci-sip-mcp in your own terminal; it prompts for the value.

`GITHUB_TOKEN` is automatic and is never created by setup. No repository Actions variables are
required. INT, UAT and Production environments remain deferred to the AWS phases; setup creates
no environments. Never put a credential in command arguments or shell history.

## Pull requests and releases

The government sponsor approves changes; the NCI SI MCP project coordinator relays that approval,
and the reviewer acts for the coordinator.

Merge clearance is a reviewer PR comment naming the head commit; a subsequent push needs new
clearance, after all triggered workflows pass. See [AGENTS.md](AGENTS.md#how-work-is-done-issues-into-a-milestone-branch-one-reviewed-milestone-into-main)
for the milestone process and [operations](docs/operations.md) for available signals, ATO
evidence inputs and open hosting responsibilities.

Every change reaches `main` through a pull request that is squash-merged. The pull request
title becomes the commit subject, and the next version is computed from it, so the title is a
[Conventional Commit](https://www.conventionalcommits.org) subject. A check on the pull request
fails otherwise. Changes to `spec/` and `acceptance/` follow the change control in
[acceptance/README.md](acceptance/README.md#change-control): the NCI SI MCP project coordinator is
their code owner.

| Title | Release (while the version is below 1.0) |
| --- | --- |
| `feat: …` | Minor: 0.3.1 → 0.4.0 |
| `fix: …`, `perf: …` | Patch: 0.3.1 → 0.3.2 |
| `!` after the type, as in `feat!: …` | Minor |
| `docs`, `style`, `refactor`, `test`, `chore`, `build`, `ci`, `revert`, `security`, `deprecate` | None |

A scope is optional, in lower case: `fix(index): …`. Choose the type with care, because it
decides the version. Only the title counts: the squash commit has no body, so mark a breaking
change with `!`, and do not edit the commit message in the merge dialog.

Review the exact PDM (including its bootstrap hishel, unearth and virtualenv constraints), Ruff,
basedpyright, Zensical and python-semantic-release tool pins monthly,
using a separate reviewed dependency refresh and the release-age/security gates above.

When CI passes on `main`, the release workflow tags that commit `vX.Y.Z` and creates a GitHub
release using semantic-release from the hash-locked `release` dependency group, not a third-party
action. The release job installs only that group, without the project. No file holds the
version, and the workflow never commits to `main`. If a release is missing although `main`
is green, start the Release workflow by hand
(`gh workflow run release.yml`). Version 1.0.0 is tagged by hand.

### Complete fixture ratchet

For the complete fixture ratchet, select the registry checks this server supports and supply
the furnished secured adapter and index preparation explicitly, as CI does:

```bash
NCI_SI_ACCEPTANCE_REGISTRY_RELEASE=required NCI_SI_ACCEPTANCE_SECURITY_SERVER='python ../scripts/permissions_fixture.py' NCI_SI_ACCEPTANCE_PREPARE='nci-si-mcp index-sample $(cat "$NCI_SI_ACCEPTANCE_INDEX_CODES")' pdm run acceptance -n 4 --dist worksteal --report=fixture.json
```

The harness default remains `optional`. Successors must supply their own secured adapter;
without it the secured cases report NOT RUN, not a security pass.
