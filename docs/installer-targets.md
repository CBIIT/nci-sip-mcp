# Installer target interface

The installer owns the local server only. Follow the [local guide's prerequisites and server
sequence](local-guide.md#prerequisites) for the canonical installation/lifecycle commands; this
reference explains identity, state and failures.

## Prerequisites and selection

Local server checks cover macOS/Linux, the running Python 3.14 or newer, the Git executable used
by pip for the released VCS URL, and the configured EVS/caDSR endpoints. They require neither
PDM nor a Git checkout. Checks change nothing. Expected failures carry static corrections; bugs
propagate. The index, documentation site and validation companion are installed independently
with their documented local commands; they are not installer targets or flags.

`nci-si-install plan --target local` describes the flow: **check prerequisites → install →
configure → verify**

The local target runs those stages through the same resumable state path. `run --transport stdio
--profile unified` names the defaults; transport also accepts `http`, and profile accepts `evs`
or `cadsr`. The complete flow creates owned `venv`, `server-data` and private `runtime`
children: install recreates `venv`, while configure creates `server-data` and `runtime`, records
the selected values and verifies the runtime directory's `0700` mode.

## Installation identity

A supported installer is bootstrapped from this repository's released Git tag; its PEP 610
record supplies the exact commit, and its package supplies the hash-locked server dependencies
and build backend. Install recreates the owned server environment, installs the lock with hashes
and builds that commit without dependency resolution or build isolation. Verification requires
the installed version and commit to match the running installer. Development, wheel, sdist, PyPI
and editable bootstraps are BLOCKED deliberately; reinstall from the documented Git URL. Close
stdio clients and stop an owned HTTP server before reinstalling. A failed or interrupted
reinstall leaves the step FAILED until a new install attempt completes and makes no rollback
claim.

## State and retry

`run` and `status` require `--data-dir`; `NCI_SI_DATA_DIR` belongs to the server. Reconciliation
and the status snapshot hold `installer-state.lock`. State version 2 binds the target,
components, local transport and domain profile. Version 1 is refused without migration. State
uses an fsynced temporary sibling, atomic replacement and a directory fsync.

`status` revalidates recorded stages and continues after verified recovery, stopping at the
first unresolved stage. It never installs or applies steps; unrecorded stages stay pending.

Non-restart runs reverify from the beginning; the first stale step and later records are
discarded. Missing prerequisites are BLOCKED with static corrections. HTTP readiness failures
with no missing input are FAILED. Stdio launch or timeout failures, and any unsuccessful server
initialization or MCP verification, exit with code 2; child-process failures name the owned
`runtime/server.log`. `--restart` clears recorded progress for the same selection but deletes no
installed assets. Another selection requires a new `--data-dir`. Invalid, truncated or
unsupported state remains unchanged. On a terminal, BLOCKED `run` prints fixes to stderr; Enter
rechecks and EOF returns BLOCKED JSON. Non-TTY stdin and `--non-interactive` never read input.
`help <step> --target local` explains every executable stage.

## Processes and removal

`start --transport stdio` prints a JSON client configuration containing the owned executable,
arguments and required environment; it opens no listener. HTTP binds `127.0.0.1`, uses
trusted-local access and explicitly sets `NCI_SI_HTTP_REQUIRE_INDEX=0`: the installer stays
server-only, while the index uses the [independent local index path](local-guide.md#index). HTTP
verification reuses a process whose PID and recorded start-time identity match. With no recorded
running process, it starts a transient process, checks `/ready` and stops it; an unverifiable or
mismatched ownership record fails closed. An unowned listener on port 8000 is BLOCKED. `start`
and `stop` operate the recorded HTTP process. `uninstall` refuses a running owned process,
removes only the three owned children, then removes installer state so later status reports no
installation.

## Verify a released local installation

Released installers from v0.29.1 accept both exact canonical repository origins and fetch
new server installations from `CBIIT/nci-sip-mcp` at the installer's full commit. Use the
new origin below for current releases. Package, import, command, environment and
data-directory names remain unchanged.

Set `PYTHON` to an absolute Python 3.14-or-newer executable, `RELEASE_TAG` to a released `vX.Y.Z`,
and choose fresh absolute paths for the other variables below. Run from a directory outside a
source checkout. The HTTP commands inherit any configured upstream URLs; trusted-local HTTP remains
on loopback.

```bash
"$PYTHON" -m venv "$INSTALLER_VENV"
"$INSTALLER_VENV/bin/python" -m pip install "nci-si-mcp @ git+https://github.com/CBIIT/nci-sip-mcp@${RELEASE_TAG}"
"$INSTALLER_VENV/bin/nci-si-install" check --target local
"$INSTALLER_VENV/bin/nci-si-install" run --target local --transport stdio --data-dir "$STDIO_ROOT" --non-interactive
"$INSTALLER_VENV/bin/nci-si-install" status --target local --transport stdio --data-dir "$STDIO_ROOT"
"$INSTALLER_VENV/bin/nci-si-install" start --target local --transport stdio --data-dir "$STDIO_ROOT"
"$INSTALLER_VENV/bin/nci-si-install" uninstall --target local --transport stdio --data-dir "$STDIO_ROOT"
"$INSTALLER_VENV/bin/nci-si-install" check --target local
"$INSTALLER_VENV/bin/nci-si-install" run --target local --transport http --data-dir "$HTTP_ROOT" --non-interactive
"$INSTALLER_VENV/bin/nci-si-install" status --target local --transport http --data-dir "$HTTP_ROOT"
"$INSTALLER_VENV/bin/nci-si-install" start --target local --transport http --data-dir "$HTTP_ROOT"
"$INSTALLER_VENV/bin/nci-si-install" stop --target local --transport http --data-dir "$HTTP_ROOT"
"$INSTALLER_VENV/bin/nci-si-install" uninstall --target local --transport http --data-dir "$HTTP_ROOT"
```

For immutable v0.28.0 or v0.29.0 installers, keep the old origin: they accept only the
old PEP 610 origin, even though GitHub redirects Git fetches. Set `LEGACY_RELEASE_TAG`
to the required older tag and bootstrap in a fresh environment outside a checkout:

```bash
"$PYTHON" -m venv "$INSTALLER_VENV"
"$INSTALLER_VENV/bin/python" -m pip install "nci-si-mcp @ git+https://github.com/CBIIT/nci-si-mcp@${LEGACY_RELEASE_TAG}"
```

CI selects the source before installation and checks scheduled releases against protected `main`. Both CI installer environments bootstrap from that selected full commit (`INSTALL_REF`), deliberately differing from the public released-tag example above. Moving a tag after selection cannot select different bootstrap source. The selected tag still identifies the release and drives the controlled moved-tag negative check.
