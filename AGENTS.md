# AGENTS.md

Guidance for coding agents and contributors working in this repository. It is the one set of
project instructions; the README, QUICKSTART, CONTRIBUTING and ARCHITECTURE documents hold the
detail.

## What this is

The NCI Semantic Infrastructure MCP server, and the acceptance suite it and its successors are
measured by. Describe the repository as the NCI Semantic Infrastructure MCP server, built to
production standards and likely to become the production codebase: never as a prototype, never
as in production, and never by contract terms or documents outside the repository. It is a lean
wrapper over EVS and caDSR (plus the NCIt index), never a shadow of them; a functional gap in
an upstream system is reported to its team and stays visible here (see the first standard
below). Until NCI issues caDSR credentials, CDE Match and the lists-of-values API are tested
against fixtures crafted from the published contracts; the data element API, vmMatch and
Form API 2.0 are anonymous and may be tested live.
The milestones and issues on GitHub (Phases 0 to 7 delivered; later phases open) are the plan.
`docs/functional-status.md` gives dated functional status and evidence, linked from README.md;
QUICKSTART.md holds the usage details.

Documentation, from short to detailed: `README.md` (what the repository is, who it is for, the
status per tool group), `QUICKSTART.md` (install and run; settings, tools, resources, error
codes), `CONTRIBUTING.md` (how to work on it: commands, gates, standards, releases),
`ARCHITECTURE.md` (components, the four main flows, the SQLite schema).

The specification of the required tools and their behaviour is owned by this repository. Its
source of record is the data in `spec/` (conventions, records, tools, requirements);
`docs/specification.md` is generated from it with `pdm run spec-render` and is never edited by
hand. Tokens such as `(A4.4)`, `M2.2` or `X-22` in code, tests and documents are convention and
requirement IDs from `spec/conventions.yaml` and `spec/requirements.yaml`; `OP-…` tokens such as
`OP-C10` name platform operations: those with recorded request forms are listed in
`acceptance/request-forms/`, and those raised with an upstream team in `docs/upstream/`. `docs/implementation-plan.md` plans the implementation, and `acceptance/` holds the
acceptance suite that tests the requirements (its README says how).

## Engineering standards

These are the owner's rules. They apply to every change.

- **The server stays a lean wrapper and invents nothing.** It never creates, infers, defaults or
  backfills content that EVS or caDSR manage: no concept, CDE, form, value domain or permissible
  value, and none of their properties or metadata. Every value in a result has one of three
  origins: what upstream returned in this call, what the NCIt index holds from a recorded EVS
  release, or the server's own handling of the request (provenance, correlation, release pin,
  cursors, truncation and error records). A field upstream omits stays absent; a capability
  upstream lacks is reported as the gap it is, never rebuilt here. Workflow and orchestration
  tools may bundle the results of their own upstream calls into one response or into a follow-up
  request's arguments; that is handling, and the provenance shows it. Anything else is a `spec/`
  decision, not a code change.
- **Never lose sight of the stated goal.** Do what the task asks; do not drift into adjacent work.
- **Tests are written for their value, always.** Each test verifies behaviour a user or caller
  could observe and must be able to fail on a regression. A test of a workflow or other
  configuration file asserts the exact value of each property it protects (a job's condition,
  permissions, dependencies, environment and commands), never a substring or one member of a list.
- **Coverage: the checked minimum is 95%, the aim is above 98%.** Local `pdm run test` checks the
  core and scripts together. CI checks both the combined core-and-scripts and core-only minimums,
  and warns on the combined core-and-scripts aim; the acceptance harness checks its own minimum
  and aim. The gaps exist so that nobody pads
  coverage to pass a threshold. Take every real opportunity to cover untested behaviour; never
  write a test whose purpose is the number (no assertion-free tests, no tests that only check that
  a mock was called).
- **Design: KISS and DRY.** The simplest structure that does the job, one place for each fact,
  and the established architecture (thin adapters over the registry, one error path, closed value
  sets in `validation.py`). No abstraction for a single use.
- **Readable, maintainable, extendable code.** Small functions (cyclomatic complexity below 8 is
  a gate), names that say what a thing is, comments that give the reason.
- **Remove dead code.** Unused functions, parameters, branches and files go in the same change
  that makes them unused.
- **Documentation stays in step with the code**, in the same change. It is intuitive and layered
  (progressive disclosure): the README answers the first questions briefly and links onward.
  Less is more; nobody reads for hours.
- **One set of agent instructions; shared tool settings are tracked.** `AGENTS.md` holds every
  agent rule; `CLAUDE.md` only imports it. Shared tool settings in `.claude/settings.json` are
  tracked. Personal files and runtime artifacts are ignored, and nothing tracked may depend on
  an untracked file. A self-test in `tests/test_docs.py` enforces it.

## Commands

The project is managed with PDM (Python 3.14 or newer); `pdm install` builds `.venv` from
`pdm.lock` with the test and lint tools and the `server` and `index` extras.

```bash
pdm run test                              # whole suite, with the 95% coverage floor
pdm run pytest tests/test_index.py        # one file, no coverage floor
pdm run pytest tests/test_handlers.py -k LookupTest
pdm run lint                              # ruff check + basedpyright, the fast check
pdm run fmt                               # ruff format
pdm run pre-commit run --all-files        # every commit-stage hook, as the CI quality job runs them
pdm run mutation-gate                    # changed Python sites; review every survivor before a PR
NCI_SI_ACCEPTANCE_REGISTRY_RELEASE=required NCI_SI_ACCEPTANCE_SECURITY_SERVER='python ../scripts/permissions_fixture.py' NCI_SI_ACCEPTANCE_PREPARE='nci-si-mcp index-sample $(cat "$NCI_SI_ACCEPTANCE_INDEX_CODES")' pdm run acceptance -n 4 --dist worksteal --report=fixture.json  # fixture mode, prepared as in CI
pdm run acceptance-expected check acceptance/fixture.json    # the report against the expected outcomes
pdm run acceptance-expected update acceptance/fixture.json   # rewrite the expected outcomes
pdm run acceptance-status                 # regenerate the functional-status tables
pdm run acceptance-selftest               # the acceptance harness's own tests
pdm run spec-render                       # regenerate docs/specification.md from spec/
pdm run quickstart-tools                  # regenerate the tool table of QUICKSTART.md from spec/
```

- **Know the executing shell.** At session start, use the tool's declared shell or inspect the
  current shell process; do not infer it from `$SHELL` alone. This workstation's command tool uses
  zsh. In zsh, `status` is a read-only special parameter: use `gate_status`, `exit_code` or another
  non-special name for exit-status capture. Run Bash-specific scripts explicitly with `bash`; a
  terminal tool named Bash does not imply its commands execute in Bash. Before an expensive run,
  exercise a new wrapper with cheap success and failure commands in the same shell and verify
  status capture and propagation. A syntax-only check cannot catch read-only-variable assignments.
  A wrapper failure before test launch is not a test run.
- **Distinguish the working tree from the index.** Before testing, inspect both `git diff` and
  `git diff --cached`: staging changes commit contents, not the files a test executes. Preserve
  the test command's exact output; never present a paraphrase as captured output.
- Run the tests through `pdm run` (`pdm run test`, `pdm run pytest ...`), never a bare `pytest`,
  so the project's environment and configuration apply.
- The tests are unittest-style and offline. They import shared doubles with `from fakes import
  ...`; `tests/` is on the path through the pytest configuration.
- Hooks are never skipped (`--no-verify`, `SKIP=`). A failing hook is fixed. The pre-push hook
  runs `pdm run test`. Besides Ruff and
  basedpyright (`src`, `scripts` and `acceptance/src`), the hooks run `scripts/validation/check_complexity.py`
  (every function below 8, nested ones and the tests included), `check_test_quality.py` (no test
  without a behaviour assertion; an assertion in a nested function or class that the test never
  uses does not count), vulture, gitleaks, zizmor, `scripts/upstream_requirements.py --check`
  (the upstream requirement packages are the current render of their catalogue), and the package-shipped
  installer lock exported from the `server` and `build` groups with
  `pdm export -G server -G build --no-default -o src/nci_si_mcp/server-requirements.txt`. All Python tools run from the PDM
  environment, so `pdm.lock` decides their versions.
- `tests/test_docs.py`, `tests/test_server.py` and `tests/test_release_config.py` compare
  QUICKSTART.md, ARCHITECTURE.md and the title check with the code (settings and defaults, error
  codes, modules, tools, public arguments, resources, commit types). Change the document
  with the code.
- Do not enable PDM's uv mode (`use_uv`): it rewrites `pyproject.toml` during an install, which
  marks every installed version as locally modified.

```bash
pdm run nci-si-mcp release-info      # live EVS
pdm run nci-si-mcp index-sample C3262 C2991 C40704 C153397 C116938   # live EVS
pdm run nci-si-mcp search "kinase inhibition" --mode hybrid           # local only
pdm run nci-si-mcp lookup C3262 --live-only
pdm run nci-si-mcp traverse C4817 --max-depth 3 --edge-type descendant
pdm run nci-si-mcp serve
```

Every command opens the index in the data directory, `.nci-si-mcp/` relative to the working
directory. Set `NCI_SI_DATA_DIR` to a scratch directory for experiments.

## Tooling beyond the server

The repository deliberately holds four things: the server (`src/`), the acceptance suite
(`acceptance/`), the documentation site hosted alongside the MCP,
and the validation companion with its benchmark and evaluation tooling. `scripts/` serves the
last two and the suite's CI; each family below is operational, and what depends on it says why.

| Family | Scripts | Entry point | Depended on by |
|---|---|---|---|
| Gates | `validation/check_complexity.py`, `validation/check_test_quality.py`, `dependency_age.py`, `dependency_lock.py` | pre-commit hooks; the `audit` job; `pdm run lock` | every commit, the CI `quality` job; every committed dependency lock; reviewed dependency refreshes |
| Acceptance in CI | `acceptance_http.py`, `permissions_fixture.py`, `upstream_requirements.py`, `ssis_contracts.py` | `pdm run acceptance-http`; the `acceptance` job's environment; a pre-commit hook; `pdm run python -m scripts.ssis_contracts` | the `acceptance` and `acceptance-http` jobs, weekly `Acceptance (live)` Shared SI contracts, `docs/upstream/` |
| Server container | `container_lock.py`, `container_smoke.py`, `image_scan.py`, `image_publish.py`, `Dockerfile` | the CI `image` job, the Release workflow | the published image |
| Documentation site | `docs_site.py`, `docs_links.py`, `docs_stories.py`, `site_assets.py`, `static_server.py`, `companion_context.py`, `companion_lock.py`, `container/Docs.Dockerfile` | `pdm run docs-build`; the `documentation` and `companions` jobs | the hosted site |
| Validation companion | `portal*.py`, `operator_*.py`, `evidence_*.py`, `companion_entry.py`, `companion_relay.py`, `companion_smoke.py`, `container/Admin.Dockerfile`, `container/compose.local.yaml` | `pdm run portal`, `pdm run operator-worker`; the `companions` job | `docs/local-validation.md`, `docs/companion-containers.md` |
| Benchmarks and evaluation | `benchmark*.py`, `http_measurement.py`, `assisted_evaluation.py`, `assisted_scoring.py` | `pdm run benchmark-http`; the companion's workers | `docs/benchmark.md`, `docs/assisted-evaluation.md` |
| Shared process helpers | `processes.py` | imported by the acceptance, container, companion, operator and benchmark scripts | every script that owns or waits for a process |
| Shared formats, vocabularies, lock lines and files | `formats.py`, `vocabulary.py`, `requirements_lock.py`, `operator_files.py` | imported by the evidence, portal, docs, operator, benchmark and lock scripts | every script that checks a hash, job id or release tag, every run and job stop reason and state, both requirement locks, and every atomic JSON write |
| README badges | `coverage_badges.py` | the CI `coverage-badges` job | the README |
| Test reports | `junit_summary.py` | CI unit and self-test report steps | per-run summaries and JUnit artifacts |
| Workflow setup | `github_setup.py` | `pdm run github-setup` | repository maintainers; the live workflow's credential inventory |

A script that served an ephemeral purpose and is no longer needed to operate or maintain the
server, the suite or the site is removed with its tests, after verifying that nothing depends on
it.

## Acceptance suite and its CI ratchet

`acceptance/` tests the requirements in `spec/` against the server. The CI job `acceptance
(fixture)` runs it against the recorded fixtures and compares every test's outcome with
`acceptance/expected/fixture.json`. The job stays green while tools are not implemented or fail,
and fails when an outcome differs from the expected one. A change that moves an outcome on purpose
updates that file in the same pull request, written with `pdm run acceptance-expected update` from
a fresh report, and `pdm run acceptance-status` updates the functional-status tables that follows from it; the
diff of `expected/fixture.json` is what the review reads. A base `passed` outcome that regresses or
is removed also needs its exact base-bound transition in `acceptance/expected/regressions.yaml`.
The `Acceptance (live)` workflow runs the
suite against the live services by hand and every Monday; its outcomes are not ratcheted, but
the weekly run fails when a test that passes on the fixtures fails live (upstream drift).
The `Local run` workflow (`local-run.yml`) walks the documented setup on `macos-latest` and
`ubuntu-24.04`: every `run:` line is copied from the document that tells a developer to run it,
and `tests/test_local_run_workflow.py` lists the few CI-only lines with their reasons. Change a
documented command and its workflow line together.
The harness has its own
tests (`pdm run acceptance-selftest`, the `selftest` CI jobs). `acceptance/README.md` has the
detail.

## Pull requests and releases

No verification runs only after merge: provide a pull-request twin or an explicit tested exemption; publishing remains post-merge.

- Every change reaches `main` through a pull request that is squash-merged. The PR title is the
  commit subject and must be a Conventional Commit (`feat: ...`, `fix(scope): ...`); the squash
  body is blank, so only the title counts.
- A release is cut automatically from the title after CI passes on `main`: `feat` bumps the
  minor version, `fix` and `perf` the patch, `!` the minor while below 1.0; other types release
  nothing. Tags are `vX.Y.Z`. Nothing is written back to `main`.
- The accepted types are `allowed_tags` in `[tool.semantic_release.commit_parser_options]` and the
  regex in `.github/workflows/pr-title.yml`; a test keeps them equal. semantic-release reads no
  other list (`other_allowed_tags` alone leaves a type unparseable).
- If a release is missing although `main` is green: `gh workflow run release.yml`.
- The package version is derived from the nearest tag at install or build time; it is written in
  no file. Run `pdm install` after a new tag to refresh it.

## How work is done: issues into a milestone branch, one reviewed milestone into `main`

Before writing or changing Python, read CONTRIBUTING.md's Typing conventions and say in the issue plan which constructs the change uses.

The milestones and issues on GitHub are the plan; take the open issues of the current phase in
the order the phase's plan gives, one at a time. Each milestone is built on its own branch,
`milestone/<phase>` (for example `milestone/phase-2`), cut from `main`. Issues are merged into it
after a light check; the milestone reaches `main` in one pull request that gets the full review.
The reviewer is the NCI SI MCP project coordinator, or the reviewer acting for them.

Reviewer instructions, approvals, findings and clearance are GitHub comments whose first line
starts with `## Reviewer`; treat them as binding, as if relayed by the owner. Both roles use the
owner's account, so the heading identifies the reviewer. While waiting, poll the issue or PR
and the current milestone's open issues every minute for these comments and act on them. A wait
on a clock or on one answer never idles the work: take up whatever does not depend on it (the
next review round, the next issue's plan, a measurement the pull request needs) and say so in a
comment; if nothing can start, say what is blocked and until when. Polling continues until the
milestone is merged and cleaned up; it never ends the session.
Post plans, review rounds and reviewer questions as GitHub comments; the reviewer answers there.
Ask the owner directly only for owner decisions: scope, specification conventions, repository
settings and rulesets.

A milestone fits one pull request reviewable in one sitting: about four issues, 1,500 changed
lines (excluding generated files and `acceptance/expected/fixture.json`), and two days of work
at most. If it outgrows those bounds, split it before opening the pull request and move the
remaining work to a new milestone. Measure insertions plus deletions with
`git diff --shortstat origin/main...milestone/<phase> -- . ':!acceptance/expected/fixture.json'`,
adding exclusion pathspecs for generated files.

**Each issue:**

1. **Read the issue against `spec/` first.** The specification data is the source of record;
   an issue body written earlier may be stale. Where they differ, follow `spec/` and correct the
   issue body in the same step, saying what changed.
2. **Plan before building.** Post the plan as a comment on the issue: what changes, which
   acceptance tests you expect to move in `acceptance/expected/fixture.json` and why, open
   questions with your recommendation, and the commit subject (a Conventional Commit).
   The plan also lists the invariants the change must keep (security, fail-safe and scope
   properties, such as token permissions, the conditions a job runs under, or what a failure
   falls back to), each with the test that will fail if it breaks; the reviewer checks this list
   before approving. Wait for the reviewer's answer on the
   issue before writing code; a correction there is binding.
3. **Build on an issue branch cut from the milestone branch.** Every behavioural change has
   at least two commits: a test-only red commit, subject `test(<scope>): … (red)`, whose body
   quotes the test node id and failing assertion line exactly as `pdm run pytest <node>` printed
   them, then a green commit with the change that makes it pass. The failure must demonstrate
   the stated behaviour, not a collection, import or fixture error. For a new entry point, red
   may come from its absence if the test drives the real interface and the failing line is a
   behavioural assertion. The green commit changes the red tests only for renames required by
   the implementation. Never squash or rebase the issue branch: the `--no-ff` merge preserves
   red before green for review. Pure refactors say
   "no behaviour change" in the plan and commit body, with existing tests passing before and
   after; documentation-only changes are exempt.
   Write tests for their value (see the standards). Before merging run
   `pdm run test`, `pdm run acceptance-selftest`, `pdm run pre-commit run --all-files`, then the
   fixture run and, where outcomes moved on purpose,
   `pdm run acceptance-expected update acceptance/fixture.json` and `pdm run acceptance-status`,
   so the ratchet file moves in the same pull request. A base `passed` outcome that regresses or is
   removed also adds its exact base-bound transition to `acceptance/expected/regressions.yaml`.
   An issue that changes a workflow dispatches that workflow on the issue branch before the
   `--no-ff` merge and links the green run in its completion comment. A red run blocks the merge.
4. **Merge the issue branch into the milestone branch yourself** once the gates pass and the
   outcomes moved as the plan predicted: `git merge --no-ff issue/<branch>` on the milestone
   branch, then push, and delete the issue branch. There is no pull request per issue, no review
   agent and no reviewer clearance at this step; the issue's commit message says what it does,
   names the issue (`#N`) and anything deferred; the milestone pull request body closes the issue
   (step 6). The reviewer reads each merged issue and says on the
   issue if it is not done. The branch-cleanup workflow deletes merged issue branches and the
   milestone branch after its pull request merges, as a backstop to this immediate deletion.

**The milestone, once all its issues are merged:**

5. **Review the milestone branch in five passes to convergence before opening its pull
   request,** each a separate agent or a fresh pass over the whole diff, split by module where
   the diff is large, with one focus each:
   1. **Code review:** the engineering standards above, the architecture, and the scope.
   2. **Silent failures:** swallowed exceptions, broad `except`, fallbacks that hide an error,
      results that look complete but are not.
   3. **Tests:** apply the outside-in rules below to every changed behavioral claim.
      Run `pdm run mutation-gate` before the PR; fix every survivor or allowlist a reviewed
      equivalent `SURVIVED` mutant in `scripts/validation/mutation-allowlist.yaml` with its reason.
      `NO_TESTS` is never allowlisted: close reachable gaps at their owning boundary and
      remove verified dead code. Timeouts count as killed by policy, not as assertion-strength
      evidence. See [CONTRIBUTING.md](CONTRIBUTING.md#mutation-gate) for classification and execution details.
      A behavioural change without a red commit is a finding; check its quoted failure. Identify the claim's
      caller scenario, owning interface and test, plausible regression, and observed
      assertion failure under the original defect or a restored targeted mutation. Distinguish
      setup failures from sensitivity evidence. Check exact error contracts, boundary cases,
      and independent expected values. Challenge internal spies, duplicate claims and
      development scaffolding; retain component tests with independent contracts. Before any
      removal, show which remaining test preserves each distinct failure mechanism. Record
      each finding's retention, replacement or removal rationale in the review table. Coverage
      percentages and passing tests alone do not establish assertion quality or uniqueness.
   4. **Types and records:** invariants are expressed in the types, and records match `spec/`.
      Check the diff against the Typing conventions in CONTRIBUTING.md.
   5. **Comments and documentation:** docstrings, comments and documents say what the code
      does now.

   Fix what is real directly on the milestone branch (commit and push it there), without opening
   issues (except for work deferred to a later milestone, recorded in that milestone's issue), and
   run all five again until a full round finds nothing new. Record each round as a short table:
   finding, pass, fixed or rejected (with the reason). The rounds so far go into the pull
   request body when it is opened (step 6); later rounds are posted as pull request comments.
6. **Open the milestone pull request into `main`.** Its title is the release, a Conventional
   Commit that tells the truth about what a client sees (`feat(evs)!:` where it breaks
   something); its body lists `Closes #N` for every issue of the milestone, so they close when
   it merges, the combined expected-file diff grouped by test function with before and after
   counts, the predicted tests that did not move and why, and the review rounds of step 5.
7. **The reviewer then runs an independent mutation review** on the open pull request and posts
   the surviving mutants; close each real gap with a test that fails without the fix, and say
   which you judged equivalent and why. The fixes are pushed to the milestone branch after the
   pull request exists, and their rounds are posted as comments, as in step 5.

The government sponsor approves changes; the NCI SI MCP project coordinator relays that approval,
and the reviewer acts for the coordinator.

8. **Merge only on the reviewer's clearance,** given as a PR comment that names the head commit,
   with `gh pr merge N --squash --subject "<title>" --body "" --delete-branch --match-head-commit
   <sha>`. A push after the clearance needs a new one. Wait for every workflow the pull request
   triggered to finish and pass before asking; never hand over on "CI is running".
9. **After the merge,** confirm CI, Audit, CodeQL and Release on the merge commit, that the
    issues closed, and that the release was cut. After confirming every milestone issue is
    closed, close the milestone explicitly through the GitHub API; GitHub does not close it
    automatically. Then remove your branches, worktrees, scratch files and any process or
    wait loop you started. Verify that no merged issue branches or the milestone branch remain
    on origin; check the branch-cleanup run and remove any leftovers yourself.

The context that creates a temporary file or directory, branch, worktree, process or watcher owns
its cleanup through the artifact's full lifecycle, not merely the current session. A handoff names
the owner and purpose, exact path or ref, retained-evidence location and cleanup condition; the
successor inherits that duty. Before deleting, verify project ownership, no active work/open
pull request/current dependency, a clean worktree or preserved changes, merge or supersession
status, and evidence-retention obligations. Remove only known-obsolete project artifacts. Preserve
retained evidence outside deleted worktrees, and retain unknown-owner items; never glob-delete,
reset another session's tree or remove another project's scratch. At milestone closure or session
handoff, record exact paths and refs removed and those retained with their reason and next owner.

The `milestone branches` ruleset forbids force pushes to `milestone/*` and requires no status
checks, because the milestone pull request into `main` runs the full CI on the milestone head and
`main` accepts nothing else. Issue work reaches the milestone branch by a merge of its issue
branch (steps 3 and 4). Review fixes (steps 5 and 7) are committed on the milestone branch
itself and pushed; run the local gates first, as for an issue. When `main` moves, sync it with a
merge commit (`git merge origin/main` on the milestone branch, then push), never a rebase or a
squash, so `main` stays an ancestor and the next sync does not conflict; `main` itself accepts
squash merges only. Findings made along the
way are fixed on the branch they belong to; only an unrelated problem gets an issue. Do not change
the ruleset, repository settings, `spec/`'s conventions or another issue's scope without the
reviewer's agreement.

Rules learned the hard way:

- A change to what the server returns reaches the acceptance harness: run the whole
  `pdm run acceptance-selftest` and the fixture ratchet, not only the unit tests. Keep
  `NCI_SI_ACCEPTANCE_PREPARE` unset for the self-tests; set it only on the fixture command above.
- An interrupted self-test run can leave the `compliant_server.py` stub running; find it with
  `ps` and stop it by its process id.
- A wait loop ends when the file or process it watches is gone, and never matches its own
  command line (`pgrep -f` on a string in the loop does).
- Before a run projected to take over an hour (mutation, live audit, benchmark), run about 20
  items of that run's own workload, break down where the time goes and explain every outlier.
  Launch the full run only with the projection from that sample stated in the issue.
- When a run falls more than 25% behind its projection, find the cause before waiting longer;
  a new estimate is not a cause.
- Name roles, never people, in code, issues and pull requests.
- Licence and attribution text is passed through from what the upstream API returns; the server
  keeps no licence data of its own. A licence key or credential is never logged or committed.

## Constraints that shape the code

- The core package has no dependencies (`dependencies = []`). `mcp` and `sentence_transformers`
  are optional extras, imported lazily inside `create_mcp()` and
  `SentenceTransformersProvider.__init__`. `cli.py` imports `server.py` at module level, so nothing
  at the top level of `server.py` may import `mcp`.
- `mcp` is bounded to `>=2.0,<3` because 2.0 renamed `FastMCP` to `MCPServer` and broke the unbounded
  requirement. Check the migration notes before lifting the bound.
- Stateful HTTP release pins and secured-mode principal binding read the SDK's private
  `ServerSession._connection` (`server._session_state`). The bound does not protect a private name,
  so `create_mcp` raises a `RuntimeError` at startup when it is missing; the real-SDK HTTP session
  test is the regression guard. Ask upstream for a public accessor when lifting the bound.
- `server._serve_schemas` rewrites each tool's published schemas through the SDK's private
  `mcp._tool_manager` and `Tool.fn_metadata.output_schema`. A rename fails at startup with an
  `AttributeError`; `tests/test_server.py` compares the published schemas.
- Synchronous tool callbacks run in a worker thread through `anyio.to_thread.run_sync`, which
  copies the calling task's context. The correlation, audit record, permission, session
  release-pin and cache-decision context variables, set by server middleware, reach a handler
  only through that copy; `registry.invoke` opens the deadline and per-call release selection
  inside the worker. A value set in the worker does not flow back, hence the mutable carriers
  (the cache-decision dict of `caching.cache_call`, the `audit.Audit` record and
  `release_selection.SessionRelease`).
- The package must be installed to be imported: `__version__` reads the installed metadata.

## Architecture in brief

`cli.py` and `server.py` read `registry.SPECS`. Each `ToolSpec` declares its handler, output
union, cache policy and adapter exposure; the handler signature supplies the shared input model,
defaults and choices. Business operations live in `handlers.py` and, for the specification's
content tools, `content.py`, with injectable collaborators in `context.Context`. Closed value
sets live once in `validation.py`. A profile selects the tools, resources and prompts the server
serves (M1.5, M1.6); caller policy can only narrow that surface.

`bounds.py` owns the shared call deadline; `ToolSpec.deadline` exempts long-running index
operator commands. `validation.py` owns input bounds in `TEXT_LIMITS` and
`MAX_TEXT_TARGET_BYTES`. Every limit is a named constant with a comment stating its reason,
evidence and what to re-measure; a new limit also needs a spec `why`, an `inputSchema` entry
and a QUICKSTART limits row. Tool descriptions carry the shared deadline and input-limit
wording from one source so clients receive the same contract.

Each ToolSpec classifies its parameters as plain or hashed for audit. Undeclared parameters
default to hashed. `audit.py` emits one JSON completion record per call, including validation
failures, with correlation, result size, truncation and request counts from the HTTP client's
per-attempt instrumentation. MCP and registry scopes share the record; concurrent calls do not.
Free text is SHA-256 hashed to correlate repeated inputs, not to keep guessable text secret.
All application diagnostics are JSON on stderr; exception messages and upstream bodies are
not logged. Diagnostic verbosity does not suppress the required completion record.

### One error path

The error codes are those of the specification's error record (`spec/records.yaml`), closed in
`errors.py` as `ErrorCode`. A failure is a `PlatformError`: its code, a message that names the
caller's next step, and the `details` that `spec/records.yaml` lists for that code (`error.detail_keys`). `errors.serialise` is
the only function that turns one into the result, `{"error": {"code", "message", "details"?,
"correlationId"}}`; nothing builds that dict by hand. The audit boundary opens `errors.correlated()` once
per call (the request's `_meta.correlationId`, else generated) for tools, resources and CLI.
`registry.invoke` shares that scope and enters `invocation.call` for expected failures.
The raisers own their wording (`cadsr_matching` the match-timeout step, `cadsr_content` the
OP-C03 hint on `upstream.MaskedSuccessError` only); `invocation` never branches on a tool name.
A result that embeds an error uses `invocation.error_record`, which logs no `call_failed`.
`invocation.call` converts the expected exception types listed in `invocation._ERROR_CODES` (with the next
step appended to their message and their `details` attribute carried over) and logs a warning; an
exception gets the entry of its nearest listed class. To add a failure mode,
raise a specific exception type and add it to that table, or raise a `PlatformError` where the
message needs data (the releases served). Anything not in the table is a bug and propagates: do not
add broad `except` clauses. An empty result is never an error and an error is never empty.

`upstream.parse_upstream_json` is the one place that classifies a failure masked as a success
(webMethods `apiResponse.type` `E`, FHIR `OperationOutcome` error, HTML where JSON was asked for)
as `upstream_unavailable`; every upstream client parses its bodies through it (in `http_client.HttpClient`).

The reviewer-approved Form-by-ID exception lives in `cadsr._form_absence`: after public-id
validation, only HTTP 200 with an explicit `form: null` and `apiResponse.type: E` is `not_found`.
The recording `recorded/cadsr/form-unknown.json` has no other discriminator, so a genuine failure
in exactly that shape is indistinguishable; the upstream package (`docs/upstream/cadsr.md`,
cadsr-forms) asks caDSR for an explicit absence signal. No message
matching or second request. Every other shape, status and operation keeps common X-15 handling.

`http_client.HttpClient` is the one HTTP client. Its `Upstream*` errors reach the invocation
boundary directly; EVS errors identify only EVS-specific failures. Attempts are counted and reported to
the `on_request` hook. A credential is a header of one client and goes to that client's origin only;
it is redacted from every message built from what the platform said.

`ncit_index.index.LocalIndex._connect` turns SQLite failures of the database itself (locked,
unreadable, not a database) into `ncit_index.errors.IndexStorageError` naming the file;
constraint and usage errors propagate as bugs. Index exceptions belong to `ncit_index.errors`,
not MCP `errors.py`; `invocation._ERROR_CODES` maps them with existing messages and details.
A 404 from EVS means "no such concept" only for `EVSClient.get_concept`. Every other method goes
through `_get_existing`, which converts a 404 to `EVSResponseError` (a wrong base URL).

`server.py` turns an error record into a protocol-level error (`CallToolResult(is_error=True)` for
tools, `ResourceError` for resources). The handler docstrings are the contract sent to MCP clients;
update them when behaviour changes.

### Release pinning

- `release.resolve_evs_release(evs, terminology, channel)` asks EVS for the rows that are `latest`
  and tagged with the channel (`?terminology=…&latest=true&tag=…`) and requires exactly one; any
  other count is `release_not_available`, with no fallback to another channel. EVS sets `latest`
  per channel, so the unfiltered listing can show two `ncit` rows as latest. `resolve_release`
  resolves the channel once per call. NCIt content calls may omit `release`: the shared
  invocation scope resolves it once or reuses the first implicit pin of a stateful handshake
  HTTP session or stdio connection. Sessionless 2026-07-28 HTTP resolves per call regardless of
  `NCI_SI_HTTP_SESSIONS`; callers needing stable content pass the first result's provenance
  release explicitly. Explicit
  calls never change that pin; other terminologies require release. No process-wide pin is
  retained. Stateless HTTP and CLI resolve per call. A withdrawn session pin fails closed
  without rediscovery, asking for a new session or explicit release. Completion audit names
  explicit, session-held or freshly-resolved selection.
  A 404 `Terminology not found` is `EVSReleaseNotFoundError`
  (`release_not_available`).
- `release.registry_state` is the pure part of the caDSR registry state: no registry identifier is
  ever made up. The caDSR client reads the exact distribution row in the export folder;
  its local date-time has minute precision and no timezone, which is preserved without an offset.
- Every concept request uses `release.pinned_terminology` (for example `ncit_26.09d`) as the path
  segment, and `evs.verify_content` checks the terminology and version of each full concept at the client
  boundary. Content methods require a `ReleaseContext`; compact descendant entries name the
  release addressed by their request without inventing upstream version fields.
- `lookup` returns `release_mismatch` when the index holds another release, unless `live_only`. It
  falls back to the cache only on `UpstreamUnavailableError`, and marks the result with `fallback`.
- Each index build holds one release. Activation retains the previous build for rollback.

### Index and search

The interim index is the top-level `ncit_index` package. Its modules, schema, build lifecycle and
search are described in [ARCHITECTURE.md](ARCHITECTURE.md#portable-index-package); the rules
below are the ones a change must keep. The package imports no `nci_si_mcp` or MCP modules, and
NumPy and sentence-transformers stay lazy. Stored records and index exceptions belong to the
package; EVS normalization and protocol provenance stay MCP-side.

`Context.index` is the read-only `IndexPort` (`index_port.py`), implemented by
`LocalIndexAdapter`; `cursor.py` remains MCP-owned. Context has no operator accessor: CLI index
commands and `evaluation.py` open the package's public operator API through the small factory in
`indexing.py`, never through the read port.

`ncit_index.index.LocalIndex._connect()` commits or rolls back and then closes; use it for
every database access. Sample activation checks that another writer has not changed the active
build in the meantime; when it has, the new inactive build is deleted before the error is raised.
Release mix and missing or duplicate codes are checked once, per batch, as fields are prepared.
Model providers return float32 rows, which `vector_bytes` stores without Python floats.

### Traversal

`traverse_ncit` walks breadth-first, one depth at a time over all start codes; the walk, its
limits, the continuation checks and the `descendant` edges are described in
[ARCHITECTURE.md](ARCHITECTURE.md#traverse). Each batch is cut down to the keys the walk uses as
it is extracted, so a frontier's memory follows the node and edge limits, not the response sizes.
One table (`EDGE_KINDS`) says, per edge type, the relation list, direction, include flag and
whether it is a hierarchy link. Walks that follow inverse roles or inverse associations use
batches of 10, because those lists run to megabytes for hub concepts; a batch that exceeds the
response limit is halved. The state of a walk (limits, emitted nodes and edges) lives in the
`_Walk` object in `traversal.py`, which also builds the `TraversalProvenance` of each node and
edge. All reads share the request budget, and the first bound remains the one reported, except
that start codes the budget left unread are reported as `requests` for every selected edge type,
replacing a depth claim made without them (an earlier real bound stays).

The `level` EVS gives a descendant can be deeper than its shortest path, so a `child` walk of the
same depth can reach more concepts (59 against 56 for C3262 at depth 2). The traversal handler
calls `select_edge_types` before resolving the release, so invalid selections never reach the
network.

## Tests

Every child program, SDK call and HTTP path has at least one test that really runs it.
Fakes may cover the remaining cases.

### Outside in test design and review

Apply the [outside-in testing skill](https://gist.githubusercontent.com/imaurer/ac31f596bcfd7f46afe1c7dceedcba21/raw/faf1bfda7316a647cf0bd5a03d433dae5a7d4fa2/outside-in-tests.md)
at this pinned revision, as summarized here. This tracked guidance is self-contained.

- Describe the caller's scenario as Given/When/Then before implementation; demonstrate
  the intended failure, implement the behavior, then review the tests for retention.
- Prefer MCP, CLI and HTTP boundaries. Test a component directly when its interface owns
  an independent contract, such as parsing, storage or ranking.
- Retain observable behavior checks, boundary/input tables, externally relied-on contracts,
  and demonstrated regressions. Remove development scaffolding that protects no distinct claim.
- Identify each claim's owning test. Additional layers need a different failure mechanism
  or contract, not repetition of the same assertion.
- Before adding a test, identify its behavior, a plausible regression, the existing coverage
  gap, and a real interface through which to exercise it. Avoid test-only production switches.
- Review internal spies, setup-derived assertions and historical test groupings for coupling;
  names should explain behavior. Neither mocking nor a unit-test label alone proves redundancy.
- Use coverage to locate gaps and targeted mutations to evaluate sensitivity and uniqueness.
  Replace a sole behavioral check before removing it; consolidate in small verified batches.
  Correct product defects rather than weakening expectations to obtain green tests.

`tests/fakes.py` holds `FakeEVS`, an in-memory stand-in for `EVSClient` that records calls,
honours `include` and returns batches in the request's order rotated by one (EVS keeps no batch
order), plus `concept()` and `release()` builders. Nothing touches the network.
`test_server.py` drives the real server through an in-process `mcp.client.Client` session.

## EVS facts worth knowing

Verified live on 2026-10-01 against release 26.09d.

- An unknown code returns HTTP 404 with a JSON body whose `message` says so; the batch endpoint
  (`?list=`) silently omits unknown codes instead, and keeps no order: mostly lexicographic, but
  the same request came back in two orders a minute apart (2026-10-02). Never pair by position.
- On 2026-10-02 one machine's IPv6 route to EVS failed while IPv4 worked, and Python's urllib
  waited about 120 s per request. If live calls are that slow, check `curl -4` against `curl -6`
  and force IPv4 in a scratch wrapper; it is not a code problem.
- `/concept/{terminology}/{code}/descendants` returns every level unless `maxLevel` is given
  (15,808 concepts for C3262); each item carries its `level`.
- Responses carry no `Content-Length`, so an oversized response is only detected after reading up
  to the limit.
- The terminology listing holds every served release (33 rows), including older monthly NCIt
  releases with `latest: false`; the current monthly row can carry both `monthly` and `weekly` tags.
- `/descendants` items are ordered by name, not by level.
- A batch of 100 concepts with all six relation lists took 1.3 s and 625 KB for ordinary concepts.
