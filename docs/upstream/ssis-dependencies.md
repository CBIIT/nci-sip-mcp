# Shared SI query dependencies

Generated from [ssis-queries.yaml](ssis-queries.yaml) by `pdm run python scripts/upstream_requirements.py`; never edit the table by hand.

The [team response relayed on 8 October 2026](team-responses-2026-10-08.md) states maintenance mode and no funding for new API development or maintenance. This is an attributed dated report, not an independently verified current status.

Risk-record owner: **NCI SIP MCP project coordinator**. EVS SPARQL and caDSR APIs are published candidate alternatives, not implemented or equivalent fallbacks. A missing upstream capability stays visible.

Transport: `{'method': 'POST', 'path': '/sparql', 'accept': 'application/sparql-results+json', 'form': 'query'}`. Coverage: Acceptance (live), anonymous structural check for every family. Caller error fallback: `upstream_unavailable, timeout or bound_exceeded; graph comparison may return release_not_available or release_mismatch`.

Run `pdm run python -m scripts.ssis_contracts` to attempt all registered families. Each is reported; any structural or transport failure exits nonzero. The checker validates SELECT variables, bindings, term shapes, identifiers and row bounds. Valid empty content results and content/count/release changes pass; graph identities require both known graphs. This is platform conformance, not a new MCP acceptance requirement.

| Family / live check ID | Client operation | Consumers | Requirements | Evidence |
|---|---|---|---|---|
| `graph-identities` | `get_graph_identities` | find_data_elements_for_concept, get_release_alignment, ground_value | X-1, X-2, X-3, get_release_alignment-1 | [crafted/ssis-sparql/graph-identities-bounded.json](../../acceptance/fixtures/crafted/ssis-sparql/graph-identities-bounded.json) |
| `elements-exact` | `find_data_elements` | find_data_elements_for_concept, ground_value | find_data_elements_for_concept-1, ground_value-2 | [recorded/ssis-sparql/data-elements-statuses-c17357.json](../../acceptance/fixtures/recorded/ssis-sparql/data-elements-statuses-c17357.json) |
| `elements-descendants` | `find_data_elements` | find_data_elements_for_concept | find_data_elements_for_concept-1 | [recorded/ssis-sparql/data-elements-statuses-c17357-descendants.json](../../acceptance/fixtures/recorded/ssis-sparql/data-elements-statuses-c17357-descendants.json) |
| `values-exact` | `find_permissible_values` | find_data_elements_for_concept, ground_value | find_data_elements_for_concept-2, ground_value-2 | [crafted/ssis-public-id/values-c20197.json](../../acceptance/src/nci_sip_acceptance/craft.py) |
| `values-descendants` | `find_permissible_values` | find_data_elements_for_concept | find_data_elements_for_concept-2 | [crafted/ssis-public-id/values-c17357-descendants.json](../../acceptance/src/nci_sip_acceptance/craft.py) |

The bounded identity request is crafted from recorded evidence, not a new live recording. Element queries have new live recordings with complete role tuples; value public-ID projections are crafted in memory from historical captures and are not complete current answers. Identifier and remaining-budget LIMIT instances are parameters of these families. Historical REST façade, unbounded identity and values-by-public-id forms in the [request history](../../acceptance/request-forms/ssis.md) are inactive. The checker calls only anonymous Shared SI; it does not join EVS or caDSR or substitute their results when Shared SI fails.
