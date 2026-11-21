# CDE AI Engine: contract evaluation

**Reviewed 25 October 2026 (UTC). Recommendation: WAIT.** No CDE AI Engine contract
or documented anonymous Stage endpoint was found in the sources reviewed below.
This is a contract-discovery blocker, not proof that the API is absent or inaccessible.
No paired case comparison was run; retrieval quality and latency remain unmeasured.

## Dated evidence and its limits

- **Team-reported, 8 October 2026:** the [owner-relayed team response](team-responses-2026-10-08.md)
  describes a purpose-built retrieval/reranking pipeline with customized SapBERT,
  on Stage then, with production expected at the end of October. It says the vector
  database is private and the supported interface is the CDE AI Engine API.
  That report supplies neither an endpoint nor a schema. The production date is a
  forecast; neither environment's current availability was independently verified.
- **Observed, 25 October 2026:** the public [NCI API Portal menu][portal] lists
  CDE Match, linking to the [Match 2.0 documentation][match-page].
  It has no separately named CDE AI Engine entry. The [linked Match JSON contract][match-json]
  is anonymously readable, identifies version `2.0`, host `cadsrapi.cancer.gov`,
  and the operation below. Reading that contract does not demonstrate anonymous
  operation access or establish that Match 2.0 is the AI Engine.
- **Existing project evidence, reviewed 25 October 2026:** the 3 October
  [recorded Match contract](../../acceptance/fixtures/recorded/cadsr-contracts/cde-match.json)
  and [anonymous refusal](../../acceptance/fixtures/recorded/cadsr/cde-match-refused-object.json)
  underpin the current adapter; the latter records HTTP 401, not a new measurement.

## Contract and status comparison

Match 2.0 cells distinguish the public contract observed on 25 October from this
repository's handling in [the matching adapter](../../src/nci_sip_mcp/cadsr_matching.py),
[client](../../src/nci_sip_mcp/cadsr.py) and
[offline contract tests](../../tests/test_cadsr_matching.py). Engine unknowns must
not be filled from Match 2.0 or from the team's implementation description.

| Field | Current CDE Match 2.0 | CDE AI Engine / blocker as of 25 October 2026 |
|---|---|---|
| Environment and URL | Contract: HTTPS `cadsrapi.cancer.gov`, base `/rad/NCIAPI.v2_0.cdeMatch.api:cdeMatch_rad`, `POST /cdeMatch`. No Stage URL declared there. | Stage reported 8 October; production forecast end October. Authoritative Stage/production URLs and current status unknown. |
| Access | Contract declares Basic Auth and `401 Access Denied`; recorded anonymous operation gets 401. Project uses crafted matching fixtures pending issued credentials. | Access policy, anonymous tier and credential mechanism unknown. Match credentials/policy cannot be assumed to apply. |
| Input and filters | One `apiinput` object: required `entity`, optional `entityUserTip`, `pvvmData` (`name`, `userTip`). Headers: `matchLimit`, registration/workflow status, contexts, classification public ID/version, value-domain type. | Body, batching, filters and required headers unknown. |
| Output and ranking | `apiResponse` plus `matchResults` object, entity/counts and matches with CDE public ID/version, score, rule and matched text. Adapter preserves platform order and requires score/rule/text and core CDE metadata. | Retrieval/reranking is team-reported; response schema, score meaning, ordering and rule/explanation fields unknown. |
| Model / threshold | Neither `modelVariant` nor `similarityThreshold` declared; adapter refuses supplied controls with `invalid_request` (C-6). | Customized SapBERT reported; model revision, selectable controls, threshold range/meaning unknown. |
| Caps / pagination | Contract `matchLimit` defaults to 10; no maximum or continuation field declared. MCP accepts 1–10 entities, caps requested matches at 100 per entity and slices returned matches. These are MCP bounds. | Input/result caps, truncation signals and continuation semantics unknown. |
| Failures | Contract declares 200/401 and `apiResponse.type` S/E/W/I/A. MCP rejects malformed or masked-error results and fails the whole call on an entity failure. | Status/error schema, no-match distinction, partial failures and retry semantics unknown. |
| Registry provenance | Item public ID/version, but no registry-release pin transport declared. MCP refuses matching pins and never labels an unpinned result pinned (C-1). | Registry snapshot, refresh cadence, pin transport and response verification unknown. |
| Latency / usage | No SLO, quota or rate limit declared in reviewed contract. MCP's default 45-second match timeout is a local budget, not an upstream guarantee. | Latency, timeout, quota, rate limits and usage/data-handling constraints unknown; no live comparison measurements. |

## Decision and unblock request

**WAIT** rather than adopt or run both: the team's description makes this a candidate,
but no discovered contract permits a supported request or a field-level compatibility
decision. Through the project coordinator, request the caDSR team's authoritative
contract URL/version, environment URLs/status and access policy, plus the missing
schema, controls, limits, errors, provenance and operational constraints above.

Once a documented anonymous Stage operation is available, compare representative
CDE matching cases with Match 2.0 where access allows and record date, inputs,
environment/contract/model/registry identities, ordered candidates, scores/rules,
failures, counts and timings. If a key is required but unavailable, record that
access blocker and stop. Do not infer endpoints, reuse another API's credential
scheme, access the private vector database or build a local caDSR copy.

The [assisted evaluation](../assisted-evaluation.md) supplies fixed behavioral recipes
and recorded/crafted responses, including dictionary matching and unmatched columns.
Those correlated fixture cases are **not an independent domain relevance benchmark**;
their perfect score cannot establish Engine accuracy, live performance or superiority.
Any later adoption needs paired evidence and a separate implementation issue;
tool-contract changes remain an owner decision in `spec/`.

[portal]: https://cadsrapi.cancer.gov/NCIAPIPortal/menu.dsp
[match-page]: https://cadsrapi.cancer.gov/NCIAPI/2.0/cdeMatch.html
[match-json]: https://cadsrapi.cancer.gov/invoke/pub.swagger:getSwaggerJsonDoc?radName=NCIAPI.v2_0.cdeMatch.api:cdeMatch_rad
