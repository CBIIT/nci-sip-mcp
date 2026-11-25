# Container operation

See the [prerequisites](../QUICKSTART.md#platforms-and-prerequisites) for the tools a local build needs.

The server image is cloud-neutral and CPU-only, initially Linux amd64. Cloud One's
production design remains with the hosting team: no AWS credentials, roles, infrastructure
or identity provider are configured here. The default image **requires authentication** and
refuses startup without the approved identity integration configured through the
[transport hooks](transport.md#authentication-and-authorization-hooks).
The [deployment diagrams](deployment.md) show local stdio, a local container, and the proposed
Cloud One layout, including storage, network and session boundaries.

## Start with external assets

Copy a completed index directory to **local writable storage** before starting the server.
Preserve the whole SQLite database, including the active build and its retained predecessor;
use a consistent backup, not a copy of a live database without its journal/WAL. Do not share
the serving database over a network filesystem. SQLite opens transactions and may need
journal/WAL files beside `nci_si.sqlite3`; UID/GID 65532 must be able to write this directory.
Serving never builds or activates an index.

Supply the embedding model separately in a read-only Hugging Face cache mounted at
`/model-cache`. `NCI_SIP_EMBEDDING_MODEL` must retain the model identity in the index manifest,
not a new path to the same weights. If the index was built using a local model path, mount
the model at that same path, with content identical to the build's: the directory is hashed at
start-up and a difference refuses start-up (the
[hash definition](ncit-semantic-index.md#model-rationale-and-reproducibility-limits)).
A Hub model name loads the snapshot found in `/model-cache` as it is; the container never
refreshes it. The image sets `HF_HOME=/model-cache`, so snapshots sit under
`/model-cache/hub/`; leave `SENTENCE_TRANSFORMERS_HOME` unset, because setting it moves the
cache the model is looked up in. The snapshot's commit must equal the revision stored in the index: a refreshed
cache holding a newer commit refuses start-up, like a changed model directory (the container
exits with `container_startup_failed` for the `index` asset), so mount the snapshot the index
was built with. The image sets `HF_HUB_OFFLINE=1` and `TRANSFORMERS_OFFLINE=1`;
the container entry point additionally requests local files only. Missing or incompatible
assets fail startup with one JSON diagnostic identifying the asset and error class.

Use the digest recorded on the GitHub release, for example:

```bash
docker run --rm --read-only --stop-timeout 20 --cap-drop=ALL --security-opt=no-new-privileges \
  --tmpfs /tmp:rw,noexec,nosuid -p 127.0.0.1:8000:8000 \
  -v "$INDEX_DIR:/data" -v "$MODEL_CACHE:/model-cache:ro" \
  --env-file "$SERVER_ENV" "$IMAGE_DIGEST"
```

The environment file is supplied by the operator, never built into the image. It must configure
the approved `NCI_SIP_HTTP_AUTH_FACTORY` integration, or explicitly set
`NCI_SIP_HTTP_AUTH_MODE=trusted-local` for a trusted local deployment with loopback-only publishing
(`-p 127.0.0.1:...`, as above). Otherwise startup refuses before listening with
`Required HTTP authentication needs an installed integration factory`. Host allow-lists are
not authentication: a reachable client can supply an allowed Host header. Set the
model identity, upstream endpoints/credentials and the public Host/Origin allow-lists
from [QUICKSTART](../QUICKSTART.md#settings). The model needs no download access; runtime
upstream requests still need egress to their configured origins. No live caDSR capability
requiring CDE Match or the lists-of-values API is claimed before credentials are issued.

The image presets HTTP on `0.0.0.0:8000`, required authentication, stateless sessions and
required-index readiness.
All are environment defaults that operators can override; the application's ordinary
defaults remain unchanged. Bind address does not disable Host/Origin checks. `/health`
checks the serving process; `/ready` verifies the active index and model locally. Neither
asserts upstream availability. The image's HEALTHCHECK requests `/health`; local compose uses
`/ready` instead. Both use the configured HTTP port, a three-second request bound, 30-second
interval, five-second probe timeout, 180-second startup grace and three retries. The startup
grace matches the smoke's model/index startup allowance; increase it for larger cold assets.
If overriding `NCI_SIP_HTTP_ALLOWED_HOSTS`, retain `127.0.0.1:*` for these internal probes; they do
not bypass Host admission or need credentials. JSON diagnostics go to stderr, collected by container log
drivers; stdout remains reserved for MCP when stdio is explicitly selected.

Stateless calls can reach any replica and resolve omitted NCIt releases per call. Name a
release explicitly when calls must agree. Stateful handshake sessions require routing each
session ID to its owning process; another process returns 404. Restart or 30-minute idle
expiry loses the pin, and reinitialization can select a newer release. Single-exchange
protocol calls have no session even in stateful mode. See [session details](transport.md).
The server runs as PID 1 with an exec-form entry point; allow 20 seconds for graceful SIGTERM.

Build, evaluate and activate indices as separate operator jobs using the existing
[index commands](../QUICKSTART.md), then distribute consistent snapshots to serving replicas.
To roll back, activate the retained predecessor in the operator copy, verify its manifest,
and replace/restart serving replicas with that snapshot. Coordinate the rollout; the image
does not synchronize local files or sessions across replicas.

## Gateway controls and outbound access

The gateway must enforce rate limits and quotas per verified principal or authenticated client,
never an untrusted forwarded identity header. The [implementation plan](implementation-plan.md)
deliberately adds no local rate limiter. Per-call upstream request budgets, response size limits
and session caps do not replace cross-request admission limits. Each upstream request has a
timeout, but the server has no whole-call deadline: configure a gateway request timeout as the
client-facing end-to-end bound; it does not guarantee cancellation of server work.

Restrict outbound traffic at the network layer to the origins configured by these settings:

- `NCI_SIP_EVS_BASE_URL`
- `NCI_SIP_EVS_FHIR_BASE_URL`
- `NCI_SIP_CADSR_BASE_URL`
- `NCI_SIP_CADSR_FTP_URL`
- `NCI_SIP_SSIS_SPARQL_URL`

Derive the allow-list from deployment configuration, not a fixed list of production hosts.
Mounted model and index assets need no download access. Separately account for destinations
required by the approved identity integration. The local compose `serving` network does not
enforce an egress allow-list; that policy belongs to the deployment network or egress proxy.

## Build and release evidence

### Release pipeline

The image and its external data/model assets have separate lifecycles. This repository's
pipeline publishes the image; the operator prepares assets and chooses when to deploy it.

```mermaid
---
config:
  theme: neutral
  look: classic
  layout: dagre
  flowchart:
    wrappingWidth: 260
---
flowchart TB
    PR["Reviewed pull request<br/>local gates + green PR CI"]
    PR -->|"head clearance; merge commit"| Main["Green CI on main merge commit"]
    Main --> Release["Release workflow<br/>eligible tag + wheel + amd64 image"]
    Release --> Gate["Image verification<br/>offline HTTP smoke + SBOM<br/>zero High / Critical findings"]
    Gate --> Publish["Public GHCR digest + release evidence<br/>anonymous digest access verified"]
    Publish -. "operator selects digest" .-> Deploy["Deploy with prepared index<br/>and matching model"]
```

PR builds never publish. A successful tag/release job alone does not imply successful image
publication: the image job must finish too. Audit and CodeQL also run on main; neither replaces
the image scan. [Publication recovery](#scan-policy-and-publication) is described below.

### Local builds and dependency lock

Run `pdm build --no-sdist` in a tagged checkout, then
`docker build --platform linux/amd64 --load -t nci-sip-mcp:local .`; `--load` puts the image in
the local store, where a buildx `docker-container` builder would otherwise leave it in the build
cache. The `docker` CLI on a Podman engine builds the OCI format, which drops the
`HEALTHCHECK` the smoke check reads; there use `podman build --format docker` instead.
The build downloads about 200 MB of torch from `download.pytorch.org` and produces an image of
about 1.6 GB, so it needs that network access, the disk and several minutes.
The build trusts the PyTorch wheel index at `https://download.pytorch.org/whl/cpu` as an
additional dependency source; the committed lock binds the selected wheel hashes.
Keep exactly one current wheel in `dist/`. Run
`pdm run python -m scripts.container_smoke nci-sip-mcp:local` to check the image with an
external, test-only model and a recorded concept. CI builds the same wheel/image and runs
the same smoke check, including default-auth refusal, explicit local serving and Docker's
healthy status. This is packaging evidence, not production retrieval calibration.

The image uses a digest-pinned Amazon Linux 2023 base, refreshes OS packages and installs
its `python3.14` package. The builder creates a virtual environment on the same base and
copies it into the runtime; builder package installations and caches stay behind.
The OS upgrade is not pinned, so image contents vary with the build date despite the
digest-pinned base; that is accepted because every build is scanned.
The Dockerfile accepts a requirements
path as a build argument so an additional architecture can be added later. To regenerate
the amd64 CPU lock from `pdm.lock`:

```bash
pdm run python -m scripts.container_lock prepare tmp/container-lock
docker build --platform linux/amd64 -f container/Dockerfile.resolve --output type=local,dest=tmp/container-lock/report tmp/container-lock
pdm run python -m scripts.container_lock write tmp/container-lock/report/resolution.json
pdm run python -m scripts.container_lock check
```

Remove `tmp/container-lock` afterwards. Installation requires hashes. The check fails if
PDM runtime versions drift; ordinary developer dependencies are unchanged.

### Scan policy and publication

The initial [candidate scan evidence](../container/base-scans.json) records image identities,
scanner metadata and every High finding. On 6 October 2026, Python slim-trixie
had 48 High/0 Critical findings, unchanged after OS and pip upgrades. Both Chainguard's
minimal glibc Python base and the upgraded AL2023 Python base scanned at zero, as did
each base with the CPU dependencies. AL2023 was selected to match CBIIT services and
its hardened base, with retained pullable digests. Chainguard's free tier offers moving
`latest` tags and does not guarantee old pinned digests remain pullable.
These are point-in-time results;
each complete release image is scanned again.

The Release workflow chains image build, smoke tests, SBOM and vulnerability scanning before
GHCR publication. The image path is derived from `github.repository`, so moving the repository
changes the namespace without a code change. PR jobs never publish. Every HIGH or CRITICAL
finding blocks publication, even without an available fix. There are **no exceptions**;
any future exception needs an explicit owner decision recorded with its name, date and reason
in a tracked file and reviewed in its PR. Public Trivy scans are a proxy, not a claim that
NCI's inaccessible Twistlock/Prisma Cloud scanner has approved the image.

[GitHub creates new GHCR packages as private](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry)
even for public repositories. At first publication
(and after an organization move), set the package's visibility to public in its package
settings. The workflow verifies anonymous access to the exact digest and fails until that
works. If the GitHub release exists but the image job failed, correct the cause and rerun
Release with `retry_image_tag` naming that release at the current main commit. Security
reports are attached before the public-access check; a successful run adds the digest and
evidence references to the release notes. Use the digest to deploy, not a floating tag.

The renamed repository's next image destination is `ghcr.io/cbiit/nci-sip-mcp`.
Before merge clearance for a change that triggers release, the owner must confirm that
namespace, repository association, publication access and public visibility arrangement.
Repository visibility alone does not establish package access. A missing or inaccessible
package is not proof that publication will work. This transition authorizes no package
creation, transfer, deletion, visibility change or Release dispatch.

After an authorized image publication succeeds at the new destination, verify its
digest-bound provenance with
`gh attestation verify oci://ghcr.io/cbiit/nci-sip-mcp@sha256:<digest> --repo CBIIT/nci-sip-mcp --signer-workflow CBIIT/nci-sip-mcp/.github/workflows/release.yml`.
Historical images keep their original digest, release assets and attestation identities;
for an image published under the old identity, the corresponding verification is
`gh attestation verify oci://ghcr.io/cbiit/nci-si-mcp@sha256:<digest> --repo CBIIT/nci-si-mcp --signer-workflow CBIIT/nci-si-mcp/.github/workflows/release.yml`.
Verify the SBOM attestation with the same command plus
`--predicate-type https://cyclonedx.org/bom`. Both attestations are stored in GitHub and GHCR;
they bind the pushed image digest to the release workflow, not a mutable image tag. Older
images may have no attestations. Manual release and image retries require the newest main-push
CI run for the checked-out commit to have completed successfully; an unavailable or unsuccessful
CI result blocks publication.
Manual dispatch must select `main`, and its invocation commit must still equal the checked-out
commit; otherwise dispatch again so the attestation source and image source agree.

CBIIT's hardened bases are in private ECR unavailable to public CI. The hosting team may
rebuild on its approved `cbiit-amazon-linux-2023` base. Cloud One ECR publication and OIDC deployment roles are later
deployment work; this issue supplies public GHCR images only.
