"""An existing index file that migrates on open leaves a start and a finish record."""

import io
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from nci_sip_mcp import container_entry
from nci_sip_mcp.cli import main
from nci_sip_mcp.config import Settings
from ncit_index.index import LocalIndex
from ncit_index.storage import SCHEMA_VERSION
from test_index_float32 import schema_five


class MigrationRecordsTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name)

    def schema_five_directory(self):
        with closing(sqlite3.connect(self.path / "nci_si.sqlite3")) as conn, conn:
            schema_five(conn)

    def records(self, logs):
        return [record.structured for record in logs.records]

    def assert_one_migration(self, records):
        """The pair an operator sees: a start, then a finish with the elapsed time."""
        self.assertEqual(
            [(record["event"], record["from_version"], record["to_version"]) for record in records],
            [
                ("index_migration_started", 5, SCHEMA_VERSION),
                ("index_migration_finished", 5, SCHEMA_VERSION),
            ],
        )
        self.assertNotIn("elapsed_seconds", records[0])
        self.assertGreaterEqual(records[1]["elapsed_seconds"], 0)

    def run_index_builds(self):
        argv = ["nci-sip-mcp", "index-builds"]
        environment = {"NCI_SIP_DATA_DIR": str(self.path)}
        with (
            patch("nci_sip_mcp.cli.configure_logging"),
            patch.object(sys, "argv", argv),
            patch.dict(os.environ, environment, clear=True),
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            return main()

    def test_operator_command_logs_the_migration_once(self):
        self.schema_five_directory()
        with self.assertLogs("nci_sip_mcp.index", "INFO") as logs:
            self.assertEqual(self.run_index_builds(), 0)
        self.assert_one_migration(self.records(logs))

        # The file is current now: the next command migrates nothing.
        with self.assertNoLogs("nci_sip_mcp.index", "INFO"):
            self.assertEqual(self.run_index_builds(), 0)

    def test_container_startup_logs_the_migration(self):
        self.schema_five_directory()
        with self.assertLogs("nci_sip_mcp.index", "INFO") as logs:
            container_entry.prepared_index(Settings(data_dir=self.path))
        self.assert_one_migration(self.records(logs))

    def test_elapsed_time_runs_from_the_start_record_to_after_the_commit(self):
        self.schema_five_directory()
        events, versions = [], []

        def clock():
            # An independent connection sees the schema version only once it is committed.
            with closing(sqlite3.connect(self.path / "nci_si.sqlite3")) as conn:
                versions.append(conn.execute("PRAGMA user_version").fetchone()[0])
            return 10.0 if len(versions) == 1 else 12.5

        with patch("ncit_index.index.monotonic", clock):
            LocalIndex(self.path, on_event=lambda name, details: events.append((name, details)))

        self.assertEqual(versions, [5, SCHEMA_VERSION])
        self.assertEqual(
            events,
            [
                (
                    "index_migration_started",
                    {"from_version": 5, "to_version": SCHEMA_VERSION},
                ),
                (
                    "index_migration_finished",
                    {"from_version": 5, "to_version": SCHEMA_VERSION, "elapsed_seconds": 2.5},
                ),
            ],
        )

    def test_a_new_file_is_created_without_a_migration_record(self):
        events = []
        LocalIndex(self.path, on_event=lambda name, details: events.append(name))
        self.assertEqual(events, [])
        self.assertTrue((self.path / "nci_si.sqlite3").is_file())


if __name__ == "__main__":
    unittest.main()
