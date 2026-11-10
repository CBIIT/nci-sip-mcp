# Change log of the specification and the acceptance suite

The NCI SI MCP project coordinator is the code owner of `spec/` and `acceptance/`.

From the furnished tag, every change to `spec/` or to the suite is an entry here with three
things: the change, the requirement it serves, and its approval reference, the written approval of
the branch chief or a delegate ([spec/acceptance.md](../spec/acceptance.md)). Each approved
release of the suite has its digest in [approved.yaml](approved.yaml); a report from any other
tree renders MODIFIED.

## Unreleased: before the furnished tag

Changes until then are development under the plan and need no approval reference. The suite is
under construction: the per-tool tests, the protocol gates, the prompts and resources, and the
fixture set pinned to NCIt 26.09d.

- #465 (explicit controlled transport): the opt-in entry handshake finishes ledger admission
  before core entry accounting and retains uncertain entry receipts conservatively. Existing
  callable providers and unconfigured transport remain unchanged; furnished caller adoption
  is separate. Contemporary red/green and review evidence is recorded on the issue.
- #453 (§5 recorded-run provenance): selected-environment stdio benchmarking reuses the managed
  policy/isolated launcher, preserves strict recorded identity through benchmark evidence and
  comparison, and displays supplied-live provenance in combined reports and the local portal.
  Historical omission and all fixture outcomes remain unchanged; recorded policy grants no access.
  Plan approval/corrections: [6708070839](https://github.com/CBIIT/nci-sip-mcp/issues/453#issuecomment-6708070839),
  fixture compatibility: [6708189332](https://github.com/CBIIT/nci-sip-mcp/issues/453#issuecomment-6708189332).
- #447: require the reviewed named environment for managed live execution and preserve its
  immutable, credential-free provenance through native/evidence reports. Its temporary live
  benchmark refusal was superseded by #453.
  Block caDSR
  citation transport pending #448; preserve fixture comparison and all fixture expectations.
  Plan approval: [6678764601](https://github.com/CBIIT/nci-sip-mcp/issues/447#issuecomment-6678764601),
  scope revision: [6679884146](https://github.com/CBIIT/nci-sip-mcp/issues/447#issuecomment-6679884146).
- Phase 5 #41 (A11.1–A11.2): allow the stdio client helper's caller to select its read
  timeout, retaining 60 seconds for acceptance tests. The benchmark uses 300 seconds so
  upstream retries can finish and provide their actual audit counts. A real-session
  self-test verifies the override; no assertion, request form or fixture outcome changes.
  Correct the fixture README's claim of live verification for credentialed content: the
  current live-capable tests cover protocol behavior only.
- Phase 15 #265 (§5 report outcomes): a gate that cannot run makes every tool that would pass
  INCOMPLETE: the rendered report shows "INCOMPLETE (gates not run)", the README status table
  lists those tools under INCOMPLETE.
  A skip is classified by the property its site sets (`acceptance_skip`), not by its reason
  text. The weekly live workflow's fixture run runs the permission gates as CI's does. No test
  assertion of the suite or fixture outcome changes.
- Phase 15 #221 (§5 fixture set): the 1,583 fixtures `craft.py` makes are no longer committed;
  `fixture_server.load_fixtures` builds them in memory when the manifest declares them
  (`crafted:`, the digest of what `craft.py` makes now). `pdm run acceptance-craft` writes that
  digest instead of files (`--check` verifies it, `--out DIR` writes the fixtures to read), and
  a crafted name that also lies on disk is refused. The recorded fixtures and every test's
  answers are unchanged.
- Phase 15 review (§5 outcomes, `spec/acceptance.md`): INCOMPLETE also covers a gate that could
  not run (skipped or not implemented); a gate a live run skips by design (fixture mode only)
  leaves its tools as they are. Agreed by the reviewer on #265.
- Phase 33f #359 (resolve_registry_release-1, X-21, X-17): harness-only optional/required
  registry selection; optional ordinarily skips five retained discovery/published-pin/cursor
  checks, required keeps their assertions. Both modes are self-tested in existing CI. Sponsor
  proposals remain documentation only; no server behavior or historical live report changes.
  Approval: https://github.com/CBIIT/nci-si-mcp/issues/359#issuecomment-6399041641.
- Phase 33j #366 (X-1, X-6–X-7, X-9, X-13–X-15, X-17–X-18, X-20–X-21,
  P-6, P-9 and the marked EVS, caDSR, cross-domain and workflow requirements): mark
  229 anonymous cases live-capable, per parameter where contracts differ. All 229 passed
  in first hosted run 40255800463. Its two no-index failures were misclassified because
  local verification omitted preparation while the hosted live job prepares an index;
  reclassify them as d and retain their fixture assertions. Green hosted confirmation
  run 40257975716 observed 276 passed, 0 failed, 710 not_live and 5 skipped.
  Remove the redundant prepared prerequisite from
  six invalid-retired-value index-mode cases after each passed without preparation.
  All 991 fixture outcomes and all suite assertions remain unchanged. Extend the existing
  two-worker report check to cover marked/scenario siblings; retain failure and drift owners.
  Stored scheduled-main evidence is refreshed only from the first scheduled main run after
  merge. Approvals: https://github.com/CBIIT/nci-si-mcp/issues/366#issuecomment-6405785833
  and https://github.com/CBIIT/nci-si-mcp/issues/366#issuecomment-6406450495.
- Phase 33j round-one review follow-up (#366): move the live marker from the unsupported
  terminology test to its `as-prepared` parameter only. Its two index-less variants stay
  fixture-only, preserving all assertions and leaving 227 net additional live-capable cases
  from the first hosted run's 229. The follow-up hosted selection expects 274 passed,
  0 failed, 712 not_live and 5 skipped (991 total); a new milestone Acceptance (live)
  dispatch is required to confirm it. Run 40263866032 subsequently confirmed these exact
  outcomes with all 991 cases finished. Preserve earlier runs, the separate caDSR outage
  failure 40261513919, and all historical JSON.
  Clarify that live mode reuses the shared server despite `unprepared`, rather than claiming
  that every test bearing that marker is necessarily unsuitable for live selection.
  Review reference: https://github.com/CBIIT/nci-si-mcp/issues/366#issuecomment-6406638151.

<!-- The shape of a release, from the furnished tag on:

## v1.0.0: <date>

Digest: <64 hex characters, as in approved.yaml>

- <the change> (serves <requirement id>; approval: <reference>)
-->
