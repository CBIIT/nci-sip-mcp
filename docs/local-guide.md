# Run locally

The server, optional NCIt index, documentation site and validation companion are independent
local components. Choose one row, then run only the linked component sections. The installer
owns the server only. It never installs an index, documentation or the companion.

The repository URL and package namespace changed in different releases. The commands below require
a tag that contains the NCI SIP namespace (`nci-sip-mcp`, `nci_sip_mcp`, `nci-sip-install`,
`NCI_SIP_*`). Pre-SIP tags retain `nci-si-mcp`, `nci_si_mcp`, `nci-si-install` and `NCI_SI_*`;
those names are not aliases in the new package. The `.nci-si-mcp` data directory remains unchanged.
Use the [pre-SIP release instructions](installer-targets.md#pre-sip-package-release) for an older
tag. Installers from v0.29.1 accept both exact repository origins; older installers need the
[old-origin instructions](installer-targets.md#verify-a-released-local-installation). In Local run
CI, PR, push and manual branch runs verify the checked-out candidate; the weekly schedule verifies
the latest released tag.

## Combinations

| Combination | Platform | Processes | Fixed ports | Sections |
|---|---|---|---|---|
| Server | macOS/Linux | Installer stdio, then installer HTTP | 8000 | [Server](#server) |
| Server and index | Linux | Installer stopped, then required-index HTTP | 8000 | [Server](#server), [index](#index) |
| Documentation | Linux with Compose | Compose documentation | 8080 | [Containers](#documentation-and-companion-containers) |
| Companion | Linux with Compose | Compose administration and ingress | 8081 | [Containers](#documentation-and-companion-containers) |
| Everything | Linux with Compose | Required-index HTTP, Compose documentation, administration and ingress | 8000, 8080, 8081 | [Server](#server), [index](#index), [containers](#documentation-and-companion-containers) |
| Everything without Docker | macOS | Required-index HTTP, host documentation and portal | 8000, 8080, 8081 | [Server](#server), [index](#index), [host documentation and companion](#host-documentation-and-companion) |

For a combination with an index, finish the installer lifecycle before starting the index server.
Run only one server on `127.0.0.1:8000`: the `[server,index]` environment's required-index
server replaces the installer's HTTP server. Documentation uses 8080. The companion uses 8081.

## Prerequisites

Use Python 3.14 or newer and Git. Replace `vX.Y.Z` below with an exact post-SIP namespace tag
from the releases page. Before the server and index commands, set `SERVER_VENV`, `INDEX_VENV`, `STDIO_ROOT`,
`HTTP_ROOT` and `INDEX_DATA` to five fresh absolute paths; the two installer roots must differ
because each state record binds its transport and profile. An omitted `--profile` means `unified`;
when selecting `evs` or `cadsr`, pass that same profile to every installer lifecycle command.

Host documentation and container construction run from a repository checkout with PDM installed.
They need Node.js 24 or newer. The site and companion-context output directories must not exist;
use a fresh `--output` for a later build. Companion context construction also requires a clean,
committed checkout. Container combinations need a Docker-compatible engine with Compose; the
images target Linux amd64.

## Server

> **Current upstream access limits:** Read the
> [dated access and transition guidance](../QUICKSTART.md#upstream-access-and-ssis-transition)
> before the installer lifecycle. Its default connectivity checks contact production caDSR,
> which is currently prohibited; use the linked offline fixture validation while holds remain.

Create a virtual environment and install the tagged server package:

```bash
python3.14 -m venv "$SERVER_VENV"
"$SERVER_VENV/bin/python" -m pip install "nci-sip-mcp[server] @ git+https://github.com/CBIIT/nci-sip-mcp@vX.Y.Z"
```

Walk the stdio lifecycle:

```bash
"$SERVER_VENV/bin/nci-sip-install" check --target local
"$SERVER_VENV/bin/nci-sip-install" run --target local --transport stdio --data-dir "$STDIO_ROOT" --non-interactive
"$SERVER_VENV/bin/nci-sip-install" status --target local --transport stdio --data-dir "$STDIO_ROOT"
"$SERVER_VENV/bin/nci-sip-install" uninstall --target local --transport stdio --data-dir "$STDIO_ROOT"
```

Walk the loopback HTTP lifecycle:

```bash
"$SERVER_VENV/bin/nci-sip-install" check --target local
"$SERVER_VENV/bin/nci-sip-install" run --target local --transport http --data-dir "$HTTP_ROOT" --non-interactive
"$SERVER_VENV/bin/nci-sip-install" status --target local --transport http --data-dir "$HTTP_ROOT"
"$SERVER_VENV/bin/nci-sip-install" start --target local --transport http --data-dir "$HTTP_ROOT"
"$SERVER_VENV/bin/nci-sip-install" stop --target local --transport http --data-dir "$HTTP_ROOT"
"$SERVER_VENV/bin/nci-sip-install" uninstall --target local --transport http --data-dir "$HTTP_ROOT"
```

The stdio `start` command prints the client configuration instead of opening a listener. The HTTP
server binds `127.0.0.1:8000`; `/ready` verifies the installed server process.

## Index

Install the index extra into a separate environment, build the explicit sample, evaluate it and
start the one required-index server for this combination:

```bash
python3.14 -m venv "$INDEX_VENV"
"$INDEX_VENV/bin/python" -m pip install "nci-sip-mcp[server,index] @ git+https://github.com/CBIIT/nci-sip-mcp@vX.Y.Z"
export NCI_SIP_DATA_DIR="$INDEX_DATA"
"$INDEX_VENV/bin/nci-sip-mcp" index-sample C3262 C2991 C40704 C153397 C116938
"$INDEX_VENV/bin/nci-sip-mcp" evaluate
export NCI_SIP_HTTP_REQUIRE_INDEX=1
"$INDEX_VENV/bin/nci-sip-mcp" serve --transport streamable-http
```

Verify `http://127.0.0.1:8000/ready`, then call `search_concepts` with terminology `ncit`, query
`Neoplasm` and mode `semantic`. The first result is C3262 with `source: evs_index` and
`servedBy: index`. Opening an index never downloads a model or starts a full build. This subset is a smoke test,
not full-corpus retrieval evidence. The [semantic index guide](ncit-semantic-index.md) owns the
full build, field/model policy, evaluation and activation procedure.

## Host documentation and companion

Build the public site, then start its preview and the local validation portal in separate shells:

```bash
pdm install -G docs
npm ci --prefix docs/site-assets --ignore-scripts
npm audit --prefix docs/site-assets --audit-level high
npm run build --prefix docs/site-assets
pdm run docs-build
pdm run python -m scripts.static_server --directory tmp/docs-site --port 8080
pdm run portal serve
```

Verify documentation at `http://127.0.0.1:8080/health` and the portal at
`http://127.0.0.1:8081/health`. Ctrl+C stops each foreground process.

## Documentation and companion containers

Build the allowlisted contexts and local images. This route builds the public site as part of
context creation:

```bash
pdm install -G docs
npm ci --prefix docs/site-assets --ignore-scripts
npm run build --prefix docs/site-assets
pdm run python -m scripts.companion_context
docker build --platform linux/amd64 --load -t nci-si-docs:local tmp/companion-context/docs
docker build --platform linux/amd64 --load -t nci-si-admin:local tmp/companion-context/admin
```

Start exactly one selection:

```bash
docker compose -f container/compose.local.yaml --profile docs up -d
docker compose -f container/compose.local.yaml --profile companion up -d
docker compose -f container/compose.local.yaml --profile docs --profile companion up -d
```

Verify documentation on 8080 and the companion on 8081. Stop and remove one component without
changing the other:

```bash
docker compose -f container/compose.local.yaml stop documentation
docker compose -f container/compose.local.yaml rm -f documentation
docker compose -f container/compose.local.yaml stop admin-ingress administration
docker compose -f container/compose.local.yaml rm -f admin-ingress administration
```

The evidence volume remains. See [local companion containers](companion-containers.md) for the
security boundaries, persistent evidence and optional serving-MCP profile.

To reuse a separately built, same-commit site instead, run `pdm run docs-build` and replace the
single context command above with
`pdm run python -m scripts.companion_context --site tmp/docs-site`. These are alternatives; never
run both against the same output directory.
