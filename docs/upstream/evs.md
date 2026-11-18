# EVS upstream requirements

Generated from [catalogue.yaml](catalogue.yaml). Read the [evidence boundary](README.md) first.

## evs-status-metadata

**Explain DEFAULT and active versus current status semantics**

Platform requirement/operation: **OP-E01; OP-E05; OP-E12**.
Specification requirements: `resolve_retired_code-2`.
Source discussion: [issue evidence](https://github.com/CBIIT/nci-si-mcp/issues/380).

**Observation.** On 26 October 2026 the pinned conceptStatuses endpoint omitted DEFAULT; recorded C4817 supplies DEFAULT. Newly recorded C65159 supplies Obsolete_Concept and active true. Compact descendants supply no active/status. The fact audit's copied-name/retired-code observation is context for deferred alignment work, not a new cross-system comparison.

**Reproduction.** GET /api/v1/metadata/ncit_26.09d/conceptStatuses and the linked pinned concept/descendant requests anonymously.

**Expected.** Explain why DEFAULT is absent from the status catalogue, whether active includes obsolete concepts, and which upstream field establishes currentness.

**Impact.** A caller treating active as current or the metadata list as exhaustive can misclassify concepts.

**Workaround.** Preserve returned active/status exactly; do not inject DEFAULT into metadata or infer currentness from status or copied names.

**Acceptance criteria.** Published metadata and documentation explain every supplied concept status and active/current semantics; compact entries remain unclassified unless separately read.

Evidence (linked request/response files retain their original form):

- [acceptance/fixtures/recorded/evs/concept-statuses.json](../../acceptance/fixtures/recorded/evs/concept-statuses.json) — recorded on 2026-10-26; GET evs /api/v1/metadata/ncit_26.09d/conceptStatuses; SHA-256 `7f9ef58e448394c6216a7346165837f7782517f0d3b488c037a801cf9a047c05`.
- [acceptance/fixtures/recorded/evs/concepts/C4817.json](../../acceptance/fixtures/recorded/evs/concepts/C4817.json) — recorded on 2026-10-02; GET evs /api/v1/concept/ncit_26.09d/C4817; SHA-256 `27dd97c30a3be2c984f609c08c986169a6e1fbf6e5adc765a0c079b9ffe2cebb`.
- [acceptance/fixtures/recorded/evs/concepts/C65159.json](../../acceptance/fixtures/recorded/evs/concepts/C65159.json) — recorded on 2026-10-26; GET evs /api/v1/concept/ncit_26.09d/C65159; SHA-256 `3940060461ac01a346ec135c91f8ec9771df1dea6c56d1f97d1284779a036785`.
- [acceptance/fixtures/recorded/evs/descendants-c4817-level-2.json](../../acceptance/fixtures/recorded/evs/descendants-c4817-level-2.json) — recorded on 2026-10-03; GET evs /api/v1/concept/ncit_26.09d/C4817/descendants; SHA-256 `b2e014a84312e4ca4d5bc55ca661b8e060303a0abcca2ab4280c8dcf052d5889`.

| Affected test function | Fixture | Live |
|---|---|---|
| `acceptance/tests/test_evs.py::test_an_active_code_is_active_with_its_status_and_no_replacement` | 1 passed | 1 not_live |

## evs-polarity

**Publish stable relationship polarity by code**

Platform requirement/operation: **E-4; OP-E02**.
Specification requirements: `get_concept_neighborhood-2`, `list_relationships-2`.
Source discussion: [issue evidence](https://github.com/CBIIT/nci-si-mcp/issues/42).

**Observation.** The catalogue exposes relationship codes and names; the exclusion scenario deliberately gives misleading names. That crafted scenario is a regression probe, not an observed production mislabelling.

**Reproduction.** Read the pinned role catalogue (OP-E02); run the traversal/exclusions scenario, crafted in memory by traversal_exclusions in acceptance/src/nci_sip_acceptance/craft.py, to distinguish classification by code from name matching.

**Expected.** Publish a versioned machine-readable exclusion/polarity classification keyed by stable relationship code.

**Impact.** A name-based inference can include excluded concepts in a cohort.

**Workaround.** Use the configured exclusion code set; fail closed if the release catalogue lacks a required code.

**Acceptance criteria.** Renaming a relationship does not change polarity; positive and negative fixtures produce the same code-based classification as the published catalogue.

Evidence (linked request/response files retain their original form):

- [acceptance/fixtures/recorded/evs/roles.json](../../acceptance/fixtures/recorded/evs/roles.json) — recorded on 2026-10-02; GET evs /api/v1/metadata/ncit_26.09d/roles; SHA-256 `45df4a71fc5d3fcf0c4d818faac07223b1054de7ca568d8044b974d70bf4c593`.

| Affected test function | Fixture | Live |
|---|---|---|
| `acceptance/tests/test_evs.py::test_polarity_follows_the_relationship_code_not_its_name` | 1 passed | 1 not_live |
| `acceptance/tests/test_evs.py::test_a_relationship_s_polarity_follows_its_code_not_its_name` | 1 passed | 1 not_live |

## evs-semantic

**Platform semantic retrieval and local-index retirement**

Platform requirement/operation: **E-9**.
Specification requirements: `search_concepts-3`, `search_concepts-5`, `search_concepts-8`.
Source discussion: [issue evidence](https://github.com/CBIIT/nci-si-mcp/issues/42).

**Observation.** The furnished implementation performs embedding semantic/hybrid retrieval locally. The EVS team's response relayed on 8 October 2026 confirms that /search type controls lexical matching, while structured and ontology/relationship-aware retrieval, including SPARQL, are also available. Graph semantics do not establish an equivalent embedding-ranked operation; see team-responses-2026-10-08.md.

**Reproduction.** Compare lexical OP-E08 with semantic/hybrid search using an activated full index and then without an index; use the recorded full-corpus evaluation, not fixture timing, for quality floors.

**Expected.** Provide release-addressable semantic/hybrid results, scores, paging, provenance and declared ranking behavior.

**Impact.** Operators currently distribute an embedding model and full index and maintain release compatibility.

**Workaround.** Keep the local index; refuse missing or mismatched builds rather than substituting lexical results.

**Acceptance criteria.** Before retiring the local index, the replacement passes the tool contracts and full-corpus quality and latency evaluation on the same queries and release; sample-only scores cannot justify retirement.

Evidence (linked request/response files retain their original form):

- [docs/retrieval-evaluation.md](../../docs/retrieval-evaluation.md) — document; SHA-256 `32f98e22180a728a88ba40a6796be7c87cb18a27a92964ee149c2606d13d74c9`.
- [acceptance/request-forms/evs.md](../../acceptance/request-forms/evs.md) — document; SHA-256 `69ff7219080985879da44bf1f9f8f595d267772cd6414b8b6f095b4a21c0b895`.

| Affected test function | Fixture | Live |
|---|---|---|
| `acceptance/tests/test_evs.py::test_index_search_returns_scored_indexed_concepts_and_the_named_one_first_page` | 2 passed | 2 not_live |
| `acceptance/tests/test_evs.py::test_an_index_search_for_another_release_fails_closed` | 2 passed | 2 not_live |
| `acceptance/tests/test_evs.py::test_an_index_mode_without_an_index_is_unavailable` | 2 passed | 2 not_live |

## evs-retired

**Consistent active and retired search filters**

Platform requirement/operation: **OP-E08; A8.3; A9.3**.
Specification requirements: `search_concepts-6`, `search_concepts-7`.
Source discussion: [issue evidence](https://github.com/CBIIT/nci-si-mcp/issues/42).

**Observation.** The issue records DEFAULT and true rejected by conceptStatus (400), and Retired_Concept accepted but ignored for GO. The retained recordings cover NCIt retired-only and the unfiltered GO baseline, not those failed probes.

**Reproduction.** Compare GO obsolete search with conceptStatus omitted, Retired_Concept, and the listing's retiredStatusValue=true; try DEFAULT for NCIt. These are the issue's 2 October observations, not newly executed probes.

**Expected.** An active filter or DEFAULT selection, plus retired-only selection using each terminology's documented status value.

**Impact.** An exclude-retired request cannot be served, and retired-only is unavailable for affected non-NCIt terminologies.

**Workaround.** Offer include/only, never exclude; refuse retired-only where the status cannot be selected.

**Acceptance criteria.** Active-only omits every retired result; retired-only returns no active result across NCIt and GO, with correct totals and continuation pages.

Evidence (linked request/response files retain their original form):

- [acceptance/fixtures/recorded/evs/search-retired-only.json](../../acceptance/fixtures/recorded/evs/search-retired-only.json) — recorded on 2026-10-02; GET evs /api/v1/concept/ncit_26.09d/search; SHA-256 `1e569d7ee583f4380676ded15a80ace6e2b11e75a2a001131719ed0e10eecbef`.
- [acceptance/fixtures/recorded/evs/search-go-obsolete.json](../../acceptance/fixtures/recorded/evs/search-go-obsolete.json) — recorded on 2026-10-02; GET evs /api/v1/concept/go_2026-07-26/search; SHA-256 `1268b848cb84608b235a4082186ca4ec723c5b0f944da9b4a5213ac950b105e8`.
- [acceptance/fixtures/recorded/evs/terminologies.json](../../acceptance/fixtures/recorded/evs/terminologies.json) — recorded on 2026-10-02; GET evs /api/v1/metadata/terminologies; SHA-256 `ba9c019d97d677bdf68dee2f119f62047d6448f715ec4b3157fb9c59e1b7fe4e`.

| Affected test function | Fixture | Live |
|---|---|---|
| `acceptance/tests/test_evs.py::test_retired_only_returns_the_retired_concepts_alone` | 1 passed | 1 not_live |
| `acceptance/tests/test_evs.py::test_retired_only_where_the_status_is_none_the_search_selects_is_invalid` | 1 passed | 1 not_live |

## evs-licence

**Carry upstream licence text with concepts and maps**

Platform requirement/operation: **OP-E05; OP-E20; A7.3; X-19**.
Specification requirements: `X-19`.
Source discussion: [issue evidence](https://github.com/CBIIT/nci-si-mcp/issues/42).

**Observation.** The listing supplies metadata.licenseText; content does not consistently carry it. license/attributed is a crafted future answer, not proof of a served capability.

**Reproduction.** Compare the licensed terminology listing with a concept/map response; exercise license/attributed for the requested passthrough shape.

**Expected.** Include upstream licence and attribution text per concept/map, or once per response with an unambiguous association.

**Impact.** Clients reading content alone cannot receive the terms supplied only by a separate listing.

**Workaround.** Pass through text actually retrieved; the MCP server maintains no licence text of its own and does not invent it.

**Acceptance criteria.** Every affected content response delivers the platform's exact text through the tool unchanged, without exposing a licence credential.

Evidence (linked request/response files retain their original form):

- [acceptance/fixtures/recorded/evs/terminologies.json](../../acceptance/fixtures/recorded/evs/terminologies.json) — recorded on 2026-10-02; GET evs /api/v1/metadata/terminologies; SHA-256 `ba9c019d97d677bdf68dee2f119f62047d6448f715ec4b3157fb9c59e1b7fe4e`.
- [acceptance/request-forms/evs.md](../../acceptance/request-forms/evs.md) — document; SHA-256 `69ff7219080985879da44bf1f9f8f595d267772cd6414b8b6f095b4a21c0b895`.

| Affected test function | Fixture | Live |
|---|---|---|
| `acceptance/tests/test_crosscutting.py::test_licence_text_the_platform_gives_with_an_item_is_passed_through_unchanged` | 4 passed | 4 not_live |

## evs-pinned-forms

**Address subset, mapset and FHIR content states explicitly**

Platform requirement/operation: **OP-E06; OP-E23; OP-E24; OP-E25; OP-F05**.
Specification requirements: `X-1`, `X-2`, `X-3`, `expand_value_set-1`.
Source discussion: [issue evidence](https://github.com/CBIIT/nci-si-mcp/issues/42).

**Observation.** The request register records the missing pinned subset path and rejected FHIR system-version form; mapsets have heterogeneous versions, not uniformly NCIt releases.

**Reproduction.** Use the register's served and prescribed forms side by side, including an unavailable release; preserve mapset-specific version semantics.

**Expected.** Document and serve explicit content-state selection and report the selected state in each payload.

**Impact.** Unpinned operations cannot retrieve an older requested state after the platform moves.

**Workaround.** The approved unpinned fallback verifies the payload's reported release and fails on mismatch; a mapset's own version is never relabelled as a verified NCIt release.

**Acceptance criteria.** Known pins return that exact state, unknown pins fail closed, and mixed or missing identities cannot masquerade as the requested release.

Evidence (linked request/response files retain their original form):

- [acceptance/request-forms/evs.md](../../acceptance/request-forms/evs.md) — document; SHA-256 `69ff7219080985879da44bf1f9f8f595d267772cd6414b8b6f095b4a21c0b895`.
- [acceptance/fixtures/crafted/OP-E06/subset-gdc.json](../../acceptance/fixtures/crafted/OP-E06/subset-gdc.json) — crafted; GET evs /api/v1/subset/ncit_26.09d/C177537; SHA-256 `4a2ad1a01962939f03d09147f91a0ca446717df1e72bbf08eb35b1afb06620ae`.
- [acceptance/fixtures/crafted/OP-F05/expand-c85492.json](../../acceptance/fixtures/crafted/OP-F05/expand-c85492.json) — crafted; GET evs-fhir /ValueSet/$expand; SHA-256 `007f987080252d9294440a22f54c3cc4a21dc3879bed70f412da4f337ccb533c`.

| Affected test function | Fixture | Live |
|---|---|---|
| `acceptance/tests/test_evs.py::test_count_and_offset_select_the_members_and_total_counts_them_all` | 3 passed | 3 not_live |
| `acceptance/tests/test_cross_domain.py::test_a_gdc_value_resolves_through_the_mapset_its_source_names` | 1 passed | 1 not_live |

## evs-inactive-expansion

**Define inactive membership and expansion paging**

Platform requirement/operation: **OP-F05; A8.3; A9.5**.
Specification requirements: `expand_value_set-1`, `expand_value_set-2`.
Source discussion: [issue evidence](https://github.com/CBIIT/nci-si-mcp/issues/42).

**Observation.** The recorded expansion ignores count, offset and activeOnly. Whether production ever emits inactive members marked contains.inactive remains unknown; valueset/inactive-members crafts that case.

**Reproduction.** Compare expansion with and without count/offset/activeOnly; request an upstream example known to include an inactive member.

**Expected.** State whether inactive members are returned, mark them explicitly, and honor or explicitly document pagination/filter behavior.

**Impact.** Live evidence cannot currently demonstrate inactive filtering; full expansions require local slicing.

**Workaround.** Filter only members marked inactive and slice the retrieved expansion locally; never issue one lookup per member.

**Acceptance criteria.** A provided inactive-member example is excluded by activeOnly and included otherwise, with consistent totals/pages and bounded request counts.

Evidence (linked request/response files retain their original form):

- [acceptance/fixtures/recorded/evs-fhir/expand-c85492.json](../../acceptance/fixtures/recorded/evs-fhir/expand-c85492.json) — recorded on 2026-10-02; GET evs-fhir /ValueSet/$expand; SHA-256 `836a85af27adff039bc0ba948313dc3e62f31ff9aca3d03ec2091f0331bd840e`.
- [acceptance/request-forms/evs.md](../../acceptance/request-forms/evs.md) — document; SHA-256 `69ff7219080985879da44bf1f9f8f595d267772cd6414b8b6f095b4a21c0b895`.

| Affected test function | Fixture | Live |
|---|---|---|
| `acceptance/tests/test_evs.py::test_active_only_leaves_out_the_members_marked_inactive` | 3 passed | 3 not_live |
| `acceptance/tests/test_evs.py::test_count_and_offset_select_the_members_and_total_counts_them_all` | 3 passed | 3 not_live |
