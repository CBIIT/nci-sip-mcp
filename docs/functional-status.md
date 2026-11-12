# Functional status

**Evidence checkpoint: 12 November 2026.** All 1,005 fixture acceptance cases passed on
`52521535f34c28288b3b6615d0bea33548f8af88`, including tests that correctly refuse unsupported
operations. This establishes behavior against recorded and crafted answers, not current access
to every service. The [original report](evidence/acceptance-fixture/2026-11-12.json) and
[provenance](evidence/acceptance-fixture/2026-11-12.yaml) are retained in the repository.

The new anonymous EVS discovery attempt received **403 / text/html** on 12 November and stopped.
The planned in-process MCP concept call was **unexecuted**, blocked by discovery. There was no
new native live-suite run or UAT. The cause and issuing layer of the 403 are unknown.
[First result and independent disposition](https://github.com/CBIIT/nci-sip-mcp/issues/461#issuecomment-6830205261).

Production caDSR traffic is prohibited, SSIS traffic is paused, protected caDSR credentials have
not been issued, and automated Acceptance(live) is disabled. These runner restrictions do not
establish that every separately authorized individual diagnostic is impossible. No further
request is authorized by this page. See the [dated access guidance](../QUICKSTART.md#upstream-access-and-ssis-transition).

## What the evidence covers

| Evidence | Source and time | Result and limits |
|---|---|---|
| Full MCP fixture acceptance | Main CI [43244797057](https://github.com/CBIIT/nci-sip-mcp/actions/runs/43244797057), source `52521535f34c28288b3b6615d0bea33548f8af88`; artifact created 2026-11-12T15:15:43Z | 1,005 passed, 0 failed/skipped, selected = finished = 1,005, no worker crashes or failed/unrun gates. Every node/outcome matches that source's expected file. Artifact creation is not the test start time. |
| Original fixture content | Suite label `ncit_26.09d, recorded 2026-10-26`; individual files retain their own dates, including 2026-10-02 | Recorded and contract-crafted sources stay distinct. These dates are not today's execution date. Original pre-tag suite version `0.37.2.dev1+g5252153` is preserved. |
| Historical scheduled live acceptance | Stored 19 October report, source/date/hash in the generated table below | 46 passed / 0 failed / 920 skipped, a different historical cohort. Unrecorded skip reasons remain unknown; a passed refusal is not content retrieval. |
| Later reported live acceptance | [#381](https://github.com/CBIIT/nci-sip-mcp/issues/381), 27 October, run 40424723816 attempt 2 | Reported 275 passed / 0 failed; 5 registry-release skips, 195 crafted cases, 530 not marked live. This aggregate is not operation-level evidence for the current SHA and does not replace the retained scheduled report. |
| Local driver preflight | Same `52521535…` source, owned synthetic loopback receiver, 12 November | 13 focused controls passed after the separately retained original failures. Real in-process MCP/policy path, two ordered contacts, refusal/response-contract/cleanup controls. Local evidence, not live acceptance. [Independent review](https://github.com/CBIIT/nci-sip-mcp/issues/461#issuecomment-6830013415). |
| New controlled discovery | Production-configured EVS HTTPS route, 12 November, same source | One attempt, 403 / text/html, 118 bytes retained privately; stopped before release selection/content. [Sanitized observation provenance](evidence/functional-status/2026-11-12.yaml). |
| Earlier stage smoke summaries | [2 November investigator manifest](https://github.com/CBIIT/nci-sip-mcp/issues/383#issuecomment-6590423102), source `084557569bf0101348f7cd45f2f22ea776d11e87` | Reported 200 for DataElement, Form, LOV, vmMatch and CDE Match; most bodies/headers and full handler results were not retained. These summaries are not independently graded acceptance or proof of protected entitlement. |
| Stage DataElement diagnostic | [11 November direct GET](https://github.com/CBIIT/nci-sip-mcp/issues/383#issuecomment-6803408769) | 200 JSON, publicId 2200604/version 4. Direct API evidence only; neither MCP acceptance, stage registry identity nor protected-access verification. |

The fixture report is 239,956 original bytes, SHA-256
`a5f55f31c76ee88ca7e6df071968e12d20b95e45e87d38996debedc5f982f8cf`.
Its provenance also preserves the original ZIP identity. The repository copy remains available
when the CI artifact expires. For each caller scenario and its assertions, use the
[behavioral test stories](behavioural-tests.md); operation forms and historical capture details
remain in the linked request registers. An example owning contract test is not proof that
every wire variant of that operation ran separately.

## Services, environments and destinations

A configured URL identifies where a client would send a request. A dated response establishes
only the particular observed request, not the availability of every operation on that host.
SSIS is the service; production, stage and fixture/local name environments.

| Service / environment | Exact base or operation | Evidence of contact |
|---|---|---|
| EVS REST / production-configured | `https://api-evsrest.nci.nih.gov`; `GET /api/v1/metadata/terminologies?terminology=ncit&latest=true&tag=monthly` | New controlled discovery received 403. The issuing layer is unestablished. Other operation recordings are dated historical evidence, not new contacts. |
| EVS FHIR / production-configured | `https://api-evsrest.nci.nih.gov/fhir/r4`; `/ValueSet/$expand` | Historical recordings only; no new FHIR request. |
| caDSR DataElement, Form, Match and LOV / production-configured | `https://cadsrapi.cancer.gov/rad`; operation paths below | Historical recordings and crafted protected answers; no new production caDSR contact. |
| caDSR export / production-configured | `https://cadsr.nci.nih.gov/ftp/caDSR_Downloads`; `/CDE/XML/` and its export files | Historical export metadata; no new export download. |
| caDSR published contracts / production-configured | `https://cadsrapi.cancer.gov/invoke/pub.swagger:getSwaggerJsonDoc` | Retained contract recordings; reading a contract does not prove operation access. |
| caDSR DataElement / stage | `https://cadsrapi-stage.cancer.gov/rad/NCIAPI/1.0/api/DataElement/2200604` | Exactly one 11 November direct diagnostic succeeded. No registry release/export/refresh identity established. |
| Shared SI REST / production-configured | `https://cadsrapi.cancer.gov`; `/si-api/v1/…` | Historical recorded façade responses; current traffic paused. |
| Shared SI SPARQL / production-configured | `https://shared.semantics.cancer.gov/sparql` | Historical recorded queries/refusals; current traffic paused. No replacement adopted. |
| CDE AI Engine / stage, investigator-reported | `https://cadsrapi-stage.cancer.gov/rad/cdeai.api.producer.v2:cdeai_rad/getEmbeddingVariants` | The 2 November manifest identifies this request target and 401; body/authentication headers were not retained. Deployment/contract authority remains unresolved; AI matching unexecuted. |
| Fixture / local | Owned loopback fixture origins with dynamic ports; the native report does not retain an exact receiver URL | The 1,005-case suite used fixture/stdio mode. Local driver controls used an independent owned loopback receiver. Neither contacted the services above. |

## Operation-level picture

### EVS terminology and FHIR

The fixture contracts exercise these MCP capabilities, including their failure boundaries.
[EVS request forms](../acceptance/request-forms/evs.md) retain the exact historical paths,
queries, status codes and source files. All operations except the single discovery attempt
remain **not newly observed** on the current source.

| Capability / operation | MCP interface and evidence | Current limitation |
|---|---|---|
| Discover a channel release and list terminologies (OP-E01) | `resolve_release`, `list_terminologies`; `/api/v1/metadata/terminologies`, release-scoped conceptStatuses | Fixture selection and ambiguity/refusal cases pass. New monthly discovery failed 403; no release selected. |
| Read concept details, includes and batches (OP-E05/E07, concept rules) | `get_concept`, `get_concepts`; `/api/v1/concept/{pinned terminology}/{code}` and list form | Identity/status/section/order/missing-code contracts pass in fixtures. Planned C3262 MCP observation was unexecuted; fixture names/statuses are not today's answers. |
| Search or type ahead (OP-E08) | `search_concepts`; pinned `/search` | Lexical paging/filter behavior is fixture-tested. Semantic/hybrid retrieval uses the local index; no equivalent platform embedding-ranked capability has been established. |
| Read relationship catalogues and neighborhoods (OP-E02/E03, concept relation rules) | `list_relationships`, `get_concept_neighborhood`; metadata roles/associations and concept relation paths | Code-based polarity is tested; upstream stable machine-readable polarity remains a gap. No new traversal performed. |
| Read hierarchy (OP-E12/E13) | `get_concept_hierarchy`; `/descendants`, `/pathsToRoot` | Paging/depth/bounds pass on fixtures. Compact descendant rows omit status; the server does not infer it. |
| Read subsets and expand members (OP-E06/E19) | `get_concept_subsets`, `expand_value_set`; `/subset/…`, `/subsetMembers/…` | Some advertised pinned forms are unavailable upstream. Fixtures test refusal rather than silently using an unpinned answer. |
| Read mappings / resolve stored values (OP-E23/E24/E25 and concept maps) | `get_concept_mappings`, `resolve_stored_value`; `/mapset`, individual mapset and `/maps` | Pinned mapset transport is not established. Source/value bindings and unavailable forms stay explicit. |
| Resolve retired concepts (OP-E21) | `resolve_retired_code`; `/history/{pin}/…/replacements` | Fixture status/action/date behavior passes. Upstream active/current semantics remain unresolved; active does not necessarily mean current. |
| Reject an unavailable release (OP-E06 variants) | Release-bound EVS content tools | Correct fixture refusals pass; they are not successful content checks. |
| Expand FHIR value sets (OP-F05) | `expand_value_set`; FHIR `/ValueSet/$expand` | Recorded unpinned support does not establish pinned expansion. No new FHIR check or unpinned fallback. |
| Licensed terminology boundary (A7.5) | Concept/error/credential-isolation contracts | Fixture credential forwarding/redaction does not establish issued access or a new licensed-content retrieval. |
| CLI version baseline | `nci-si-mcp release-info`; `/api/v1/version` | Historical recording; no owning MCP acceptance case or new version request is claimed. |

[EVS gaps and their owning tests](upstream/evs.md) cover status catalogues, polarity,
licensing, retired content, pinned forms and semantic retrieval. These gaps remain upstream
questions; successful local handling does not fill them.

### caDSR metadata, matching and registry state

[caDSR request forms](../acceptance/request-forms/cadsr.md) distinguish original recordings,
contract-crafted protected answers and unavailable forms. Production traffic remains held.

| Capability / operation | MCP interface and exact relative path | What is established / missing |
|---|---|---|
| Data element by ID/version or question text (OP-C01) | `get_data_element`; `/NCIAPI/1.0/api/DataElement/{id}` or `/NCIAPI/1.0/api/DataElements/ReferenceDocument` | Fixture fields, versions, statuses, citations and unique/ambiguous question matches pass. Stage's one 200 is a separate direct diagnostic. |
| Long-name lookup (OP-C02) | `get_data_element(longName=…)` | Correct `capability_unavailable`, not a successful lookup. |
| Data element permissible values (OP-C07) | `get_data_element` include; values nested in the DataElement | Standalone `/DataElement/{id}/permissibleValues` recorded 404. Nested-value contracts pass; missing values are not invented. |
| Concept / classification search (OP-C09, C-11) | `search_data_elements`; `/NCIAPI/1.0/api/DataElements/Concept` and `/NCIAPI/1.0/api/DataElements/Classification` | Recorded queries have a 1,000-row cap; fixture truncation and unsupported-filter behavior pass. |
| Form details (OP-C12) | `get_form`; `/NCIFormAPI.v2_0:NciFormApiRad/Form/{id}` | Recorded anonymous form/module/status handling passes against fixtures; no new Form request. |
| CRDC crosswalk/code maps (OP-C06) | `get_code_map`, `resolve_stored_value`; `/NCIAPI/1.0/api/DataElements/getCRDCList` | Fixture crosswalk/value-level binding behavior passes; no unsupported value is synthesized. |
| Permissible value by public ID (OP-C10) | `get_permissible_value`; `/NCIAPI/1.0/api/permissibleValues/{id}` | Recorded 404; tested result is unavailability. |
| Data-element matching (OP-M01) | `match_data_elements`, `harmonize_data_dictionary`; `/NCIAPI.v2_0.cdeMatch.api:cdeMatch_rad/cdeMatch` | Protected success answers are contract-crafted. Issued protected access remains unverified; this is not the AI Engine. |
| Value-meaning matching (OP-M02) | `match_value_meanings`; `/vmMatch/v1/vmMatch` | Anonymous response contracts are recorded; current behavior tested against fixtures only. No new vmMatch request. |
| LOV/context names and classification schemes (OP-C13, C-11) | `list_contexts`; `/NCILovAPI/1.0/api/getContextNames`; `list_classification_schemes` | Protected LOV access is unverified. Scheme metadata/filtering is not a supported standalone scheme-list operation; fixture refusal is intentional. |
| Registry release / export state (OP-C08, A3.8.2) | `resolve_registry_release`, `get_release_alignment`; `/NCIAPI/1.0/api/registry/releases` and export metadata | No production registry release is published. Fixture `required` mode retains all five published-pin/discovery/cursor assertions; default `optional` skips them explicitly. Export date is content state, not an invented registry release. Stage registry identity remains unknown. |
| Published Match/LOV contracts | `/invoke/pub.swagger:getSwaggerJsonDoc` at the contract base above | Recorded contracts support crafted tests; contract availability does not establish entitlement. |

The [caDSR upstream requests](upstream/cadsr.md) own missing metadata, access and capability
questions. No newly unblocked live tests are claimed from the registry-release work.

### Shared SI joins and workflows

| Capability / operation | Service, interface and evidence | Limitation / next owner |
|---|---|---|
| Graph identities and concept-to-CDE joins (OP-S01) | Shared SI SPARQL; `find_data_elements_for_concept`, `get_release_alignment`, `ground_value`; `/sparql` | Recorded queries and crafted bounds cover graph identities, roles, release mismatch and missing version. Traffic paused; current graph identity not refreshed. |
| REST façade queries (OP-S02) | Shared SI REST `/si-api/v1/database/graph_names`, `/data_elements/with_concept_id`, `/data_elements/with_specific_object_class` | Historical cap/ignored-argument responses do not establish complete results. Required joins use registered SPARQL contracts, not an assumed equivalent REST replacement. |
| Permissible-value joins (OP-S04) | Shared SI SPARQL; `get_concept_for_permissible_value`, `find_data_elements_for_concept`, `resolve_stored_value` and workflow hops | Fixture graph/API-primary distinctions and unresolved references pass. No current live join or replacement endpoint validation. |
| Cohort and grounding orchestration | `expand_cohort`, `ground_value`, `harmonize_data_dictionary` | Fixture orchestration passes; this does not establish a complete live workflow or UAT journey. |

[Exact query forms](../acceptance/request-forms/ssis.md) and the
[Shared SI dependency register](upstream/ssis-dependencies.md) preserve the service's
maintenance/development gap and the coordinator's unresolved replacement assessment.
EVS SPARQL and caDSR APIs remain candidates, with no automatic alternate-upstream fallback.

### CDE AI Engine

The [25 October WAIT assessment](upstream/cadsr-ai-engine.md) is historical. The later
[#438 investigation](https://github.com/CBIIT/nci-sip-mcp/issues/438) preserves a 2 November
reported stage contract and access failure; it does not silently replace that assessment.

| Operation | Retained evidence | Missing / untested |
|---|---|---|
| OpenAPI identity | Embedded HTML reported version 2.0; selected downloaded JSON reported 1.0 | Original complete contracts were not retained. Producer.v2 spelling is not version authority; authoritative contract/schema and deployment identity remain unresolved; the reported request hostname is known. |
| getEmbeddingVariants | Historical 401, cause unknown | No repeat, entitlement comparison or model-list request authorized. |
| performCDEAIMatch | No matching call | Model/revision, semantics, bounds, provenance and compatibility with CDE Match/vmMatch unestablished; no quality/latency evaluation. |

## Next checkpoints

- The project coordinator owns [#381's access/route follow-up](https://github.com/CBIIT/nci-sip-mcp/issues/381).
  The single 12 November authorization is consumed. There is no repeat or automatic
  troubleshooting authorization. The 403 does not establish a VPN, credential or EVS defect.
- The selected native-suite milestone depends on the offline [#479 lane](https://github.com/CBIIT/nci-sip-mcp/issues/479).
  Its later scheduled run needs its own disposition; today's in-process proposal produced
  no MCP content result and cannot count toward native-suite readiness.
- The coordinator's [#459 readiness checkpoint](https://github.com/CBIIT/nci-sip-mcp/issues/459)
  and conditional [#460 UAT](https://github.com/CBIIT/nci-sip-mcp/issues/460) require actual
  participants, client/provider and domain readiness. UAT has not run.
- Implementer/current-role-successor retains private first-attempt evidence; the Reviewer
  owns independent clearance. [#461](https://github.com/CBIIT/nci-sip-mcp/issues/461) tracks this
  publication and review. Public provenance contains safe counts/hashes, not response bodies,
  credentials or private ledger paths.

## Generated fixture expectations and historical scheduled live outcomes

These tables are generated offline by `pdm run acceptance-status`. Fixture expectations describe
the current test contract, while the live table is the original stored scheduled-main report;
it is not a fresh probe. A PASS includes correct failure behavior where a capability is absent.

<!-- acceptance-status:begin -->
Fixture-mode outcome of each tool, from the expected outcomes of the 1005 tests of the acceptance suite ([acceptance/expected/fixture.json](../acceptance/expected/fixture.json)); [acceptance/README.md](../acceptance/README.md#the-report) defines the outcomes. This table is generated by `pdm run acceptance-status`.

| Group | Tools | Outcome of each tool |
|---|---|---|
| EVS tools | 12 | PASS: `resolve_release`, `get_concept`, `get_concepts`, `search_concepts`, `get_concept_hierarchy`, `expand_value_set`, `get_concept_neighborhood`, `get_concept_subsets`, `get_concept_mappings`, `resolve_retired_code`, `list_relationships`, `list_terminologies` |
| caDSR tools | 10 | PASS: `resolve_registry_release`, `get_data_element`, `search_data_elements`, `match_data_elements`, `match_value_meanings`, `get_form`, `get_permissible_value`, `get_code_map`, `list_contexts`, `list_classification_schemes` |
| Cross-domain tools | 4 | PASS: `find_data_elements_for_concept`, `get_concept_for_permissible_value`, `resolve_stored_value`, `get_release_alignment` |
| Workflow tools | 3 | PASS: `ground_value`, `expand_cohort`, `harmonize_data_dictionary` |

Latest stored scheduled main live run: [39214306172](https://github.com/CBIIT/nci-si-mcp/actions/runs/39214306172), 2026-10-19T07:10:38Z; commit [782747778543](https://github.com/CBIIT/nci-si-mcp/commit/782747778543b0fb78498d7a3bc7e718f319475e). **Complete**.
Report SHA-256: `8a6ae3a9a2050838e9d1096f687e2cc1fe50fb4886be407e7628aadcb7254479`; artifact 12169206506.

| Tool | Live passed / failed / skipped | Skip reason / context |
|---|---|---|
| `resolve_release` | 1 / 0 / 17 | 17 unrecorded |
| `get_concept` | 6 / 0 / 41 | 41 unrecorded |
| `get_concepts` | 0 / 0 / 38 | 38 unrecorded |
| `search_concepts` | 0 / 0 / 79 | 79 unrecorded |
| `get_concept_hierarchy` | 1 / 0 / 50 | 50 unrecorded |
| `expand_value_set` | 0 / 0 / 47 | 47 unrecorded |
| `get_concept_neighborhood` | 0 / 0 / 55 | 55 unrecorded |
| `get_concept_subsets` | 0 / 0 / 32 | 32 unrecorded |
| `get_concept_mappings` | 1 / 0 / 33 | 33 unrecorded |
| `resolve_retired_code` | 0 / 0 / 34 | 34 unrecorded |
| `list_relationships` | 1 / 0 / 27 | 27 unrecorded |
| `list_terminologies` | 0 / 0 / 8 | 8 unrecorded |
| `resolve_registry_release` | 0 / 0 / 2 | 2 unrecorded |
| `get_data_element` | 8 / 0 / 35 | 35 unrecorded |
| `search_data_elements` | 2 / 0 / 23 | 23 unrecorded |
| `match_data_elements` | 3 / 0 / 21 | 21 unrecorded |
| `match_value_meanings` | 1 / 0 / 15 | 15 unrecorded |
| `get_form` | 1 / 0 / 27 | 27 unrecorded |
| `get_permissible_value` | 1 / 0 / 6 | 6 unrecorded |
| `get_code_map` | 1 / 0 / 33 | 33 unrecorded |
| `list_contexts` | 0 / 0 / 17 | 17 unrecorded |
| `list_classification_schemes` | 1 / 0 / 0 | — |
| `find_data_elements_for_concept` | 2 / 0 / 44 | 44 unrecorded |
| `get_concept_for_permissible_value` | 0 / 0 / 37 | 37 unrecorded |
| `resolve_stored_value` | 0 / 0 / 35 | 35 unrecorded |
| `get_release_alignment` | 0 / 0 / 10 | 10 unrecorded |
| `ground_value` | 0 / 0 / 48 | 48 unrecorded |
| `expand_cohort` | 0 / 0 / 46 | 46 unrecorded |
| `harmonize_data_dictionary` | 0 / 0 / 15 | 15 unrecorded |
| Unattributed gates | 16 / 0 / 45 | 45 unrecorded |
| **Total** | **46 / 0 / 920** | |

46 passed / 0 failed / 920 skipped. Skipped includes `not_live`, `not_implemented` and ordinary skips; only `not_live` has the reason contexts above. Passed validation/refusal tests do not establish live content retrieval. Unrecorded reasons are never inferred from current test markers.
<!-- acceptance-status:end -->
