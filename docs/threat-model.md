# Content-injection threat model

The NCI Semantic Infrastructure Program MCP server retrieves governed content; it contains no LLM.
An assistant consuming its results may nevertheless act on hostile instructions in a concept
definition, CDE name, question text or permissible value. Structured fields are not sanitisation.
The server preserves upstream-managed content rather than detecting, rewriting or removing it.

See also: [transport and sessions](transport.md), [governed HTTP](governed-http.md),
[container supply chain](container.md), [installer source and local state](installer-targets.md),
and [local evidence files](evidence-contract.md).

## Assets and trust boundaries

- **Upstream → server:** EVS, caDSR and Shared SI supply content. The NCIt index holds snapshots
  of a recorded EVS release, not a new authority. Registrar-authored text remains untrusted as
  instructions even when its terminology identity and release are valid.
- **Server → client host/model:** tools and resources return structured data and JSON text;
  the host decides how to present it to its model and which subsequent actions to allow.
- **Caller → upstream:** caller free text enters encoded parameter values or body fields.
  A prompt substitutes caller arguments literally; that is distinct from the server adding
  upstream content to its fixed instructions.
- **Operator → process/network:** configuration chooses destinations, credentials and the served
  profile. The operator controls the installed package and local index. Governed HTTP adds
  verified caller policy; trusted-local mode relies on the local process boundary.

Assets include faithful content and provenance, discovery metadata and prompt templates,
upstream credentials, caller queries, and the authority of other tools available to the host.

## Threats, controls and evidence

| Threat | Implemented control and location | Owning evidence | Residual risk / owner |
|---|---|---|---|
| Instructions embedded in returned content | Content adapters project source values into data fields; `server.py` serialises the same result as JSON. Every furnished `spec/prompts.yaml` template starts with the data-not-instructions paragraph (X-29). `http_client.py` uses standard HTTP status wording, and `upstream.py` describes masked error envelopes without their prose. Full body prose remains internal to domain classification; EVS may extract the caller's requested identifier into the existing structured error details, as X-29 allows. | `acceptance/tests/test_content_injection.py`: four crafted EVS, caDSR, cross-domain and workflow calls check decoded UTF-8 preservation, exact allowed paths, JSON mirror and unchanged instruction channels. `tests/test_content_injection.py` checks exact MCP error records and diagnostics with hostile HTTP message, webMethods message and FHIR issue fields. | A model can still obey data. The host must preserve the trust boundary across retrieval, summarisation and later calls. These deterministic checks do not measure model obedience. |
| Tool/description poisoning | Tool schemas come from the installed registry/handler signatures, not upstream text; prompt templates are packaged specification data. | P-6 in `acceptance/tests/test_protocol.py` checks exact discovery stability; X-29 checks exact tool/schema and prompt discovery/rendering before and after hostile calls. P-8 checks exact rendered specification text; `tests/test_prompts.py` checks the independent paragraph and spec/package equality. | A compromised package, repository or installation can change instructions. Operators review and pin trusted releases; hosts review discovery metadata before granting authority. |
| Caller text becomes query syntax or instructions | A7.6/A7.7 validation and encoding in `validation.py`, `http_client.py` and `ssis.py`; prompt substitution does not recursively interpret braces. | X-23/X-24 in `acceptance/tests/test_crosscutting.py`; `tests/test_ssis_client.py`; literal caller arguments in `tests/test_prompts.py`. | Encoding is not confidentiality. Queries reach configured upstreams, and caller-authored prompt arguments may themselves be hostile. The host owns sensitive-input handling. |
| Credential theft or attacker-selected egress | `HttpClient` binds credential headers to its origin, refuses cross-origin redirects and redacts credentials. Tools expose read operations rather than arbitrary network fetching; configured base URLs are operator-controlled. | `tests/test_http_client.py` credential-origin/redirect contracts; X-12 in `acceptance/tests/test_crosscutting.py`. | An authorised query still sends its text upstream. Operators own endpoint configuration, proxy policy, secret storage and network egress restrictions. |
| Read-only results induce consequential actions elsewhere | Read-only annotations describe this surface; optional caller policy narrows discovery and execution, including composed child operations. | P-10 in `acceptance/tests/test_protocol.py`; X-25–X-28 in `acceptance/tests/test_permissions.py`, with implementation contracts in `tests/test_permissions.py`. | Read-only is not harmless: returned text could persuade a host to write files, send data or invoke another server. The host owns cross-server permissions and human confirmation for consequential actions. |

No instruction detector or server-added instruction marker is implemented. Exact preservation
means equality of decoded UTF-8 strings, including whitespace and Unicode, not HTTP wire bytes.
The JSON text mirror is an allowed representation of the same data. The tests protect the four
crafted projection paths and shared HTTP/envelope error wording; newly added projections need their own
contract review. A prompt paragraph is guidance, not an enforcement boundary against an LLM.

## Client-host controls and sources

Keep tool/resource text in an untrusted-data channel; never promote it to system instructions.
Apply least privilege across servers, isolate tool execution and secrets, restrict egress, and
require human confirmation before consequential side effects or sensitive-data disclosure.
Review descriptions when connecting or updating a server. These are host recommendations,
not controls this read-only server can enforce on other servers.

This model follows the indirect-injection threat described by
[OWASP LLM01](https://genai.owasp.org/llmrisk/llm01-prompt-injection/) and the boundaries in
[MCP security best practices](https://modelcontextprotocol.io/specification/2025-11-25/basic/security_best_practices).
The [MCP tool guidance](https://modelcontextprotocol.io/specification/2025-11-25/server/tools#security-considerations)
places consent and access controls with the host. The repository's binding behavior is
[X-29 and the existing conventions](specification.md), not a claim of general prompt-injection immunity.
