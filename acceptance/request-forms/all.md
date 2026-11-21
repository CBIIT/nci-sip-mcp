# Upstream request forms, for every team

Generated from `acceptance/fixtures/manifest.yaml` by `pdm run acceptance-register`; do not edit by hand.

The upstream requests the acceptance suite's fixtures answer, each with the platform operation it
serves (its `OP-` id, or the requirement where the operation is missing) and why it has this
form. They are initial versions, from the published API documentation and live checks
where the operation exists and a draft where it does not, for the EVS, caDSR and Shared SI teams
to refine as needed, each change with the approval of the branch chief or a delegate.

Two kinds of request are answered whatever their form: EVS concept requests, by rules over one
recording per concept (below), and requests whose parameters the service is shown to ignore.

## Operations without a pinned form upstream

These are served unpinned today. The verified fallback is approved for this server: the tool
calls the unpinned form, compares the release the payload reports with the one requested, and
fails closed on a difference. NCI approves each fallback from this list. A
mapset that reports a version of its own, not an NCIt release, cannot be verified that way: it is a
content state of its own, named in provenance and not presented as release-verified.

| Operation | Prescribed form (crafted) | Served form (recorded) | Release reported |
|---|---|---|---|
| OP-E06 | `GET evs /api/v1/subset/ncit_26.09d/C177537` | `GET evs /api/v1/subset/ncit/C177537` | 26.09d |
| OP-E23 | — | `GET evs /api/v1/mapset?include=minimal` | 2016_07_31, 2017-12-21, 2026_03_01, 26.09d, February2020, July2023, June2026, November2011, September2026 |
| OP-E24 | — | `GET evs /api/v1/mapset/NCIt_Maps_To_GDC` | 26.09d |
| OP-E25 | — | `GET evs /api/v1/mapset/NCIt_Maps_To_GDC/maps?term=Ewing sarcoma&fromRecord=0&pageSize=10` | 26.09d |
| OP-E25 | — | `GET evs /api/v1/mapset/NCIt_Maps_To_GDC/maps?term=C4817&fromRecord=0&pageSize=10` | 26.09d |
| OP-E25 | — | `GET evs /api/v1/mapset/NCIt_Maps_To_GDC/maps?term=C116977&fromRecord=0&pageSize=10` | none |
| OP-F05 | `GET evs-fhir /ValueSet/$expand?url=http://ncicb.nci.nih.gov/xml/owl/EVS/Thesaurus.owl?fhir_vs=C85492&system-version=http://ncicb.nci.nih.gov/xml/owl/EVS/Thesaurus.owl\|26.09d` | `GET evs-fhir /ValueSet/$expand?url=http://ncicb.nci.nih.gov/xml/owl/EVS/Thesaurus.owl?fhir_vs=C85492` | 26.09d |

## caDSR: no registry release, and JSON only when asked for

caDSR publishes no registry release (C-1, A3.8.1): every caDSR form below is served without one,
the path the inventory names for it (OP-C08) answers 404, and the export's date is the registry's
only content state (A3.8.2). The forms the inventory names with `registryRelease` are crafted in
`cadsr/with-registry-release`. Every caDSR request names `Accept: application/json`, which the
contracts prescribe (M3.2): without it the API answers HTTP 200 with HTML, recorded for two paths,
and the fixture naming the most headers a request carries answers it. A refusal of the arguments
and an unknown data element both come back as HTTP 200, with `apiResponse` saying so (X-15).

harmonize_data_dictionary's form is prescribed: each column goes to CDE Match as one entity, its
name and its description as `userTip`, and its sample values go to vmMatch only. CDE Match is
matched on its whole body, so a form left to each server would leave its answers unanswerable.

## Shared SI: graph identities, and the query text

The Shared SI Service names no release (S-1, S-2): the NCIt and caDSR graphs each carry an
untyped `dc:date`, in two formats, and only NCIt an `owl:versionInfo`; the identity query below
reads them (A3.7.1). For the SPARQL endpoint the suite prescribes the query text: each query
below is matched with runs of whitespace collapsed, sent as a form-encoded POST (a direct POST
of the query is refused) asking for `application/sparql-results+json`. Bounded identities ask for
three rows; content asks for at most the shared remaining maximum + 1, to detect a cut. The
bounded variants reuse recorded answers as explicitly crafted fixtures. Public-ID projections
are crafted in memory: element queries group publicId and value queries select distinct
publicId/value/concept before LIMIT. Historical bounded captures are not complete current
distinct answers; cap scenarios use 1001 synthetic distinct IDs. Original queries stay below
as recorded evidence. Teams may propose changes
to any form. The façade answers HTML unless `Accept: application/json` is sent, and a
missing argument with HTTP 200 (X-15).

## Requests

| Operation | Request | Expected | Made | Rationale | Fixture |
|---|---|---|---|---|---|
| baseline | `GET evs /api/v1/version` | 200 | recorded | The furnished server's CLI release-info reads it; no required tool needs it. | `recorded/evs/version.json` |
| OP-E01 | `GET evs /api/v1/metadata/terminologies` | 200 | recorded | The listing behind list_terminologies, and what the furnished server filters today. | `recorded/evs/terminologies.json` |
| OP-E01 | `GET evs /api/v1/metadata/terminologies?terminology=ncit&latest=true&tag=monthly` | 200 | recorded | The one-row release query: latest is channel-scoped, so the channel is given as a tag (E-1). | `recorded/evs/release-monthly.json` |
| OP-E01 | `GET evs /api/v1/metadata/terminologies?terminology=ncit&latest=true&tag=weekly` | 200 | recorded | The same query for the weekly channel. | `recorded/evs/release-weekly.json` |
| OP-E02 | `GET evs /api/v1/metadata/ncit_26.09d/roles` | 200 | recorded | The role catalogue of the pinned release, for polarity by code. | `recorded/evs/roles.json` |
| OP-E03 | `GET evs /api/v1/metadata/ncit_26.09d/associations` | 200 | recorded | The association catalogue of the pinned release. | `recorded/evs/associations.json` |
| OP-E08 | `GET evs /api/v1/concept/ncit_26.09d/search?term=ewing sarcoma&type=contains&include=minimal,highlights&fromRecord=0&pageSize=10` | 200 | recorded | Lexical search: type=contains, minimal concepts with highlights for matchedOn, the page given in full. | `recorded/evs/search-contains.json` |
| OP-E08 | `GET evs /api/v1/concept/ncit_26.09d/search?term=ewing sarcoma&type=contains&include=minimal,highlights&fromRecord=10&pageSize=10` | 200 | recorded | The second page of the same search. | `recorded/evs/search-contains-page-2.json` |
| OP-E08 | `GET evs /api/v1/concept/ncit_26.09d/search?term=ewing&type=startsWith&include=minimal,highlights&fromRecord=0&pageSize=10` | 200 | recorded | Typeahead: type=startsWith. | `recorded/evs/search-starts-with.json` |
| OP-E08 | `GET evs /api/v1/concept/ncit_26.09d/search?term=Transgender Identity&type=contains&include=minimal,highlights&fromRecord=0&pageSize=10` | 200 | recorded | Retired concepts in a lexical search: the default returns them with the others, C154421 first. | `recorded/evs/search-retired-default.json` |
| OP-E08 | `GET evs /api/v1/concept/ncit_26.09d/search?term=Transgender Identity&type=contains&include=minimal,highlights&fromRecord=0&pageSize=10&conceptStatus=Retired_Concept` | 200 | recorded | The same search for retired concepts alone: conceptStatus with the status the listing names as retired. | `recorded/evs/search-retired-only.json` |
| OP-E08 | `GET evs /api/v1/concept/ncit_26.09d/search?term=Transgender Identity&type=contains&include=minimal,highlights&fromRecord=10&pageSize=10&conceptStatus=Retired_Concept` | 200 | recorded | The second page of the retired-only search, which the cursor continues with the same selection. | `recorded/evs/search-retired-only-page-2.json` |
| OP-E08 | `GET evs /api/v1/concept/ncit_26.09d/search?term=transgender&type=startsWith&include=minimal,highlights&fromRecord=0&pageSize=10&conceptStatus=Retired_Concept` | 200 | recorded | Typeahead for retired concepts alone: startsWith honours conceptStatus as contains does. | `recorded/evs/search-retired-typeahead.json` |
| OP-E08 | `GET evs /api/v1/concept/go_2026-07-26/search?term=obsolete&type=contains&include=minimal,highlights&fromRecord=0&pageSize=10` | 200 | recorded | A lexical search of GO, whose listing names a retired status ("true") that is no concept status: searched by default, it is answered; retired only is refused (search_concepts-7). | `recorded/evs/search-go-obsolete.json` |
| OP-E08 | `GET evs /api/v1/concept/ncit_26.09d/search?term=qqxyzzyqq&type=contains&include=minimal,highlights&fromRecord=0&pageSize=10` | 200 | recorded | A search matching nothing: empty is not an error. | `recorded/evs/search-no-match.json` |
| OP-E13 | `GET evs /api/v1/concept/ncit_26.09d/C4817/pathsToRoot?include=minimal` | 200 | recorded | The paths from the concept to the root of the hierarchy. | `recorded/evs/paths-to-root.json` |
| OP-E12 | `GET evs /api/v1/concept/ncit_26.09d/C4817/descendants?maxLevel=2` | 200 | recorded | The descendants of C4817 to two levels (27), each with its level: the route a cohort or hierarchy to depth 2 may take in place of a children walk (expand_cohort's default maxDepth). | `recorded/evs/descendants-c4817-level-2.json` |
| OP-E21 | `GET evs /api/v1/history/ncit_26.09d/C4817/replacements` | 200 | recorded | An active code's replacements, one code. | `recorded/evs/replacement-active.json` |
| OP-E21 | `GET evs /api/v1/history/ncit_26.09d/C15368/replacements` | 200 | recorded | Source-specific merge replacement with its own action and date. | `recorded/evs/replacement-merge-c15368.json` |
| OP-E01 | `GET evs /api/v1/metadata/ncit_26.09d/conceptStatuses` | 200 | recorded | Status metadata omits DEFAULT, although concept payloads supply it. | `recorded/evs/concept-statuses.json` |
| OP-E21 | `GET evs /api/v1/history/ncit_26.09d/C13111/replacements` | 200 | recorded | A retired code the platform names no replacement for: action retire, no replacementCode. | `recorded/evs/replacement-retired.json` |
| OP-E21 | `GET evs /api/v1/history/ncit_26.09d/replacements?list=C4817` | 200 | recorded | The ?list= batch form, which fails the whole batch on one unknown code. | `recorded/evs/replacements-active.json` |
| OP-E06 | `GET evs /api/v1/subset/ncit/C177537` | 200 | recorded | The subset by its unpinned path: the pinned path answers 404 today; the payload names its release. | `recorded/evs/subset-gdc-unpinned.json` |
| OP-E06 | `GET evs /api/v1/subset/ncit_26.09d/C177537` | 200 | crafted for OP-E06: every content path accepts {terminology}_{release}. EVS answered this pinned path with 404 "Subset not found" on 2 October 2026 | The pinned subset path a requirement prescribes, carrying the unpinned answer. | `crafted/OP-E06/subset-gdc.json` |
| OP-E19 | `GET evs /api/v1/concept/ncit_26.09d/subsetMembers/C177537?fromRecord=0&pageSize=10&include=minimal` | 200 | recorded | The first page of a subset's members, keyed by the subset's code. | `recorded/evs/subset-gdc-members.json` |
| OP-E23 | `GET evs /api/v1/mapset?include=minimal` | 200 | recorded | Mapsets as first-class objects, each with its own version. | `recorded/evs/mapsets.json` |
| OP-E24 | `GET evs /api/v1/mapset/NCIt_Maps_To_GDC` | 200 | recorded | The GDC mapset, versioned by the NCIt release. | `recorded/evs/mapset-gdc.json` |
| OP-E25 | `GET evs /api/v1/mapset/NCIt_Maps_To_GDC/maps?term=Ewing sarcoma&fromRecord=0&pageSize=10` | 200 | recorded | GDC maps found by a GDC value (resolve_stored_value); term matches as a prefix. | `recorded/evs/mapset-gdc-maps-value.json` |
| OP-E25 | `GET evs /api/v1/mapset/NCIt_Maps_To_GDC/maps?term=C4817&fromRecord=0&pageSize=10` | 200 | recorded | GDC maps found by an NCIt code. | `recorded/evs/mapset-gdc-maps-code.json` |
| OP-E25 | `GET evs /api/v1/mapset/NCIt_Maps_To_GDC/maps?term=C116977&fromRecord=0&pageSize=10` | 200 | recorded | The GDC maps of a concept GDC does not map (C116977), none: an empty answer (resolve_stored_value, X-4). | `recorded/evs/mapset-gdc-maps-none.json` |
| OP-F05 | `GET evs-fhir /ValueSet/$expand?url=http://ncicb.nci.nih.gov/xml/owl/EVS/Thesaurus.owl?fhir_vs=C85492` | 200; ignores activeOnly, count, offset | recorded | The unpinned expansion: system-version answers 400 today; count, offset and activeOnly are ignored. | `recorded/evs-fhir/expand-c85492.json` |
| OP-F05 | `GET evs-fhir /ValueSet/$expand?url=http://ncicb.nci.nih.gov/xml/owl/EVS/Thesaurus.owl?fhir_vs=C85492&system-version=http://ncicb.nci.nih.gov/xml/owl/EVS/Thesaurus.owl\|26.09d` | 200; ignores activeOnly, count, offset | crafted for OP-F05: $expand pinned by system-version. EVS answered "Input parameter 'system-version' is not supported" (400) on 2 October 2026 | $expand pinned by system-version, as the operation inventory prescribes (OP-F05), carrying the unpinned answer. | `crafted/OP-F05/expand-c85492.json` |
| OP-C01 | `GET cadsr /NCIAPI/1.0/api/DataElement/2200604` with `Accept: application/json` | 200 | recorded | A data element, its latest version; its permissible values are in ValueDomain.PermissibleValues, the only form of OP-C07 today (its own path answers 404). The inventory's form is /DataElement?publicId=&version=&registryRelease=. | `recorded/cadsr/data-element-2200604.json` |
| OP-C01 | `GET cadsr /NCIAPI/1.0/api/DataElement/2200604?version=1` with `Accept: application/json` | 200 | recorded | The same data element at version 1, which carries other names. | `recorded/cadsr/data-element-2200604-version-1.json` |
| OP-C01 | `GET cadsr /NCIAPI/1.0/api/DataElement/2200604?version=4` with `Accept: application/json` | 200 | recorded | Explicit version 4 for independent API/MCP component citation comparison. | `recorded/cadsr/data-element-2200604-version-4.json` |
| OP-C01 | `GET cadsr /NCIAPI/1.0/api/DataElement/2413218` with `Accept: application/json` | 200 | recorded | Latest version 2 is RETIRED ARCHIVED and Superceded. | `recorded/cadsr/data-element-2413218.json` |
| OP-C01 | `GET cadsr /NCIAPI/1.0/api/DataElement/3113185` with `Accept: application/json` | 200 | recorded | Latest version 3 is DRAFT MOD. | `recorded/cadsr/data-element-3113185.json` |
| OP-C01 | `GET cadsr /NCIAPI/1.0/api/DataElement/2180822` with `Accept: application/json` | 200 | recorded | Normal enumerated domain with month/week/day permissible values. | `recorded/cadsr/data-element-2180822.json` |
| OP-C01 | `GET cadsr /NCIAPI/1.0/api/DataElement/14883075` with `Accept: application/json` | 200 | recorded | Enumerated by Reference with referenceTerminology and an Uberon URI value. | `recorded/cadsr/data-element-14883075.json` |
| OP-C01 | `GET cadsr /NCIAPI/1.0/api/DataElement/2179689` with `Accept: application/json` | 200 | recorded | Confirmed Non-enumerated domain without a permissible-value list. | `recorded/cadsr/data-element-2179689.json` |
| OP-C01 | `GET cadsr /NCIAPI/1.0/api/DataElement/99999999` with `Accept: application/json` | 200 | recorded | An unknown public id: HTTP 200 with DataElement null and apiResponse type I, "No data returned for the input criteria" (X-15, not_found). | `recorded/cadsr/data-element-unknown.json` |
| OP-C01 | `GET cadsr /NCIAPI/1.0/api/DataElement/notanumber` with `Accept: application/json` | 200 | recorded | A public id that is no number: HTTP 200 with DataElement null and apiResponse type E, "Parameter 'publicId' must be a number." (X-15, invalid_request). | `recorded/cadsr/data-element-refused.json` |
| OP-C07 | `GET cadsr /NCIAPI/1.0/api/DataElement/2200604/permissibleValues` with `Accept: application/json` | 404 | recorded | The inventory's path for a data element's permissible values answers 404; they come inside the data element. | `recorded/cadsr/permissible-values.json` |
| OP-C01 | `GET cadsr /NCIAPI/1.0/api/DataElements/ReferenceDocument?documentText=Sex of a Person&documentType=Preferred Question Text&headerOnly=true` with `Accept: application/json` | 200 | recorded | A data element by its preferred question text, header fields only; the platform matches the text as contained, and this one names 2200604 alone. | `recorded/cadsr/question-text-sex-of-a-person.json` |
| OP-C01 | `GET cadsr /NCIAPI/1.0/api/DataElements/ReferenceDocument?documentText=Date of birth&documentType=Preferred Question Text&headerOnly=true` with `Accept: application/json` | 200 | recorded | A question text 18 data elements have, as the platform matches it. | `recorded/cadsr/question-text-date-of-birth.json` |
| OP-C01 | `GET cadsr /NCIAPI/1.0/api/DataElements/ReferenceDocument?documentText=qqxyzzyqq&documentType=Preferred Question Text&headerOnly=true` with `Accept: application/json` | 200 | recorded | A question text no data element has, which the platform answers with an empty list. | `recorded/cadsr/question-text-unmatched.json` |
| OP-C01 | `GET cadsr /NCIAPI/1.0/api/DataElement/2200604` with no header | 200 | recorded | The same request without Accept: HTTP 200 with HTML. The fixture naming Accept answers a request that carries it; this one any other (M3.2, X-15). | `recorded/cadsr/data-element-2200604-html.json` |
| OP-C06 | `GET cadsr /NCIAPI/1.0/api/DataElements/getCRDCList` with `Accept: application/json` | 200 | recorded | The CRDC crosswalk, unparameterised. | `recorded/cadsr/crdc-list.json` |
| OP-C09 | `GET cadsr /NCIAPI/1.0/api/DataElements/Concept?conceptCode=C17357&headerOnly=true` with `Accept: application/json` | 200 | recorded | Data elements by concept code, header fields only: the contract caps every query at 1,000 results, "To bring back all results, utilize the headerOnly field". | `recorded/cadsr/concept-c17357.json` |
| OP-C09 | `GET cadsr /NCIAPI/1.0/api/DataElements/Concept?conceptCode=C17357&headerOnly=true` with no header | 200 | recorded | The same request without Accept, answered with HTML (M3.2, X-15). | `recorded/cadsr/concept-c17357-html.json` |
| C-11 | `GET cadsr /NCIAPI/1.0/api/DataElements/Classification?classificationSchemePublicId=3685569&classificationSchemeVersion=1&headerOnly=true` with `Accept: application/json` | 200 | recorded | The data elements of one classification scheme, header fields only. OP-C13, the list of schemes, has no REST form: schemes come only nested in a data element and as this filter (C-11). The scheme is named by classificationSchemePublicId (publicId answers an empty list). 3685569 (Newborn Examination, NICHD) holds 40, 2200604 among them; 2008601, another of 2200604's schemes, answered 4 MB in 149 s. | `recorded/cadsr/classification-3685569.json` |
| OP-C13 | `GET cadsr /NCILovAPI/1.0/api/getContextNames` with `Accept: application/json` | 401 | recorded | The context list on the lists-of-values API refuses an anonymous caller (401); cadsr/credentialed holds the answer to the contract. | `recorded/cadsr/context-names-refused.json` |
| OP-C12 | `GET cadsr /NCIFormAPI.v2_0:NciFormApiRad/Form/5406471` with `Accept: application/json` | 200 | recorded | A form by public id on the Form 2.0 API, the contract's own example; the Form 1.0 API refuses the same form anonymously (401). The form is RETIRED ARCHIVED (context ONC, retired 8 April 2025), so it serves get_form's status surfaced (A8.1); it is the only form, since no released one could be found anonymously: Form/query takes a public or protocol id only, and the caDSR portal (OneData) needs a login. | `recorded/cadsr/form-5406471.json` |
| OP-C12 | `GET cadsr /NCIFormAPI.v2_0:NciFormApiRad/Form/99999999` with `Accept: application/json` | 200 | recorded | An unknown form id: HTTP 200 with form null and apiResponse type E, "No data found for input criteria." (X-15, not_found). | `recorded/cadsr/form-unknown.json` |
| OP-C10 | `GET cadsr /NCIAPI/1.0/api/permissibleValues/9192925` with `Accept: application/json` | 404 | recorded | A permissible value by the id caDSR REST publishes for it (9192925, "Unknown" of 2200604): the inventory's path answers 404. | `recorded/cadsr/permissible-value-9192925.json` |
| OP-M02 | `POST cadsr /vmMatch/v1/vmMatch` with `Accept: application/json`, `Content-Type: application/json`, `matchType: Restricted`, `function: match`, body `[{"name": "Male"}]` | 200 | recorded | Value meanings matched to one value, restricted; answered anonymously. | `recorded/cadsr/vm-match-male.json` |
| OP-M01 | `POST cadsr /NCIAPI.v2_0.cdeMatch.api:cdeMatch_rad/cdeMatch` with `Accept: application/json`, `Content-Type: application/json`, body `[{"entity": "Patient Gender"}]` | 401 | recorded | CDE Match refuses an anonymous caller (401) since 3 October 2026 at the latest; it answered one on 10 September. cadsr/credentialed holds the answer to the contract. | `recorded/cadsr/cde-match-refused.json` |
| OP-M01 | `POST cadsr /NCIAPI.v2_0.cdeMatch.api:cdeMatch_rad/cdeMatch` with `Accept: application/json`, `Content-Type: application/json`, body `{"entity": "Patient Gender"}` | 401 | recorded | CDE Match asked with the contract's body, one apiinput object, refused alike (401). Which form the service takes, the contract's object or the array it answered on 10 September, is asked in #42; the fixtures answer both. | `recorded/cadsr/cde-match-refused-object.json` |
| OP-C08 | `GET cadsr /NCIAPI/1.0/api/registry/releases` with `Accept: application/json` | 404 | recorded | No registry release is published: the path the inventory names (/registry/releases, placed here under the data element API) answers 404 (C-1). cadsr/with-registry-release crafts one. | `recorded/cadsr/registry-releases.json` |
| OP-M01 | `GET cadsr-contracts /pub.swagger:getSwaggerJsonDoc?radName=NCIAPI.v2_0.cdeMatch.api:cdeMatch_rad` | 200 | recorded | The CDE Match 2.0 contract (swagger 2.0, path /cdeMatch, 7 definitions), read anonymously; its body is one apiinput object. | `recorded/cadsr-contracts/cde-match.json` |
| OP-C13 | `GET cadsr-contracts /pub.swagger:getSwaggerJsonDoc?radName=NCILovAPI.v1_0:LovRad` | 200 | recorded | The lists-of-values 1.0 contract (swagger 2.0, getContextNames among 5 paths, 9 definitions), read anonymously. | `recorded/cadsr-contracts/lists-of-values.json` |
| A3.8.2 | `GET cadsr-ftp /CDE/XML/` with no header | 200 | recorded | The folder of the XML export, which dates releasedCDEsXML-OD.zip 2026-07-01 22:19 in the server's time (Last-Modified: Thu, 02 Jul 2026 02:19:40 GMT); its README says it is updated daily. The inventory's timestamped xml_cde_YYYYMMDDHHMM.zip names are gone. | `recorded/cadsr-ftp/cde-xml-listing.json` |
| A3.8.2 | `GET cadsr-ftp /CDE/XML/README` with no header | 200 | recorded | What the export holds and how often it is updated. | `recorded/cadsr-ftp/cde-xml-readme.json` |
| OP-S02 | `GET ssis /si-api/v1/database/graph_names?limit=100` with `Accept: application/json` | 200 | recorded | The graphs the façade names. Neither Thesaurus.owl nor Thesaurus.rdf is among them, though the SPARQL endpoint serves both. | `recorded/ssis/graph-names.json` |
| OP-S02 | `GET ssis /si-api/v1/database/graph_names?limit=100` with no header | 200 | recorded | The same request without Accept: HTTP 200 with an HTML table, though the swagger declares JSON. The fixture naming Accept answers a request that carries it; this one any other (M3.2, X-15). | `recorded/ssis/graph-names-html.json` |
| OP-S02 | `GET ssis /si-api/v1/database/graph_names` with `Accept: application/json` | 200 | recorded | Without its required limit: HTTP 200 with apiResponse type E, "Error executing SPARQL query" (X-15). upstream/masked-error serves this answer to well-formed requests. | `recorded/ssis/graph-names-without-limit.json` |
| OP-S02 | `GET ssis /si-api/v1/data_elements/with_concept_id?graph_name=http://cbiit.nci.nih.gov/caDSR&resource_name=caDSR&dec_pub_id=2226947` with `Accept: application/json` | 200 | recorded | The data elements of one data element concept, Person Sex (2226947, the concept of 2200604). The façade's nearest operation to a concept's data elements is keyed by the DEC's public id, not by a concept code. | `recorded/ssis/data-elements-of-dec-2226947.json` |
| OP-S02 | `GET ssis /si-api/v1/data_elements/with_concept_id?graph_name=http://cbiit.nci.nih.gov/caDSR&resource_name=caDSR` with `Accept: application/json` | 200 | recorded | Without its required dec_pub_id: HTTP 200 with apiResponse type I, "No data found", a missing argument answered as an empty result (X-15). | `recorded/ssis/data-elements-without-dec.json` |
| OP-S02 | `GET ssis /si-api/v1/data_elements/with_specific_object_class?graph_name=http://cbiit.nci.nih.gov/caDSR&resource_name=caDSR&concept_id=C25190` with `Accept: application/json` | 200 | recorded | The data elements whose object class is Person (C25190): exactly 1,000 rows with apiResponse type S, where the caDSR graph holds 2,088. The cap is silent (A5.4), and the same 1,000 came back in another order a few minutes later. | `recorded/ssis/data-elements-of-object-class-c25190.json` |
| OP-S01 | `POST ssis-sparql /sparql` with `Accept: application/sparql-results+json`, form `query` (below) | 200 | recorded | The release identity of the NCIt and caDSR graphs: owl:versionInfo and dc:date, two untyped dates in two formats ("September 28, 2026" and "2026-07-01"), the stand-in for OP-S03 (A3.7.1). | `recorded/ssis-sparql/graph-identities.json` |
| OP-S01 | `POST ssis-sparql /sparql` with `Accept: application/sparql-results+json`, form `query` (below) | 200 | crafted for A3.7.1 and M6.1; bounded identities or remaining shared result capacity plus one sentinel. | Bounded query under the shared result cap; reuses the recorded answer as crafted. | `crafted/ssis-sparql/graph-identities-bounded.json` |
| OP-S01 | `POST ssis-sparql /sparql` with `Accept: application/sparql-results+json`, form `query` (below) | 200 | recorded | The data elements that use Gender (C17357) as an object class, property or permissible value concept, main or minor (find_data_elements_for_concept). | `recorded/ssis-sparql/data-elements-c17357.json` |
| OP-S01 | `POST ssis-sparql /sparql` with `Accept: application/sparql-results+json`, form `query` (below) | 200 | recorded | The data elements that use C116977, none: an empty answer (find_data_elements_for_concept, X-4). | `recorded/ssis-sparql/data-elements-c116977.json` |
| OP-S01 | `POST ssis-sparql /sparql` with `Accept: application/sparql-results+json`, form `query` (below) | 200 | recorded | The same, over C17357 and its descendants in NCIt's hierarchy (A5.4). | `recorded/ssis-sparql/data-elements-c17357-descendants.json` |
| OP-S01 | `POST ssis-sparql /sparql` with `Accept: application/sparql-results+json`, form `query` (below) | 200 | recorded | The same over Disease or Disorder (C2991) and its descendants: 1,001 rows, so more exist than the tool's maximum (A5.4). | `recorded/ssis-sparql/data-elements-c2991-descendants.json` |
| OP-S01 | `POST ssis-sparql /sparql` with `Accept: application/sparql-results+json`, form `query` (below) | 200 | recorded | The permissible values of data element 2200604 with the concepts their value meanings stand for, asked by its registry id alone: the caller's value never goes into the query text (A7.7), and the server picks it by exact equality (get_concept_for_permissible_value). | `recorded/ssis-sparql/values-of-2200604.json` |
| OP-S04 | `POST ssis-sparql /sparql` with `Accept: application/sparql-results+json`, form `query` (below) | 200 | crafted for get_concept_for_permissible_value; latest numeric version first, exact value and main concept only. | Preserve the latest version without values and values without main concepts; minor concepts do not compete. | `crafted/ssis-sparql/values-of-2200604-optional.json` |
| OP-S04 | `POST ssis-sparql /sparql` with `Accept: application/sparql-results+json`, form `query` (below) | 200 | recorded | The permissible values whose value meaning stands for Male (C20197), each with its data element and concept: the reverse lookup no façade operation offers. | `recorded/ssis-sparql/values-c20197.json` |
| OP-S04 | `POST ssis-sparql /sparql` with `Accept: application/sparql-results+json`, form `query` (below) | 200 | recorded | The permissible values that stand for Gender (C17357): find_data_elements_for_concept with includePermissibleValues. | `recorded/ssis-sparql/values-c17357.json` |
| OP-S04 | `POST ssis-sparql /sparql` with `Accept: application/sparql-results+json`, form `query` (below) | 200 | crafted for A3.7.1 and M6.1; bounded identities or remaining shared result capacity plus one sentinel. | Bounded query under the shared result cap; reuses the recorded answer as crafted. | `crafted/ssis-sparql/values-c17357-bounded.json` |
| OP-S04 | `POST ssis-sparql /sparql` with `Accept: application/sparql-results+json`, form `query` (below) | 200 | recorded | The same over C17357 and its descendants, each value naming its concept (A5.4). | `recorded/ssis-sparql/values-c17357-descendants.json` |
| OP-S04 | `POST ssis-sparql /sparql` with `Accept: application/sparql-results+json`, form `query` (below) | 200 | crafted for A3.7.1 and M6.1; bounded identities or remaining shared result capacity plus one sentinel. | Bounded query under the shared result cap; reuses the recorded answer as crafted. | `crafted/ssis-sparql/values-c17357-descendants-bounded.json` |
| OP-S01 | `POST ssis-sparql /sparql` with `Accept: application/sparql-results+json`, form `query` (below) | 200 | recorded | The data elements that use Ewing Sarcoma (C4817): ground_value's data element hop. | `recorded/ssis-sparql/data-elements-c4817.json` |
| OP-S04 | `POST ssis-sparql /sparql` with `Accept: application/sparql-results+json`, form `query` (below) | 200 | recorded | The permissible values that stand for Ewing Sarcoma (C4817): ground_value's value hop. | `recorded/ssis-sparql/values-c4817.json` |
| OP-S04 | `POST ssis-sparql /sparql` with `Accept: application/sparql-results+json`, form `query` (below) | 200 | crafted for A3.7.1 and M6.1; bounded identities or remaining shared result capacity plus one sentinel. | Bounded query under the shared result cap; reuses the recorded answer as crafted. | `crafted/ssis-sparql/values-c4817-bounded.json` |
| OP-S01 | `POST ssis-sparql /sparql` with `Accept: application/sparql-results+json`, form `query` (below) | 200 | recorded | The data elements that use Disease or Disorder (C2991), 1,001 rows: ground_value's data element hop reaches its bound. | `recorded/ssis-sparql/data-elements-c2991.json` |
| OP-S04 | `POST ssis-sparql /sparql` with `Accept: application/sparql-results+json`, form `query` (below) | 200 | recorded | The permissible values that stand for Disease or Disorder (C2991), 461 rows: ground_value's value hop, complete while the data element hop is cut. | `recorded/ssis-sparql/values-c2991.json` |
| OP-S01 | `POST ssis-sparql /sparql` with `Accept: application/sparql-results+json`, form `query` (below) | 200 | recorded | #370: anonymous public-ID role and unpaired status evidence. | `recorded/ssis-sparql/data-elements-statuses-c116977.json` |
| OP-S01 | `POST ssis-sparql /sparql` with `Accept: application/sparql-results+json`, form `query` (below) | 200 | recorded | #370: anonymous public-ID role and unpaired status evidence. | `recorded/ssis-sparql/data-elements-statuses-c17357-descendants.json` |
| OP-S01 | `POST ssis-sparql /sparql` with `Accept: application/sparql-results+json`, form `query` (below) | 200 | recorded | #370: anonymous public-ID role and unpaired status evidence. | `recorded/ssis-sparql/data-elements-statuses-c17357.json` |
| OP-S01 | `POST ssis-sparql /sparql` with `Accept: application/sparql-results+json`, form `query` (below) | 200 | recorded | #370: anonymous public-ID role and unpaired status evidence. | `recorded/ssis-sparql/data-elements-statuses-c25332-descendants.json` |
| OP-S01 | `POST ssis-sparql /sparql` with `Accept: application/sparql-results+json`, form `query` (below) | 200 | recorded | #370: anonymous public-ID role and unpaired status evidence. | `recorded/ssis-sparql/data-elements-statuses-c25332.json` |
| OP-S01 | `POST ssis-sparql /sparql` with `Accept: application/sparql-results+json`, form `query` (below) | 200 | recorded | #370: anonymous public-ID role and unpaired status evidence. | `recorded/ssis-sparql/data-elements-statuses-c2991-descendants.json` |
| OP-S01 | `POST ssis-sparql /sparql` with `Accept: application/sparql-results+json`, form `query` (below) | 200 | recorded | #370: anonymous public-ID role and unpaired status evidence. | `recorded/ssis-sparql/data-elements-statuses-c2991.json` |
| OP-S01 | `POST ssis-sparql /sparql` with `Accept: application/sparql-results+json`, form `query` (below) | 200 | recorded | #370: anonymous public-ID role and unpaired status evidence. | `recorded/ssis-sparql/data-elements-statuses-c4817.json` |
| OP-S01 | `POST ssis-sparql /sparql` with `Accept: application/sparql-results+json`, form `query` (below) | 200 | recorded | #370: anonymous public-ID role and unpaired status evidence. | `recorded/ssis-sparql/data-elements-statuses-c48179.json` |
| OP-S01 | `POST ssis-sparql /sparql` with `Accept: application/sparql-results+json`, form `query` (below) | 200 | recorded | #370: anonymous public-ID role and unpaired status evidence. | `recorded/ssis-sparql/data-elements-statuses-c49508.json` |
| OP-S01 | `POST ssis-sparql /sparql` with `Accept: application/sparql-results+json`, form `query` (below) | 200 | recorded | #365: newly recorded public-ID query with complete graph role tuples. | `recorded/ssis-sparql/data-elements-roles-c17357.json` |
| OP-S01 | `POST ssis-sparql /sparql` with `Accept: application/sparql-results+json`, form `query` (below) | 200 | recorded | #365: newly recorded expanded role query for Gender. | `recorded/ssis-sparql/data-elements-roles-c17357-descendants.json` |
| OP-S01 | `POST ssis-sparql /sparql` with `Accept: application/sparql-results+json`, form `query` (below) | 200 | recorded | #365: newly recorded empty role query. | `recorded/ssis-sparql/data-elements-roles-c116977.json` |
| OP-S01 | `POST ssis-sparql /sparql` with `Accept: application/sparql-results+json`, form `query` (below) | 200 | recorded | #365: newly recorded bounded Disease role query. | `recorded/ssis-sparql/data-elements-roles-c2991.json` |
| OP-S01 | `POST ssis-sparql /sparql` with `Accept: application/sparql-results+json`, form `query` (below) | 200 | recorded | #365: newly recorded bounded expanded Disease role query. | `recorded/ssis-sparql/data-elements-roles-c2991-descendants.json` |
| OP-S01 | `POST ssis-sparql /sparql` with `Accept: application/sparql-results+json`, form `query` (below) | 200 | recorded | #365: newly recorded Ewing sarcoma role query used in grounding. | `recorded/ssis-sparql/data-elements-roles-c4817.json` |
| OP-S01 | `POST ssis-sparql /sparql` with `Accept: application/sparql-results+json`, form `query` (below) | 200 | recorded | #365: live 2178439 order-one graph-main inversion evidence. | `recorded/ssis-sparql/data-elements-roles-c49508.json` |
| OP-S01 | `POST ssis-sparql /sparql` with `Accept: application/sparql-results+json`, form `query` (below) | 200 | recorded | #365: live mixed-role and 2178439 order-zero evidence; exact probe. | `recorded/ssis-sparql/data-elements-roles-c25332.json` |
| OP-S01 | `POST ssis-sparql /sparql` with `Accept: application/sparql-results+json`, form `query` (below) | 200 | recorded | #365: live mixed-role descendant query; expanded probe. | `recorded/ssis-sparql/data-elements-roles-c25332-descendants.json` |
| OP-S01 | `POST ssis-sparql /sparql` with `Accept: application/sparql-results+json`, form `query` (below) | 403 | recorded | The inspection layer's refusal of a query it does not pass: HTTP 403 with an HTML body. ssis/query-rejected serves it to every query. | `recorded/ssis-sparql/query-refused.json` |
| OP-S01 | `POST ssis-sparql /sparql` with `Accept: application/sparql-results+json`, `Content-Type: application/sparql-query`, body as written (below) | 403 | recorded | The identity query as a direct POST (application/sparql-query, SPARQL 1.1 protocol): refused with HTTP 403 and an HTML body, so a server that asks this way gets no answer. | `recorded/ssis-sparql/graph-identities-direct.json` |

## Query texts

### `recorded/ssis-sparql/graph-identities.json`: `query`

```sparql
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX owl: <http://www.w3.org/2002/07/owl#>
PREFIX dc: <http://purl.org/dc/elements/1.1/>
PREFIX mdr: <http://www.iso.org/11179/MDR#>
PREFIX cadsr: <http://cbiit.nci.nih.gov/caDSR#>
PREFIX ncit: <http://ncicb.nci.nih.gov/xml/owl/EVS/Thesaurus.owl#>
SELECT ?graph ?version ?date
WHERE {
  VALUES ?graph { <http://ncicb.nci.nih.gov/xml/owl/EVS/Thesaurus.rdf> <http://cbiit.nci.nih.gov/caDSR> }
  GRAPH ?graph {
    ?ontology dc:date ?date .
    OPTIONAL { ?ontology owl:versionInfo ?version }
  }
}
```

### `recorded/ssis-sparql/data-elements-c17357.json`: `query`

```sparql
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX owl: <http://www.w3.org/2002/07/owl#>
PREFIX dc: <http://purl.org/dc/elements/1.1/>
PREFIX mdr: <http://www.iso.org/11179/MDR#>
PREFIX cadsr: <http://cbiit.nci.nih.gov/caDSR#>
PREFIX ncit: <http://ncicb.nci.nih.gov/xml/owl/EVS/Thesaurus.owl#>
SELECT ?id ?version (MIN(?label) AS ?name)
WHERE {
  VALUES ?concept { ncit:C17357 }
  GRAPH <http://cbiit.nci.nih.gov/caDSR> {
    VALUES ?role { cadsr:main_concept cadsr:minor_concept }
    ?node ?role ?concept .
    { VALUES ?part { mdr:Object_Class mdr:Property } ?element ?part ?node . }
    UNION
    { ?value cadsr:has_concept ?node . ?element mdr:permitted_value ?value . }
    ?element cadsr:publicId ?id ;
      mdr:version ?version ;
      rdfs:label ?label .
  }
}
GROUP BY ?id ?version
ORDER BY ?id ?version
LIMIT 1001
```

### `recorded/ssis-sparql/data-elements-c116977.json`: `query`

```sparql
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX owl: <http://www.w3.org/2002/07/owl#>
PREFIX dc: <http://purl.org/dc/elements/1.1/>
PREFIX mdr: <http://www.iso.org/11179/MDR#>
PREFIX cadsr: <http://cbiit.nci.nih.gov/caDSR#>
PREFIX ncit: <http://ncicb.nci.nih.gov/xml/owl/EVS/Thesaurus.owl#>
SELECT ?id ?version (MIN(?label) AS ?name)
WHERE {
  VALUES ?concept { ncit:C116977 }
  GRAPH <http://cbiit.nci.nih.gov/caDSR> {
    VALUES ?role { cadsr:main_concept cadsr:minor_concept }
    ?node ?role ?concept .
    { VALUES ?part { mdr:Object_Class mdr:Property } ?element ?part ?node . }
    UNION
    { ?value cadsr:has_concept ?node . ?element mdr:permitted_value ?value . }
    ?element cadsr:publicId ?id ;
      mdr:version ?version ;
      rdfs:label ?label .
  }
}
GROUP BY ?id ?version
ORDER BY ?id ?version
LIMIT 1001
```

### `recorded/ssis-sparql/data-elements-c17357-descendants.json`: `query`

```sparql
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX owl: <http://www.w3.org/2002/07/owl#>
PREFIX dc: <http://purl.org/dc/elements/1.1/>
PREFIX mdr: <http://www.iso.org/11179/MDR#>
PREFIX cadsr: <http://cbiit.nci.nih.gov/caDSR#>
PREFIX ncit: <http://ncicb.nci.nih.gov/xml/owl/EVS/Thesaurus.owl#>
SELECT ?id ?version (MIN(?label) AS ?name)
WHERE {
  GRAPH <http://ncicb.nci.nih.gov/xml/owl/EVS/Thesaurus.rdf> {
    ?concept rdfs:subClassOf* ncit:C17357 .
  }
  GRAPH <http://cbiit.nci.nih.gov/caDSR> {
    VALUES ?role { cadsr:main_concept cadsr:minor_concept }
    ?node ?role ?concept .
    { VALUES ?part { mdr:Object_Class mdr:Property } ?element ?part ?node . }
    UNION
    { ?value cadsr:has_concept ?node . ?element mdr:permitted_value ?value . }
    ?element cadsr:publicId ?id ;
      mdr:version ?version ;
      rdfs:label ?label .
  }
}
GROUP BY ?id ?version
ORDER BY ?id ?version
LIMIT 1001
```

### `recorded/ssis-sparql/data-elements-c2991-descendants.json`: `query`

```sparql
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX owl: <http://www.w3.org/2002/07/owl#>
PREFIX dc: <http://purl.org/dc/elements/1.1/>
PREFIX mdr: <http://www.iso.org/11179/MDR#>
PREFIX cadsr: <http://cbiit.nci.nih.gov/caDSR#>
PREFIX ncit: <http://ncicb.nci.nih.gov/xml/owl/EVS/Thesaurus.owl#>
SELECT ?id ?version (MIN(?label) AS ?name)
WHERE {
  GRAPH <http://ncicb.nci.nih.gov/xml/owl/EVS/Thesaurus.rdf> {
    ?concept rdfs:subClassOf* ncit:C2991 .
  }
  GRAPH <http://cbiit.nci.nih.gov/caDSR> {
    VALUES ?role { cadsr:main_concept cadsr:minor_concept }
    ?node ?role ?concept .
    { VALUES ?part { mdr:Object_Class mdr:Property } ?element ?part ?node . }
    UNION
    { ?value cadsr:has_concept ?node . ?element mdr:permitted_value ?value . }
    ?element cadsr:publicId ?id ;
      mdr:version ?version ;
      rdfs:label ?label .
  }
}
GROUP BY ?id ?version
ORDER BY ?id ?version
LIMIT 1001
```

### `recorded/ssis-sparql/values-of-2200604.json`: `query`

```sparql
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX owl: <http://www.w3.org/2002/07/owl#>
PREFIX dc: <http://purl.org/dc/elements/1.1/>
PREFIX mdr: <http://www.iso.org/11179/MDR#>
PREFIX cadsr: <http://cbiit.nci.nih.gov/caDSR#>
PREFIX ncit: <http://ncicb.nci.nih.gov/xml/owl/EVS/Thesaurus.owl#>
SELECT ?version ?value ?concept ?role
WHERE {
  GRAPH <http://cbiit.nci.nih.gov/caDSR> {
    VALUES ?role { cadsr:main_concept cadsr:minor_concept }
    ?element cadsr:publicId "2200604" ;
      mdr:version ?version ;
      mdr:permitted_value ?pv .
    ?pv mdr:value ?value ;
      cadsr:has_concept ?node .
    ?node ?role ?concept .
  }
}
ORDER BY ?version ?value ?role ?concept
LIMIT 1001
```

### `recorded/ssis-sparql/values-c20197.json`: `query`

```sparql
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX owl: <http://www.w3.org/2002/07/owl#>
PREFIX dc: <http://purl.org/dc/elements/1.1/>
PREFIX mdr: <http://www.iso.org/11179/MDR#>
PREFIX cadsr: <http://cbiit.nci.nih.gov/caDSR#>
PREFIX ncit: <http://ncicb.nci.nih.gov/xml/owl/EVS/Thesaurus.owl#>
SELECT DISTINCT ?id ?version ?value ?concept
WHERE {
  VALUES ?concept { ncit:C20197 }
  GRAPH <http://cbiit.nci.nih.gov/caDSR> {
    VALUES ?role { cadsr:main_concept cadsr:minor_concept }
    ?node ?role ?concept .
    ?pv cadsr:has_concept ?node ;
      mdr:value ?value .
    ?element mdr:permitted_value ?pv ;
      cadsr:publicId ?id ;
      mdr:version ?version .
  }
}
ORDER BY ?id ?version ?value ?concept
LIMIT 1001
```

### `recorded/ssis-sparql/values-c17357.json`: `query`

```sparql
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX owl: <http://www.w3.org/2002/07/owl#>
PREFIX dc: <http://purl.org/dc/elements/1.1/>
PREFIX mdr: <http://www.iso.org/11179/MDR#>
PREFIX cadsr: <http://cbiit.nci.nih.gov/caDSR#>
PREFIX ncit: <http://ncicb.nci.nih.gov/xml/owl/EVS/Thesaurus.owl#>
SELECT DISTINCT ?id ?version ?value ?concept
WHERE {
  VALUES ?concept { ncit:C17357 }
  GRAPH <http://cbiit.nci.nih.gov/caDSR> {
    VALUES ?role { cadsr:main_concept cadsr:minor_concept }
    ?node ?role ?concept .
    ?pv cadsr:has_concept ?node ;
      mdr:value ?value .
    ?element mdr:permitted_value ?pv ;
      cadsr:publicId ?id ;
      mdr:version ?version .
  }
}
ORDER BY ?id ?version ?value ?concept
LIMIT 1001
```

### `recorded/ssis-sparql/values-c17357-descendants.json`: `query`

```sparql
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX owl: <http://www.w3.org/2002/07/owl#>
PREFIX dc: <http://purl.org/dc/elements/1.1/>
PREFIX mdr: <http://www.iso.org/11179/MDR#>
PREFIX cadsr: <http://cbiit.nci.nih.gov/caDSR#>
PREFIX ncit: <http://ncicb.nci.nih.gov/xml/owl/EVS/Thesaurus.owl#>
SELECT DISTINCT ?id ?version ?value ?concept
WHERE {
  GRAPH <http://ncicb.nci.nih.gov/xml/owl/EVS/Thesaurus.rdf> {
    ?concept rdfs:subClassOf* ncit:C17357 .
  }
  GRAPH <http://cbiit.nci.nih.gov/caDSR> {
    VALUES ?role { cadsr:main_concept cadsr:minor_concept }
    ?node ?role ?concept .
    ?pv cadsr:has_concept ?node ;
      mdr:value ?value .
    ?element mdr:permitted_value ?pv ;
      cadsr:publicId ?id ;
      mdr:version ?version .
  }
}
ORDER BY ?id ?version ?value ?concept
LIMIT 1001
```

### `recorded/ssis-sparql/data-elements-c4817.json`: `query`

```sparql
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX owl: <http://www.w3.org/2002/07/owl#>
PREFIX dc: <http://purl.org/dc/elements/1.1/>
PREFIX mdr: <http://www.iso.org/11179/MDR#>
PREFIX cadsr: <http://cbiit.nci.nih.gov/caDSR#>
PREFIX ncit: <http://ncicb.nci.nih.gov/xml/owl/EVS/Thesaurus.owl#>
SELECT ?id ?version (MIN(?label) AS ?name)
WHERE {
  VALUES ?concept { ncit:C4817 }
  GRAPH <http://cbiit.nci.nih.gov/caDSR> {
    VALUES ?role { cadsr:main_concept cadsr:minor_concept }
    ?node ?role ?concept .
    { VALUES ?part { mdr:Object_Class mdr:Property } ?element ?part ?node . }
    UNION
    { ?value cadsr:has_concept ?node . ?element mdr:permitted_value ?value . }
    ?element cadsr:publicId ?id ;
      mdr:version ?version ;
      rdfs:label ?label .
  }
}
GROUP BY ?id ?version
ORDER BY ?id ?version
LIMIT 1001
```

### `recorded/ssis-sparql/values-c4817.json`: `query`

```sparql
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX owl: <http://www.w3.org/2002/07/owl#>
PREFIX dc: <http://purl.org/dc/elements/1.1/>
PREFIX mdr: <http://www.iso.org/11179/MDR#>
PREFIX cadsr: <http://cbiit.nci.nih.gov/caDSR#>
PREFIX ncit: <http://ncicb.nci.nih.gov/xml/owl/EVS/Thesaurus.owl#>
SELECT DISTINCT ?id ?version ?value ?concept
WHERE {
  VALUES ?concept { ncit:C4817 }
  GRAPH <http://cbiit.nci.nih.gov/caDSR> {
    VALUES ?role { cadsr:main_concept cadsr:minor_concept }
    ?node ?role ?concept .
    ?pv cadsr:has_concept ?node ;
      mdr:value ?value .
    ?element mdr:permitted_value ?pv ;
      cadsr:publicId ?id ;
      mdr:version ?version .
  }
}
ORDER BY ?id ?version ?value ?concept
LIMIT 1001
```

### `recorded/ssis-sparql/data-elements-c2991.json`: `query`

```sparql
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX owl: <http://www.w3.org/2002/07/owl#>
PREFIX dc: <http://purl.org/dc/elements/1.1/>
PREFIX mdr: <http://www.iso.org/11179/MDR#>
PREFIX cadsr: <http://cbiit.nci.nih.gov/caDSR#>
PREFIX ncit: <http://ncicb.nci.nih.gov/xml/owl/EVS/Thesaurus.owl#>
SELECT ?id ?version (MIN(?label) AS ?name)
WHERE {
  VALUES ?concept { ncit:C2991 }
  GRAPH <http://cbiit.nci.nih.gov/caDSR> {
    VALUES ?role { cadsr:main_concept cadsr:minor_concept }
    ?node ?role ?concept .
    { VALUES ?part { mdr:Object_Class mdr:Property } ?element ?part ?node . }
    UNION
    { ?value cadsr:has_concept ?node . ?element mdr:permitted_value ?value . }
    ?element cadsr:publicId ?id ;
      mdr:version ?version ;
      rdfs:label ?label .
  }
}
GROUP BY ?id ?version
ORDER BY ?id ?version
LIMIT 1001
```

### `recorded/ssis-sparql/values-c2991.json`: `query`

```sparql
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX owl: <http://www.w3.org/2002/07/owl#>
PREFIX dc: <http://purl.org/dc/elements/1.1/>
PREFIX mdr: <http://www.iso.org/11179/MDR#>
PREFIX cadsr: <http://cbiit.nci.nih.gov/caDSR#>
PREFIX ncit: <http://ncicb.nci.nih.gov/xml/owl/EVS/Thesaurus.owl#>
SELECT DISTINCT ?id ?version ?value ?concept
WHERE {
  VALUES ?concept { ncit:C2991 }
  GRAPH <http://cbiit.nci.nih.gov/caDSR> {
    VALUES ?role { cadsr:main_concept cadsr:minor_concept }
    ?node ?role ?concept .
    ?pv cadsr:has_concept ?node ;
      mdr:value ?value .
    ?element mdr:permitted_value ?pv ;
      cadsr:publicId ?id ;
      mdr:version ?version .
  }
}
ORDER BY ?id ?version ?value ?concept
LIMIT 1001
```

### `recorded/ssis-sparql/data-elements-statuses-c116977.json`: `query`

```sparql
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX owl: <http://www.w3.org/2002/07/owl#>
PREFIX dc: <http://purl.org/dc/elements/1.1/>
PREFIX mdr: <http://www.iso.org/11179/MDR#>
PREFIX cadsr: <http://cbiit.nci.nih.gov/caDSR#>
PREFIX ncit: <http://ncicb.nci.nih.gov/xml/owl/EVS/Thesaurus.owl#>
SELECT ?id (MIN(?label) AS ?name)
  (GROUP_CONCAT(DISTINCT CONCAT(ENCODE_FOR_URI(?useRole),"|",ENCODE_FOR_URI(STR(?concept)),"|",COALESCE(ENCODE_FOR_URI(STR(?order)),""));separator=" ") AS ?roles)
  (GROUP_CONCAT(DISTINCT IF(BOUND(?workflowStatus),CONCAT("v",ENCODE_FOR_URI(STR(?workflowStatus))),"");separator=" ") AS ?workflowStatuses)
  (GROUP_CONCAT(DISTINCT IF(BOUND(?registrationStatus),CONCAT("v",ENCODE_FOR_URI(STR(?registrationStatus))),"");separator=" ") AS ?registrationStatuses)
WHERE {
  VALUES ?concept { ncit:C116977 }
  GRAPH <http://cbiit.nci.nih.gov/caDSR> {
    VALUES ?membership { cadsr:main_concept cadsr:minor_concept }
    ?node ?membership ?concept .
    { ?element mdr:Object_Class ?node . BIND("objectClass" AS ?useRole) }
    UNION
    { ?element mdr:Property ?node . BIND("property" AS ?useRole) }
    UNION
    { ?value cadsr:has_concept ?node . ?element mdr:permitted_value ?value . BIND("valueMeaning" AS ?useRole) }
    OPTIONAL { ?node cadsr:display_order ?order }
    ?element cadsr:publicId ?id ;
      rdfs:label ?label .
    OPTIONAL { ?element mdr:administration_status ?workflowStatus }
    OPTIONAL { ?element mdr:registration_status ?registrationStatus }
  }
}
GROUP BY ?id
ORDER BY ?id
LIMIT 1001
```

### `recorded/ssis-sparql/data-elements-statuses-c17357-descendants.json`: `query`

```sparql
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX owl: <http://www.w3.org/2002/07/owl#>
PREFIX dc: <http://purl.org/dc/elements/1.1/>
PREFIX mdr: <http://www.iso.org/11179/MDR#>
PREFIX cadsr: <http://cbiit.nci.nih.gov/caDSR#>
PREFIX ncit: <http://ncicb.nci.nih.gov/xml/owl/EVS/Thesaurus.owl#>
SELECT ?id (MIN(?label) AS ?name)
  (GROUP_CONCAT(DISTINCT CONCAT(ENCODE_FOR_URI(?useRole),"|",ENCODE_FOR_URI(STR(?concept)),"|",COALESCE(ENCODE_FOR_URI(STR(?order)),""));separator=" ") AS ?roles)
  (GROUP_CONCAT(DISTINCT IF(BOUND(?workflowStatus),CONCAT("v",ENCODE_FOR_URI(STR(?workflowStatus))),"");separator=" ") AS ?workflowStatuses)
  (GROUP_CONCAT(DISTINCT IF(BOUND(?registrationStatus),CONCAT("v",ENCODE_FOR_URI(STR(?registrationStatus))),"");separator=" ") AS ?registrationStatuses)
WHERE {
  GRAPH <http://ncicb.nci.nih.gov/xml/owl/EVS/Thesaurus.rdf> { ?concept rdfs:subClassOf* ncit:C17357 . }
  GRAPH <http://cbiit.nci.nih.gov/caDSR> {
    VALUES ?membership { cadsr:main_concept cadsr:minor_concept }
    ?node ?membership ?concept .
    { ?element mdr:Object_Class ?node . BIND("objectClass" AS ?useRole) }
    UNION
    { ?element mdr:Property ?node . BIND("property" AS ?useRole) }
    UNION
    { ?value cadsr:has_concept ?node . ?element mdr:permitted_value ?value . BIND("valueMeaning" AS ?useRole) }
    OPTIONAL { ?node cadsr:display_order ?order }
    ?element cadsr:publicId ?id ;
      rdfs:label ?label .
    OPTIONAL { ?element mdr:administration_status ?workflowStatus }
    OPTIONAL { ?element mdr:registration_status ?registrationStatus }
  }
}
GROUP BY ?id
ORDER BY ?id
LIMIT 1001
```

### `recorded/ssis-sparql/data-elements-statuses-c17357.json`: `query`

```sparql
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX owl: <http://www.w3.org/2002/07/owl#>
PREFIX dc: <http://purl.org/dc/elements/1.1/>
PREFIX mdr: <http://www.iso.org/11179/MDR#>
PREFIX cadsr: <http://cbiit.nci.nih.gov/caDSR#>
PREFIX ncit: <http://ncicb.nci.nih.gov/xml/owl/EVS/Thesaurus.owl#>
SELECT ?id (MIN(?label) AS ?name)
  (GROUP_CONCAT(DISTINCT CONCAT(ENCODE_FOR_URI(?useRole),"|",ENCODE_FOR_URI(STR(?concept)),"|",COALESCE(ENCODE_FOR_URI(STR(?order)),""));separator=" ") AS ?roles)
  (GROUP_CONCAT(DISTINCT IF(BOUND(?workflowStatus),CONCAT("v",ENCODE_FOR_URI(STR(?workflowStatus))),"");separator=" ") AS ?workflowStatuses)
  (GROUP_CONCAT(DISTINCT IF(BOUND(?registrationStatus),CONCAT("v",ENCODE_FOR_URI(STR(?registrationStatus))),"");separator=" ") AS ?registrationStatuses)
WHERE {
  VALUES ?concept { ncit:C17357 }
  GRAPH <http://cbiit.nci.nih.gov/caDSR> {
    VALUES ?membership { cadsr:main_concept cadsr:minor_concept }
    ?node ?membership ?concept .
    { ?element mdr:Object_Class ?node . BIND("objectClass" AS ?useRole) }
    UNION
    { ?element mdr:Property ?node . BIND("property" AS ?useRole) }
    UNION
    { ?value cadsr:has_concept ?node . ?element mdr:permitted_value ?value . BIND("valueMeaning" AS ?useRole) }
    OPTIONAL { ?node cadsr:display_order ?order }
    ?element cadsr:publicId ?id ;
      rdfs:label ?label .
    OPTIONAL { ?element mdr:administration_status ?workflowStatus }
    OPTIONAL { ?element mdr:registration_status ?registrationStatus }
  }
}
GROUP BY ?id
ORDER BY ?id
LIMIT 1001
```

### `recorded/ssis-sparql/data-elements-statuses-c25332-descendants.json`: `query`

```sparql
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX owl: <http://www.w3.org/2002/07/owl#>
PREFIX dc: <http://purl.org/dc/elements/1.1/>
PREFIX mdr: <http://www.iso.org/11179/MDR#>
PREFIX cadsr: <http://cbiit.nci.nih.gov/caDSR#>
PREFIX ncit: <http://ncicb.nci.nih.gov/xml/owl/EVS/Thesaurus.owl#>
SELECT ?id (MIN(?label) AS ?name)
  (GROUP_CONCAT(DISTINCT CONCAT(ENCODE_FOR_URI(?useRole),"|",ENCODE_FOR_URI(STR(?concept)),"|",COALESCE(ENCODE_FOR_URI(STR(?order)),""));separator=" ") AS ?roles)
  (GROUP_CONCAT(DISTINCT IF(BOUND(?workflowStatus),CONCAT("v",ENCODE_FOR_URI(STR(?workflowStatus))),"");separator=" ") AS ?workflowStatuses)
  (GROUP_CONCAT(DISTINCT IF(BOUND(?registrationStatus),CONCAT("v",ENCODE_FOR_URI(STR(?registrationStatus))),"");separator=" ") AS ?registrationStatuses)
WHERE {
  GRAPH <http://ncicb.nci.nih.gov/xml/owl/EVS/Thesaurus.rdf> { ?concept rdfs:subClassOf* ncit:C25332 . }
  GRAPH <http://cbiit.nci.nih.gov/caDSR> {
    VALUES ?membership { cadsr:main_concept cadsr:minor_concept }
    ?node ?membership ?concept .
    { ?element mdr:Object_Class ?node . BIND("objectClass" AS ?useRole) }
    UNION
    { ?element mdr:Property ?node . BIND("property" AS ?useRole) }
    UNION
    { ?value cadsr:has_concept ?node . ?element mdr:permitted_value ?value . BIND("valueMeaning" AS ?useRole) }
    OPTIONAL { ?node cadsr:display_order ?order }
    ?element cadsr:publicId ?id ;
      rdfs:label ?label .
    OPTIONAL { ?element mdr:administration_status ?workflowStatus }
    OPTIONAL { ?element mdr:registration_status ?registrationStatus }
  }
}
GROUP BY ?id
ORDER BY ?id
LIMIT 1001
```

### `recorded/ssis-sparql/data-elements-statuses-c25332.json`: `query`

```sparql
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX owl: <http://www.w3.org/2002/07/owl#>
PREFIX dc: <http://purl.org/dc/elements/1.1/>
PREFIX mdr: <http://www.iso.org/11179/MDR#>
PREFIX cadsr: <http://cbiit.nci.nih.gov/caDSR#>
PREFIX ncit: <http://ncicb.nci.nih.gov/xml/owl/EVS/Thesaurus.owl#>
SELECT ?id (MIN(?label) AS ?name)
  (GROUP_CONCAT(DISTINCT CONCAT(ENCODE_FOR_URI(?useRole),"|",ENCODE_FOR_URI(STR(?concept)),"|",COALESCE(ENCODE_FOR_URI(STR(?order)),""));separator=" ") AS ?roles)
  (GROUP_CONCAT(DISTINCT IF(BOUND(?workflowStatus),CONCAT("v",ENCODE_FOR_URI(STR(?workflowStatus))),"");separator=" ") AS ?workflowStatuses)
  (GROUP_CONCAT(DISTINCT IF(BOUND(?registrationStatus),CONCAT("v",ENCODE_FOR_URI(STR(?registrationStatus))),"");separator=" ") AS ?registrationStatuses)
WHERE {
  VALUES ?concept { ncit:C25332 }
  GRAPH <http://cbiit.nci.nih.gov/caDSR> {
    VALUES ?membership { cadsr:main_concept cadsr:minor_concept }
    ?node ?membership ?concept .
    { ?element mdr:Object_Class ?node . BIND("objectClass" AS ?useRole) }
    UNION
    { ?element mdr:Property ?node . BIND("property" AS ?useRole) }
    UNION
    { ?value cadsr:has_concept ?node . ?element mdr:permitted_value ?value . BIND("valueMeaning" AS ?useRole) }
    OPTIONAL { ?node cadsr:display_order ?order }
    ?element cadsr:publicId ?id ;
      rdfs:label ?label .
    OPTIONAL { ?element mdr:administration_status ?workflowStatus }
    OPTIONAL { ?element mdr:registration_status ?registrationStatus }
  }
}
GROUP BY ?id
ORDER BY ?id
LIMIT 1001
```

### `recorded/ssis-sparql/data-elements-statuses-c2991-descendants.json`: `query`

```sparql
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX owl: <http://www.w3.org/2002/07/owl#>
PREFIX dc: <http://purl.org/dc/elements/1.1/>
PREFIX mdr: <http://www.iso.org/11179/MDR#>
PREFIX cadsr: <http://cbiit.nci.nih.gov/caDSR#>
PREFIX ncit: <http://ncicb.nci.nih.gov/xml/owl/EVS/Thesaurus.owl#>
SELECT ?id (MIN(?label) AS ?name)
  (GROUP_CONCAT(DISTINCT CONCAT(ENCODE_FOR_URI(?useRole),"|",ENCODE_FOR_URI(STR(?concept)),"|",COALESCE(ENCODE_FOR_URI(STR(?order)),""));separator=" ") AS ?roles)
  (GROUP_CONCAT(DISTINCT IF(BOUND(?workflowStatus),CONCAT("v",ENCODE_FOR_URI(STR(?workflowStatus))),"");separator=" ") AS ?workflowStatuses)
  (GROUP_CONCAT(DISTINCT IF(BOUND(?registrationStatus),CONCAT("v",ENCODE_FOR_URI(STR(?registrationStatus))),"");separator=" ") AS ?registrationStatuses)
WHERE {
  GRAPH <http://ncicb.nci.nih.gov/xml/owl/EVS/Thesaurus.rdf> { ?concept rdfs:subClassOf* ncit:C2991 . }
  GRAPH <http://cbiit.nci.nih.gov/caDSR> {
    VALUES ?membership { cadsr:main_concept cadsr:minor_concept }
    ?node ?membership ?concept .
    { ?element mdr:Object_Class ?node . BIND("objectClass" AS ?useRole) }
    UNION
    { ?element mdr:Property ?node . BIND("property" AS ?useRole) }
    UNION
    { ?value cadsr:has_concept ?node . ?element mdr:permitted_value ?value . BIND("valueMeaning" AS ?useRole) }
    OPTIONAL { ?node cadsr:display_order ?order }
    ?element cadsr:publicId ?id ;
      rdfs:label ?label .
    OPTIONAL { ?element mdr:administration_status ?workflowStatus }
    OPTIONAL { ?element mdr:registration_status ?registrationStatus }
  }
}
GROUP BY ?id
ORDER BY ?id
LIMIT 1001
```

### `recorded/ssis-sparql/data-elements-statuses-c2991.json`: `query`

```sparql
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX owl: <http://www.w3.org/2002/07/owl#>
PREFIX dc: <http://purl.org/dc/elements/1.1/>
PREFIX mdr: <http://www.iso.org/11179/MDR#>
PREFIX cadsr: <http://cbiit.nci.nih.gov/caDSR#>
PREFIX ncit: <http://ncicb.nci.nih.gov/xml/owl/EVS/Thesaurus.owl#>
SELECT ?id (MIN(?label) AS ?name)
  (GROUP_CONCAT(DISTINCT CONCAT(ENCODE_FOR_URI(?useRole),"|",ENCODE_FOR_URI(STR(?concept)),"|",COALESCE(ENCODE_FOR_URI(STR(?order)),""));separator=" ") AS ?roles)
  (GROUP_CONCAT(DISTINCT IF(BOUND(?workflowStatus),CONCAT("v",ENCODE_FOR_URI(STR(?workflowStatus))),"");separator=" ") AS ?workflowStatuses)
  (GROUP_CONCAT(DISTINCT IF(BOUND(?registrationStatus),CONCAT("v",ENCODE_FOR_URI(STR(?registrationStatus))),"");separator=" ") AS ?registrationStatuses)
WHERE {
  VALUES ?concept { ncit:C2991 }
  GRAPH <http://cbiit.nci.nih.gov/caDSR> {
    VALUES ?membership { cadsr:main_concept cadsr:minor_concept }
    ?node ?membership ?concept .
    { ?element mdr:Object_Class ?node . BIND("objectClass" AS ?useRole) }
    UNION
    { ?element mdr:Property ?node . BIND("property" AS ?useRole) }
    UNION
    { ?value cadsr:has_concept ?node . ?element mdr:permitted_value ?value . BIND("valueMeaning" AS ?useRole) }
    OPTIONAL { ?node cadsr:display_order ?order }
    ?element cadsr:publicId ?id ;
      rdfs:label ?label .
    OPTIONAL { ?element mdr:administration_status ?workflowStatus }
    OPTIONAL { ?element mdr:registration_status ?registrationStatus }
  }
}
GROUP BY ?id
ORDER BY ?id
LIMIT 1001
```

### `recorded/ssis-sparql/data-elements-statuses-c4817.json`: `query`

```sparql
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX owl: <http://www.w3.org/2002/07/owl#>
PREFIX dc: <http://purl.org/dc/elements/1.1/>
PREFIX mdr: <http://www.iso.org/11179/MDR#>
PREFIX cadsr: <http://cbiit.nci.nih.gov/caDSR#>
PREFIX ncit: <http://ncicb.nci.nih.gov/xml/owl/EVS/Thesaurus.owl#>
SELECT ?id (MIN(?label) AS ?name)
  (GROUP_CONCAT(DISTINCT CONCAT(ENCODE_FOR_URI(?useRole),"|",ENCODE_FOR_URI(STR(?concept)),"|",COALESCE(ENCODE_FOR_URI(STR(?order)),""));separator=" ") AS ?roles)
  (GROUP_CONCAT(DISTINCT IF(BOUND(?workflowStatus),CONCAT("v",ENCODE_FOR_URI(STR(?workflowStatus))),"");separator=" ") AS ?workflowStatuses)
  (GROUP_CONCAT(DISTINCT IF(BOUND(?registrationStatus),CONCAT("v",ENCODE_FOR_URI(STR(?registrationStatus))),"");separator=" ") AS ?registrationStatuses)
WHERE {
  VALUES ?concept { ncit:C4817 }
  GRAPH <http://cbiit.nci.nih.gov/caDSR> {
    VALUES ?membership { cadsr:main_concept cadsr:minor_concept }
    ?node ?membership ?concept .
    { ?element mdr:Object_Class ?node . BIND("objectClass" AS ?useRole) }
    UNION
    { ?element mdr:Property ?node . BIND("property" AS ?useRole) }
    UNION
    { ?value cadsr:has_concept ?node . ?element mdr:permitted_value ?value . BIND("valueMeaning" AS ?useRole) }
    OPTIONAL { ?node cadsr:display_order ?order }
    ?element cadsr:publicId ?id ;
      rdfs:label ?label .
    OPTIONAL { ?element mdr:administration_status ?workflowStatus }
    OPTIONAL { ?element mdr:registration_status ?registrationStatus }
  }
}
GROUP BY ?id
ORDER BY ?id
LIMIT 1001
```

### `recorded/ssis-sparql/data-elements-statuses-c48179.json`: `query`

```sparql
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX owl: <http://www.w3.org/2002/07/owl#>
PREFIX dc: <http://purl.org/dc/elements/1.1/>
PREFIX mdr: <http://www.iso.org/11179/MDR#>
PREFIX cadsr: <http://cbiit.nci.nih.gov/caDSR#>
PREFIX ncit: <http://ncicb.nci.nih.gov/xml/owl/EVS/Thesaurus.owl#>
SELECT ?id (MIN(?label) AS ?name)
  (GROUP_CONCAT(DISTINCT CONCAT(ENCODE_FOR_URI(?useRole),"|",ENCODE_FOR_URI(STR(?concept)),"|",COALESCE(ENCODE_FOR_URI(STR(?order)),""));separator=" ") AS ?roles)
  (GROUP_CONCAT(DISTINCT IF(BOUND(?workflowStatus),CONCAT("v",ENCODE_FOR_URI(STR(?workflowStatus))),"");separator=" ") AS ?workflowStatuses)
  (GROUP_CONCAT(DISTINCT IF(BOUND(?registrationStatus),CONCAT("v",ENCODE_FOR_URI(STR(?registrationStatus))),"");separator=" ") AS ?registrationStatuses)
WHERE {
  VALUES ?concept { ncit:C48179 }
  GRAPH <http://cbiit.nci.nih.gov/caDSR> {
    VALUES ?membership { cadsr:main_concept cadsr:minor_concept }
    ?node ?membership ?concept .
    { ?element mdr:Object_Class ?node . BIND("objectClass" AS ?useRole) }
    UNION
    { ?element mdr:Property ?node . BIND("property" AS ?useRole) }
    UNION
    { ?value cadsr:has_concept ?node . ?element mdr:permitted_value ?value . BIND("valueMeaning" AS ?useRole) }
    OPTIONAL { ?node cadsr:display_order ?order }
    ?element cadsr:publicId ?id ;
      rdfs:label ?label .
    OPTIONAL { ?element mdr:administration_status ?workflowStatus }
    OPTIONAL { ?element mdr:registration_status ?registrationStatus }
  }
}
GROUP BY ?id
ORDER BY ?id
LIMIT 1001
```

### `recorded/ssis-sparql/data-elements-statuses-c49508.json`: `query`

```sparql
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX owl: <http://www.w3.org/2002/07/owl#>
PREFIX dc: <http://purl.org/dc/elements/1.1/>
PREFIX mdr: <http://www.iso.org/11179/MDR#>
PREFIX cadsr: <http://cbiit.nci.nih.gov/caDSR#>
PREFIX ncit: <http://ncicb.nci.nih.gov/xml/owl/EVS/Thesaurus.owl#>
SELECT ?id (MIN(?label) AS ?name)
  (GROUP_CONCAT(DISTINCT CONCAT(ENCODE_FOR_URI(?useRole),"|",ENCODE_FOR_URI(STR(?concept)),"|",COALESCE(ENCODE_FOR_URI(STR(?order)),""));separator=" ") AS ?roles)
  (GROUP_CONCAT(DISTINCT IF(BOUND(?workflowStatus),CONCAT("v",ENCODE_FOR_URI(STR(?workflowStatus))),"");separator=" ") AS ?workflowStatuses)
  (GROUP_CONCAT(DISTINCT IF(BOUND(?registrationStatus),CONCAT("v",ENCODE_FOR_URI(STR(?registrationStatus))),"");separator=" ") AS ?registrationStatuses)
WHERE {
  VALUES ?concept { ncit:C49508 }
  GRAPH <http://cbiit.nci.nih.gov/caDSR> {
    VALUES ?membership { cadsr:main_concept cadsr:minor_concept }
    ?node ?membership ?concept .
    { ?element mdr:Object_Class ?node . BIND("objectClass" AS ?useRole) }
    UNION
    { ?element mdr:Property ?node . BIND("property" AS ?useRole) }
    UNION
    { ?value cadsr:has_concept ?node . ?element mdr:permitted_value ?value . BIND("valueMeaning" AS ?useRole) }
    OPTIONAL { ?node cadsr:display_order ?order }
    ?element cadsr:publicId ?id ;
      rdfs:label ?label .
    OPTIONAL { ?element mdr:administration_status ?workflowStatus }
    OPTIONAL { ?element mdr:registration_status ?registrationStatus }
  }
}
GROUP BY ?id
ORDER BY ?id
LIMIT 1001
```

### `recorded/ssis-sparql/data-elements-roles-c17357.json`: `query`

```sparql
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX owl: <http://www.w3.org/2002/07/owl#>
PREFIX dc: <http://purl.org/dc/elements/1.1/>
PREFIX mdr: <http://www.iso.org/11179/MDR#>
PREFIX cadsr: <http://cbiit.nci.nih.gov/caDSR#>
PREFIX ncit: <http://ncicb.nci.nih.gov/xml/owl/EVS/Thesaurus.owl#>
SELECT ?id (MIN(?label) AS ?name)
  (GROUP_CONCAT(DISTINCT CONCAT(ENCODE_FOR_URI(?useRole),"|",ENCODE_FOR_URI(STR(?concept)),"|",COALESCE(ENCODE_FOR_URI(STR(?order)),""));separator=" ") AS ?roles)
WHERE {
  VALUES ?concept { ncit:C17357 }
  GRAPH <http://cbiit.nci.nih.gov/caDSR> {
    VALUES ?membership { cadsr:main_concept cadsr:minor_concept }
    ?node ?membership ?concept .
    { ?element mdr:Object_Class ?node . BIND("objectClass" AS ?useRole) }
    UNION
    { ?element mdr:Property ?node . BIND("property" AS ?useRole) }
    UNION
    { ?value cadsr:has_concept ?node . ?element mdr:permitted_value ?value . BIND("valueMeaning" AS ?useRole) }
    OPTIONAL { ?node cadsr:display_order ?order }
    ?element cadsr:publicId ?id ;
      rdfs:label ?label .
  }
}
GROUP BY ?id
ORDER BY ?id
LIMIT 1001
```

### `recorded/ssis-sparql/data-elements-roles-c17357-descendants.json`: `query`

```sparql
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX owl: <http://www.w3.org/2002/07/owl#>
PREFIX dc: <http://purl.org/dc/elements/1.1/>
PREFIX mdr: <http://www.iso.org/11179/MDR#>
PREFIX cadsr: <http://cbiit.nci.nih.gov/caDSR#>
PREFIX ncit: <http://ncicb.nci.nih.gov/xml/owl/EVS/Thesaurus.owl#>
SELECT ?id (MIN(?label) AS ?name)
  (GROUP_CONCAT(DISTINCT CONCAT(ENCODE_FOR_URI(?useRole),"|",ENCODE_FOR_URI(STR(?concept)),"|",COALESCE(ENCODE_FOR_URI(STR(?order)),""));separator=" ") AS ?roles)
WHERE {
  GRAPH <http://ncicb.nci.nih.gov/xml/owl/EVS/Thesaurus.rdf> { ?concept rdfs:subClassOf* ncit:C17357 . }
  GRAPH <http://cbiit.nci.nih.gov/caDSR> {
    VALUES ?membership { cadsr:main_concept cadsr:minor_concept }
    ?node ?membership ?concept .
    { ?element mdr:Object_Class ?node . BIND("objectClass" AS ?useRole) }
    UNION
    { ?element mdr:Property ?node . BIND("property" AS ?useRole) }
    UNION
    { ?value cadsr:has_concept ?node . ?element mdr:permitted_value ?value . BIND("valueMeaning" AS ?useRole) }
    OPTIONAL { ?node cadsr:display_order ?order }
    ?element cadsr:publicId ?id ;
      rdfs:label ?label .
  }
}
GROUP BY ?id
ORDER BY ?id
LIMIT 1001
```

### `recorded/ssis-sparql/data-elements-roles-c116977.json`: `query`

```sparql
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX owl: <http://www.w3.org/2002/07/owl#>
PREFIX dc: <http://purl.org/dc/elements/1.1/>
PREFIX mdr: <http://www.iso.org/11179/MDR#>
PREFIX cadsr: <http://cbiit.nci.nih.gov/caDSR#>
PREFIX ncit: <http://ncicb.nci.nih.gov/xml/owl/EVS/Thesaurus.owl#>
SELECT ?id (MIN(?label) AS ?name)
  (GROUP_CONCAT(DISTINCT CONCAT(ENCODE_FOR_URI(?useRole),"|",ENCODE_FOR_URI(STR(?concept)),"|",COALESCE(ENCODE_FOR_URI(STR(?order)),""));separator=" ") AS ?roles)
WHERE {
  VALUES ?concept { ncit:C116977 }
  GRAPH <http://cbiit.nci.nih.gov/caDSR> {
    VALUES ?membership { cadsr:main_concept cadsr:minor_concept }
    ?node ?membership ?concept .
    { ?element mdr:Object_Class ?node . BIND("objectClass" AS ?useRole) }
    UNION
    { ?element mdr:Property ?node . BIND("property" AS ?useRole) }
    UNION
    { ?value cadsr:has_concept ?node . ?element mdr:permitted_value ?value . BIND("valueMeaning" AS ?useRole) }
    OPTIONAL { ?node cadsr:display_order ?order }
    ?element cadsr:publicId ?id ;
      rdfs:label ?label .
  }
}
GROUP BY ?id
ORDER BY ?id
LIMIT 1001
```

### `recorded/ssis-sparql/data-elements-roles-c2991.json`: `query`

```sparql
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX owl: <http://www.w3.org/2002/07/owl#>
PREFIX dc: <http://purl.org/dc/elements/1.1/>
PREFIX mdr: <http://www.iso.org/11179/MDR#>
PREFIX cadsr: <http://cbiit.nci.nih.gov/caDSR#>
PREFIX ncit: <http://ncicb.nci.nih.gov/xml/owl/EVS/Thesaurus.owl#>
SELECT ?id (MIN(?label) AS ?name)
  (GROUP_CONCAT(DISTINCT CONCAT(ENCODE_FOR_URI(?useRole),"|",ENCODE_FOR_URI(STR(?concept)),"|",COALESCE(ENCODE_FOR_URI(STR(?order)),""));separator=" ") AS ?roles)
WHERE {
  VALUES ?concept { ncit:C2991 }
  GRAPH <http://cbiit.nci.nih.gov/caDSR> {
    VALUES ?membership { cadsr:main_concept cadsr:minor_concept }
    ?node ?membership ?concept .
    { ?element mdr:Object_Class ?node . BIND("objectClass" AS ?useRole) }
    UNION
    { ?element mdr:Property ?node . BIND("property" AS ?useRole) }
    UNION
    { ?value cadsr:has_concept ?node . ?element mdr:permitted_value ?value . BIND("valueMeaning" AS ?useRole) }
    OPTIONAL { ?node cadsr:display_order ?order }
    ?element cadsr:publicId ?id ;
      rdfs:label ?label .
  }
}
GROUP BY ?id
ORDER BY ?id
LIMIT 1001
```

### `recorded/ssis-sparql/data-elements-roles-c2991-descendants.json`: `query`

```sparql
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX owl: <http://www.w3.org/2002/07/owl#>
PREFIX dc: <http://purl.org/dc/elements/1.1/>
PREFIX mdr: <http://www.iso.org/11179/MDR#>
PREFIX cadsr: <http://cbiit.nci.nih.gov/caDSR#>
PREFIX ncit: <http://ncicb.nci.nih.gov/xml/owl/EVS/Thesaurus.owl#>
SELECT ?id (MIN(?label) AS ?name)
  (GROUP_CONCAT(DISTINCT CONCAT(ENCODE_FOR_URI(?useRole),"|",ENCODE_FOR_URI(STR(?concept)),"|",COALESCE(ENCODE_FOR_URI(STR(?order)),""));separator=" ") AS ?roles)
WHERE {
  GRAPH <http://ncicb.nci.nih.gov/xml/owl/EVS/Thesaurus.rdf> { ?concept rdfs:subClassOf* ncit:C2991 . }
  GRAPH <http://cbiit.nci.nih.gov/caDSR> {
    VALUES ?membership { cadsr:main_concept cadsr:minor_concept }
    ?node ?membership ?concept .
    { ?element mdr:Object_Class ?node . BIND("objectClass" AS ?useRole) }
    UNION
    { ?element mdr:Property ?node . BIND("property" AS ?useRole) }
    UNION
    { ?value cadsr:has_concept ?node . ?element mdr:permitted_value ?value . BIND("valueMeaning" AS ?useRole) }
    OPTIONAL { ?node cadsr:display_order ?order }
    ?element cadsr:publicId ?id ;
      rdfs:label ?label .
  }
}
GROUP BY ?id
ORDER BY ?id
LIMIT 1001
```

### `recorded/ssis-sparql/data-elements-roles-c4817.json`: `query`

```sparql
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX owl: <http://www.w3.org/2002/07/owl#>
PREFIX dc: <http://purl.org/dc/elements/1.1/>
PREFIX mdr: <http://www.iso.org/11179/MDR#>
PREFIX cadsr: <http://cbiit.nci.nih.gov/caDSR#>
PREFIX ncit: <http://ncicb.nci.nih.gov/xml/owl/EVS/Thesaurus.owl#>
SELECT ?id (MIN(?label) AS ?name)
  (GROUP_CONCAT(DISTINCT CONCAT(ENCODE_FOR_URI(?useRole),"|",ENCODE_FOR_URI(STR(?concept)),"|",COALESCE(ENCODE_FOR_URI(STR(?order)),""));separator=" ") AS ?roles)
WHERE {
  VALUES ?concept { ncit:C4817 }
  GRAPH <http://cbiit.nci.nih.gov/caDSR> {
    VALUES ?membership { cadsr:main_concept cadsr:minor_concept }
    ?node ?membership ?concept .
    { ?element mdr:Object_Class ?node . BIND("objectClass" AS ?useRole) }
    UNION
    { ?element mdr:Property ?node . BIND("property" AS ?useRole) }
    UNION
    { ?value cadsr:has_concept ?node . ?element mdr:permitted_value ?value . BIND("valueMeaning" AS ?useRole) }
    OPTIONAL { ?node cadsr:display_order ?order }
    ?element cadsr:publicId ?id ;
      rdfs:label ?label .
  }
}
GROUP BY ?id
ORDER BY ?id
LIMIT 1001
```

### `recorded/ssis-sparql/data-elements-roles-c49508.json`: `query`

```sparql
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX owl: <http://www.w3.org/2002/07/owl#>
PREFIX dc: <http://purl.org/dc/elements/1.1/>
PREFIX mdr: <http://www.iso.org/11179/MDR#>
PREFIX cadsr: <http://cbiit.nci.nih.gov/caDSR#>
PREFIX ncit: <http://ncicb.nci.nih.gov/xml/owl/EVS/Thesaurus.owl#>
SELECT ?id (MIN(?label) AS ?name)
  (GROUP_CONCAT(DISTINCT CONCAT(ENCODE_FOR_URI(?useRole),"|",ENCODE_FOR_URI(STR(?concept)),"|",COALESCE(ENCODE_FOR_URI(STR(?order)),""));separator=" ") AS ?roles)
WHERE {
  VALUES ?concept { ncit:C49508 }
  GRAPH <http://cbiit.nci.nih.gov/caDSR> {
    VALUES ?membership { cadsr:main_concept cadsr:minor_concept }
    ?node ?membership ?concept .
    { ?element mdr:Object_Class ?node . BIND("objectClass" AS ?useRole) }
    UNION
    { ?element mdr:Property ?node . BIND("property" AS ?useRole) }
    UNION
    { ?value cadsr:has_concept ?node . ?element mdr:permitted_value ?value . BIND("valueMeaning" AS ?useRole) }
    OPTIONAL { ?node cadsr:display_order ?order }
    ?element cadsr:publicId ?id ;
      rdfs:label ?label .
  }
}
GROUP BY ?id
ORDER BY ?id
LIMIT 1001
```

### `recorded/ssis-sparql/data-elements-roles-c25332.json`: `query`

```sparql
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX owl: <http://www.w3.org/2002/07/owl#>
PREFIX dc: <http://purl.org/dc/elements/1.1/>
PREFIX mdr: <http://www.iso.org/11179/MDR#>
PREFIX cadsr: <http://cbiit.nci.nih.gov/caDSR#>
PREFIX ncit: <http://ncicb.nci.nih.gov/xml/owl/EVS/Thesaurus.owl#>
SELECT ?id (MIN(?label) AS ?name)
  (GROUP_CONCAT(DISTINCT CONCAT(ENCODE_FOR_URI(?useRole),"|",ENCODE_FOR_URI(STR(?concept)),"|",COALESCE(ENCODE_FOR_URI(STR(?order)),""));separator=" ") AS ?roles)
WHERE {
  VALUES ?concept { ncit:C25332 }
  GRAPH <http://cbiit.nci.nih.gov/caDSR> {
    VALUES ?membership { cadsr:main_concept cadsr:minor_concept }
    ?node ?membership ?concept .
    { ?element mdr:Object_Class ?node . BIND("objectClass" AS ?useRole) }
    UNION
    { ?element mdr:Property ?node . BIND("property" AS ?useRole) }
    UNION
    { ?value cadsr:has_concept ?node . ?element mdr:permitted_value ?value . BIND("valueMeaning" AS ?useRole) }
    OPTIONAL { ?node cadsr:display_order ?order }
    ?element cadsr:publicId ?id ;
      rdfs:label ?label .
  }
}
GROUP BY ?id
ORDER BY ?id
LIMIT 1001
```

### `recorded/ssis-sparql/data-elements-roles-c25332-descendants.json`: `query`

```sparql
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX owl: <http://www.w3.org/2002/07/owl#>
PREFIX dc: <http://purl.org/dc/elements/1.1/>
PREFIX mdr: <http://www.iso.org/11179/MDR#>
PREFIX cadsr: <http://cbiit.nci.nih.gov/caDSR#>
PREFIX ncit: <http://ncicb.nci.nih.gov/xml/owl/EVS/Thesaurus.owl#>
SELECT ?id (MIN(?label) AS ?name)
  (GROUP_CONCAT(DISTINCT CONCAT(ENCODE_FOR_URI(?useRole),"|",ENCODE_FOR_URI(STR(?concept)),"|",COALESCE(ENCODE_FOR_URI(STR(?order)),""));separator=" ") AS ?roles)
WHERE {
  GRAPH <http://ncicb.nci.nih.gov/xml/owl/EVS/Thesaurus.rdf> { ?concept rdfs:subClassOf* ncit:C25332 . }
  GRAPH <http://cbiit.nci.nih.gov/caDSR> {
    VALUES ?membership { cadsr:main_concept cadsr:minor_concept }
    ?node ?membership ?concept .
    { ?element mdr:Object_Class ?node . BIND("objectClass" AS ?useRole) }
    UNION
    { ?element mdr:Property ?node . BIND("property" AS ?useRole) }
    UNION
    { ?value cadsr:has_concept ?node . ?element mdr:permitted_value ?value . BIND("valueMeaning" AS ?useRole) }
    OPTIONAL { ?node cadsr:display_order ?order }
    ?element cadsr:publicId ?id ;
      rdfs:label ?label .
  }
}
GROUP BY ?id
ORDER BY ?id
LIMIT 1001
```

### `recorded/ssis-sparql/query-refused.json`: `query`

```sparql
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX owl: <http://www.w3.org/2002/07/owl#>
PREFIX dc: <http://purl.org/dc/elements/1.1/>
PREFIX mdr: <http://www.iso.org/11179/MDR#>
PREFIX cadsr: <http://cbiit.nci.nih.gov/caDSR#>
PREFIX ncit: <http://ncicb.nci.nih.gov/xml/owl/EVS/Thesaurus.owl#>
SELECT ?concept
WHERE {
  GRAPH <http://ncicb.nci.nih.gov/xml/owl/EVS/Thesaurus.rdf> {
    ?concept rdfs:subClassOf ?parent
      OPTION (TRANSITIVE, t_distinct, t_in(?concept), t_out(?parent), t_max(3)) .
    FILTER (?parent = ncit:C17357)
  }
}
```

### `recorded/ssis-sparql/graph-identities-direct.json`: `body`

```sparql
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX owl: <http://www.w3.org/2002/07/owl#>
PREFIX dc: <http://purl.org/dc/elements/1.1/>
PREFIX mdr: <http://www.iso.org/11179/MDR#>
PREFIX cadsr: <http://cbiit.nci.nih.gov/caDSR#>
PREFIX ncit: <http://ncicb.nci.nih.gov/xml/owl/EVS/Thesaurus.owl#>
SELECT ?graph ?version ?date
WHERE {
  VALUES ?graph { <http://ncicb.nci.nih.gov/xml/owl/EVS/Thesaurus.rdf> <http://cbiit.nci.nih.gov/caDSR> }
  GRAPH ?graph {
    ?ontology dc:date ?date .
    OPTIONAL { ?ontology owl:versionInfo ?version }
  }
}
```

### `crafted/ssis-sparql/values-of-2200604-optional.json`: `query`

```sparql
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX owl: <http://www.w3.org/2002/07/owl#>
PREFIX dc: <http://purl.org/dc/elements/1.1/>
PREFIX mdr: <http://www.iso.org/11179/MDR#>
PREFIX cadsr: <http://cbiit.nci.nih.gov/caDSR#>
PREFIX ncit: <http://ncicb.nci.nih.gov/xml/owl/EVS/Thesaurus.owl#>
SELECT DISTINCT ?version ?value ?concept ?role
WHERE {
  GRAPH <http://cbiit.nci.nih.gov/caDSR> {
    ?element cadsr:publicId "2200604" ;
      mdr:version ?version .
    OPTIONAL {
      ?element mdr:permitted_value ?pv .
      ?pv mdr:value ?value .
      OPTIONAL {
        VALUES ?role { cadsr:main_concept }
        ?pv cadsr:has_concept ?node .
        ?node ?role ?concept .
      }
    }
  }
}
ORDER BY ?version ?value ?role ?concept
LIMIT 1001
```

### `crafted/ssis-sparql/graph-identities-bounded.json`: `query`

```sparql
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX owl: <http://www.w3.org/2002/07/owl#>
PREFIX dc: <http://purl.org/dc/elements/1.1/>
PREFIX mdr: <http://www.iso.org/11179/MDR#>
PREFIX cadsr: <http://cbiit.nci.nih.gov/caDSR#>
PREFIX ncit: <http://ncicb.nci.nih.gov/xml/owl/EVS/Thesaurus.owl#>
SELECT ?graph ?version ?date
WHERE {
  VALUES ?graph { <http://ncicb.nci.nih.gov/xml/owl/EVS/Thesaurus.rdf> <http://cbiit.nci.nih.gov/caDSR> }
  GRAPH ?graph {
    ?ontology dc:date ?date .
    OPTIONAL { ?ontology owl:versionInfo ?version }
  }
}
LIMIT 3
```

### `crafted/ssis-sparql/values-c17357-bounded.json`: `query`

```sparql
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX owl: <http://www.w3.org/2002/07/owl#>
PREFIX dc: <http://purl.org/dc/elements/1.1/>
PREFIX mdr: <http://www.iso.org/11179/MDR#>
PREFIX cadsr: <http://cbiit.nci.nih.gov/caDSR#>
PREFIX ncit: <http://ncicb.nci.nih.gov/xml/owl/EVS/Thesaurus.owl#>
SELECT DISTINCT ?id ?version ?value ?concept
WHERE {
  VALUES ?concept { ncit:C17357 }
  GRAPH <http://cbiit.nci.nih.gov/caDSR> {
    VALUES ?role { cadsr:main_concept cadsr:minor_concept }
    ?node ?role ?concept .
    ?pv cadsr:has_concept ?node ;
      mdr:value ?value .
    ?element mdr:permitted_value ?pv ;
      cadsr:publicId ?id ;
      mdr:version ?version .
  }
}
ORDER BY ?id ?version ?value ?concept
LIMIT 984
```

### `crafted/ssis-sparql/values-c17357-descendants-bounded.json`: `query`

```sparql
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX owl: <http://www.w3.org/2002/07/owl#>
PREFIX dc: <http://purl.org/dc/elements/1.1/>
PREFIX mdr: <http://www.iso.org/11179/MDR#>
PREFIX cadsr: <http://cbiit.nci.nih.gov/caDSR#>
PREFIX ncit: <http://ncicb.nci.nih.gov/xml/owl/EVS/Thesaurus.owl#>
SELECT DISTINCT ?id ?version ?value ?concept
WHERE {
  GRAPH <http://ncicb.nci.nih.gov/xml/owl/EVS/Thesaurus.rdf> {
    ?concept rdfs:subClassOf* ncit:C17357 .
  }
  GRAPH <http://cbiit.nci.nih.gov/caDSR> {
    VALUES ?role { cadsr:main_concept cadsr:minor_concept }
    ?node ?role ?concept .
    ?pv cadsr:has_concept ?node ;
      mdr:value ?value .
    ?element mdr:permitted_value ?pv ;
      cadsr:publicId ?id ;
      mdr:version ?version .
  }
}
ORDER BY ?id ?version ?value ?concept
LIMIT 974
```

### `crafted/ssis-sparql/values-c4817-bounded.json`: `query`

```sparql
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX owl: <http://www.w3.org/2002/07/owl#>
PREFIX dc: <http://purl.org/dc/elements/1.1/>
PREFIX mdr: <http://www.iso.org/11179/MDR#>
PREFIX cadsr: <http://cbiit.nci.nih.gov/caDSR#>
PREFIX ncit: <http://ncicb.nci.nih.gov/xml/owl/EVS/Thesaurus.owl#>
SELECT DISTINCT ?id ?version ?value ?concept
WHERE {
  VALUES ?concept { ncit:C4817 }
  GRAPH <http://cbiit.nci.nih.gov/caDSR> {
    VALUES ?role { cadsr:main_concept cadsr:minor_concept }
    ?node ?role ?concept .
    ?pv cadsr:has_concept ?node ;
      mdr:value ?value .
    ?element mdr:permitted_value ?pv ;
      cadsr:publicId ?id ;
      mdr:version ?version .
  }
}
ORDER BY ?id ?version ?value ?concept
LIMIT 965
```

## Concept requests answered by rule

One recording per concept answers each projection (`include`) and relation list of that concept
it covers, and a batch is composed from the recordings of the concepts it names
(`acceptance/src/nci_sip_acceptance/concepts.py`).

| Operations | Form |
|---|---|
| OP-E05 | `GET /api/v1/concept/{terminology}_{release}/{code}?include=…` |
| OP-E07 | `GET /api/v1/concept/{terminology}_{release}?list=…&include=…`, ≤ 1,000 codes |
| OP-E10, E11, E14 to E17, E20 | `GET /api/v1/concept/{terminology}_{release}/{code}/{relation}` |

## Scenarios

Each scenario provokes one case; its fixtures answer before the ordinary
ones while a test selects it. A recorded fixture is what the service answers today. A crafted one
stands in for a case the service does not produce on demand, under the requirement it names, and
answers the ordinary forms above.

### `cadsr/omitted-citation-fields`

Crafted from recorded 2200604 by omitting each cited item's dateModified; explicit-null endDate retained. Original recording bytes unchanged.. Crafted, 1 fixture, for get_data_element-1: derived omissions; explicit null endDate retained from recording.

### `ssis/missing-order`

Crafted omission of order from a newly recorded role tuple; no primary or order is inferred.. Crafted, 1 fixture, for find_data_elements_for_concept-1: crafted missing order from a new live recording.

### `cadsr/primary-inversion`

Crafted from issue #367's live observation of CDE 2178439: To a large extent has API primary C25332 while the graph calls qualifier C49508 main. Other metadata and EVS names and NCI_CONCEPT_CODE source labels are synthetic; this is not a recording.. Crafted, 4 fixtures, for get_concept_for_permissible_value-1: #367 observed API primary C25332, graph main C49508.

### `cadsr/concept-sources`

Contract-crafted from issue #368's live source/code observations (LOINC, UMLS and compound NCIt); all surrounding metadata, UMLS public id and stored compound value are synthetic. Not a capture of 5024313 or 14940584. Recorded form 5406471's LOINC values belong to 5301153, not 5024313; 2200604 v1's UMLS reference is representation.. Crafted, 3 fixtures, for get_concept_for_permissible_value-1: #368 observed source/code; synthetic metadata.

### `injection/evs`

Hostile C4817 definition remains decoded source text. Crafted, 1 fixture, for X-29: source instructions remain data, never server-authored instructions.

### `injection/cadsr`

Hostile 2200604 definition remains decoded source text. Crafted, 1 fixture, for X-29: source instructions remain data, never server-authored instructions.

### `injection/cross-domain`

Hostile C17357 data element name remains decoded source text. Crafted, 1 fixture, for X-29: source instructions remain data, never server-authored instructions.

### `injection/workflow`

Hostile C4817 permissible value remains decoded source text. Crafted, 1 fixture, for X-29: source instructions remain data, never server-authored instructions.

### `injection/evs-error`

HTTP 400 with hostile text in the EVS message field. Crafted, 1 fixture, for X-29: source instructions remain data, never server-authored instructions.

### `injection/cadsr-error`

HTTP 400 with hostile text in the caDSR message field. Crafted, 1 fixture, for X-29: source instructions remain data, never server-authored instructions.

### `injection/cross-domain-error`

HTTP 400 with hostile text in the cross-domain message field. Crafted, 1 fixture, for X-29: source instructions remain data, never server-authored instructions.

### `injection/workflow-error`

HTTP 400 with hostile text in the workflow message field. Crafted, 1 fixture, for X-29: source instructions remain data, never server-authored instructions.

### `injection/webmethods-error`

HTTP 200 with hostile text in apiResponse.message. Crafted, 1 fixture, for X-29: masked failure prose never becomes server-authored instructions.

### `injection/fhir-error`

HTTP 200 with hostile FHIR issue diagnostics and details text. Crafted, 1 fixture, for X-29: masked failure prose never becomes server-authored instructions.

### `release/unknown`

Every content path of `ncit_99.99z` answers 404. Recorded.

Each request: An unknown release answers 404 on every content path: the server fails closed.

| Operation | Request | Expected | Fixture |
|---|---|---|---|
| OP-E06 | `GET evs /api/v1/concept/ncit_99.99z/C4817?include=summary` | 404; every parameter ignored | `scenarios/release/unknown/concept.json` |
| OP-E06 | `GET evs /api/v1/concept/ncit_99.99z?list=C4817&include=summary` | 404; every parameter ignored | `scenarios/release/unknown/concepts.json` |
| OP-E06 | `GET evs /api/v1/concept/ncit_99.99z/C20197?include=summary` | 404; every parameter ignored | `scenarios/release/unknown/concept-c20197.json` |
| OP-E06 | `GET evs /api/v1/concept/ncit_99.99z/C4817/parents` | 404; every parameter ignored | `scenarios/release/unknown/parents.json` |
| OP-E06 | `GET evs /api/v1/concept/ncit_99.99z/C4817/children` | 404; every parameter ignored | `scenarios/release/unknown/children.json` |
| OP-E06 | `GET evs /api/v1/concept/ncit_99.99z/C4817/roles` | 404; every parameter ignored | `scenarios/release/unknown/roles.json` |
| OP-E06 | `GET evs /api/v1/concept/ncit_99.99z/C4817/inverseRoles` | 404; every parameter ignored | `scenarios/release/unknown/inverse-roles.json` |
| OP-E06 | `GET evs /api/v1/concept/ncit_99.99z/C4817/associations` | 404; every parameter ignored | `scenarios/release/unknown/associations.json` |
| OP-E06 | `GET evs /api/v1/concept/ncit_99.99z/C4817/inverseAssociations` | 404; every parameter ignored | `scenarios/release/unknown/inverse-associations.json` |
| OP-E06 | `GET evs /api/v1/concept/ncit_99.99z/C4817/maps` | 404; every parameter ignored | `scenarios/release/unknown/maps.json` |
| OP-E06 | `GET evs /api/v1/concept/ncit_99.99z/search?term=ewing sarcoma&type=contains&include=minimal,highlights&fromRecord=0&pageSize=10` | 404; every parameter ignored | `scenarios/release/unknown/search.json` |
| OP-E06 | `GET evs /api/v1/concept/ncit_99.99z/C4817/pathsToRoot?include=minimal` | 404; every parameter ignored | `scenarios/release/unknown/paths-to-root.json` |
| OP-E06 | `GET evs /api/v1/metadata/ncit_99.99z/roles` | 404; every parameter ignored | `scenarios/release/unknown/role-catalogue.json` |
| OP-E06 | `GET evs /api/v1/metadata/ncit_99.99z/associations` | 404; every parameter ignored | `scenarios/release/unknown/association-catalogue.json` |
| OP-E06 | `GET evs /api/v1/history/ncit_99.99z/C154421/replacements` | 404; every parameter ignored | `scenarios/release/unknown/replacement.json` |
| OP-E06 | `GET evs /api/v1/history/ncit_99.99z/replacements?list=C154421` | 404; every parameter ignored | `scenarios/release/unknown/replacements.json` |
| OP-E06 | `GET evs /api/v1/history/ncit_99.99z/C4817/replacements` | 404; every parameter ignored | `scenarios/release/unknown/replacement-active.json` |
| OP-E06 | `GET evs /api/v1/subset/ncit_99.99z/C177537` | 404; every parameter ignored | `scenarios/release/unknown/subset.json` |
| OP-E06 | `GET evs /api/v1/concept/ncit_99.99z/subsetMembers/C177537?fromRecord=0&pageSize=10&include=minimal` | 404; every parameter ignored | `scenarios/release/unknown/subset-members.json` |
| OP-E06 | `GET evs /api/v1/concept/ncit_99.99z/C4817/descendants?maxLevel=4` | 404; every parameter ignored | `scenarios/release/unknown/descendants.json` |
| OP-E06 | `GET evs /api/v1/metadata/ncit_99.99z/properties` | 404; every parameter ignored | `scenarios/release/unknown/property-catalogue.json` |

### `release/mismatch`

Every payload that reports the pinned release names 26.08e instead, the unpinned forms included; release discovery is left as recorded. Crafted, 186 fixtures, for A3.4: the content served names another release than the one requested.

### `release/two-latest`

A weekly and a monthly row are both `latest`, the weekly first in every list; each form of the release query gives a channel the same release. Crafted, 4 fixtures, for A3.6.1-A3.6.3: two releases carry latest at once, one per channel.

### `release/duplicate-tag`

Two releases are both latest with the monthly tag; the monthly query and the listing name both. Crafted, 2 fixtures, for A3.6.3: a release with duplicate tags fails closed.

### `release/moving-session`

The monthly query changes after its first read; both releases serve the session test concept, so implicit re-resolution and explicit overrides are distinguishable. Crafted, 3 fixtures, for X-22.

### `release/withdrawn-session`

The monthly query changes after its first read and the original concept path then answers terminology-not-found; a session must fail closed instead of adopting the new release. Crafted, 3 fixtures, for X-22.

### `batch/silent-drop`

A code EVS does not know is left out of a batch. Recorded.

| Operation | Request | Expected | Rationale | Fixture |
|---|---|---|---|---|
| OP-E07 | `GET evs /api/v1/concept/ncit_26.09d/C999999999?include=full` | 404 | A code EVS does not know: a batch leaves it out without a signal (E-5). | `scenarios/batch/silent-drop/concepts/C999999999.json` |

### `retired/with-replacement`

C154421 is retired with a replacement, and one bad code fails a batch. Recorded.

| Operation | Request | Expected | Rationale | Fixture |
|---|---|---|---|---|
| OP-E05 | `GET evs /api/v1/concept/ncit_26.09d/C154421?include=full` | 200 | A retired concept (A8). | `scenarios/retired/with-replacement/concepts/C154421.json` |
| OP-E21 | `GET evs /api/v1/history/ncit_26.09d/C154421/replacements` | 200 | A retired code with its replacement. | `scenarios/retired/with-replacement/replacement.json` |
| OP-E21 | `GET evs /api/v1/history/ncit_26.09d/replacements?list=C154421` | 200 | The batch form for the retired code. | `scenarios/retired/with-replacement/replacements.json` |
| OP-E21 | `GET evs /api/v1/history/ncit_26.09d/replacements?list=C154421,C4817` | 200 | A batch of a retired and an active code, answered per code. | `scenarios/retired/with-replacement/replacements-with-active.json` |
| OP-E21 | `GET evs /api/v1/history/ncit_26.09d/replacements?list=C154421,CBOGUS999999` | 404 | One unknown code fails the whole batch (404). | `scenarios/retired/with-replacement/replacements-with-unknown.json` |

### `traversal/deep-fanout`

One root has 1,001 children, and a chain runs deeper than depth 4 from its first child, C99000001 (the node maximum is reached at depth 1 from the root). Crafted, 1,007 fixtures, for A5.1-A5.4: descendants beyond the depth and node bounds.

### `traversal/exclusions`

Exclusion roles are named like positive ones, and two the other way. Crafted, 2 fixtures, for A5.6, A5.7, E-4: polarity by relationship code, not by name; and expand_cohort-1: an R135 role from C4817 to its first child, invented to show a cohort withholding a code, since C4817's real exclusion roles point outside its subtree.

### `traversal/starvation`

One concept has 300 roles and 2 associations, and another 300 associations and 2 roles, to the same targets. Crafted, 304 fixtures, for A5.5: a budget per relationship kind; no kind starved.

### `valueset/inactive-members`

Two members of value set C85492 are marked inactive (FHIR contains.inactive), in both forms of its expansion. Crafted, 2 fixtures, for A8.3: activeOnly leaves out the members an expansion marks inactive. EVS was never seen to mark one, so against live EVS activeOnly cannot be shown to do anything (an upstream question, #42).

### `relationships/exclusion-missing`

The role catalogue lacks R135, a code of the exclusion set. Crafted, 1 fixture, for A5.7: a release whose catalogue lacks R135, of the exclusion set, fails closed.

### `upstream/unavailable`

Every upstream request, whatever its surface and path, gets a closed connection, then 503, then no answer within the timeout. Crafted, 7 fixtures, for A2.5, A5.3: bounded retries, counted, then a structured error.

### `upstream/rate-limited`

429 with `Retry-After` on the release query, then the answer. Crafted, 2 fixtures, for E-7, P-1: back-off honoured and counted.

### `license/restricted`

403 without the licence key; invented content with it. Recorded. Crafted, 10 fixtures, for A7.5: EVS refuses every request for mdr without the licence key. Crafted, 11 fixtures, for E-7, A7.5: the licence key sent from configuration.

| Operation | Request | Expected | Rationale | Fixture |
|---|---|---|---|---|
| A7.5 | `GET evs /api/v1/concept/mdr_29_0/10000000?include=summary` | 403; every parameter ignored | EVS refuses mdr without the X-EVSRESTAPI-License-Key header (403). | `scenarios/license/restricted/refused.json` |

### `release/graph-behind`

The Shared SI Service's NCIt graph names another release; EVS serves the one pinned. Crafted, 2 fixtures, for A3.4: the content served names another release than the one requested.

### `release/concept-behind`

EVS serves C4817 of another release; the NCIt graph names the one pinned. Crafted, 1 fixture, for A3.4: the content served names another release than the one requested.

### `release/graph-version-missing`

Both graph identities and dates remain; only the optional NCIt version is omitted. Crafted, 2 fixtures, for find_data_elements_for_concept-4: only expansion requires the NCIt graph version.

### `search/first-not-named`

The lexical search for "ewing sarcoma" with Disease or Disorder (C2991) put first, an invented order. Crafted, 1 fixture, for ground_value-3: a search whose first result is not the concept named like the text.

### `license/attributed`

The invented licensed content of license/restricted, each concept carrying the terminology's licence text as EVS is asked to send it (#42). Crafted, 11 fixtures, for A7.3, X-19: the licence text given with the content, in `licenseText` on each concept, as asked of EVS (#42).

### `cadsr/with-registry-release`

caDSR publishes a registry release: /registry/releases names one, and a data element asked with it is answered with the release echoed (C-1). Crafted, 3 fixtures, for C-1: a published registry release, named in every answer and accepted on every content call.

### `cadsr/credentialed`

The server holds caDSR credentials: contexts and CDE Match answer to the contracts, where the API refuses an anonymous caller (401). Crafted, 1 fixture, for OP-M01, A9.3: CDE Match to its 2.0 contract, which refuses an anonymous caller since 3 October 2026 at the latest (401, recorded/cadsr/cde-match-refused*.json); the array the call of 10 September sent. Crafted, 1 fixture, for OP-M01, A9.3: CDE Match to its 2.0 contract for a second entity; the array the call of 10 September sent. Crafted, 1 fixture, for OP-M01, A9.3: CDE Match to its 2.0 contract for a second entity. Crafted, 1 fixture, for OP-M01, A9.3: CDE Match to its 2.0 contract for an entity no data element matches; the array the call of 10 September sent. Crafted, 1 fixture, for OP-M01, A9.3: CDE Match to its 2.0 contract for an entity no data element matches. Crafted, 1 fixture, for OP-M01, A9.3: CDE Match to its 2.0 contract, which refuses an anonymous caller since 3 October 2026 at the latest (401, recorded/cadsr/cde-match-refused*.json). Crafted, 1 fixture, for OP-C13, A9.3: the context list to the lists-of-values contract, which refuses an anonymous caller (401, recorded/cadsr/context-names-refused.json).

### `cadsr/match-timeout`

vmMatch answers later than the match timeout the scenario sets. Crafted, 1 fixture, for A2.5: matching slower than its declared timeout is a timeout error; the array the call of 10 September sent. Crafted, 2 fixtures, for A2.5: matching slower than its declared timeout is a timeout error.

### `cadsr/html-for-json`

A data element asked for with Accept: application/json is answered with the HTML caDSR sends to a request without it. Crafted, 1 fixture, for X-15: HTML where JSON was asked for is an upstream error.

### `upstream/masked-error`

Every request to the Shared SI façade and to the caDSR API is answered with HTTP 200 and an apiResponse of type E, the failure each sends inside a success. Crafted, 2 fixtures, for X-15: an error envelope in an HTTP 200 is an upstream error.

### `ssis/distinct-cap`

Synthetic 1001 distinct public IDs exercise the sentinel independently of incomplete historical Disease captures (981 exact and 998 descendant distinct IDs from 1001 rows).. Crafted, 2 fixtures, for find_data_elements_for_concept-5 and ground_value-4: synthetic distinct-key sentinel.

### `ssis/merged-versions`

Crafted public-ID associations from a merged-version CDE node; no version assignment.. Crafted, 4 fixtures, for find_data_elements_for_concept-1 and ground_value-2: synthetic merged-version association.

### `ssis/query-rejected`

Every SPARQL query is refused by the Shared SI Service's inspection layer: HTTP 403 with an HTML body. Crafted, 1 fixture, for X-15: HTML where JSON was asked for is an upstream error.
