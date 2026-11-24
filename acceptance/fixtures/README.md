# Fixtures

The upstream responses the suite runs against in fixture mode.
Each is a JSON file in the format `src/nci_sip_acceptance/fixture_server.py` describes, and states
whether it was **recorded** from a live service (and when) or **crafted** for a case the live
service does not produce on demand (and which requirement it stands in for).

- `manifest.yaml`: the pinned release (NCIt `ncit_26.09d`), the concept rules, what each
  scenario provokes, every request `pdm run acceptance-record` records, with its operation and
  rationale, and the digest of the crafted fixtures (`crafted:`).
- `recorded/<surface>/`: captured from the live service by `acceptance-record` (EVS and its FHIR
  surface, the caDSR APIs and the caDSR export folder, the Shared SI façade and SPARQL
  endpoint); concepts in `recorded/evs/concepts/`.
- `crafted/<requirement>/`: the form a requirement prescribes where the service does not answer
  it yet, carrying a recording's answer (`acceptance-record`, the manifest's `derived`), or
  crafted outright, in memory, by `craft.py` (for example caDSR's capped keyword search).
- `scenarios/<group>/<name>/`: the fixtures of one scenario, which answer before the ordinary
  ones while a test selects it (`@pytest.mark.scenario("<group>/<name>")`). A `settings.json`
  there holds the `NCI_SIP_*` settings the scenario's server process starts with. Only recorded
  scenario fixtures are files; the crafted ones are built in memory.

## Scenarios

The scenario fixtures are recorded from live where EVS produces the
case on demand (`acceptance-record`) and crafted otherwise by `src/nci_sip_acceptance/craft.py`,
from the recorded set where they change a real answer. Crafted fixtures are not files: they are
built in memory whenever the set loads, and the manifest's `crafted:` line holds the digest of
what `craft.py` makes now, so a change to them shows in review as that one line. To review a
change to `craft.py`, write the fixtures before and after with `pdm run acceptance-craft --out
DIR` and compare the two directories. The manifest's `scenarios` section says what each
provokes; the register below lists them with how each is made and its requests.

## Recording

After `pdm run acceptance-record` or a change to `craft.py`, run `pdm run acceptance-craft` and
`pdm run acceptance-register`: the self-tests check that the crafted digest and the register are
current.
`acceptance-record` re-records the set from live and writes nothing unless every check
holds: the live monthly release is still the pinned one; every request answers with the status
its entry expects; every concept recording is one the fixture server accepts and the composed
concept answers equal real ones; every derived fixture comes from a recording of the pinned
release; and no file under `recorded/`, and no derived fixture, is left over. The set grows with
the tests: a test that needs another upstream answer adds its request to the manifest and
re-records. Re-pinning the release is a re-recording under change control. The fixture set is
versioned on its own: a re-recording or a re-pinning makes a new fixture-set version,
independent of the suite's. A full re-recording takes about three and a half minutes: caDSR
is slow (a concept query 24 s, the CRDC list 29 s), and the set leaves out what is slower still
(a classification scheme of 2008601's size took 149 s, a context query passed 180 s).

## Request forms

The fixture set constrains the form of an upstream request only where the specification
prescribes it: the release-pinned path, the batch endpoint in place of one request per concept,
the one-row release query, `Accept: application/json`, the licence key, the caDSR
credentials and the text of a SPARQL query. Elsewhere it answers any form a fixture records.

caDSR's own behaviour is in the ordinary fixtures, with no scenario of its own:

- **JSON only when asked for.** Every caDSR request names `Accept: application/json`, which the
  contracts prescribe (M3.2). Without it the API answers HTTP 200 with HTML: two paths are
  recorded both ways, and the fixture naming the most headers a request carries answers it, so
  a server that leaves the header out gets HTML, as from the live API.
- **No registry release.** The path the inventory names for it answers 404 (C-1);
  `cadsr/with-registry-release` crafts one.
- **Failures inside HTTP 200.** An unknown data element and a refusal of the arguments come
  back as HTTP 200, with `apiResponse` saying so (X-15).
- **Credentials.** The lists-of-values API and CDE Match refuse an anonymous caller (401,
  recorded). History: the owner's decision of 3 October 2026 established that
  `cadsr/credentialed` answers for a server that holds `NCI_SIP_CADSR_CREDENTIAL` with answers
  crafted from the published contracts, recorded under `recorded/cadsr-contracts/`. Current status: the project now has a staging
  credential, but protected access remains unverified after the 24 November 401s
  ([#383](https://github.com/CBIIT/nci-sip-mcp/issues/383#issuecomment-7139797516); direct HTTP, not an MCP acceptance run; earlier 23 November 401s: [#383](https://github.com/CBIIT/nci-sip-mcp/issues/383#issuecomment-7114203427)), so those
  fixtures remain crafted pending verification. The data
  elements and contexts in them are the recorded content's; what no recording can give (CDE
  Match's scores, its rule, marked "Crafted") is invented. A self-test holds each answer to its
  contract: its operation and base path, its answer's definition with no field the definition
  does not name, its request body, and the basic authentication the contract requires. Their
  content evidence is fixture-only. The current suite does not mark these content tests
  live-capable, so its combined PASS table is not live verification; see the
  [validation evidence](../../docs/benchmark.md). With credentials the recorder records the answers instead,
  taking the credential from the operator's environment and writing it into no fixture.

The Shared SI Service's behaviour is in the ordinary fixtures too:

- **The same question on each surface.** A concept's data elements are recorded from the caDSR
  API, and from SPARQL over the caDSR graph, the export of 1 July 2026; the answers differ, and
  a test checks an answer against the surface its provenance names, never one surface against
  another. The façade has no operation keyed by a concept code.
- **The query text is the form.** A SPARQL query is a form-encoded POST whose text the register
  publishes; the fixture server matches it with runs of whitespace collapsed. A body is a form
  only where its content type says so, as the endpoint reads it, and a direct POST
  (`application/sparql-query`) gets the 403 the endpoint answered it with, recorded.
- **Failures inside HTTP 200.** The façade answers a missing argument with apiResponse type E or
  I, and HTML without `Accept: application/json`, all recorded; `upstream/masked-error` serves
  the failure to every façade and caDSR request, and `ssis/query-rejected` the inspection
  layer's HTML 403 to every query (X-15).
- **Blank nodes.** Permissible values and concept links are blank nodes, whose labels change with
  each load; a test identifies a data element by public id and version, a value by its text.

The register of request forms, generated from the manifest, is written for the service teams, in
a view for each team (EVS, caDSR, the Shared SI Service) and one for all:
[`../request-forms/`](../request-forms/). It opens with the operations without a pinned form
upstream, with the release each payload reports; then lists every ordinary request with the
platform operation it serves (its `OP-` id, from `operations.yaml` of the programme's platform
conformance suite), whether it is recorded or crafted
(and for which requirement), and the reason for its form; then each scenario.
Where EVS does not yet answer the form a requirement prescribes, the ordinary fixture is crafted
to the requirement and names it. A live run exposes that gap only when the affected test is
live-capable; otherwise the live report records it as unrun.

Two kinds of request are answered whatever their form:

- **Concepts.** One recording per concept answers every projection (`include`) of that concept
  and each of its relation lists the recording covers, and every batch (`?list=`) is composed
  from the recordings of the concepts it names, by the rules of `src/nci_sip_acceptance/concepts.py`. A batch comes back in no particular order: EVS keeps none.
- **Ignored parameters.** A parameter the live service is shown to ignore is declared with the
  evidence and left out of the match (FHIR `$expand` ignores `count`, `offset` and `activeOnly`).

A request in any other form finds no fixture, and the test that sent it reports NO FIXTURE.
Two more things decide which fixture answers (`fixture_server.py` has the rules): a fixture may
name request headers it requires (a licence key), and while a test selects a scenario, the
scenario's fixtures answer before any ordinary one, `"*"` under `ignored` making one
answer whatever parameters are asked, and a scenario fixture whose path is `"*"` answering
every request of its surface that the scenario's other fixtures do not
(`upstream/unavailable`). A request the concept rules answer has no ordinary exact fixture:
the loader refuses one, since it would answer before a scenario's recording.

## Content and terms

The recorded content is as the EVS REST API (`api-evsrest.nci.nih.gov`) serves it publicly,
without a licence key, retrieved on 2 October 2026 from NCI Thesaurus™ release 26.09d, which NCI
releases under the Creative Commons Attribution 4.0 International licence
([terms of use](https://evs.nci.nih.gov/ftp1/NCI_Thesaurus/ThesaurusTermsofUse.htm)). The caDSR
content is as the caDSR APIs (`cadsrapi.cancer.gov`) and the export folder
(`cadsr.nci.nih.gov/ftp/caDSR_Downloads`) serve it without credentials, retrieved on
3 October 2026. Neither names terms of its own, so NCI's
[reuse policy](https://www.cancer.gov/policies/copyright-reuse) applies: text in NCI products
is free of copyright unless otherwise indicated, with NCI credited as the source. The code of
the suite is under the Apache License 2.0.
