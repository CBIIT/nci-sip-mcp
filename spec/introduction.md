# Specification of the required MCP tools and their behavior

Generated from `spec/` by `pdm run spec-render`; do not edit by hand.

This is the specification of the required MCP tools and their behavior: the source of record for
what the tools do. Its source is data: the conventions
(`spec/conventions.yaml`), the tools (`spec/tools.yaml`), the resources and prompts
(`spec/resources.yaml`, `spec/prompts.yaml`) and the requirements (`spec/requirements.yaml`).
The server implements the tools, and the acceptance suite tests the requirements, each test
citing the requirements it enforces by id.

## What the server must do

The NCI Semantic Infrastructure Program MCP server gives AI assistants and applications access to NCI
terminology and metadata through the Model Context Protocol. The sections below hold the
requirements; this is what they add up to.

- **EVS tools:** look up, search and traverse the NCI Thesaurus: concepts, hierarchy, roles,
  associations and mapsets.
- **caDSR tools:** find and read common data elements, forms, value domains and permissible
  values, and match text to data elements and value meanings.
- **Cross-domain and workflow tools:** join the two, such as the data elements that use a concept
  or the concept a permissible value means, and compose common sequences, such as grounding a
  value or harmonising a data dictionary.
- **Release pinning and provenance:** answer from one named EVS release and caDSR registry
  state, and say in every result where each value came from.
- **Transports and secured mode:** serve over stdio and streamable HTTP, with a secured mode that
  authenticates callers and can only narrow the tools they reach.
- **Auditability:** one completion record per call, with its correlation id, and no secret in a
  result or a log.

The acceptance suite (section 5) is the measure: a tool is accepted when its tests pass.

### What belongs in the MCP layer, and what in the systems behind it

- **The MCP layer may** shape requests and results for the protocol, validate input, bound and
  page results, pin releases and record provenance, correlate and audit calls, map errors to one
  closed set, cache under a stated policy, secure access, and orchestrate calls whose results it
  bundles as they were returned. For example, an NCIt call that names no release is pinned to
  the current one, and the result says which.
- **The MCP layer must not** create, infer, default or backfill content the source system
  manages, or rebuild a capability the source system lacks. For example, a caDSR search filter
  that the keyword search does not serve is refused, not applied to the results afterwards.
- **A gap belongs to the source system.** A missing field, a missing capability, a failure masked
  as a success, or no explicit signal that something is absent is reported to the source system
  and stays visible to the caller; it is not worked around in the layer. For example, caDSR
  publishes no registry release identifier, so a result names none. The concrete requests are in
  the [upstream requirements](../docs/upstream/README.md).

### The local NCIt index: the one interim exception

Semantic search over the NCI Thesaurus is not available from EVS today, so the server provides
it from a local index. It is the one stated exception to "the MCP layer must not rebuild a
capability the source system lacks", and it is interim:

- **Built from EVS releases.** The index holds one release EVS published, pinned and recorded, so
  every result names the release it came from. It invents no content.
- **Meant for the source system.** Semantic search is intended to move into EVS and be maintained
  there; the server would then call it like any other upstream capability
  ([the upstream request](../docs/upstream/evs.md#evs-semantic)).
- **Always labelled.** A result says that it comes from the local index and which release the index
  holds. The index never stands in silently for a live answer.

The index's build, activation and rollback are described in the
[architecture](../ARCHITECTURE.md#index-lifecycle). The [migration guide](../docs/index-migration.md)
describes its read-only boundary and the future handover to EVS; the hosted service and switch
are not implemented, and any future provenance change requires explicit specification agreement.
