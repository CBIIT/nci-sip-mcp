# Shared SI upstream requirements

Generated from [catalogue.yaml](catalogue.yaml). Read the [evidence boundary](README.md) first.

## ssis-facade

**Make façade caps, discovery and failures explicit**

Platform requirement/operation: **S-5; OP-S02; X-15**.
Specification requirements: `X-15`, `find_data_elements_for_concept-5`.
Source discussion: [issue evidence](https://github.com/CBIIT/nci-si-mcp/issues/42#issuecomment-5968943275).

**Observation.** The missing-dec_pub_id recording is HTTP 200 with informational apiResponse type I and No data found, not a masked error; the team's response relayed on 8 October 2026 treats HTTP 400 for that invalid request as a backlog design improvement. with_concept_id takes a DEC public id, not an NCIt code. The Person query is capped at 1000. The issue reports 2088 graph rows, missing hierarchy graphs in discovery, resources/graph_name?resource_name=caDSR returning 500, and a listed empty NCIm graph; these latter probes are issue evidence, not fixture assertions. See team-responses-2026-10-08.md for the team's maintenance constraints and alternative endpoint guidance.

**Reproduction.** Use the linked graph_names and Person requests, compare the graph count, and repeat the issue's resource-name/NCIm probes without interpreting listed graphs as populated.

**Expected.** Signal truncation or paginate; list all loaded graphs with their roles; fix/document resource-name lookup; remove or populate empty advertised graphs; use 400 JSON for invalid arguments and default to JSON.

**Impact.** Consumers may accept incomplete joins, miss the complete hierarchy, or mistake a bad request for no data.

**Workaround.** Use bounded SPARQL with a maximum-plus-one sentinel, explicit graph identities, Accept JSON, validated inputs and central masked-error handling.

**Acceptance criteria.** A query above 1000 either pages to the true count or explicitly marks its cap; discovery identifies the complete hierarchy and resource errors are distinct from empty results.

Evidence (linked request/response files retain their original form):

- [acceptance/fixtures/recorded/ssis/graph-names.json](../../acceptance/fixtures/recorded/ssis/graph-names.json) — recorded on 2026-10-03; GET ssis /si-api/v1/database/graph_names; SHA-256 `8ebf13d77b912b7421450eba2b5a25203b1460d3f1068913123b2e1a6f2d6a3e`.
- [acceptance/fixtures/recorded/ssis/graph-names-without-limit.json](../../acceptance/fixtures/recorded/ssis/graph-names-without-limit.json) — recorded on 2026-10-03; GET ssis /si-api/v1/database/graph_names; SHA-256 `6d131b67a3f37b295f02d6549fc340896846be29cbea681ca66944bf69e3dc7f`.
- [acceptance/fixtures/recorded/ssis/data-elements-without-dec.json](../../acceptance/fixtures/recorded/ssis/data-elements-without-dec.json) — recorded on 2026-10-03; GET ssis /si-api/v1/data_elements/with_concept_id; SHA-256 `4868c3f1a8266d80412e865b7277673e2e81b30c9730aadd233565ba9840be9f`.
- [acceptance/fixtures/recorded/ssis/data-elements-of-object-class-c25190.json](../../acceptance/fixtures/recorded/ssis/data-elements-of-object-class-c25190.json) — recorded on 2026-10-03; GET ssis /si-api/v1/data_elements/with_specific_object_class; SHA-256 `d2db65446b18e1e0719b7e9a9a025d7c148fdede4761261630cde8b7068f50c8`.
- [acceptance/fixtures/recorded/ssis/graph-names-html.json](../../acceptance/fixtures/recorded/ssis/graph-names-html.json) — recorded on 2026-10-03; GET ssis /si-api/v1/database/graph_names; SHA-256 `e14ffcdc747aa7cc444c3e184347776cfef70e2a42955478a08d32b7bcb33a27`.

| Affected test function | Fixture | Live |
|---|---|---|
| `acceptance/tests/test_cross_domain.py::test_descendants_beyond_the_tool_s_maximum_are_truncated_with_how_much_was_left_out` | 1 passed | 1 not_live |

## ssis-inspection

**Publish query-inspection rules and protocol errors**

Platform requirement/operation: **S-5; X-15**.
Specification requirements: `X-15`, `find_data_elements_for_concept-1`.
Source discussion: [issue evidence](https://github.com/CBIIT/nci-si-mcp/issues/42#issuecomment-5968943275).

**Observation.** Direct SPARQL POST and some plain FILTER regex queries are refused with HTML 403. The issue reports SERVICE and OPTION (TRANSITIVE) refused, while rdfs:subClassOf* works; there is no blanket property-path ban. Reviewer observation on 26 October 2026 (#365, comment 6417595705): form-encoded POST and GET of a valid GROUP_CONCAT query without an explicit separator each returned Apache HTTP 403 text/html on every attempt, while the same query with an explicit separator returned HTTP 200. Valid SPARQL can therefore be refused based on query text without published rules.

**Reproduction.** Compare recorded form-encoded requests with direct POST and the issue's refused FILTER(regex(str(?p), "date|version|generat|export|created", "i")) probe; also compare form-encoded POST and GET of SELECT (GROUP_CONCAT(?x) AS ?g) WHERE { VALUES ?x { "a" "b" } } with SELECT (GROUP_CONCAT(?x;separator="|") AS ?g) WHERE { VALUES ?x { "a" "b" } }. The former was refused with HTML 403 and the latter returned 200 on 26 October 2026, as reported in #365 comment 6417595705. Use the exact registered query text for regression tests.

**Expected.** Publish accepted constructs/transport forms and return a machine-readable SPARQL refusal distinct from an outage.

**Impact.** Clients cannot reliably diagnose policy refusal from an HTML error page.

**Workaround.** Use approved form-encoded templates and validated identifiers, maximum-plus-one rows, no OPTION (TRANSITIVE), and fail malformed/HTML responses as upstream_unavailable.

**Acceptance criteria.** Published supported queries pass, prohibited constructs return a documented structured refusal, and injected identifier delimiters are rejected before any request.

Evidence (linked request/response files retain their original form):

- [acceptance/fixtures/recorded/ssis-sparql/query-refused.json](../../acceptance/fixtures/recorded/ssis-sparql/query-refused.json) — recorded on 2026-10-03; POST ssis-sparql /sparql; SHA-256 `b137d06b4c6ba1660d450366515031519de60486750347d97ec26cd63954db20`.
- [acceptance/fixtures/recorded/ssis-sparql/graph-identities-direct.json](../../acceptance/fixtures/recorded/ssis-sparql/graph-identities-direct.json) — recorded on 2026-10-03; POST ssis-sparql /sparql; SHA-256 `c1dd168f191478bcd7e2bf71df530b20ef68ec71d142c3b664dcb943ad68b4ff`.
- [acceptance/request-forms/ssis.md](../../acceptance/request-forms/ssis.md) — document; SHA-256 `5efbe44a24d2b326683068515baa6982eb42708469e72ccc640dc35752ea380b`.

| Affected test function | Fixture | Live |
|---|---|---|
| `acceptance/tests/test_cross_domain.py::test_the_data_elements_are_the_concept_s_and_with_expansion_its_descendants_too` | 2 passed | 2 not_live |

## ssis-identities

**Publish typed graph dates and a registry version**

Platform requirement/operation: **S-5; C-1**.
Specification requirements: `find_data_elements_for_concept-3`, `get_release_alignment-1`.
Source discussion: [issue evidence](https://github.com/CBIIT/nci-si-mcp/issues/42#issuecomment-5968943275).

**Observation.** NCIt has version 26.09d and an untyped September 28, 2026 date; caDSR has untyped 2026-07-01 and no version. Their recorded interval is 89 days, not a claim about current freshness.

**Reproduction.** Read both graph identity rows from the linked query, requiring both graphs; inspect datatype and version rather than substituting today's date.

**Expected.** Use xsd:date consistently and publish a stable caDSR export/version identifier alongside the NCIt version.

**Impact.** Cross-domain provenance is dated but registry reproducibility and automated alignment are weaker than a shared version contract.

**Workaround.** Preserve each graph's identity, parse only supported date forms, compare NCIt version for descendant expansion and never manufacture a registry identifier. PV lookup reads caDSR API primary flags and pinned EVS content, with caDSR and EVS provenance and no Shared SI dependency. The [dependency register](ssis-dependencies.md) records the registered queries and their weekly structural check.

**Acceptance criteria.** Both graphs return unambiguous identities; missing/duplicate identities fail closed and dates compute the correct interval. Expansion refuses a mismatched NCIt version with release_mismatch or an absent version with release_not_available; exact find and grounding preserve the actual metadata without comparing or backfilling versions.

Evidence (linked request/response files retain their original form):

- [acceptance/fixtures/recorded/ssis-sparql/graph-identities.json](../../acceptance/fixtures/recorded/ssis-sparql/graph-identities.json) — recorded on 2026-10-03; POST ssis-sparql /sparql; SHA-256 `1766c631d26999e22a5062f00c9fd4c83ddb3ec8150cc02128af1986bb4b84c6`.
- [docs/upstream/ssis-queries.yaml](../../docs/upstream/ssis-queries.yaml) — document; SHA-256 `224683b8281e442e25b607837b978aded9d1227a869125df787f2843bcab83a4`.

| Affected test function | Fixture | Live |
|---|---|---|
| `acceptance/tests/test_cross_domain.py::test_an_answer_of_the_shared_si_service_names_both_graphs_and_both_content_states` | 1 passed | 1 not_live |
| `acceptance/tests/test_cross_domain.py::test_every_dataset_is_dated_with_the_largest_interval_and_a_warning_above_the_threshold` | 2 passed | 2 not_live |

## ssis-version-status-pairing

**Preserve version-specific status pairs**

Platform requirement/operation: **S-3; S-5**.
Specification requirements: `find_data_elements_for_concept-1`, `ground_value-2`.
Source discussion: [issue evidence](https://github.com/CBIIT/nci-si-mcp/issues/370#issuecomment-6429093919).

**Observation.** DE2672191 is one graph node with version literals 1 and 2, administration_status RELEASED and RETIRED ARCHIVED, and registration_status Standard and Superceded. Anonymous outgoing, incoming and axiom inspection on 26 October 2026 found no structure tying these literals together. The retained bounded C48179 query records the two independent status sets, not version/status pairs.

**Reproduction.** Read the retained C48179 status query and inspect outgoing, incoming and axiom assertions of caDSR#DE2672191 without joining independent literals.

**Expected.** Model each data element version as its own node, or explicitly pair version, administration status and registration status.

**Impact.** Joining the independent sets invents combinations and cannot identify one version's status pair or a current status.

**Workaround.** Return unchanged unpaired workflowStatuses and registrationStatuses on each public-ID use; direct callers needing a version's own pair to get_data_element with an explicit version.

Current owning test: [`acceptance/tests/test_graph_roles.py::test_graph_status_sets_preserve_unpaired_public_id_evidence`](../../acceptance/tests/test_graph_roles.py) (post-Phase-5).

**Acceptance criteria.** A published version-specific model permits the exact status pair to be read without inferred joins; until then preserve unpaired public-ID evidence.

Evidence (linked request/response files retain their original form):

- [acceptance/fixtures/recorded/ssis-sparql/data-elements-statuses-c48179.json](../../acceptance/fixtures/recorded/ssis-sparql/data-elements-statuses-c48179.json) — recorded on 2026-10-26; POST ssis-sparql /sparql; SHA-256 `29634826a576f476e4265e782751b903176ede8518cffb15ed77a6a58fe34575`.

| Affected test function | Fixture | Live |
|---|---|---|
| `acceptance/tests/test_cross_domain.py::test_an_answer_of_the_shared_si_service_names_both_graphs_and_both_content_states` | 1 passed | 1 not_live |

## ssis-public-id-associations

**Clarify merged CDE versions and value concept roles**

Platform requirement/operation: **S-3; S-5**.
Specification requirements: `find_data_elements_for_concept-1`, `find_data_elements_for_concept-2`, `find_data_elements_for_concept-5`, `ground_value-2`.
Source discussion: [issue evidence](https://github.com/CBIIT/nci-si-mcp/issues/367).

**Observation.** Reported live on 25 October 2026 in issue 367, not retained as a new recording: cadsr:DE2008092 merges CDE versions, associating Tis, TX and T0 with version 1 although only version 2 lists them. Value-node main/minor roles can invert the API's primary concept; display_order is a string with a leading space. The retained bounded Disease captures contain 1001 rows but only 981 exact-code or 998 descendant public IDs, not complete distinct answers. The recorded graph dates differ by 89 days, not evidence of current lag.

**Reproduction.** Inspect all version assertions and permissible values on DE2008092 against the API's individual versions; compare value meaning primaryIndicator with main/minor and the raw display_order datatype. Inspect the retained queries and both graph metadata rows separately.

**Expected.** Confirm the merge model and whether per-version publication is planned; explain the value main/minor inversion and string display_order semantics or publish numeric order. Add the representation-term concept link and publish and maintain a caDSR graph refresh cadence that addresses the graph lag.

**Impact.** A merged graph cannot establish a CDE version or assign a value to one; version duplicates consume a row cap before distinct public-ID associations are counted.

**Workaround.** Find and grounding use public-ID associations, grouping complete encoded role/concept/order tuples by publicId and distinct values by publicId/value/concept before the sentinel limit. Graph roles preserve trimmed integer display order without inferring primary; missing order stays absent and malformed order fails. API records keep versions and primary flags. Representation-term use is not covered by the graph route.

**Acceptance criteria.** Graph associations make no version claim; distinct keys precede maximum-plus-one LIMIT and omissions remain lower bounds. Original recordings and requests remain unchanged; new derived projections and synthetic cap cases are explicitly crafted.

Evidence (linked request/response files retain their original form):

- [acceptance/fixtures/recorded/ssis-sparql/data-elements-c2991.json](../../acceptance/fixtures/recorded/ssis-sparql/data-elements-c2991.json) — recorded on 2026-10-03; POST ssis-sparql /sparql; SHA-256 `d3553e88a67afb26e248a53b92b2a9ff7f6c987e512c5d90468726bcf043f156`.
- [acceptance/fixtures/recorded/ssis-sparql/data-elements-c2991-descendants.json](../../acceptance/fixtures/recorded/ssis-sparql/data-elements-c2991-descendants.json) — recorded on 2026-10-03; POST ssis-sparql /sparql; SHA-256 `fa026931a2054ac8018e45fa5eafdae976f278d1a9dd73b8ea5c7e64b3d15241`.
- [acceptance/fixtures/recorded/ssis-sparql/values-of-2200604.json](../../acceptance/fixtures/recorded/ssis-sparql/values-of-2200604.json) — recorded on 2026-10-03; POST ssis-sparql /sparql; SHA-256 `b38e92731c54266d78d7673711ee83e5ae02e94a409d26a88e562b5af3b9667d`.
- [acceptance/fixtures/recorded/ssis-sparql/graph-identities.json](../../acceptance/fixtures/recorded/ssis-sparql/graph-identities.json) — recorded on 2026-10-03; POST ssis-sparql /sparql; SHA-256 `1766c631d26999e22a5062f00c9fd4c83ddb3ec8150cc02128af1986bb4b84c6`.

| Affected test function | Fixture | Live |
|---|---|---|
| `acceptance/tests/test_cross_domain.py::test_the_data_elements_are_the_concept_s_and_with_expansion_its_descendants_too` | 2 passed | 2 not_live |
| `acceptance/tests/test_workflow.py::test_each_hop_is_what_its_surface_answers_for_the_concept` | 1 passed | 1 not_live |
