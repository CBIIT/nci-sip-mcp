"""The state-change hook of the self-tests that run the suite against a remote server: (re)start
the compliant server over streamable HTTP, as an operator's hook restarts a deployment.

It stops the server whose process id `RESTART_PID_FILE` holds, appends the scenario set it was
named, the licence key it was given (the scenario's setting, or none) and whether it was given the
harness's credential to the file `RESTART_RECORD` names, and starts the server again with its own
environment; then it returns at once, the server still starting, as a restart does.
"""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

COMPLIANT_SERVER = Path(__file__).parent / "compliant_server.py"


def stop(pid_file: Path) -> None:
    """Stop the server of `pid_file`, if it is running, and wait until it has gone."""

    if not pid_file.exists():
        return
    pid = int(pid_file.read_text(encoding="utf-8"))
    try:
        os.kill(pid, signal.SIGTERM)
        for _ in range(100):
            os.kill(pid, 0)
            time.sleep(0.05)
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def main() -> None:
    pid_file = Path(os.environ["RESTART_PID_FILE"])
    stop(pid_file)
    if record := os.environ.get("RESTART_RECORD"):
        seen = {
            "scenarios": os.environ.get("NCI_SIP_ACCEPTANCE_SCENARIOS"),
            "licenceKey": os.environ.get("NCI_SIP_EVS_LICENSE_KEY"),
            "credentialGiven": "NCI_SIP_ACCEPTANCE_AUTHORIZATION" in os.environ,
        }
        with Path(record).open("a", encoding="utf-8") as log:
            log.write(json.dumps(seen) + "\n")
    server = subprocess.Popen(  # noqa: S603 - the self-tests' own interpreter and stub
        [sys.executable, str(COMPLIANT_SERVER)],
        env=os.environ | {"COMPLIANT_SERVER_TRANSPORT": "http"},
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    pid_file.write_text(str(server.pid), encoding="utf-8")


if __name__ == "__main__":
    main()
