"""Only an explicitly configured remote profile receives an upstream credential."""

import json
import shutil
import subprocess
import sys
import threading
import unittest
from contextlib import chdir
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from scripts.operator_evidence import finish_evidence
from scripts.operator_execution import (
    AUTHORIZATION,
    REMOTE_TARGET,
    ROOT,
    RemoteProbe,
    _perform,
    execute_job,
)
from scripts.operator_process import run_owned
from scripts.operator_source import SOURCE_PATHS
from scripts.operator_worker import clean_environment
from scripts.portal_store import EvidenceStore


class OperatorExecutionTest(unittest.TestCase):
    def snapshot(self, _repository, _commit, destination):
        destination.mkdir()
        for name in SOURCE_PATHS:
            source = ROOT / name
            if source.is_dir():
                shutil.copytree(
                    source, destination / name, ignore=shutil.ignore_patterns("__pycache__")
                )
            else:
                shutil.copy2(source, destination / name)

    def test_fixture_pipeline_binds_real_http_measurements_to_original_inventory(self):
        with TemporaryDirectory() as temporary:
            directory = Path(temporary)
            with patch("scripts.operator_execution.archive_source", side_effect=self.snapshot):
                code = _perform(directory, "a" * 40, "benchmark-http-fixture")
            self.assertEqual(code, 0)
            report = json.loads((directory / "bundle/report.json").read_text())
            self.assertTrue(report["complete"])
            job = {
                "run_id": "1" * 32,
                "commit": "a" * 40,
                "profile": "benchmark-http-fixture",
                "started_at": "2026-10-08T00:00:00+00:00",
            }
            store = EvidenceStore(directory / "store.sqlite")
            result = finish_evidence(
                job, directory, {"state": "completed", "exit_code": code, "reason": None}, store
            )
            self.assertEqual(result["state"], "completed")
            record = store.get(job["run_id"])
            self.assertEqual(record["evidence"]["state"], "completed")
            self.assertEqual(record["evidence"]["server_commit"], "a" * 40)

    def test_relative_workspace_is_resolved_before_child_working_directory_changes(self):
        observed = []

        def cancelled(_command, **options):
            observed.append(options["directory"])
            return {"state": "cancelled", "exit_code": None, "reason": "cancelled"}

        with TemporaryDirectory() as temporary, chdir(temporary):
            job = {"run_id": "1" * 32, "profile": "benchmark-http-fixture", "commit": "a" * 40}
            with patch("scripts.operator_execution.run_owned", side_effect=cancelled):
                execute_job(
                    job, Path("job"), threading.Event(), store=EvidenceStore(Path("store.sqlite"))
                )
        self.assertTrue(observed[0].is_absolute())

    def test_fixture_job_never_receives_the_configured_remote_credential(self):
        observed = []

        def cancelled(_command, **options):
            observed.append(options["environment"])
            return {"state": "cancelled", "exit_code": None, "reason": "cancelled"}

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            job = {"run_id": "1" * 32, "profile": "benchmark-http-fixture", "commit": "a" * 40}
            remote = RemoteProbe("https://approved.example/mcp", "Bearer PRIVATE-CANARY")
            with patch("scripts.operator_execution.run_owned", side_effect=cancelled):
                result = execute_job(
                    job,
                    root / "job",
                    threading.Event(),
                    store=EvidenceStore(root / "store.sqlite"),
                    remote=remote,
                )
            self.assertEqual(result["state"], "cancelled")
            self.assertNotIn("PRIVATE-CANARY", str(observed))
            self.assertNotIn("PRIVATE-CANARY", repr(remote))

    def test_remote_configuration_rejects_url_credentials_and_plaintext(self):
        for target in ("http://approved.example/mcp", "https://secret@approved.example/mcp"):
            with self.subTest(target=target), self.assertRaises(ValueError):
                RemoteProbe(target, None)

    def test_cancelled_worker_scratch_is_removed_without_removing_bound_evidence(self):
        def cancelled(_command, **options):
            directory = options["directory"]
            scratch = directory / "bundle/worker-interrupted"
            scratch.mkdir(parents=True)
            (scratch / "server.log").write_text("disposable diagnostics")
            (directory / "bundle/selection.json").write_text("{}")
            (directory / "source").mkdir()
            (directory / "worker.log").write_text("worker diagnostics")
            return {"state": "cancelled", "exit_code": 130, "reason": "cancelled"}

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            job = {"run_id": "1" * 32, "profile": "benchmark-http-fixture", "commit": "a" * 40}
            with patch("scripts.operator_execution.run_owned", side_effect=cancelled):
                result = execute_job(
                    job, root / "job", threading.Event(), store=EvidenceStore(root / "store.sqlite")
                )
            self.assertEqual(result["state"], "cancelled")
            self.assertFalse((root / "job/bundle/worker-interrupted").exists())
            self.assertFalse((root / "job/source").exists())
            self.assertEqual((root / "job/bundle/selection.json").read_text(), "{}")
            self.assertEqual((root / "job/worker.log").read_text(), "worker diagnostics")

    def test_inventory_and_worker_output_reach_the_job_log_of_the_owned_run(self):
        # The pipeline runs as the controller runs it, in an owned child whose output is logged.
        child = (
            "import sys\nfrom pathlib import Path\nfrom unittest.mock import patch\n"
            "from scripts import operator_execution\n"
            "def snapshot(_repository, _commit, destination):\n"
            "    (destination / 'scripts').mkdir(parents=True)\n"
            "    (destination / 'scripts/operator_inventory.py').write_text(\n"
            "        \"import sys\\nprint('INVENTORY-OUT', flush=True)\\n\"\n"
            "        \"sys.stderr.write('INVENTORY-ERR\\\\n')\\nraise SystemExit(7)\\n\")\n"
            "with patch.object(operator_execution, 'archive_source', snapshot):\n"
            "    raise SystemExit(operator_execution._perform(\n"
            "        Path(sys.argv[1]), 'a' * 40, 'benchmark-http-fixture'))\n"
        )
        with TemporaryDirectory() as temporary:
            directory = Path(temporary)
            result = run_owned(
                [sys.executable, "-c", child, str(directory)],
                directory=directory,
                environment=clean_environment(directory),
                seconds=60,
                cancelled=threading.Event(),
            )
            self.assertEqual(result, {"state": "failed", "exit_code": 7, "reason": "worker_failed"})
            self.assertTrue((directory / "worker.log").is_file())
            self.assertEqual(
                (directory / "worker.log").read_text(), "INVENTORY-OUT\nINVENTORY-ERR\n"
            )


class RemoteExecutionTest(unittest.TestCase):
    snapshot = OperatorExecutionTest.snapshot

    def test_remote_configuration_is_required_before_creating_work(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            job = {"run_id": "1" * 32, "profile": "benchmark-http-remote", "commit": "a" * 40}
            with self.assertRaisesRegex(ValueError, "explicit startup configuration"):
                execute_job(
                    job, root / "job", threading.Event(), store=EvidenceStore(root / "store.sqlite")
                )
            self.assertFalse((root / "job").exists())

    def test_remote_header_rejects_injection_and_non_ascii(self):
        for header in ("Bearer token\r\nOther: value", "Bearer token\x00", "Bearer café"):
            with (
                self.subTest(header=repr(header)),
                self.assertRaisesRegex(ValueError, "printable ASCII"),
            ):
                RemoteProbe("https://approved.example/mcp", header)

    def test_remote_worker_receives_only_its_explicit_target_and_optional_header(self):
        for header in (None, "Bearer PRIVATE-CANARY"):
            with self.subTest(authenticated=header is not None), TemporaryDirectory() as temporary:
                root = Path(temporary)
                facts = root / "facts.json"

                def cancelled(command, facts=facts, **options):
                    code = (
                        "import os,json,sys; "
                        'open(sys.argv[1], "w").write(json.dumps(dict(os.environ)))'
                    )
                    subprocess.run(  # noqa: S603 - owned child records its supplied environment
                        [sys.executable, "-c", code, str(facts)],
                        env=options["environment"],
                        check=True,
                    )
                    self.assertNotIn("PRIVATE-CANARY", str(command))
                    return {"state": "cancelled", "exit_code": None, "reason": "cancelled"}

                job = {"run_id": "1" * 32, "profile": "benchmark-http-remote", "commit": "a" * 40}
                with patch("scripts.operator_execution.run_owned", side_effect=cancelled):
                    result = execute_job(
                        job,
                        root / "job",
                        threading.Event(),
                        store=EvidenceStore(root / "store.sqlite"),
                        remote=RemoteProbe("https://approved.example/mcp", header),
                    )
                observed = json.loads(facts.read_text())
                self.assertEqual(observed[REMOTE_TARGET], "https://approved.example/mcp")
                self.assertEqual(observed.get(AUTHORIZATION), header)
                self.assertEqual(result["state"], "cancelled")
                self.assertNotIn("PRIVATE-CANARY", json.dumps(result))

    def test_inventory_failure_prevents_worker_execution_and_binding(self):
        def snapshot(repository, commit, destination):
            self.snapshot(repository, commit, destination)
            (destination / "scripts/operator_inventory.py").write_text("raise SystemExit(7)\n")
            (destination / "scripts/operator_worker.py").write_text(
                'from pathlib import Path; Path("unexpected-worker").touch()\n'
            )

        with TemporaryDirectory() as temporary:
            directory = Path(temporary)
            with patch("scripts.operator_execution.archive_source", side_effect=snapshot):
                code = _perform(directory, "a" * 40, "benchmark-http-fixture")
            self.assertEqual(code, 7)
            self.assertFalse((directory / "binding.json").exists())
            self.assertFalse((directory / "source/unexpected-worker").exists())

    def test_remote_pipeline_preserves_target_and_authorization_without_shell_arguments(self):
        def snapshot(repository, commit, destination):
            self.snapshot(repository, commit, destination)
            (destination / "scripts/benchmark_http_cli.py").write_text(
                "import json,os,sys\nfrom pathlib import Path\n"
                'output = Path(sys.argv[sys.argv.index("--output") + 1])\n'
                'output.write_text(json.dumps({"arguments":sys.argv[1:], '
                '"authorization":os.environ.get("NCI_SIP_BENCHMARK_AUTHORIZATION")}))\n'
                "raise SystemExit(9)\n"
            )

        with TemporaryDirectory() as temporary:
            directory = Path(temporary)
            environment = {
                REMOTE_TARGET: "https://approved.example/mcp",
                AUTHORIZATION: "Bearer PRIVATE-CANARY",
            }
            with (
                patch("scripts.operator_execution.archive_source", side_effect=snapshot),
                patch.dict("os.environ", environment),
            ):
                code = _perform(directory, "a" * 40, "benchmark-http-remote")
            observed = json.loads((directory / "bundle/report.json").read_text())
            self.assertEqual(code, 9)
            self.assertEqual(
                observed["arguments"],
                [
                    "--allow-remote",
                    "--target",
                    environment[REMOTE_TARGET],
                    "--allow-target",
                    environment[REMOTE_TARGET],
                    "--output",
                    str(directory / "bundle/report.json"),
                ],
            )
            self.assertEqual(observed["authorization"], environment[AUTHORIZATION])
            self.assertNotIn("PRIVATE-CANARY", (directory / "binding.json").read_text())
