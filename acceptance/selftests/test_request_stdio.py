"""An explicit controlled session owns its actual child beyond the client's context."""

import io
import json
import sys
from pathlib import Path

from nci_si_acceptance.client import open_session
from nci_si_acceptance.request_authorization import Authorization, Limits, RequestGrant
from nci_si_acceptance.request_supervision import SupervisedRun


def test_real_sdk_session_keeps_completion_pending_until_external_cleanup(tmp_path):
    replies = tmp_path / "replies.json"
    replies.write_text(
        json.dumps([{"tool": "owned", "arguments": {"value": "input"}, "result": {"ok": True}}])
    )
    command = [
        sys.executable,
        str(Path(__file__).with_name("live_content_server.py")),
        str(replies),
    ]
    authorization = Authorization(
        (RequestGrant("GET", "http://127.0.0.1:9/unused", ()),), Limits(), "a" * 64
    )
    owner = SupervisedRun.create(authorization, tmp_path / "run")
    errors = io.StringIO()
    with owner:
        try:
            with open_session(command, {}, errlog=errors, timeout=3, owner=owner) as session:
                observed = {
                    "lists": [[tool.name for tool in session.list_tools().tools] for _ in range(2)],
                    "call": session.call_tool("owned", {"value": "input"}).structured_content,
                }
                assert owner.snapshot()["observationComplete"] is False
            assert owner.snapshot()["observationComplete"] is False
        except TypeError as error:
            # The real entry's refusal is an observable failed session, including absence.
            observed = {"entry_error": str(error)}
        assert observed == {"lists": [["owned"], ["owned"]], "call": {"ok": True}}
    summary = json.loads((owner.run.directory / "summary.json").read_text())
    assert summary["state"] == "completed" and summary["observationComplete"] is True
    assert not errors.closed
