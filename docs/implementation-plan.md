# Implementation history: the shared NCI Semantic Infrastructure Program MCP platform

**Status:** record of delivered work (Phases 0 to 6 are merged; Phase 7 is recorded in [portal-plan.md](portal-plan.md); open work is on the GitHub milestones) · **Written:** 1 October 2026 · **Last updated to the code:** 2 October 2026, `main` after `v0.2.0`; later sections carry their own dates · **Furnished commit:** tagged when the owner cuts it (§1.2, §11)

For current module boundaries, use [ARCHITECTURE.md](../ARCHITECTURE.md); this document retains
the dated implementation decisions and original baseline rather than a current setup recipe.

The work is tracked on GitHub as one milestone per phase and one for the furnished package (§11), with one issue per deliverable; issues cite this document by section. Where a section describes the code "today", it means `main` at the *Updated* date above.

This document is the implementation plan: how `nci_sip_mcp` is extended from the original EVS-first implementation into the shared platform, the EVS module and the caDSR module, together with the acceptance suite that is the acceptance instrument for both. It is written against the code as it is, so every change is stated as a delta from a named module, and it is ordered so that each phase leaves the repository releasable.

The specification of the required tools and their behaviour is the source of record. It lives in this repository as data in `spec/` (conventions, tools, requirements), rendered as [`docs/specification.md`](specification.md), and is cited below by convention id (A1–A11, M1–M5) and requirement id. The requests to the platform teams are the [upstream requirements](upstream/README.md). Where this plan is more specific than the specification, it is because the code forces a decision the specification leaves open.

---

## 1. Scope, baseline and ground rules

### 1.1 What is being built

| Deliverable | Content | Owner |
|---|---|---|
| **Shared platform** | Transport, authentication/authorization hooks, audit, common error and provenance envelopes, configuration, deployment profiles, release model, bounds, caching hints, `outputSchema` generation | this repository, `nci_sip_mcp.platform` |
| **EVS module** | 12 terminology tools over EVS REST, EVS FHIR and the interim NCIt index | `nci_sip_mcp.evs` |
| **caDSR module** | 10 metadata tools over the caDSR Open APIs | `nci_sip_mcp.cadsr` |
| **Cross-domain** | 4 seam tools over the Shared SI Service and the two REST surfaces | `nci_sip_mcp.seam` |
| **Workflow** | 3 composite tools | `nci_sip_mcp.workflows` |
| **Acceptance suite** | Harness, fixture server, fixture set, direct tool dispatch, per-tool tests | `acceptance/` (separate package in this repository, separately versioned) |

Twenty-nine tools in total, named and typed exactly as in the specification (`spec/tools.yaml`). Three profiles: `evs`, `cadsr`, `unified`. A profile selects the tools, resources and prompts the server serves (M1.5, M1.6); the surface within a profile is static (M1.2).

### 1.2 Baseline

Done:

- The hardening branch is merged (#1) and tagged `v0.1.0`. It changed traversal node codes, embedding-provider selection, the release check in `upsert_concepts` and the error module, and added `ARCHITECTURE.md` and CI.
- The migration to `mcp>=2.0,<3` is committed.
- Since `v0.1.0`: Python 3.13 and PDM (#43), every function below cyclomatic complexity 8 (#44), the lint and quality gates with the engineering standards in `CONTRIBUTING.md` (#45), and automatic releases from Conventional Commit pull request titles (#46). `v0.2.0` is the first automatic release.
- The package is licensed under the Apache License 2.0 (#50).

Remaining:

- Tag the furnished commit, when the owner decides to cut it (§11), as `baseline-YYYY-MM` for the month it is cut (`baseline-2026-10` if that is this month). This is the commit the baseline package furnishes and the commit the baseline run reports against. It is a manual tag beside the automatic `vX.Y.Z` release tags.

### 1.3 Ground rules carried forward from the current code

These hold today and continue to hold:

- The core has no runtime dependencies. `mcp` and `sentence_transformers` are optional extras, imported lazily; nothing at module level in `server.py` imports `mcp`. The supported Python is 3.14 and newer (owner decision of 3 October 2026, replacing 3.13 and newer of 1 October 2026, which replaced the earlier rule that the core stay importable on Python 3.9).
- The package version is derived from the git tag at build time and written in no file; `setup.py` is gone.
- Unit tests are `unittest.TestCase` classes run by pytest, offline, with hand-written doubles, and every change passes the gates in `CONTRIBUTING.md`. The acceptance suite is a separate package with pytest conventions of its own (fixtures, markers, a fixture server), so that the two do not meet.
- The removed caDSR stub never returned fabricated CDE data. That rule survives: **no tool returns content it did not retrieve from a platform or from a fixture that declares itself as such.**

### 1.4 Ground rules that change

| Original baseline | Target |
|---|---|
| Every concept request addresses `ncit_{release}` of the one current monthly release, and each concept payload's `version` is checked; the terminology and the release cannot be chosen. An unknown release is `evs_invalid_response` on the batch and descendants endpoints but `concept_not_found` on a single-concept lookup (a 404 there cannot be told from an unknown code), and a payload mismatch is `evs_invalid_response` | Every content request addresses `{terminology}_{release}` from the call's `ReleaseContext` explicitly (verified to work and to fail closed with 404 on an unknown release); the payload check becomes a second guard, not the only one; an unknown release is `release_not_available` and a payload mismatch `release_mismatch` (A3.1, A3.4) |
| Expected exceptions are mapped to codes by one table (`_ERROR_CODES`, applied by `service._enveloped`); with the envelopes that the service, the CLI and the resources build directly, `ErrorCode` has fourteen values | One error model, the error record's closed set of codes (A2.5), one serialisation — every tool returns a structured error through the same path (§3.1) |
| `include_raw` and `live_only` are MCP tool parameters | Both are removed from the MCP surface. `include_raw` stays on the CLI for debugging; `live_only` becomes the `servedBy` field in provenance, reported rather than requested |
| Edge types are selected independently of relationship names, but the name filter also applies to hierarchy edges, which carry the pseudo-names `is_a_parent`, `is_a_child`, `is_a_descendant`: a role-name filter drops them unless those names are listed | Edge kinds and relationship names are separate fields, and hierarchy edges carry no invented name; a filter on one never silently removes the other (A5.5) |
| Exclusion polarity is not represented | Polarity is derived from the relationship **code** against the pinned release's catalogue, never from the label (A5.7) |
| Traversal bounds nodes and edges, nearest first | Implemented in §3.5: the budget also counts outbound requests **including retries** (A5.3), with per-kind node allowances |
| `cadsr_status` is a stub | The caDSR module is real; the stub is deleted |


---

## 2. Package layout

```
src/nci_sip_mcp/
  __init__.py
  config.py                 settings, profiles, upstream base URLs, timeouts, credentials source
  cli.py                    thin adapter; gains cadsr-* and seam commands
  server.py                 thin adapter; builds tools from the registry below, per profile

  platform/
    errors.py               the taxonomy (A2.5) and the single serialiser
    provenance.py           ProvenanceEnvelope, TraversalProvenance, Truncation (A4, A5.4)
    release.py              ReleaseContext: EVS release, caDSR registry state, SI graph identities (A3)
    bounds.py               Budget: outbound requests incl. retries, nodes, edges, per-kind (A5)
    http.py                 one instrumented HTTP client: retries, Accept, correlation header, request log hook
    audit.py                structured audit record per tool call (A6)
    caching.py              ttlMs / cacheScope policy per tool class (M2)
    results.py              typed wire records; SDK-generated outputSchema; static-surface assertion
    registry.py             ToolSpec: name, group, profile, input model, output model, handler
    transport.py            stdio and streamable-HTTP entry points

  evs/
    client.py               was evs.py; release-pinned addressing; batch; FHIR; replacements; mapsets
    catalogue.py            relationship catalogue per release with polarity by code
    traversal.py            reworked walker (A5)
    index/                  was index.py, embeddings.py, retrieval.py, evaluation.py
      store.py              SQLite; per-release tables; manifests with embedding metadata
      activation.py         atomic activation and rollback (M4.1)
      build.py              full-NCIt build from the batch endpoint
      evaluate.py           retrieval evaluation set and scoring protocol
    tools.py                the 12 EVS tools

  cadsr/
    client.py               Data Element, Form 2.0, LOV, Model, CDE Match, VM Match, FTP listing
    registry_state.py       export date and item versions in lieu of a registry release (A3.8)
    tools.py                the 10 caDSR tools

  seam/
    ssis.py                 Shared SI SPARQL client; required-parameter enforcement
    tools.py                the 4 cross-domain tools

  workflows/
    tools.py                the 3 workflow tools

acceptance/                 separate package, see §9
```

Implemented in the current flat package: `service.py` is retired. `registry.py` holds one `ToolSpec` per operation with its output union, cache default and adapter exposure. The four cross-domain tools omit that default because their handlers select the complete policy. Handler signatures supply typed input models, defaults and choices to both adapters. `handlers.py` composes the use cases; `context.py` holds injectable collaborators; `audit.py` owns the correlation scope; `invocation.py` owns the one expected-error path for tools, resources and CLI. Shared HTTP errors propagate unchanged; EVS exceptions identify domain failures only. The package-layout drawing above remains a target for later modules, not a reason to move existing files.

---

## 3. Shared platform

### 3.1 Error model (`platform/errors.py`)

Implemented with the closed `ErrorCode` literal in `errors.py` and the `_ERROR_CODES` table at the shared boundary in `invocation.py`, using the codes of the specification's error record (`spec/records.yaml`): each failure carries its `code`, a message, the call's correlation identifier, and the `details` the caller needs for its next step. The `details` keys of each code are owned by `spec/records.yaml` (`error.detail_keys`) and rendered in the specification; this document no longer repeats them.

Two rules. **An empty result is never an error**: a tool that matched nothing returns its normal shape with an empty collection and a complete provenance envelope. **A platform failure carried inside a `2xx` body is an error**: the HTTP client (§3.4) recognises the webMethods envelope (`apiResponse.type == "E"`), FHIR `OperationOutcome` with severity `error`, and an HTML body where JSON was requested, and raises `upstream_unavailable` before any tool sees the payload.

Serialisation: every tool handler returns a dict or raises one of the expected exception types; `invocation.call` converts the exception through `_ERROR_CODES` and `errors.serialise` into the error record, and `server.tool_call` returns it as an MCP result with `isError: true` and `structuredContent` conforming to the error schema. No handler builds an `isError` dict itself.

### 3.2 Provenance (`platform/provenance.py`)

Extend `models.py`'s per-concept fields into one `ProvenanceEnvelope` attached **per item** (A4.4), with the fields of the specification's provenance record (`spec/records.yaml`), and a `TraversalProvenance` adding those of its traversal record. For unpinned caDSR API content, `release` is `{registry: cadsr}` with neither identifier nor date; the export date does not identify newer API content (A3.8.2). The `raw` payload is dropped from MCP results and kept only behind the CLI flag.

`Truncation` carries the fields of the specification's truncation record (`spec/records.yaml`): `omitted` is always a number, with `exact` false where it is only a lower bound or an estimate (A5.4).

### 3.3 Release model (`platform/release.py`)

`ReleaseContext` is resolved once per tool call and threaded through every upstream request. For NCIt content, omitted/null release is resolved lazily in the shared invocation scope, or reused from the MCP session’s first implicit resolution (X-22). Explicit calls override without changing that pin. Other terminologies require release; stateless HTTP and CLI resolve per call. Withdrawn session pins fail closed and ask for a new session or explicit release. Completion audit names explicit, session-held or freshly-resolved selection. Implicit calls return 0/private; explicit-release calls keep the pinned long/public cache policy (M2.2). `resolve_release` and `resolve_registry_release` are discovery operations; where a content operation has no pinned form, A3.2 also permits its unpinned form with exact payload-release verification before content is returned, as for FHIR expansion.

**EVS.** `resolve_evs_release(terminology, channel)` calls `/metadata/terminologies?terminology=…&latest=true&tag={channel}` and requires exactly one row. It replaces `select_monthly_ncit_release`; `latest` is channel-scoped, and the one-row query moves the selection upstream. Zero or several rows, or a row without a version, raise `release_not_available`; `requested` names the requested channel and optional `found` lists the ambiguous versions. The serialized release contains `terminology`, `channel`, `version` and `date`; the pinned path stays internal. Content requests address `/concept/{terminology}_{release}/…`; a 404 with `Terminology not found` maps to `release_not_available`. The payload's `version` is compared as a second guard and a mismatch is `release_mismatch`, never silently accepted. The first #18 slice exposes `resolve_release(terminology, channel?)` as the flat release record plus provenance and `alternatives`: other served version identifiers for the same terminology, deduplicated in listing order. The channel query remains authoritative; the unfiltered listing supplies alternatives. `list_terminologies` selects each terminology's sole latest row, scoped to the configured channel for NCIt. Both are uncached status results and report top-level errors. The legacy MCP names are removed. CLI release reports use `selected_release`; resources require explicit versions, with all moving aliases removed.

**caDSR.** The pure `registry_state(generation_date, upstream_identifier, source_distribution=…)` builds the specification's `registry_release` record: `{published, identifier?, generatedAt, sourceDistribution}`. Without a published release, `published` is false, `identifier` is absent, and the export folder's row for `releasedCDEsXML-OD.zip` supplies ISO-8601 local `generatedAt` at minute precision without an offset (for example `2026-07-01T22:19`). The folder gives local server time with no zone; the client never assumes one or substitutes the ZIP's HTTP timestamp. When a registry release appears upstream (C-1), `published` is true and its identifier and own ISO-8601 date are passed through unchanged. `sourceDistribution` names the distribution the caller read the date from. A missing or invalid date, blank supplied identifier or missing distribution raises `RegistryMetadataError`; the shared error path reports `upstream_unavailable` with `surface: cadsr`. No identifier or date is invented, and registry reproducibility is not achievable while no registry release is published. The instrumented folder GET and exact-link row parser belong to #31; tool exposure belongs to #33. A missing or duplicate distribution row or unparsable date fails closed without a HEAD fallback.

**Shared SI.** The client reads both content graphs' `owl:versionInfo` / `dc:date` in one combined query, bounded to three rows for the two expected identities. It preserves the graph IRI, original date and optional version without comparing them with EVS. The two graph-joined content tools normalize dates for provenance and verify the NCIt graph against the call's effective release. Their implicit calls use 0/private; explicit mixed content uses a short public TTL. Release alignment independently discovers the current NCIt release and reports differences between dataset states, with 0/public caching (§3.6). No graph identity is inferred from another surface.

### 3.4 HTTP client (`platform/http.py`)

One client for all surfaces, replacing `EVSClient._get_json` and the per-module ad hoc calls:

- JSON API GET/POST requests send `Accept: application/json` (several caDSR routes return HTML otherwise); the export listing explicitly requests bounded text. Both carry the correlation header (M7.1).
- Retries `5xx`, `429` and connection errors except TLS certificate verification failures, with jittered backoff, **and counts only entered attempts against the call's `Budget`** (A5.3). Honours `Retry-After` on `429`.
- Classifies the response before returning it (§3.1): status, content type, webMethods envelope, FHIR `OperationOutcome`.
- Exposes a request-log hook. In production it feeds the audit record; under the acceptance suite it is what the fixture server mirrors.
- Holds credentials (licence keys, any caDSR credential) from configuration and never logs them.

### 3.5 Bounds (`platform/bounds.py`)

Implemented in `bounds.py`: the traversal handler creates one `Budget`, including depth, from caller limits clamped to the documented maxima and passes it explicitly to the walker. Its context variable is scoped and restored like the correlation context so the HTTP client uses that same instance. The HTTP client counts attempts, including release discovery, retries and split batches, against the 200-request allowance declared for hierarchy and neighborhood. Each cross-domain handler also creates a 200-request budget, or shares the enclosing call's budget; discovery, content requests and retries all count, and exhaustion returns `bound_exceeded` without partial content. For neighborhood and CLI traversal, exhaustion without graph content returns `bound_exceeded`; with graph content it returns a partial graph. The first bound that dropped anything wins, so request exhaustion reports `requests` only when no earlier bound applies. Hierarchy page replay instead fails with `bound_exceeded` whenever its request budget exhausts, asking the caller to narrow the query. Unknown per-kind omissions use the lower bound zero with `exact: false`.

`clamp_limits`, `clamp_edge_limit` and the `HARD_MAX_*` constants now live in `bounds.py`; the defaults and maxima are the tools' `bounds` in `spec/tools.yaml`. The walker rotates relationship kinds across each breadth-first frontier. Starts count against the global node limit; a kind's optional allowance counts only new nodes it admits. Existing-node edges, duplicates and filtered edges spend no node allowance. Truncation includes per-kind records for mixed-kind walks.

### 3.6 Caching hints (`platform/caching.py`)

The values are the specification's M2.2 and M2.3 (`spec/conventions.yaml`): explicitly release-pinned content 86,400,000 and public; implicit NCIt calls 0 and private; governed content no release pins (caDSR content while caDSR publishes no registry release) short and positive, at most 3,600,000, public; results computed from caller-supplied values 0 and private; the resolve tools 0 and public; `tools/list` long and public.

A tool result carries both in its `_meta` (M2.5). A cursor binds the effective release. Explicit-release cursors continue while EVS serves that release; withdrawal expires them. An implicit session pin instead fails with `release_not_available` and asks for a new session or a named release, without rediscovery (X-22). Replacing an active index build expires its cursors even at the same release.

Implemented for current producers in `caching.py` and the MCP adapter: explicitly pinned content and
the list/discovery surface use 86,400,000/public, resolution/status uses 0/public, and tool
errors and implicit NCIt calls use 0/private. List, discovery and resource-read hints are protocol result fields;
tool hints are protocol `_meta`, preserving other metadata. All EVS resource URIs are
release-pinned and use the content policy. Missing indexes and mismatched releases are
protocol errors, not status content. CLI release reports remain status results.
Registration supplies an optional cache default; when omitted, the handler owns the complete
policy. Response middleware reads the per-call declaration, not tool names, URI tables or JSON
content. Tests check handler-owned policies, renamed producers, concurrent calls and rejection
of successful responses without a declared policy.

Per the #14 review, cursors ship with their first producers in #23 (hierarchy) and #27
(search): pin the release, reject supersession with `cursor_expired` and both release
details, bind arguments as applied including limit/default equivalence, and test malformed
cursors and continuation to the end. No unused codec is introduced here. Unpinned caDSR
content and caller-computed policies ship with #33/#34; mixed-state and workflow policies
ship with #38/#39. Those issue bodies record the requirements; #14 covers cache policy only.

### 3.7 Output schemas (`results.py`, MCP adapter)

`outputSchema` is generated by the MCP SDK from the serialized result types in `results.py`,
success and error shapes alike. Standard-library TypedDicts describe the wire keys separately
from the stored dataclasses, including optional keys and recursive `perKind`. The adapter
wraps each ToolSpec's success/error output union in Pydantic's `RootModel`, imported lazily
with MCP: this preserves the object shape where the SDK's bare union support would introduce
a `result` wrapper. No custom JSON Schema generator or hand-written schema copy is needed.

Unit tests render `tools/list` in every configured profile, validate every schema and real
success/error results, and reject malformed records. They assert byte-identical listings
across release channels, upstream modes, calls and upstream failure (M1.2), and check rendered
descriptions for unfinished text and unsupported values against behavior (A2.3, A2.4).
Profiles select the current inventory: twelve tools for `evs`, ten for `cadsr`, and all
twenty-nine for `unified`, including four cross-domain and three workflow tools. The legacy MCP names and caDSR stub are removed. An input schema states no
`maximum` for a bounded argument: a value above it is applied as the maximum (the tools'
`bounds` in `spec/tools.yaml`), and the argument's description states its default and maximum.

### 3.8 Transport (`platform/transport.py`)

stdio stays. Add the NCI-approved remote transport — streamable HTTP in `mcp>=2.0` — behind the same registry. Authentication and authorisation are hooks on the transport layer with a no-op default; the mechanism is NCI's to approve, and the hook is what lets it be supplied without touching tools.

Implemented in `transport.py`: stateful HTTP retains process-local sessions and needs affinity;
stateless HTTP resolves implicit releases per call and needs none. A known ID sent to another
stateful process returns 404. SDK auth/scopes run before dispatch, Host/Origin lists remain
enabled, and oversized bodies return 413 before parsing. Readiness verifies a supplied active
index locally. [Transport documentation](transport.md) records the protocol distinction,
settings, startup requirements and reproducible remote fixture CI gate. Production runbook: #121.

### 3.9 Audit (`platform/audit.py`)

Phase 1 implements this in `audit.py`: one JSON completion record per call, including MCP
validation failures, with timestamp, correlationId, tool, safe supplied parameters, target
(terminology/context), requested/resolved release context, status/responseCode, outboundRequests
including retries, resultSize, elapsedMs and truncation. Result size is the UTF-8 byte length
of compact JSON content, excluding protocol framing; no structured result means null. Full
per-kind truncation is retained, but result content is never logged.

Each parameter's plain/hash class lives in the ToolSpec, beside input/output and cache policy.
Undeclared fields default to hashed. SHA-256 permits correlating repeated inputs; it does not
keep guessable public terminology queries secret. Credentials and echoes are redacted at the
record boundary. Expected errors log their code, unexpected errors their type and propagate;
exception messages and raw upstream bodies are excluded. All diagnostics are JSON on stderr,
with hashes for external messages. Diagnostic verbosity does not suppress completion records.

MCP and registry layers share a request-scoped audit context, preventing duplicate records
and concurrent counter leakage. Actual HTTP attempts feed the count through the existing
instrumentation, preserving optional observers and the independent traversal budget. No
local rate limiter, persistent audit store, keyed hash or unapproved platform audit header is
added; transport authentication hooks are supplied by #40, and consumer audit integration
and its verification remain #41.

---

## 4. EVS module

### 4.1 Client (`evs/client.py`)

Delta from `evs.py`:

| Method | Change |
|---|---|
| `get_concept`, `get_concepts_by_codes`, `get_related`, `search` | Address `/concept/{terminology}_{release}/…`; take `ReleaseContext` |
| `get_concepts_by_codes` | The endpoint omits unresolvable codes silently and keeps no order: mostly lexicographic, but the same request answered in two orders on 2 October 2026. Traversal and indexing already reconcile requested against returned codes by code and request only the relation lists they need through `include=`. The public `get_concepts` returns `{concepts: [concepts], missing: [codes]}` in input order, preserving duplicate input occurrences while deduplicating the platform request. It rejects more than 650 supplied codes or a request target over 7000 encoded bytes before HTTP; see ARCHITECTURE.md for the measured URL margin (get_concepts-1) |
| `get_replacements(code, release)` | Implemented for the single-code `resolve_retired_code`: `/history/{t}_{r}/{code}/replacements`. Active is taken only from the verified concept boolean, with status unchanged; active concepts need no history request. A history 404 is an error; only a successful empty list or rows with no replacement code yield no replacements. Compact rows carry request-pinned provenance and validate any optional upstream terminology/version. The batch endpoint errors the whole batch on one bad code, the opposite of batch concept lookup; no unused batch/split/retry layer is built for this one-code tool |
| `get_roles_catalogue(release)`, `get_associations_catalogue(release)` | New; feed `evs/catalogue.py` |
| `get_concept` for subsets/maps | `get_concept_subsets` reads `minimal,associations` and selects `Concept_In_Subset`; `get_concept_mappings` reads `minimal,maps`, preserving record values and order with an exact target-label filter. No subset/mapset wrappers are needed for these tools. Cross-domain mapset reads belong to #38; the 9 subset/mapset paths and 18 mapsets remain endpoint evidence, not these tools' contract |
| FHIR expansion | Implemented through the shared HTTP client: EVS's unpinned R4 `$expand` is verified against the requested release under A3.2. NCIt subsets only; count (default 200, maximum 1000), offset (default 0) and activeOnly (default false) apply locally. Offset-continuable pages are not truncation, including when count is clamped. The recorded C85492 expansion has 534 members. `$lookup`, `$validate-code`, `$subsumes` and `$translate` need a caller contract (tool name, inputs and record) before implementation; no unused wrappers are built |
| `search` | `type` restricted to the seven documented values; `exact` is accepted upstream but behaves as `OR` and is **not** exposed |
| licence-restricted terminologies | `X-EVSRESTAPI-License-Key` from configuration when the terminology requires it; the 403 message is mapped to `invalid_request` with the terminology named |

### 4.2 Relationship catalogue (`evs/catalogue.py`)

Implemented in `catalogue.py`: each call reads the caller-selected release’s roles and associations once, inside its request budget, and verifies every row’s terminology/version and unique code/name identity. `list_relationships` returns code, terminology, name, kind, polarity and provenance. The NCIt exclusion set is configured by code, with defaults pinned by a test to spec/records.yaml (R135–R142); other terminologies use no exclusions today. Listing and neighborhood calls fail closed with `internal_error.details.missingCodes` if configured codes are absent. This check happens before returning content, without startup network access or a cross-call cache. The added error detail key resolves the error/relationship record contradiction in the specification. Traversal receives the same terminology-scoped configured set; names never determine polarity.

### 4.3 Traversal (`evs/traversal.py`)

Implemented: hierarchy and neighborhood are separate tools. Hierarchy excludes the seed from its node allowance and has no edge limit; neighborhood includes the seed and applies both node and edge limits. Both share a request budget and return projected concept records with verified status and traversal provenance. Hierarchy paging replays the pinned breadth-first walk with a page window and lookahead under 200 requests; pages can continue past 1,000 total nodes. Page boundaries do not truncate content, and exhausted replay fails with bound_exceeded, asking the caller to narrow the query. pathsToRoot returns all platform paths and unique reached nodes, ignoring depth, limit and cursor.

Each depth is read in batches of 50 concepts, or 10 when inverse relations are followed, asking for minimal content and only the selected relation lists (A5.8). Oversized batches halve; a single oversized concept stays unexpanded and contributes to upstream_cap truncation and a structured diagnostic. Final nodes without fetched payloads are hydrated with minimal content. Descendant edges come from one /descendants request per start code. Limits are claimed nearest first, with per-kind rotation.

The bounded final-frontier check distinguishes depth cuts from leaves and cycles, counting distinct unseen targets one level further with exact=false. Descendant checks read child lists. Selected inverse kinds report unknown continuation at a nonempty final frontier with omitted=0, exact=false, regardless of which kind reached those nodes. Their expensive lists are not fetched solely for this check. A global node cut or a prior kind cut skips the corresponding check.

Negative edges carry polarity by code. Targets remain visible but are expanded only through positive routes unless includeNegative=true. Re-admitted positive edges can reopen an earlier negative-only target. Expansion depth follows the eligible route; a node retains its first arrival provenance. Computed associations without upstream codes stay positive and retain their name, qualifiers and evidence. The legacy CLI name filter applies only to non-hierarchy assertions; hierarchy pseudo-names are removed.

### 4.4 Index (`evs/index/`)

The interim index (M4.1), built from `index.py` / `index_storage.py` / `index_scoring.py` / `embeddings.py` / `evaluation.py`:

- **Per field (implemented #28):** preferred name, synonyms and definitions are embedded and indexed separately, deduplicating identical text within a concept. The winning field is `matchedOn`; ties prefer name, synonym, definition. Exact preferred-name queries ignore case after Unicode NFC and whitespace collapsing, score 1, and win ties before other concepts.
- **Build snapshots (schema 6):** internal build ids permit same-release rebuilds beside the active snapshot. Concept/field rows refer to that build. Manifests carry release, embedding configuration, build time, build classification and internal evaluation reports. The public record contains only terminology, version, concepts, embedding, builtAt and provenance. Each build has its own FTS corpus. Schema-5 migration preserves those identities, activation, FTS ids and retirement status while grouping float32 field vectors into one BLOB per concept.
- **Atomic activation and rollback:** activation selects a completed build in one transaction and retains only it and its predecessor. Activating that predecessor is rollback. Embedding runs outside transactions; bounded writes use a building state, followed by a short completion transaction. Partial builds cannot activate or serve search; the next start cleans stale builds, distinguished from running builders by a private SQLite lease. Legacy raw concepts/manifests survive migration; legacy search returns capability_unavailable naming the explicit offline rebuild command. No implicit model download or rebuild occurs at open.
- **Full NCIt build:** unfiltered pinned `/concept/{terminology}/search` pages of 1000, not the batch endpoint (which requires a code list). Every page must preserve the total and pinned release, and all codes must be unique with their final count equal to total. Reconcile the spooled download before writing an inactive snapshot. Log progress every ten pages and at completion; shared HTTP retries apply, but tool-call request budgets do not. `index-sample` remains the developer command; CLI operations add build, list, offline rebuild and activation.
- **Embedding compatibility:** provider and model are validated together at startup. A provider, model or dimension mismatch is refused on every write and search. Vectors and queries round consistently to float32; scoring normalizes them and treats a zero vector as zero cosine. Malformed BLOB lengths and nonfinite stored values raise storage errors.
- **Exact paged search (implemented #27):** no LSH or candidate cutoff remains. NumPy is lazy and optional through the `index` extra. One scan computes field cosines in bounded chunks; compact numeric arrays provide normalization, sparse BM25 combination, winning fields and exact page partitioning with stable ties. The index retains no matrix cache. A continuation binds the active build id and expires after any replacement, including a same-release rebuild. Counts, hits and identity share one SQLite snapshot.
- **Evaluation set** (`evaluation.py`, `evaluation_sets.py`): versioned NCIt scenarios with expected concepts and measured scoring thresholds. New production builds and production rebuilds require a passing evaluation before activation; developer samples are exempt. Rebuilding an unclassified legacy snapshot creates a production build, while the original snapshot remains available for rollback. The internal manifest retains the score and complete report, including per-query rankings and missing expected codes. Production thresholds come from the full corpus with the real embedding model; CI exercises the gate with explicitly test-only thresholds. See [the evaluation and SME validation protocol](retrieval-evaluation.md).
- **Retirement condition**: when EVS exposes a semantic mode (E-9), `search_concepts(mode=semantic|hybrid)` is re-pointed to it behind the same tool and the index is deactivated. The tool surface does not change.

### 4.5 Tools (`evs/tools.py`)

Twelve tools, signatures in the specification (`spec/tools.yaml`, group `evs`). Evolution from the original prototype surface:

The content entries have their complete signatures in `content.py`. NCIt calls pin the caller's
effective release; indexed search checks it inside the read transaction. Concept and node
records carry EVS's `active` and optional `conceptStatus` as `status`; requested detail is
passed through, with P106 values supplying `semanticType`. Graph nodes reuse fetched
payloads and read remaining status in minimal batches under the same request budget.
Edges use assertion orientation. MCP argument validation uses the registry's fields and
the common structured error boundary.

Live content now supports caller-selected EVS terminologies; the client requires a release
context and verifies full concept identity. Semantic/hybrid remains NCIt-only. New endpoints
reuse this contract in their owning issues. Lexical/typeahead search preserves EVS order,
with lexical highlights passed through and no invented scores. Every search mode supports
cursors and exact filtered counts. Retired-only selection requires the pinned terminology's
advertised status; unsupported selection is `invalid_request`. Hierarchy paths, paging and
selective negative expansion are implemented in #23. A hierarchy cursor remains valid
while its explicit release is served; only a pinned Terminology not found failure means
supersession, with current-channel discovery then supplying cursor_expired.currentRelease.
No discovery read runs on normal continuation, including a historical release.
Depth cuts are reported by the walker (§4.3); legacy MCP names and the tool-map mechanism
are removed in #18. The descriptions state these interim limits; they do not claim the whole
Phase 2 contract is implemented.

| Original | Specification tool | Note |
|---|---|---|
| `ncit_release_info` | `resolve_release(terminology, channel?)` + `list_terminologies()` | one row per channel; `ttlMs` 0 |
| `ncit_lookup` | `get_concept(terminology, release, code, include[]?)` | `live_only` and `include_raw` removed |
| — | `get_concepts(…, codes[], include[]?)` | returns ordered `concepts` + `missing` |
| `ncit_search` | `search_concepts(…, query, mode?, limit?, cursor?)` | `lexical`/`typeahead` → EVS REST, EVS's highlight as `matchedOn` where it gives one and no score; `semantic`/`hybrid` → index, with a score and the field matched |
| `ncit_traverse` | `get_concept_hierarchy(…)` and `get_concept_neighborhood(…)` | hierarchy = `parent|child|pathsToRoot` only; neighbourhood = §4.3 |
| — | `expand_value_set`, `get_concept_subsets`, `get_concept_mappings`, `resolve_retired_code`, `list_relationships` | new |

### 4.6 Resources

The furnished resources, EVS and caDSR, and the prompt templates are specified in `spec/resources.yaml` and `spec/prompts.yaml` (rendered in `docs/specification.md`, section 3). The implemented EVS templates are `ncit://concept/{release}/{code}` (the addressed resource keeps an explicit release), `ncit://release/{version}` and `ncit://index/manifest/{release}`. Only EVS and unified profiles expose these templates. Concept reads request every get_concept section. Release reads select the requested served version, preferring the configured channel among its upstream tags and rejecting absent or ambiguous metadata. Index reads serve only the active matching manifest: no active index is capability_unavailable, and another active release is release_mismatch. The caDSR module adds `cadsr://data-element/{publicId}` and `cadsr://data-element/{publicId}/{version}`, `cadsr://registry/release` and `cadsr://crosswalk/crdc`. Every `resources/read` result carries `ttlMs` / `cacheScope` per §3.6, of the class §3.6 gives what the resource holds, and a provenance record; its content is compared with the answer of the tool it names on identity and release, not section by section. The acceptance suite tests all of this at protocol level (P-8, P-9).

---

## 5. caDSR module

### 5.1 Client (`cadsr.py`)

Implemented in the existing flat package over `http_client.HttpClient`, injected through `Context`.
No caDSR credentials have been issued: client tests use contract-crafted responses and recorded
reference evidence, not live registry content or a runtime fixture fallback. JSON contract URLs
are listed in `tests/fixtures/cadsr/README.md`. API transports use Basic auth when configured;
the export transport never receives it. Matching uses `NCI_SIP_MATCH_TIMEOUT_SECONDS` (45 seconds
by default), other requests `NCI_SIP_TIMEOUT_SECONDS` (30 seconds). The client caps responses at
10 MiB and transport attempts at three, with the shared backoff and audit path.

The published Data Element contract advertises `/rad/NCIAPI.v1_0:NciApiRad`; the client retains
the recorded `/rad/NCIAPI/1.0/api` alias. The following routes include documented future consumers;
only those needed by this phase are implemented, and requested upstream operations are labeled:

| Operation | Route | Rules |
|---|---|---|
| data element by public id | `GET /rad/NCIAPI/1.0/api/DataElement/{id}` | `version` is the **item's** version, exposed as such |
| data elements by concept | `GET /rad/NCIAPI/1.0/api/DataElements/Concept?conceptCode=&headerOnly=true` | single concept only; 9.7–20 s measured — tool declares a 30 s timeout |
| CRDC crosswalk | `GET …/DataElements/getCRDCList` | filters and paging apply locally; short public hint when unpinned, with no export date on content provenance or server-side cache |
| data element search | `GET …/DataElement/search?keyword={q}&pageSize={n}` | **Requested OP-C03, not served today (C-3).** Tested against crafted OP-C03 fixtures; no filter parameters, fallback route or synthetic result. A verified future C-1 registry pin is sent as `registryRelease`, as on every content request. Failures retain their original details and code. Only when caDSR answers with its masked type-E error envelope does the message add that keyword search is not served and point to data-element lookup by public id or question text; every other failure keeps its own message and may recover on retry. The 1,000 cap belongs to the documented filtered list paths, not a served search. |
| contexts, item types, workflow statuses | `GET /rad/NCILovAPI/1.0/api/getContextNames` etc. | enumerations; definitions are absent upstream and the result says so |
| classification schemes | from the data-element payload's `ClassificationSchemes[]` with nested items | first-class objects |
| forms | `GET /rad/NCIFormAPI.v2_0:NciFormApiRad/Form/{publicId}` and `/Form/query` | keyword search requires `publicId` or `protocolId` upstream; a keyword-only request is `invalid_request` with that stated |
| CDE Match | `POST /rad/NCIAPI.v2_0.cdeMatch.api:cdeMatch_rad/cdeMatch` | built from the **JSON** contract, not the documentation page (which declares a dev host); 28.9 s measured — 45 s timeout, structured timeout error |
| VM Match | `POST /rad/vmMatch/v1/vmMatch` | 15.5 s measured; same handling |
| export date | `GET https://cadsr.nci.nih.gov/ftp/caDSR_Downloads/CDE/XML/` | Exact `releasedCDEsXML-OD.zip` link row → local ISO date-time, minute precision, no offset; no HEAD fallback |

Registry-release discovery treats only the recorded empty-body HTTP 404 as an unpublished
registry and then reads the export listing. A nonempty 404 fails as an upstream error. An
incorrect base URL returning an identically empty 404 cannot be distinguished from that
recording; no stronger absence signal is published yet.

Matching credentials stay server-side. CDE Match sends one `apiinput` object per entity, never an array fallback; transport retries repeat the identical request. VM Match sends the contract array with `matchType`, `function` and optional `evsTerminologyCodes` headers. A 401 propagates explicitly. CDE Match and LOV now refuse anonymous calls in the recorded evidence; this is not a claim that either operation is public. The #35 Form-by-ID interpretation of the recorded unknown-form envelope is specified below; it never matches message text or alters other operations.

### 5.2 Tools (`cadsr_content.py`, `cadsr_matching.py`)

All ten caDSR tools are registered through the shared registry, alongside data-element,
registry-state and CRDC crosswalk resources. Unsupported platform capabilities return explicit
errors; they never substitute empty or fabricated content. Signatures are in the specification
(group `cadsr`).

Content requests without `registryRelease` carry none and have provenance `{registry: cadsr}`.
For a requested pin, discovery must list exactly one matching identifier with a valid date;
requested pins not listed upstream are `release_not_available` before content access. Every content request
then carries the verified pin, and every response must echo it or fail `release_mismatch`.
The future C-1 data-element fixture uses `/DataElement?publicId=…&registryRelease=…`;
today's unpinned operation continues to use `/DataElement/{id}`. Neither falls back to the other.
No item version or export date is substituted. Local cursors bind all applied arguments,
including the normalized limit and registry pin, and preserve platform order.

Governed unpinned content is 3,600,000/public; verified pinned content is 86,400,000/public.
The producer selects that policy inside the shared cache scope. Tool resolution is 0/public;
the registry resource is unpinned content with a short positive TTL and source provenance.
Errors remain 0/private. Specific behaviours:

- `resolve_registry_release` → `{published: false, generatedAt, sourceDistribution}` today, with no `identifier`; when a registry release is published, `published` is true, `identifier` is present and `generatedAt` is the release's own date (§3.3); `ttlMs` 0.
- `get_data_element` → exactly one selector; preferred-question lookup resolves a unique public id to the full record, none to not_found and several to invalid_request naming candidates. Long-name lookup is unavailable (OP-C02). Only the requested sections accompany the record's own fields; permissible values and schemes have per-item provenance, and value meanings retain concept primary flags. Item versions and statuses are unchanged.
- `search_data_elements` → the requested lexical route only, with local paging of its returned list, limit 10/100, no invented score or count. A cap of 1,000 reports upstream_cap, omitted at least 1, exact false. An explicit upstream count becomes totalKnown; a contradictory count fails. Semantic/hybrid are capability_unavailable (OP-C04). Nonempty filters are capability_unavailable until #42 supplies their upstream parameter contract; no filter is ignored or sent speculatively.
- `match_data_elements` and `match_value_meanings` are implemented in `cadsr_matching.py` (#34): 1–10 inputs, caller/platform order, upstream identities and rules, required CDE scores, optional VM scores, and 0/private caching. `modelVariant` and `similarityThreshold` are **rejected with `invalid_request`** naming the parameter and citing C-6. CDE Match sends one apiinput object per entity; matchLimit defaults to 10 and caps at 100 per entity. Filters map to documented headers, with classificationScheme requiring both publicId and version. Any entity failure fails the complete call. VM Match sends the values array, matchType Restricted/Unrestricted and function=match, following spec/ and the successful recorded request; conflicting published OpenAPI descriptions are recorded on #42, without a fallback. CDE Match requires a numeric score. VM Match omits missing or null scores and concepts, and empty/NA crosswalks. The dedicated 45-second matching timeout and credential isolation remain in the shared client. Published matching pins fail capability_unavailable (pinned matching) because C-1 has no matching transport field; requested pins not listed upstream remain release_not_available. No unpinned matching result is labelled pinned.
- `get_form` → by public id and optional item version, preserving modules/questions when included (default true) and all statuses. Keyword search is invalid_request. Only the validated Form-by-ID operation interprets HTTP 200 with explicit form:null and type E as not_found, following recorded/cadsr/form-unknown.json; the residual ambiguity with a genuine failure in exactly that shape is documented and raised on #42. Other shapes/statuses/operations keep X-15. Published form pins are capability_unavailable until C-1 defines the transport.
- `get_permissible_value` → validates the REST permissibleValueId and registry pin, then capability_unavailable (OP-C10). REST supplies an id; the missing capability is retrieval by it, distinct from Shared SI blank nodes (S-3).
- `get_code_map` → getCRDCList alone, one map per data element with exact comma-split Used By filters, original values and colon-joined codes. coverage counts coded values; missing binding says valueLevelBinding false and values empty. Pages use limit 100/1000 and argument/pin-bound cursors. Future C-1 requests send and require an echoed registryRelease; the crafted fixture and generator express that requested capability. Unpinned maps and the cadsr://crosswalk/crdc resource (limit 1000, explicit exact truncation if larger) use short public caching; verified pins use long public caching. No speculative Model API requests.
- `list_contexts` → paged LOV names as identifiers, with provenance and no definition field. `list_classification_schemes` is capability_unavailable (OP-C13); get_data_element's classificationSchemes section exposes the element's schemes and nested items.

---

## 6. Cross-domain module

### 6.1 Shared SI client (`ssis.py`)

- The Shared SI façade (`https://cadsrapi.cancer.gov/si-api/v1`) is not used by this server; its recorded behaviour is kept in [upstream/ssis.md](upstream/ssis.md) for the acceptance suite's fixtures.
- SPARQL at `https://shared.semantics.cancer.gov/sparql`, asked with a form-encoded POST (a direct `application/sparql-query` POST is refused) and the query texts the request-form register prescribes. The inspection layer refuses `SERVICE`, Virtuoso's `OPTION (TRANSITIVE)` and some `FILTER regex` patterns with an HTML `403`; property paths pass (3 October 2026). Any `403`, HTML or JSON, is `upstream_unavailable`, never an empty result. The common client does not retain enough evidence to attribute a rejection to an inspection layer, so no such reason is claimed.
- Graph choice is explicit and recorded: `Thesaurus.rdf` for NCIt hierarchy (`Thesaurus.owl` runs its hierarchy through anonymous restrictions, so `subClassOf*` misses most of it: 7,109 descendants of C2991 against 22,854 on 26.09d), `caDSR` for data elements. Both graphs' identities go into `provenance.graphs[]`.
- `SSISClient` implements the combined graph-identity read and concept-keyed data-element/permissible-value query templates. Content asks for `maximum + 1` (maximum 1,000), identities for 3; the sentinel establishes a cut, not an exact total. Only validated NCIt codes enter content templates. Empty content bindings are successful; identity reads require exactly both graphs. Missing required bindings or malformed responses are upstream failures. The client keeps each date's original spelling and rejects partial, duplicate or unusable identity metadata. The identity bound adds `LIMIT 3` to the recorded combined query; the fixture register marks its reused recorded answer as crafted. The public-ID permissible-value reader was removed in #367: API primary flags, not graph main/minor, now own that lookup.

### 6.2 Tools (`seam.py`)

- `find_data_elements_for_concept` → SPARQL join with optional subsumption expansion (bounded, per `Budget`). Expansion requires the Shared SI NCIt graph version to match the effective NCIt release, whether explicit or implicitly selected (X-22); a mismatch is `release_mismatch`, an absent version is `release_not_available`. Exact-code joins require both graph identities and preserve their actual metadata, including an optional NCIt version, without comparing or backfilling versions. caDSR REST serves concept-to-data-element lookup, but its content state differs from Shared SI and it is not used as a fallback here. Standalone permissible-value retrieval by ID remains unavailable (OP-C10). Both content states are recorded (`provenance.release` and `provenance.registry`). One deterministic stream pages data-element uses before value uses, with a shared 1,000-result cap; the value query asks only for the remaining capacity plus a sentinel, and is skipped when elements fill the cap. Requested value arrays remain present on every page. Cursors bind the effective release, arguments, both graph identities and content snapshot.
- `get_concept_for_permissible_value` → reuse the caDSR data-element reader with `permissibleValues`, at optional `version` or the API's latest. Match exact value text locally; only `primaryIndicator: Yes` identifies the primary. Missing, invalid or conflicting primary evidence is an upstream failure; identical assertions may collapse. Item identity and numeric version verification stay in the shared reader, preserving upstream spelling. Read the primary concept from pinned EVS; provenance retains EVS origin and the caDSR item's origin, without graph claims. Both data-element and concept child permissions are preflighted; expiry after caDSR blocks EVS. By `permissibleValueId`, return `capability_unavailable` (OP-C10).
- `resolve_stored_value` → GDC via `NCIt_Maps_To_GDC` (mapset and FHIR ConceptMap agree; the mapset is named in provenance); other commons via `getCRDCList`; `confidence` asserted or none, `evidence` naming each source; no stored value with a coverage statement otherwise. Never returns the preferred term as a stored value. Filter GDC term-search results by exact sourceCode; coverage counts retained values. dataElementId restricts CRDC and is explicitly unsupported for GDC.
- `get_release_alignment(maxIntervalDays = 31)` → NCIt release, caDSR export date, SI graph dates, `intervalDays`, and a warning naming the threshold when `intervalDays` exceeds it; `ttlMs` 0.

---

## 7. Workflow module

Three composites (the specification's group `workflow`) reuse the registry tools' content producers with one request `Budget` across the chain. NCIt workflows select one effective `ReleaseContext`; dictionary harmonization resolves one caDSR registry state:

- `ground_value` — fails closed if either content state cannot be named; `registryRelease` optional (unpinned when absent); truncation from each hop carried through (`perHop`).
- `expand_cohort` — `codes[]` and `excluded[]`; asserted equal to child `get_concept_hierarchy` to maxDepth plus the start's depth-one `get_concept_neighborhood`. Only the start's exclusions govern the cohort, and each assertion remains present with includeNegative. maxNodes counts returned codes, including the start.
- `harmonize_data_dictionary` — one match call per column, batched where the upstream allows; shared registry state.

The four furnished templates in `spec/prompts.yaml` are registered only where every named
tool is available. Their packaged JSON copy preserves the specified text and arguments,
checked against the source by tests. Optional omitted arguments substitute empty text.

---

## 8. Configuration and profiles (`config.py`)

Settings after the change. `NCI_SIP_EVS_BASE_URL`, `NCI_SIP_TIMEOUT_SECONDS`, `NCI_SIP_DATA_DIR` and the embedding pair exist today; QUICKSTART.md's other settings (EVS retries, backoff and response limit, index batch size, log level) stay as they are. Each is validated at startup with a message naming its variable, as the existing ones are.

| Setting | Default | Purpose |
|---|---|---|
| `NCI_SIP_PROFILE` | `unified` | `evs` · `cadsr` · `unified` |
| `NCI_SIP_EVS_BASE_URL`, `NCI_SIP_EVS_FHIR_BASE_URL` | production | |
| `NCI_SIP_CADSR_BASE_URL`, `NCI_SIP_CADSR_FTP_URL` | production | |
| `NCI_SIP_SSIS_SPARQL_URL` | production | |
| `NCI_SIP_UPSTREAM_MODE` | `live` | `live` · `fixture` — selects base URLs as a set so the acceptance suite switches everything with one variable |
| `NCI_SIP_RELEASE_CHANNEL` | `monthly` | |
| `NCI_SIP_EXCLUSION_ROLE_CODES` | `R135,…,R142` | validated against the requested release catalogue per relationship listing or neighborhood call |
| `NCI_SIP_EVS_LICENSE_KEY`, `NCI_SIP_CADSR_CREDENTIAL` | unset | never logged |
| `NCI_SIP_EMBEDDING_PROVIDER`, `NCI_SIP_EMBEDDING_MODEL` | unset | both required together (today both default to `hashing`) |
| `NCI_SIP_DATA_DIR` | `.nci-si-mcp/` | |
| `NCI_SIP_TIMEOUT_SECONDS`, `NCI_SIP_MATCH_TIMEOUT_SECONDS` | 30, 45 | |

The settings the acceptance suite gives a server under test, and their formats, are specified in the specification's §4 (`spec/acceptance.md`): `NCI_SIP_CADSR_CREDENTIAL` is `user:password`, sent as HTTP Basic.

---

## 9. Acceptance suite (`acceptance/`)

The requirements it tests and its acceptance rules are in the specification's §3 and §4; this section states how it lives in the repository.

### 9.1 Layout

```
acceptance/
  pyproject.toml            separate package: nci-sip-acceptance, versioned on its own; pytest, mcp, jsonschema
  src/nci_sip_acceptance/    the harness
    client.py               connect over stdio or streamable HTTP; tools/list, tools/call
    fixture_server.py       serves fixtures by (surface, method, path, params, headers, body), active scenarios first, with a request log endpoint
    concepts.py             composes EVS concept answers from one recording per concept (§9.6)
    record.py               re-records `recorded/` from live against the manifest's pins (`pdm run acceptance-record`)
    craft.py                crafts, in memory, the scenario fixtures EVS does not produce on demand (`pdm run acceptance-craft` records their digest)
    register.py             writes the register of request forms from the manifest (`pdm run acceptance-register`)
    tools.py                direct tool dispatch and result decoding
    requirements.py         the rule that tests cite `spec/requirements.yaml` and every requirement is cited or planned
    report.py               per-tool outcome: PASS | PASS (fixture only) | FAIL | NO FIXTURE | INCOMPLETE | NOT IMPLEMENTED | NOT RUN | NO TESTS
  fixtures/
    manifest.yaml           pinned NCIt release (caDSR export date and SI graph dates to come), concept rules, the scenarios, the requests recorded
    recorded/<surface>/…    captured responses with the request that produced them
    crafted/<requirement>/… responses derived from recordings, naming the requirement they stand in for (craft.py's are in memory)
    scenarios/<group>/<name>/ the recorded fixtures of a scenario the manifest describes; craft.py's are in memory
  request-forms/            the register of request forms: a view for each team (EVS, caDSR, Shared SI) and one for all
  tests/
    test_protocol.py        the P requirements (protocol gates)
    test_crosscutting.py    the X requirements, one case per tool with a call in calls.yaml
    calls.yaml              that call of each content-returning tool, answered by the fixture set
    test_evs.py, test_cadsr.py, test_cross_domain.py, test_workflow.py   each group's tool requirements
  selftests/                the harness's own tests, run in CI in eight shards and their coverage combined
```

The root project installs the package editable (dependency group `acceptance`); `pdm run acceptance` runs the suite and `pdm run acceptance-selftest` the harness's own tests.

### 9.2 Run modes

`NCI_SIP_UPSTREAM_MODE=fixture` points the server under test at the fixture server; `live` at production. The harness sets the variable, launches the server (stdio) or connects (remote), and runs the suite. Tests marked `live_capable` run in both modes; the rest in `fixture` only.

### 9.3 The request log

The fixture server exposes `GET /_log` returning every request it received since `DELETE /_log`, each decoded (surface, path, parameters, body) and as sent (`raw`, the path and query undecoded). Tests use it for: hostile identifiers that reach no request, free text that arrives as one value, a code sent as one encoded path segment; outbound budget including retries; batch endpoint used instead of fan-out; no endpoint called twice with identical parameters in one tool call; `Accept: application/json` present on every caDSR call; licence key present on licensed calls and absent from results.

### 9.4 Direct tool dispatch

The suite calls the specification's exact tool names and arguments. A missing name reports
NOT IMPLEMENTED; an implemented tool's unsupported capability is tested as its actual result.
Phase 1 (#18) removed the last baseline-map entries and the translation mechanism together,
including its prototype-only self-tests. The remaining harness tests cover direct dispatch,
missing names, results, errors and the per-tool report. No legacy tool is substituted.

### 9.5 CI

A job beside the existing `quality` and `test` jobs (`acceptance`) runs the acceptance suite in `fixture` mode against the server built from the checkout, and becomes a required check on `main`. It is a ratchet over individual tests: the outcome of every test must equal a committed expected outcome (`acceptance/expected/fixture.json`: test id to outcome, nothing else), and the per-tool report, and the README's status table, are derived from them. The job stays green while tools are NOT IMPLEMENTED or FAIL, catches a test that stops passing inside a tool that still fails, and a pull request that changes an outcome updates the expected outcomes in the same change. `live` mode is a manual workflow (`acceptance-live.yml`) with the network location recorded: the runner, and which IP family reaches EVS.

Because the suite and the tools are written by the same hands, two rules keep the suite honest. A suite test asserts what the specification says, cites the requirements it enforces (`@pytest.mark.requirement`), and never asserts what the current implementation happens to return. A pull request that changes a suite test or fixture while making a tool pass lists each change with the passage that justifies it: correcting a wrong test before the interface baseline is frozen is expected, weakening one is not.

### 9.6 Request forms

The fixtures use the request forms of the platform operations (their `OP-` ids), release-pinned; the generated register (`acceptance/request-forms/`) gives each form's operation and rationale, including where the inventory's form and EVS differ. Where EVS does not yet answer the form a requirement prescribes, the ordinary fixture is crafted to the requirement and names it, and the live run shows the gap. Two kinds of request are answered whatever their form:

- **EVS concepts.** One recording per concept answers every projection and relation list of that concept, and every batch is composed from the recordings of the concepts it names, through declared rules (`concepts.py`): project by `include` (the include-to-key table in the manifest; EVS's `include` is a clean key projection, verified 2 October 2026), select by `list` (each code once, unknown codes left out, in no particular order, since EVS keeps none), and one relation list on its own. `record.py` checks composed answers against real ones.
- **Ignored parameters.** A parameter the service is shown to ignore is declared with its evidence and left out of the match.

The recorder sends no licence key, so what it records is what EVS serves publicly; licensed content, which EVS refuses without the key (403), is never recorded, and a recording whose request carried the key is refused.

---

## 10. Tests in the main package

Keep `unittest`-style tests under the gates in `CONTRIBUTING.md`. Extend `tests/fakes.py` with doubles for the caDSR and SSIS clients. Required new tests, each named for the rule it enforces:

- `test_errors`: every `PlatformError` serialises to the error schema; empty results never produce `isError`; the three masked-error shapes are classified.
- `test_release`: one-row resolution; 404 → `release_not_available`; payload mismatch → `release_mismatch`; caDSR state never carries a fabricated identifier.
- `test_bounds`: retries decrement the request budget; per-kind rotation; truncation report fields.
- `test_catalogue`: polarity by code; a configured code absent from the requested release catalogue fails the affected call.
- `test_batch_content`: ordered `concepts`/`missing` reconciliation; any return order handled.
- `test_index`: atomic activation and rollback; provider/model mismatch rejected; dimension mismatch rejected.
- `test_schema`: `outputSchema` present and valid for every tool in every profile; surface static across settings; no placeholder text.
- `test_cadsr_client`, `test_ssis_client`: required-parameter validation; envelope errors; `Accept` header.
- `test_server`: `tools/list` per profile; `ttlMs`/`cacheScope` on list and discovery results.

`tests/test_handlers.py` tests the migrated business operations through the registry.
`tests/test_registry.py` pins shared adapter arguments, profile inventories, annotations
and upstream error details.

---

## 11. Phases and definition of done

The baseline package contains the specification, the acceptance suite with its fixture set, and the server: the shared platform, the EVS module and the caDSR client, with the tools and tests completed when it is cut.

The suite is therefore completed first, for all twenty-nine tools, so that it is complete at whatever point the package is cut. A tool the server lacks is a valid NOT IMPLEMENTED row; a required tool without tests is a defect in the package. The phases then make the tools pass their tests, in the order of the table below.

Each phase ends with the unit suite green, the acceptance suite's expected outcomes updated for every test whose outcome changed, and a `vX.Y.Z` release where a merged `feat`, `fix` or `perf` pull request cuts one.

| Phase | Delivers | Done when |
|---|---|---|
| **0 · Acceptance suite** | Licence; §9: harness, fixture mechanics (scenarios, response sequences, request bodies), recorded and crafted fixtures for every surface and all sixteen scenarios, protocol gates, cross-cutting tests, per-tool tests for every tool group, the baseline tool map as a working aid, the per-tool report, the fixture-mode CI job | every required tool has its tests; the report runs against the server at the end of Phase 0, through the tool map where one applies |
| **1 · Platform** | §3 in full; `service.py` retired; EVS tools re-homed on the registry with no behaviour change | §3 protocol gates pass; existing EVS behaviour unchanged under the new error and provenance model |
| **2 · EVS module** | §4: release-pinned addressing, batch reconciliation, catalogue polarity, traversal rewrite, FHIR, mapsets, retired codes; index with activation and rollback | all EVS tools PASS in fixture mode; live-capable tests PASS live |
| **3 · caDSR module** | §5 | all caDSR tools PASS in fixture mode; PASS (fixture only) rows name their upstream requirement |
| **Furnished package** | The tag and the baseline package, with the baseline run report against that commit, naming the tools whose tests have never passed against any implementation | the tag is on the commit the report ran against and the package checklist is met (#3); cut when the owner decides, not before Phase 3 is done, so that both modules yield a meaningful baseline report |
| **4 · Cross-domain** | §6 | cross-domain tools PASS; both release identities on every result |
| **5 · Workflows, remote transport, audit** | §7, §3.8, §3.9 | workflow tools PASS; unified profile accepted under the specification's §4 |
| **6 · SI architecture alignment, access control and assisted workflows** | §14: approved portable access contracts, conditional evaluation and integrations | approved behavior passes its tests; optional no-go/defer decisions remain explicit; documentation and release evidence match the delivered scope |

Work proceeds in the order of the table above until the package is cut. What remains at the furnished commit is the remaining work on the modules, and the specification is what the baseline assessment measures the server against.

---

## 12. Removals

- `service.py` and the caDSR stub are removed. `live_only` and `include_raw` are CLI-only; the public EVS tools use the specification's names and arguments.
- Label-based exclusion detection, wherever it appears.
- The `is_a_parent` / `is_a_child` / `is_a_descendant` pseudo-relationship names.

## 13. Non-goals

No NCIm vectorisation or local NCIm graph; no hosted vector database; no writes to any registry or terminology; no re-embedding or index operation after the period of performance; no implementation of upstream API changes — every upstream gap this specification works around is an entry in the upstream requirements package, and the workaround is removed when the gap closes.

## 14. Phase 6: SI architecture alignment

Added 7 October 2026; the historical *Updated* date at the top still identifies the original
prototype analysis. The proposed MCP of the Semantic Infrastructure (SI) team was not built; this codebase supersedes it.

The [architecture decision](decisions/001-si-architecture-alignment.md) records the
[approved portable contracts](https://github.com/CBIIT/nci-si-mcp/issues/176#issuecomment-6043097407),
their activation issues, reserved acceptance mappings and separate deferred production decisions.
It is a plan, not a replacement for active `spec/`; amendments, implementation, generated reference,
acceptance tests and user-facing documentation land together in each owning issue.

Use `milestone/phase-6` and the [issue checklist](https://github.com/CBIIT/nci-si-mcp/issues/175),
one issue at a time: #176 contracts/evidence, #177 caller permissions and private responses,
#178 portable secured HTTP, #179 client-baseline evaluation, #180 conditional ask pilot,
#181 bounded wxMCP integration assessment, then #182 assurance and release. Issue PRs target
the milestone; full review and independent mutation review precede its PR merge into main.

Every executable behavior begins with a failing observable test, then the smallest correct
implementation and a green refactor. Retain high line and branch coverage through useful
boundary/error/concurrency assertions. Document new behavior in the same PR, including domain
stories, examples and architecture/deployment changes. The #176 documentation-only change moves
no expected acceptance outcomes and does not approve the separate furnished baseline.

After the 8 October 2026 deferral, #180 was closed as not planned: no demonstrated client need
justifies server-side generation. Reopening requires a demonstrated need clients cannot meet
and an approved A9.2 amendment. #181 remains open in the Cloud One wxMCP evaluation milestone;
runtime/adoption work is still deferred. The [research strategy](deferred-capabilities.md)
records re-entry decisions. The [Phase 6 assurance record](phase-6-assurance.md) covers the
delivered scope; the Phase 6 release shipped neither capability.
