# caDSR client fixture sources

`tests/test_cadsr_client.py` crafts minimal protocol envelopes from these published JSON
contracts, inspected on 6 October 2026. Identifiers, names and empty matches in those tests
are synthetic, not registry records. They are only served by the offline test server.

The JSON documents are available from
`https://cadsrapi.cancer.gov/invoke/pub.swagger:getSwaggerJsonDoc?radName=` with these names:

| Surface | `radName` | Used shape |
|---|---|---|
| Data Elements | `NCIAPI.v1_0:NciApiRad` | DataElement, DataElements, CRDCDataElements wrappers |
| Forms | `NCIFormAPI.v2_0:NciFormApiRad` | form wrapper and version query |
| Contexts | `NCILovAPI.v1_0:LovRad` | contextNames string array |
| CDE Match | `NCIAPI.v2_0.cdeMatch.api:cdeMatch_rad` | one apiinput object, endpoint headers, matchResults object |
| VM Match | `NCIAPI.v1_0:vmMatchRad` | entities array, match headers, matchResults array |

The CDE Match and LOV contracts are also recorded in
`acceptance/fixtures/recorded/cadsr-contracts/`. API response properties are passed through;
these tests validate the envelope and wire request, not the whole later tool projection.

`tests/test_cadsr_matching.py` crafts matching records from those same definitions and tests
their projection, ordering, limits and failures. VM Match uses `matchType=Restricted` (or
Unrestricted) and `function=match`, following `spec/` and the successful request recorded in
`acceptance/fixtures/recorded/cadsr/vm-match-male.json`. The published OpenAPI descriptions
instead assign VM/Concept/BOTH to matchType and Restricted/Unrestricted to function; this
conflict is recorded in [the #42 requirements package](https://github.com/CBIIT/nci-si-mcp/issues/42#issuecomment-6011568485).
There is no alternative-header fallback or matching registry-pin field.

Keyword search is **requested OP-C03**, absent from the published contract. Its test envelope
follows the crafted `crafted/OP-C03/search-no-match.json` (made in memory by `cadsr_over_cap` in
`acceptance/src/nci_sip_acceptance/craft.py`); it is not evidence of a live
search capability. A live type-E response stays an upstream error, with no alternate route.
The published-registry fixture is likewise a future capability from
`scenarios/cadsr/with-registry-release/registry-releases.json`, crafted in memory by
`cadsr_with_registry_release` in `acceptance/src/nci_sip_acceptance/craft.py` (C-1).

The listing parser is tested against the existing recorded export-folder fixture, plus
crafted reordered columns, ambiguous links and invalid dates. It preserves the local date-time
without inventing a timezone. Credentials are generated test sentinels, never real credentials.
