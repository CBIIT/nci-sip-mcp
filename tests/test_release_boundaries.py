"""Explicit release selection also works without adapter telemetry."""

from fakes import captured, records
from nci_sip_mcp.audit import audited
from nci_sip_mcp.release_selection import select
from test_server import ServerFixture


class DirectReleaseSelectionTest(ServerFixture):
    def test_explicit_release_selection_does_not_require_or_leak_an_audit_scope(self):
        result = select(self.context, "ncit", release="26.06e")
        self.assertEqual(
            (result.terminology, result.version, result.channel),
            ("ncit", "26.06e", self.settings.release_channel),
        )
        self.assertEqual(result.pinned_terminology, "ncit_26.06e")
        with captured() as stream, audited("later-call", {}, {}, ()) as record:
            record.result = {}
        self.assertEqual(records(stream)[0]["release"], {"requested": None, "resolved": []})
