# caDSR upstream requirements

Generated from [catalogue.yaml](catalogue.yaml). Read the [evidence boundary](README.md) first.

## cadsr-field-spelling

**Clarify status spellings and Form API field names**

Platform requirement/operation: **OP-C01; OP-C11; OP-M01**.
Specification requirements: `get_data_element-1`, `get_form-1`.
Source discussion: [issue evidence](https://github.com/CBIIT/nci-si-mcp/issues/379).

**Observation.** Latest 2413218/v2 supplies registrationStatus Superceded. Form 5406471 supplies publicID and latesetVersionIndicator, unlike DataElement publicId and latestVersionIndicator.

**Reproduction.** Read the linked anonymous DataElement and Form responses without credentials.

**Expected.** Document whether Superceded and Superseded are accepted filter spellings and whether publicID and latesetVersionIndicator are intentional, aliases or planned corrections.

**Impact.** Normalizing spelling or assuming consistent keys can discard or misattribute upstream metadata.

**Workaround.** Pass raw metadata through; registrationStatus remains a free string passed unchanged as a match header. Nonempty search filters remain unsupported under OP-C03.

**Acceptance criteria.** Publish exact accepted spellings and field names, including compatibility expectations; no local normalization or inferred filter support.

Evidence (linked request/response files retain their original form):

- [acceptance/fixtures/recorded/cadsr/data-element-2413218.json](../../acceptance/fixtures/recorded/cadsr/data-element-2413218.json) — recorded on 2026-10-26; GET cadsr /NCIAPI/1.0/api/DataElement/2413218; SHA-256 `04fbdbe474d39a8b0cc5585e3986601329c6e4ba1aa178ce7838d29e5e9de4d4`.
- [acceptance/fixtures/recorded/cadsr/form-5406471.json](../../acceptance/fixtures/recorded/cadsr/form-5406471.json) — recorded on 2026-10-03; GET cadsr /NCIFormAPI.v2_0:NciFormApiRad/Form/5406471; SHA-256 `25748e394ec5e83898095714c359969906b62c49e1b5f7e4d585638861d8d385`.

| Affected test function | Fixture | Live |
|---|---|---|
| `acceptance/tests/test_cadsr.py::test_a_form_returns_its_modules_and_its_status_unchanged_a_retired_one_too` | 1 passed | 1 not_live |

## cadsr-concept-sources

**Document concept sources and compound reference semantics**

Platform requirement/operation: **OP-C06**.
Specification requirements: `get_concept_for_permissible_value-1`.
Source discussion: [issue evidence](https://github.com/CBIIT/nci-si-mcp/issues/368).

**Observation.** The issue's 25 October sample observed NCI_CONCEPT_CODE, CTCAE_CODE, RADLEX_CODE, NCI_META_CUI, UMLS_CUI and LOINC_CODE, including C0009219 and C45255::10000. Recorded 2200604 v1 has UMLS_CUI C0332307 as representation, not a PV; recorded form 5406471 has LOINC references on CDE 5301153, not 5024313. The new source scenarios are contract-crafted from issue observations with synthetic metadata, not captures.

**Reproduction.** Read evsSource alongside conceptCode in the data element API and inspect the linked recordings; exercise the crafted cadsr/concept-sources scenario.

**Expected.** Document all source labels, missing/null evidence and compound code::value semantics without implying every concept is NCIt.

**Impact.** Treating every C-prefixed identifier as NCIt can resolve a UMLS reference against the wrong terminology.

**Workaround.** Preserve exact source/code; resolve only NCI_CONCEPT_CODE with standalone NCIt codes and return other primary references unresolved with caDSR-only provenance.

**Acceptance criteria.** LOINC, UMLS and compound primaries succeed without an NCIt concept request; unknown sources remain unchanged.

Evidence (linked request/response files retain their original form):

- [acceptance/fixtures/recorded/cadsr/data-element-2200604-version-1.json](../../acceptance/fixtures/recorded/cadsr/data-element-2200604-version-1.json) — recorded on 2026-10-03; GET cadsr /NCIAPI/1.0/api/DataElement/2200604; SHA-256 `2ac9667840a7004d7bef58983be22b0cfb15b5d26d14b85f2a13c3658c2064f2`.
- [acceptance/fixtures/recorded/cadsr/form-5406471.json](../../acceptance/fixtures/recorded/cadsr/form-5406471.json) — recorded on 2026-10-03; GET cadsr /NCIFormAPI.v2_0:NciFormApiRad/Form/5406471; SHA-256 `25748e394ec5e83898095714c359969906b62c49e1b5f7e4d585638861d8d385`.

| Affected test function | Fixture | Live |
|---|---|---|
| `acceptance/tests/test_cross_domain.py::test_a_value_resolves_to_the_concept_it_stands_for_naming_both_content_states` | 1 passed | 1 not_live |

## cadsr-registry

**Publish registry identity and a verifiable pin contract**

Platform requirement/operation: **C-1; OP-C08; OP-C06; OP-M01; OP-M02; OP-C12**.
Specification requirements: `resolve_registry_release-1`, `X-21`, `get_code_map-1`.
Source discussion: [issue evidence](https://github.com/CBIIT/nci-si-mcp/issues/42#issuecomment-6011947240).

**Observation.** The release route is recorded 404. The export folder names 2026-07-01 22:19 without a timezone while its README says daily updates; matching/forms declare no registryRelease transport. C-1 fixtures are requested future behavior.

**Reproduction.** Read the release route and the exact releasedCDEsXML-OD.zip folder row; inspect the matching/form contracts and the crafted pinned getCRDCList request/echo of the cadsr/with-registry-release scenario, crafted in memory by cadsr_with_registry_release in acceptance/src/nci_sip_acceptance/craft.py.

**Expected.** Publish stable registry identifiers and generation dates; specify pin transport and response verification for every content, form and matching operation, including getCRDCList.

**Impact.** Export dates alone cannot support reproducible registry queries; old registry content cannot be addressed.

**Workaround.** Preserve the folder's local date unchanged with published=false; unavailable pins fail release_not_available and listed but unaddressable matching/form pins fail capability_unavailable.

**Acceptance criteria.** Every supported pin is echoed and verified; an unknown pin fails, an unpinned result is never labelled pinned, and export freshness is stated accurately.

Evidence (linked request/response files retain their original form):

- [acceptance/fixtures/recorded/cadsr/registry-releases.json](../../acceptance/fixtures/recorded/cadsr/registry-releases.json) — recorded on 2026-10-03; GET cadsr /NCIAPI/1.0/api/registry/releases; SHA-256 `7104dba3ddd6cef86e2410d8572ee2b032e4a0593f5f6d72433275f29d953963`.
- [acceptance/fixtures/recorded/cadsr-ftp/cde-xml-listing.json](../../acceptance/fixtures/recorded/cadsr-ftp/cde-xml-listing.json) — recorded on 2026-10-03; GET cadsr-ftp /CDE/XML/; SHA-256 `3801e0ce4d9b8e2f1d717affe45fe5bd0d2533079c76e0cdc0736471b26de797`.
- [acceptance/fixtures/recorded/cadsr-ftp/cde-xml-readme.json](../../acceptance/fixtures/recorded/cadsr-ftp/cde-xml-readme.json) — recorded on 2026-10-03; GET cadsr-ftp /CDE/XML/README; SHA-256 `eb62930ae94a0faa6fb21ce086a22b65f0774c14b155c6fc24af999fee46a60b`.

| Affected test function | Fixture | Live |
|---|---|---|
| `acceptance/tests/test_cadsr.py::test_without_a_registry_release_the_export_date_stands_for_it` | 1 passed | 1 not_live |
| `acceptance/tests/test_cadsr.py::test_a_published_registry_release_is_returned` | 1 passed | 1 not_live |
| `acceptance/tests/test_cadsr.py::test_a_published_registry_release_is_asked_for_and_named_with_its_date` | 1 passed | 1 not_live |

## cadsr-export-retention

**Retain twelve months of dated caDSR exports**

Platform requirement/operation: **C-1; A3.8.2**.
Specification requirements: `resolve_registry_release-1`, `X-21`.
Source discussion: [issue evidence](https://github.com/CBIIT/nci-si-mcp/issues/359#issuecomment-6399041641).

**Observation.** The recorded listing names the current releasedCDEsXML-OD.zip; its README states daily updates and refers to an Archive folder for older files. These recordings do not establish immutable dated retrieval or twelve-month retention. The data-element recording describes permissible-value removals without a version change; an item version alone cannot identify content edited in place.

**Reproduction.** Inspect the recorded export listing and README and the change notes in data-element-2200604.json; this request adds no live probe.

**Expected.** Retain at least twelve months of immutable dated exports, with generation timestamps and documented retrieval by date, so a consumer can retrieve the export for the day cited.

**Impact.** Exact reproduction of a set-level answer or an item edited in place needs that day's export; current API responses and item versions do not suffice.

**Workaround.** Report supplied provenance and retrievedAt honestly. The MCP server keeps no export archive and invents no registry identifier. The sponsor's registry-contract decision remains pending.

**Acceptance criteria.** The upstream team demonstrates retrieval of dated exports spanning at least twelve months and documents generation dates, retention and retrieval. The cited existing test covers export-date discovery only, not retention; no executable MCP acceptance case is added for this upstream operational request.

Evidence (linked request/response files retain their original form):

- [acceptance/fixtures/recorded/cadsr-ftp/cde-xml-listing.json](../../acceptance/fixtures/recorded/cadsr-ftp/cde-xml-listing.json) — recorded on 2026-10-03; GET cadsr-ftp /CDE/XML/; SHA-256 `3801e0ce4d9b8e2f1d717affe45fe5bd0d2533079c76e0cdc0736471b26de797`.
- [acceptance/fixtures/recorded/cadsr-ftp/cde-xml-readme.json](../../acceptance/fixtures/recorded/cadsr-ftp/cde-xml-readme.json) — recorded on 2026-10-03; GET cadsr-ftp /CDE/XML/README; SHA-256 `eb62930ae94a0faa6fb21ce086a22b65f0774c14b155c6fc24af999fee46a60b`.
- [acceptance/fixtures/recorded/cadsr/data-element-2200604.json](../../acceptance/fixtures/recorded/cadsr/data-element-2200604.json) — recorded on 2026-10-03; GET cadsr /NCIAPI/1.0/api/DataElement/2200604; SHA-256 `14bc9fbd482626b783f7342d4e4eb39e10423471c813cb45e8233c20eccf9bdc`.

| Affected test function | Fixture | Live |
|---|---|---|
| `acceptance/tests/test_cadsr.py::test_without_a_registry_release_the_export_date_stands_for_it` | 1 passed | 1 not_live |

## cadsr-search

**Serve keyword search with named filters and visible caps**

Platform requirement/operation: **C-3; OP-C03**.
Specification requirements: `search_data_elements-1`, `search_data_elements-2`.
Source discussion: [issue evidence](https://github.com/CBIIT/nci-si-mcp/issues/42#issuecomment-5966921102).

**Observation.** Keyword search is requested OP-C03, not a verified served operation; the capped search answer is crafted. The live benchmark returned upstream_unavailable. Context/workflow/registration/value-domain filter parameter names are absent from that requested form.

**Reproduction.** Use DataElement/search with keyword and pageSize from the request inventory; compare with public-id lookup. The capped answer crafted/OP-C03/search-over-cap.json is crafted in memory by cadsr_over_cap in acceptance/src/nci_sip_acceptance/craft.py. The recorded malformed-id response illustrates the HTTP-200 error convention, not a recording of keyword search.

**Expected.** A keyword endpoint with explicit filter names, registryRelease verification, pagination or a machine-readable 1000-result truncation signal. Open question for caDSR: can legitimate preferred-question-text lookup inputs exceed 2,000 Unicode characters or a 7,000-byte encoded URL target including filters/pins? The July 2026 export maximum was 1,011 characters (CDE 6148373 v1), but excludes retired/TEST/TRAINING records; the byte ceiling is conservative server policy, not measured caDSR capacity. No legitimate-input rejection has been observed; broader export and route measurements are needed.

**Impact.** Keyword-based discovery and filtering cannot be relied on live; a capped list cannot be mistaken for a known total.

**Workaround.** Public-id or preferred-question-text lookup; propagate search failures with capability guidance, no synthetic answer or alternate-route fallback.

**Acceptance criteria.** Positive, no-match, filtered and over-cap queries have distinct correct results; every requested filter is honored or explicitly rejected, and continuation/truncation is truthful.

Evidence (linked request/response files retain their original form):

- [acceptance/fixtures/recorded/cadsr/data-element-refused.json](../../acceptance/fixtures/recorded/cadsr/data-element-refused.json) — recorded on 2026-10-03; GET cadsr /NCIAPI/1.0/api/DataElement/notanumber; SHA-256 `ed10c3eb0e0477fbd90decab2dde6ca9fabb374dd7e9abe822b393b78c8edaaf`.
- [docs/evidence/phase-5/benchmark-live.json](../../docs/evidence/phase-5/benchmark-live.json) — measurement report; SHA-256 `7c55e576784413ca805d45c9a71e5fefd524188197f1216277958676102dfbbb`.

| Affected test function | Fixture | Live |
|---|---|---|
| `acceptance/tests/test_cadsr.py::test_a_search_s_page_is_the_limit_given` | 1 passed | 1 not_live |
| `acceptance/tests/test_cadsr.py::test_a_search_the_platform_caps_reports_the_cap_and_no_total` | 1 passed | 1 not_live |

## cadsr-match-parameters

**Specify matching model and threshold controls**

Platform requirement/operation: **C-6; OP-M01**.
Specification requirements: `match_data_elements-1`.
Source discussion: [issue evidence](https://github.com/CBIIT/nci-si-mcp/issues/42).

**Observation.** The published matching contract does not take the requested modelVariant and similarityThreshold controls. The [CDE AI Engine evaluation dated 25 October 2026](cadsr-ai-engine.md) records the team's retrieval/reranking report separately from the unknown Engine contract fields and recommends waiting for contract and comparison evidence.

**Reproduction.** Compare the contract's declared headers/body with those two caller parameters; fixture tests assert rejection, not upstream support.

**Expected.** Name supported models, threshold range/meaning and their request fields, or explicitly exclude them from the service contract.

**Impact.** Callers cannot tune model choice or threshold consistently across implementations.

**Workaround.** Reject both parameters as invalid_request rather than silently ignoring them.

**Acceptance criteria.** Supported controls measurably affect the declared result semantics; unsupported values fail explicitly and are never dropped.

Evidence (linked request/response files retain their original form):

- [acceptance/fixtures/recorded/cadsr-contracts/cde-match.json](../../acceptance/fixtures/recorded/cadsr-contracts/cde-match.json) — recorded on 2026-10-03; GET cadsr-contracts /pub.swagger:getSwaggerJsonDoc; SHA-256 `1086ef480d7afa2137b9ad67ce45a07a63ba85e3ec44769c008d4556110941b6`.

| Affected test function | Fixture | Live |
|---|---|---|
| `acceptance/tests/test_cadsr.py::test_what_the_platform_does_not_take_is_an_invalid_request_never_ignored` | 3 passed | 3 not_live |

## cadsr-access

**Provide access for CDE Match and lists of values**

Platform requirement/operation: **C-6; OP-M01; OP-C13**.
Specification requirements: `match_data_elements-1`, `list_contexts-1`, `harmonize_data_dictionary-1`.
Source discussion: [issue evidence](https://github.com/CBIIT/nci-si-mcp/issues/42#issuecomment-5966334217).

**Observation.** Anonymous calls are recorded 401. A credential has been issued for caDSR staging only; protected staging attempts were last recorded as 401 on 24 November 2026 (https://github.com/CBIIT/nci-sip-mcp/issues/383#issuecomment-7139797516; direct HTTP, not an MCP acceptance run), cause not inferred, and access is unverified. On 25 November 2026 a collaborator's MCP run (unified stdio client; [#383](https://github.com/CBIIT/nci-sip-mcp/issues/383#issuecomment-7176019142)) recorded returned staging CDE Match matches for three observations, `MAP-07.match`, `MAP-07.dictionary` and `BASE-04.program-b`; that is not proof that every matching operation succeeds. Whether that run held a credential, and which, is unknown, so it does not show that the earlier credential was restored, explain the 401s, or establish lists-of-values, contexts or AI Engine access. The unrelated-input CDE Match case still returns a malformed-match error (#639). Credentialed fixture answers follow published contracts and are crafted. Live matching and harmonization measurements returned upstream errors. The [CDE AI Engine evaluation dated 25 October 2026](cadsr-ai-engine.md) found no Engine contract or documented anonymous Stage endpoint in the reviewed sources; its access policy is unknown, not inferred from Match 2.0.

**Reproduction.** Repeat the recorded anonymous requests; after access is provided, re-record with an environment-supplied credential and never write its value to evidence.

**Expected.** Restore or verify authorized access for this project's staging credential, or provide a documented public tier, with a stable access policy.

**Impact.** Matching, registry-context enumeration and dictionary harmonization lack live success evidence.

**Workaround.** Test the published contracts against crafted fixtures; preserve live errors and label content evidence fixture-only, without fabricating a formal acceptance exception.

**Acceptance criteria.** Authorized requests return real identities, scores/rules and contexts that pass the same assertions; unauthorized requests fail explicitly and credentials reach only the intended origin.

Evidence (linked request/response files retain their original form):

- [acceptance/fixtures/recorded/cadsr/cde-match-refused-object.json](../../acceptance/fixtures/recorded/cadsr/cde-match-refused-object.json) — recorded on 2026-10-03; POST cadsr /NCIAPI.v2_0.cdeMatch.api:cdeMatch_rad/cdeMatch; SHA-256 `ed6330ea604fc467f0c1dcf26584c4c8ac4f672d700b09a459ed5a14fa99731a`.
- [acceptance/fixtures/recorded/cadsr/context-names-refused.json](../../acceptance/fixtures/recorded/cadsr/context-names-refused.json) — recorded on 2026-10-03; GET cadsr /NCILovAPI/1.0/api/getContextNames; SHA-256 `8bf9285e06a75a9b417aafbb1f38218f018d170bb154c4890455d422d2dc3bce`.
- [acceptance/fixtures/recorded/cadsr-contracts/cde-match.json](../../acceptance/fixtures/recorded/cadsr-contracts/cde-match.json) — recorded on 2026-10-03; GET cadsr-contracts /pub.swagger:getSwaggerJsonDoc; SHA-256 `1086ef480d7afa2137b9ad67ce45a07a63ba85e3ec44769c008d4556110941b6`.
- [acceptance/fixtures/recorded/cadsr-contracts/lists-of-values.json](../../acceptance/fixtures/recorded/cadsr-contracts/lists-of-values.json) — recorded on 2026-10-03; GET cadsr-contracts /pub.swagger:getSwaggerJsonDoc; SHA-256 `0cf8fa218acbfb7ff47c2861cae4edcb0e04d21d434432e4e9eff7b9ebe1fdc8`.
- [docs/evidence/phase-5/benchmark-live.json](../../docs/evidence/phase-5/benchmark-live.json) — measurement report; SHA-256 `7c55e576784413ca805d45c9a71e5fefd524188197f1216277958676102dfbbb`.

| Affected test function | Fixture | Live |
|---|---|---|
| `acceptance/tests/test_cadsr.py::test_data_element_matches_are_scored_and_rule_attributed_as_the_platform_says` | 1 passed | 1 not_live |
| `acceptance/tests/test_cadsr.py::test_the_contexts_are_the_registry_s_context_names` | 1 passed | 1 not_live |
| `acceptance/tests/test_workflow.py::test_each_column_is_matched_once_and_the_unmatched_are_listed` | 1 passed | 1 not_live |

## cadsr-match-wire

**Reconcile matching request and sentinel semantics**

Platform requirement/operation: **C-6; OP-M01; OP-M02**.
Specification requirements: `match_data_elements-1`, `match_value_meanings-1`.
Source discussion: [issue evidence](https://github.com/CBIIT/nci-si-mcp/issues/42#issuecomment-6011568485).

**Observation.** CDE Match's contract declares one object, but the issue reports a successful array request in September; both recorded forms now get 401. vmMatch's published header descriptions conflict with its recorded Restricted/match wire form. Recorded crosswalkCode=NA is interpreted as no crosswalk, pending confirmation.

**Reproduction.** With approved access, compare object and array CDE requests; compare vmMatch header descriptions with the recorded Male request and inspect its NA crosswalk fields.

**Expected.** Publish one authoritative body/header mapping and declare whether NA is a reserved sentinel or a possible real crosswalk identifier.

**Impact.** Consumers otherwise implement incompatible wire forms or expose a sentinel as a real mapping.

**Workaround.** CDE uses the published single object; VM uses the approved recorded mapping with no alternate-header fallback; NA yields no crosswalk.

**Acceptance criteria.** Contract examples execute successfully, unsupported forms fail explicitly, and a sentinel cannot be confused with a real code system.

Evidence (linked request/response files retain their original form):

- [acceptance/fixtures/recorded/cadsr/cde-match-refused.json](../../acceptance/fixtures/recorded/cadsr/cde-match-refused.json) — recorded on 2026-10-03; POST cadsr /NCIAPI.v2_0.cdeMatch.api:cdeMatch_rad/cdeMatch; SHA-256 `611e1e8f3ffd99e28311683e2797a47ac6b0bb82e66907326068c782187a1040`.
- [acceptance/fixtures/recorded/cadsr/cde-match-refused-object.json](../../acceptance/fixtures/recorded/cadsr/cde-match-refused-object.json) — recorded on 2026-10-03; POST cadsr /NCIAPI.v2_0.cdeMatch.api:cdeMatch_rad/cdeMatch; SHA-256 `ed6330ea604fc467f0c1dcf26584c4c8ac4f672d700b09a459ed5a14fa99731a`.
- [acceptance/fixtures/recorded/cadsr-contracts/cde-match.json](../../acceptance/fixtures/recorded/cadsr-contracts/cde-match.json) — recorded on 2026-10-03; GET cadsr-contracts /pub.swagger:getSwaggerJsonDoc; SHA-256 `1086ef480d7afa2137b9ad67ce45a07a63ba85e3ec44769c008d4556110941b6`.
- [acceptance/fixtures/recorded/cadsr/vm-match-male.json](../../acceptance/fixtures/recorded/cadsr/vm-match-male.json) — recorded on 2026-10-03; POST cadsr /vmMatch/v1/vmMatch; SHA-256 `739daa720101c52a2d6749e112dd98ec2097a8faa5b97c46d40d74824403502e`.

| Affected test function | Fixture | Live |
|---|---|---|
| `acceptance/tests/test_cadsr.py::test_data_element_matches_are_scored_and_rule_attributed_as_the_platform_says` | 1 passed | 1 not_live |
| `acceptance/tests/test_cadsr.py::test_value_meaning_matches_are_the_platform_s_in_its_order_with_their_rule` | 1 passed | 1 not_live |

## cadsr-match-latency

**Bound common-value matching latency**

Platform requirement/operation: **C-6; OP-M02**.
Specification requirements: `X-5`, `match_value_meanings-1`.
Source discussion: [issue evidence](https://github.com/CBIIT/nci-si-mcp/issues/42#issuecomment-5968476814).

**Observation.** The issue reports Unknown timing out with HTTP 504 at 10.6 seconds on 3 October, while Not Applicable and Other, specify completed near two seconds. That timing observation is in the issue, not the Male fixture.

**Reproduction.** POST an array containing name=Unknown with Restricted/match, recording status and elapsed time; compare the two successful common values under the same conditions.

**Expected.** State the supported matching deadline and resolve common-value queries within it, or provide a defined asynchronous/bounded alternative.

**Impact.** A normal data value can fail at the gateway before a useful answer is available.

**Workaround.** Use the bounded matching timeout/retry budget and a structured error; do not turn a timeout into an empty match.

**Acceptance criteria.** Repeated common-value probes meet the agreed upstream deadline and preserve actual match semantics; timeout tests still fail explicitly.

Evidence (linked request/response files retain their original form):

- [acceptance/fixtures/recorded/cadsr/vm-match-male.json](../../acceptance/fixtures/recorded/cadsr/vm-match-male.json) — recorded on 2026-10-03; POST cadsr /vmMatch/v1/vmMatch; SHA-256 `739daa720101c52a2d6749e112dd98ec2097a8faa5b97c46d40d74824403502e`.
- [acceptance/request-forms/cadsr.md](../../acceptance/request-forms/cadsr.md) — document; SHA-256 `e74fcaf07d90c35179f787f7d50dcfcfb7383229a2b9fc075e1e704bc0e384e5`.

| Affected test function | Fixture | Live |
|---|---|---|
| `acceptance/tests/test_cadsr.py::test_matching_slower_than_the_match_timeout_is_a_timeout` | 2 passed | 2 not_live |

## cadsr-forms

**Supply a public released form and clarify form lookup**

Platform requirement/operation: **OP-C12; C-1**.
Specification requirements: `get_form-1`, `X-15`.
Source discussion: [issue evidence](https://github.com/CBIIT/nci-si-mcp/issues/42#issuecomment-6011947240).

**Observation.** The known anonymous v2 example is RETIRED ARCHIVED; v1 access and discovery limitations are recorded in the issue. An unknown form is HTTP 200, form=null, type E, indistinguishable from a genuine failure using that exact shape.

**Reproduction.** Fetch v2 Form/5406471 and Form/99999999; obtain a released public form identifier or a documented keyword discovery route.

**Expected.** Provide a released test form, document Form/query's supported identifiers and optional keyword search, and distinguish absence from internal failure using 404 or a machine-readable absence code.

**Impact.** Released-form behavior lacks a public example; identical absence/error shapes cannot be classified perfectly.

**Workaround.** Preserve retired status; accept identifiers only; interpret only the approved validated-id HTTP-200/null-form/type-E shape as not_found. Other errors use X-15.

**Acceptance criteria.** Released and retired fixtures preserve their real statuses/modules; unknown IDs are distinct from failures, and registry pins follow the C-1 contract before being offered.

Evidence (linked request/response files retain their original form):

- [acceptance/fixtures/recorded/cadsr/form-5406471.json](../../acceptance/fixtures/recorded/cadsr/form-5406471.json) — recorded on 2026-10-03; GET cadsr /NCIFormAPI.v2_0:NciFormApiRad/Form/5406471; SHA-256 `25748e394ec5e83898095714c359969906b62c49e1b5f7e4d585638861d8d385`.
- [acceptance/fixtures/recorded/cadsr/form-unknown.json](../../acceptance/fixtures/recorded/cadsr/form-unknown.json) — recorded on 2026-10-03; GET cadsr /NCIFormAPI.v2_0:NciFormApiRad/Form/99999999; SHA-256 `9b0921daa6b31b4939d89874b4b996051c1d4a85e956030cca04901f650dbcc7`.

| Affected test function | Fixture | Live |
|---|---|---|
| `acceptance/tests/test_cadsr.py::test_a_form_returns_its_modules_and_its_status_unchanged_a_retired_one_too` | 1 passed | 1 not_live |
| `acceptance/tests/test_cadsr.py::test_an_unknown_form_answered_inside_an_http_200_is_not_found` | 1 passed | 1 not_live |

## cadsr-errors

**Distinguish invalid input, absence and upstream failure**

Platform requirement/operation: **OP-C01; OP-C09; X-15**.
Specification requirements: `X-15`.
Source discussion: [issue evidence](https://github.com/CBIIT/nci-si-mcp/issues/42#issuecomment-5969077942).

**Observation.** Recorded invalid IDs and absence are HTTP 200 with different apiResponse envelopes; missing conceptCode is reported in the issue as an empty type-S success. Without Accept, recorded endpoints answer HTML.

**Reproduction.** Use the recorded unknown/malformed ID requests and omit conceptCode on DataElements/Concept?headerOnly=true; compare requests with and without Accept:application/json.

**Expected.** Return 400 for missing/invalid arguments, 404 for absence, appropriate failure status otherwise, and a consistent JSON error contract.

**Impact.** A caller may confuse a faulty request or server failure with a legitimate empty result.

**Workaround.** Validate caller input, explicitly ask for JSON, and classify known masked-error envelopes centrally without parsing arbitrary error prose.

**Acceptance criteria.** Valid no-match, invalid input, missing content and outage probes produce distinct structured outcomes; no error becomes an empty success.

Evidence (linked request/response files retain their original form):

- [acceptance/fixtures/recorded/cadsr/data-element-unknown.json](../../acceptance/fixtures/recorded/cadsr/data-element-unknown.json) — recorded on 2026-10-03; GET cadsr /NCIAPI/1.0/api/DataElement/99999999; SHA-256 `3d77e7c7940087d30bcf2339f0796136b0d3607b8da37eded431fe773eb4c4f5`.
- [acceptance/fixtures/recorded/cadsr/data-element-refused.json](../../acceptance/fixtures/recorded/cadsr/data-element-refused.json) — recorded on 2026-10-03; GET cadsr /NCIAPI/1.0/api/DataElement/notanumber; SHA-256 `ed10c3eb0e0477fbd90decab2dde6ca9fabb374dd7e9abe822b393b78c8edaaf`.
- [acceptance/fixtures/recorded/cadsr/data-element-2200604-html.json](../../acceptance/fixtures/recorded/cadsr/data-element-2200604-html.json) — recorded on 2026-10-03; GET cadsr /NCIAPI/1.0/api/DataElement/2200604; SHA-256 `eb076396dde08cfe42c72ed410ec25f02a69c45978abecca0f4e704ae65ecc07`.

| Affected test function | Fixture | Live |
|---|---|---|
| `acceptance/tests/test_cadsr.py::test_a_refusal_inside_an_http_200_is_an_invalid_request` | 1 passed | 1 not_live |
| `acceptance/tests/test_cadsr.py::test_html_where_json_was_asked_for_is_an_upstream_error` | 1 passed | 1 not_live |

## cadsr-discovery

**Complete permissible-value and classification discovery**

Platform requirement/operation: **OP-C07; OP-C10; OP-C13; C-11**.
Specification requirements: `get_permissible_value-1`, `list_contexts-1`.
Source discussion: [issue evidence](https://github.com/CBIIT/nci-si-mcp/issues/42).

**Observation.** Standalone permissible-value paths return 404; values are nested in data elements. Classification filtering is served, but it is not a catalogue of schemes.

**Reproduction.** Compare the recorded permissible-value routes with DataElement/2200604 and the classification filter for scheme 3685569 version 1.

**Expected.** Document and serve identifier-based permissible-value lookup and an explicit classification-scheme catalogue, with paging and registry identity.

**Impact.** Clients cannot discover schemes or resolve a permissible value directly through the published operation inventory.

**Workaround.** Return capability_unavailable for unsupported standalone operations; do not invent values from unrequested data or present a filtered list as a catalogue.

**Acceptance criteria.** Known/unknown value IDs resolve distinctly and the catalogue enumerates actual scheme identities with bounded continuation and no silent cap.

Evidence (linked request/response files retain their original form):

- [acceptance/fixtures/recorded/cadsr/permissible-values.json](../../acceptance/fixtures/recorded/cadsr/permissible-values.json) — recorded on 2026-10-03; GET cadsr /NCIAPI/1.0/api/DataElement/2200604/permissibleValues; SHA-256 `997cf0b7cd6993fbb17387defd88d5cebcecef3376d2215001bfadb018a7037c`.
- [acceptance/fixtures/recorded/cadsr/permissible-value-9192925.json](../../acceptance/fixtures/recorded/cadsr/permissible-value-9192925.json) — recorded on 2026-10-03; GET cadsr /NCIAPI/1.0/api/permissibleValues/9192925; SHA-256 `1926a89787f0f0f5d2f2d4446622056cbf2e07812938993bb29d25d207981b06`.
- [acceptance/fixtures/recorded/cadsr/classification-3685569.json](../../acceptance/fixtures/recorded/cadsr/classification-3685569.json) — recorded on 2026-10-03; GET cadsr /NCIAPI/1.0/api/DataElements/Classification; SHA-256 `3b53e54ff1f9f6ef5a1ccd80e1df43bcce7a4ca3903e046f2534e5f10d746d19`.
- [acceptance/request-forms/cadsr.md](../../acceptance/request-forms/cadsr.md) — document; SHA-256 `e74fcaf07d90c35179f787f7d50dcfcfb7383229a2b9fc075e1e704bc0e384e5`.

| Affected test function | Fixture | Live |
|---|---|---|
| `acceptance/tests/test_cadsr.py::test_a_capability_the_platform_lacks_is_unavailable_never_empty` | 4 passed | 4 not_live |

## cadsr-concept-release

**Name the NCIt release behind concept links**

Platform requirement/operation: **OP-C09; C-1**.
Specification requirements: `find_data_elements_for_concept-3`, `find_data_elements_for_concept-4`.
Source discussion: [issue evidence](https://github.com/CBIIT/nci-si-mcp/issues/42#issuecomment-5971715792).

**Observation.** caDSR REST's concept links do not name their NCIt release; Shared SI's NCIt graph does. The two surfaces have distinct content states and can return different rows.

**Reproduction.** Compare concept C17357 on the recorded caDSR REST and Shared SI forms and inspect each identity record; never compare their rows as if they were one snapshot.

**Expected.** Name the NCIt release or concept-load state in each link response, alongside the registry state, with a verification contract.

**Impact.** A descendant-expanded join needs a verifiable NCIt hierarchy release; exact-code references do not depend on hierarchy alignment.

**Workaround.** Use Shared SI with both actual graph identities; verify the requested NCIt version only for descendant expansion. Exact-code joins do not use the NCIt hierarchy.

**Acceptance criteria.** For descendant expansion, requested and reported NCIt hierarchy releases agree or the call fails closed. Exact-code joins preserve both actual graph/registry states without requiring hierarchy alignment.

Evidence (linked request/response files retain their original form):

- [acceptance/fixtures/recorded/cadsr/concept-c17357.json](../../acceptance/fixtures/recorded/cadsr/concept-c17357.json) — recorded on 2026-10-03; GET cadsr /NCIAPI/1.0/api/DataElements/Concept; SHA-256 `6db9648a1fe56ec7239f3b418a45341252e4d6c558d474b6786340cc48883126`.
- [acceptance/fixtures/recorded/ssis-sparql/graph-identities.json](../../acceptance/fixtures/recorded/ssis-sparql/graph-identities.json) — recorded on 2026-10-03; POST ssis-sparql /sparql; SHA-256 `1766c631d26999e22a5062f00c9fd4c83ddb3ec8150cc02128af1986bb4b84c6`.

| Affected test function | Fixture | Live |
|---|---|---|
| `acceptance/tests/test_cross_domain.py::test_an_answer_of_the_shared_si_service_names_both_graphs_and_both_content_states` | 1 passed | 1 not_live |
| `acceptance/tests/test_cross_domain.py::test_a_release_other_than_the_ncit_graph_s_fails_closed` | 1 passed | 1 not_live |
