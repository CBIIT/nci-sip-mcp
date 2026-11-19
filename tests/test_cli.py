import io
import json
import logging
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

import nci_sip_mcp
from fakes import FakeEVS, concept, release
from nci_sip_mcp import cli
from nci_sip_mcp.cli import build_parser, main
from nci_sip_mcp.config import Settings
from nci_sip_mcp.context import Context
from nci_sip_mcp.http_client import UpstreamUnavailableError
from nci_sip_mcp.index_adapter import LocalIndexAdapter, index_event
from nci_sip_mcp.registry import SPECS
from ncit_index.embeddings import HashingEmbeddingProvider
from ncit_index.index import LocalIndex
from test_server import ServerFixture
from test_traversal import complete_graph

NEOPLASM = concept(
    "C3262",
    "Neoplasm",
    synonyms=[{"name": "Tumor"}],
    parents=[{"code": "C2991", "name": "Disease or Disorder"}],
    children=[{"code": "C4741", "name": "Neoplasm by Morphology"}],
    roles=[{"type": "Disease_Has_Abnormal_Cell", "relatedCode": "C12922", "relatedName": "Cell"}],
    associations=[
        {"type": "Concept_In_Subset", "relatedCode": "C165258", "relatedName": "A Subset"}
    ],
)
KINASE = concept("C40704", "Receptor Tyrosine Kinase Inhibition")


class ParserTest(unittest.TestCase):
    def test_traverse_options(self):
        args = build_parser().parse_args(
            [
                "traverse",
                "C3262",
                "--max-edges",
                "25",
                "--edge-type",
                "role",
                "--edge-type",
                "child",
            ]
        )

        self.assertEqual(args.max_edges, 25)
        self.assertEqual(args.edge_types, ["role", "child"])
        self.assertEqual((args.direction, args.max_depth, args.max_nodes), ("out", 2, 200))
        self.assertEqual(build_parser().parse_args(["traverse", "C3262"]).max_edges, 1000)

    def test_search_defaults(self):
        args = build_parser().parse_args(["search", "tumor"])

        self.assertEqual((args.limit, args.mode), (10, "hybrid"))

    def test_unknown_choices_are_rejected_by_the_parser(self):
        for argv in (
            ["traverse", "C3262", "--edge-type", "sibling"],
            ["traverse", "C3262", "--direction", "sideways"],
            ["search", "tumor", "--mode", "fuzzy"],
        ):
            with (
                self.subTest(argv=argv),
                redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                build_parser().parse_args(argv)


@patch("nci_sip_mcp.cli.configure_logging")
class MainTest(unittest.TestCase):
    def test_cli_invocations_do_not_reuse_an_implicit_release(self, _):
        context = self.fake_context()
        first_code, first, _ = self.run_cli("lookup", "C3262", "--live-only", context=context)
        context.evs.release = release("26.07d")
        context.evs.concepts["C3262"] = dict(context.evs.concepts["C3262"], version="26.07d")
        second_code, second, _ = self.run_cli("lookup", "C3262", "--live-only", context=context)
        self.assertEqual((first_code, second_code), (0, 0))
        self.assertEqual(first["provenance"]["release"]["identifier"], "26.06e")
        self.assertEqual(second["provenance"]["release"]["identifier"], "26.07d")

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name)

    def fake_context(self):
        return Context(
            Settings(data_dir=self.path),
            evs=complete_graph(FakeEVS([NEOPLASM, KINASE])),
            index=LocalIndexAdapter(LocalIndex(self.path, on_event=index_event)),
            embedding_provider=HashingEmbeddingProvider(),
        )

    def run_cli(self, *argv, context="fake", **environment):
        """Run `main` and return (exit code, parsed stdout, stderr text).

        `context` is "fake" for a real context over FakeEVS, "real" to let the
        CLI build its own, or an object to hand to the CLI as the context.
        """

        stdout, stderr = io.StringIO(), io.StringIO()
        environment.setdefault("NCI_SIP_DATA_DIR", str(self.path))
        patches = [
            patch.object(sys, "argv", ["nci-sip-mcp", *argv]),
            patch.dict(os.environ, environment, clear=True),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ]
        if context == "fake":
            context = self.fake_context()
        if context != "real":
            patches.append(patch("nci_sip_mcp.cli.Context", return_value=context))
        with ExitStack() as stack:
            for item in patches:
                stack.enter_context(item)
            code = main()
        return code, json.loads(stdout.getvalue() or "null"), stderr.getvalue()

    def test_each_command_prints_its_result_and_exits_zero(self, _):
        code, manifest, _ = self.run_cli("index-sample", "C3262")
        self.assertEqual((code, manifest["concepts"]), (0, 1))

        code, lookup, _ = self.run_cli("lookup", "C3262", "--include-raw")
        self.assertEqual((code, lookup["provenance"]["source"]), (0, "evs_rest"))
        self.assertIn("raw", lookup)

        code, search, _ = self.run_cli("search", "tumor", "--mode", "bm25", "--limit", "1")
        self.assertEqual((code, search["hits"][0]["concept"]["code"]), (0, "C3262"))

        code, traversal, _ = self.run_cli("traverse", "C3262", "--max-depth", "1", "--no-roles")
        self.assertEqual((code, traversal["edges"][0]["target_code"]), (0, "C4741"))

        code, info, _ = self.run_cli("release-info")
        self.assertEqual((code, info["active_index"]["version"]), (0, "26.06e"))
        self.assertEqual(
            info["selected_release"],
            {
                "terminology": "ncit",
                "channel": "monthly",
                "version": "26.06e",
                "date": "2026-06-29",
            },
        )

        code, evaluation, _ = self.run_cli("evaluate")
        self.assertEqual((code, len(evaluation["results"])), (0, 3))

    def test_search_and_lookup_print_the_raw_payload_only_with_the_flag(self, _):
        self.run_cli("index-sample", "C3262")

        for argv in (("lookup", "C3262"), ("search", "neoplasm")):
            with self.subTest(argv=argv):
                _, plain, _ = self.run_cli(*argv)
                _, raw, _ = self.run_cli(*argv, "--include-raw")

                self.assertNotIn("raw", json.dumps(plain))
                self.assertIn('"raw"', json.dumps(raw))

    def test_index_search_and_lookup_options_shape_the_result(self, _):
        code, manifest, _ = self.run_cli("index-sample", "C3262", "C40704")
        self.assertEqual((code, manifest["concepts"]), (0, 2))

        options = ("--limit", "1", "--mode", "vector", "--include-raw")
        _, search, _ = self.run_cli("search", "kinase tumor", *options)
        self.assertEqual((search["mode"], len(search["hits"])), ("vector", 1))
        self.assertIn("raw", search["hits"][0]["concept"])

        # With EVS down, a lookup falls back to the index unless --live-only forbids it.
        context = self.fake_context()
        context.evs.errors = {"get_concept": UpstreamUnavailableError("down")}
        code, cached, _ = self.run_cli("lookup", "C3262", "--include-raw", context=context)
        self.assertEqual((code, cached["provenance"]["servedBy"]), (0, "index"))
        self.assertIn("raw", cached)
        code, failed, _ = self.run_cli("lookup", "C3262", "--live-only", context=context)
        self.assertEqual((code, failed["error"]["code"]), (1, "upstream_unavailable"))

    def test_one_command_reports_one_correlation_identifier(self, _):
        context = self.fake_context()
        context.evs.errors = {
            "get_api_version": UpstreamUnavailableError("down"),
            "get_terminologies": UpstreamUnavailableError("down"),
        }

        code, info, _ = self.run_cli("release-info", context=context)

        nested = [info["evs_api"]["error"], info["selected_release"]["error"]]
        self.assertEqual(code, 0)
        self.assertEqual(len({error["correlationId"] for error in nested}), 1)

    def test_kind_budget_limits_nodes_through_the_cli(self, _):
        context = self.fake_context()
        context.evs.concepts["C3262"] = dict(context.evs.concepts["C3262"])
        context.evs.concepts["C3262"]["children"] = [
            {"code": "C2", "name": "Two"},
            {"code": "C3", "name": "Three"},
        ]
        complete_graph(context.evs)
        code, result, _ = self.run_cli(
            "traverse",
            "C3262",
            "--max-depth",
            "1",
            "--edge-type",
            "child",
            "--budget-per-kind",
            "1",
            context=context,
        )
        self.assertEqual(code, 0)
        self.assertEqual([node["code"] for node in result["nodes"]], ["C3262", "C2"])
        self.assertEqual(result["truncation"]["bound"], "kind_budget")

    def test_traverse_options_shape_the_result(self, _):
        def traverse(*options):
            code, result, _ = self.run_cli("traverse", "C3262", "--max-depth", "1", *options)
            self.assertEqual(code, 0)
            return result, sorted({edge["edge_type"] for edge in result["edges"]})

        self.assertEqual(traverse()[1], ["association", "child", "role"])
        self.assertEqual(traverse("--no-hierarchy")[1], ["association", "role"])
        self.assertEqual(traverse("--no-roles")[1], ["association", "child"])
        self.assertEqual(traverse("--no-associations")[1], ["child", "role"])
        self.assertEqual(
            traverse("--edge-type", "role", "--edge-type", "child")[1], ["child", "role"]
        )

        result, edge_types = traverse("--direction", "in", "--max-nodes", "40", "--max-edges", "50")
        self.assertEqual(edge_types, ["parent"])
        self.assertEqual(
            (result["max_depth"], result["max_nodes"], result["max_edges"]), (1, 40, 50)
        )

        result, _ = traverse("--relationship-name", "Disease_Has_Abnormal_Cell")
        self.assertEqual([edge["target_code"] for edge in result["edges"]], ["C4741", "C12922"])

        code, result, _ = self.run_cli("traverse", "C3262", "C40704", "--max-depth", "0")
        self.assertEqual((code, result["start_codes"]), (0, ["C3262", "C40704"]))

    def test_error_envelope_exits_one(self, _):
        for argv, error in (
            (["lookup", "C999"], "not_found"),
            (["search", "tumor"], "internal_error"),
            (["evaluate"], "internal_error"),
            (["traverse", "C3262", "--max-depth", "-1"], "invalid_request"),
        ):
            with self.subTest(argv=argv):
                code, result, _ = self.run_cli(*argv)
                self.assertEqual((code, result["error"]["code"]), (1, error))
                self.assertTrue(result["error"]["correlationId"])

    def test_invalid_configuration_names_the_variable(self, _):
        for variable, value in (
            ("NCI_SIP_TIMEOUT_SECONDS", "0"),
            ("NCI_SIP_EMBEDDING_PROVIDER", "bogus"),
            ("NCI_SIP_EMBEDDING_MODEL", "all-MiniLM-L6-v2"),
        ):
            with self.subTest(variable):
                code, result, _ = self.run_cli("release-info", context="real", **{variable: value})
                self.assertEqual((code, result["error"]["code"]), (1, "invalid_request"))
                self.assertIn(variable, result["error"]["message"])
                self.assertEqual(result["error"]["details"]["parameter"], variable)
                self.assertTrue(result["error"]["correlationId"])

    def test_a_bad_secret_is_reported_by_variable_and_appears_nowhere(self, _):
        for variable, value in (
            ("NCI_SIP_CADSR_CREDENTIAL", "no-colon-secret"),
            ("NCI_SIP_EVS_LICENSE_KEY", "bad key secret"),
        ):
            with self.subTest(variable), self.assertLogs(level="DEBUG") as logs:
                logging.getLogger().debug("marker")
                code, result, stderr = self.run_cli(
                    "release-info", context="real", NCI_SIP_LOG_LEVEL="DEBUG", **{variable: value}
                )

                self.assertEqual((code, result["error"]["code"]), (1, "invalid_request"))
                self.assertEqual(result["error"]["details"]["parameter"], variable)
                reason = result["error"]["details"]["reason"]
                self.assertTrue(reason.startswith(variable))
                self.assertTrue(result["error"]["message"].startswith(reason))
                everything = json.dumps(result) + stderr + "\n".join(logs.output)
                self.assertNotIn("secret", everything)

    def test_valid_secrets_appear_nowhere_on_success_or_startup_failure(self, _):
        occupied = self.path / "occupied"
        occupied.write_text("not a directory")
        secrets = {
            "NCI_SIP_EVS_LICENSE_KEY": "licence-key-4711",
            "NCI_SIP_CADSR_CREDENTIAL": "reviewer:hunter2-secret",
        }
        for label, context, data_dir, expected in (
            ("success", "fake", self.path, 0),
            ("startup failure", "real", occupied, 1),
        ):
            with self.subTest(label), self.assertLogs(level="DEBUG") as logs:
                logging.getLogger().debug("marker")
                code, result, stderr = self.run_cli(
                    "release-info",
                    context=context,
                    NCI_SIP_LOG_LEVEL="DEBUG",
                    NCI_SIP_DATA_DIR=str(data_dir),
                    **secrets,
                )

                self.assertEqual(code, expected)
                everything = json.dumps(result) + stderr + "\n".join(logs.output)
                for part in ("licence-key-4711", "hunter2", "reviewer"):
                    self.assertNotIn(part, everything)

    def test_a_setting_error_that_names_no_variable_has_no_parameter_detail(self, _):
        with patch("nci_sip_mcp.cli.Settings.from_env", side_effect=ValueError("odd")):
            code, result, _ = self.run_cli("release-info", context="real")

        self.assertEqual((code, result["error"]["code"]), (1, "invalid_request"))
        self.assertNotIn("details", result["error"])

    def test_startup_failures_are_reported_not_raised(self, _):
        occupied = self.path / "occupied"
        occupied.write_text("not a directory")
        corrupt = self.path / "corrupt"
        corrupt.mkdir()
        (corrupt / "nci_si.sqlite3").write_text("not a database" * 100)
        for label, data_dir in {
            "data dir is a file": occupied,
            "corrupt database": corrupt,
        }.items():
            with self.subTest(label):
                code, result, _ = self.run_cli(
                    "search", "tumor", context="real", NCI_SIP_DATA_DIR=str(data_dir)
                )
                self.assertEqual((code, result["error"]["code"]), (1, "internal_error"))
                self.assertIn(str(data_dir), result["error"]["message"])

    def test_value_error_at_startup_is_reported(self, _):
        with patch("nci_sip_mcp.cli.Context", side_effect=ValueError("bad model name")):
            code, result, _ = self.run_cli("release-info", context="real")

        self.assertEqual((code, result["error"]["code"]), (1, "internal_error"))

    def test_serve_runs_the_server_and_keeps_stdout_for_the_protocol(self, _):
        server = MagicMock()
        with patch("nci_sip_mcp.cli.create_mcp", return_value=server) as create:
            code, printed, _ = self.run_cli("serve")

        self.assertEqual((code, printed), (0, None))
        server.run.assert_called_once_with()
        self.assertIsInstance(create.call_args.kwargs["context"], Context)

    def test_http_transport_can_be_selected_by_environment_or_cli_override(self, _):
        for arguments, environment in (
            ((), {"NCI_SIP_TRANSPORT": "streamable-http"}),
            (("--transport", "streamable-http"), {"NCI_SIP_TRANSPORT": "stdio"}),
        ):
            with self.subTest(arguments=arguments), patch("nci_sip_mcp.cli.run_http") as run:
                code, printed, stderr = self.run_cli("serve", *arguments, **environment)
            self.assertEqual((code, printed, stderr), (0, None, ""))
            self.assertEqual(run.call_args.args[0].transport, "streamable-http")

    def test_http_startup_failure_stays_on_stderr(self, _):
        with patch("nci_sip_mcp.cli.run_http", side_effect=OSError("cannot bind port")):
            code, printed, stderr = self.run_cli("serve", "--transport", "streamable-http")
        self.assertEqual((code, printed), (1, None))
        self.assertEqual(json.loads(stderr)["error"]["code"], "internal_error")

    def test_explicit_stdio_overrides_http_environment(self, _):
        with (
            patch("nci_sip_mcp.cli.create_mcp") as create,
            patch("nci_sip_mcp.cli.run_http") as http,
        ):
            code, printed, _ = self.run_cli(
                "serve", "--transport", "stdio", NCI_SIP_TRANSPORT="streamable-http"
            )
        self.assertEqual((code, printed), (0, None))
        self.assertEqual(create.call_args.args[0].transport, "stdio")
        http.assert_not_called()

    def test_required_http_auth_cannot_serve_stdio_whichever_way_stdio_is_chosen(self, _):
        for arguments, environment in (
            ((), {}),
            (("--transport", "stdio"), {"NCI_SIP_TRANSPORT": "streamable-http"}),
        ):
            with self.subTest(arguments=arguments), patch("nci_sip_mcp.cli.create_mcp") as create:
                code, printed, stderr = self.run_cli(
                    "serve", *arguments, NCI_SIP_HTTP_AUTH_MODE="required", **environment
                )
            error = json.loads(stderr)["error"]
            self.assertEqual((code, printed, error["code"]), (1, None, "invalid_request"))
            self.assertEqual(error["details"]["parameter"], "NCI_SIP_HTTP_AUTH_MODE")
            create.assert_not_called()

    def test_required_http_auth_does_not_stop_commands_that_never_serve(self, _):
        code, info, stderr = self.run_cli("release-info", NCI_SIP_HTTP_AUTH_MODE="required")

        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(info["selected_release"]["version"], "26.06e")

    def test_serve_failures_go_to_stderr(self, _):
        with patch("nci_sip_mcp.cli.create_mcp", side_effect=RuntimeError("mcp is missing")):
            code, printed, stderr = self.run_cli("serve")

        self.assertEqual((code, printed), (1, None))
        self.assertEqual(json.loads(stderr)["error"]["code"], "internal_error")
        self.assertIn("mcp is missing", stderr)

        code, printed, stderr = self.run_cli("serve", NCI_SIP_TIMEOUT_SECONDS="0")

        self.assertEqual((code, printed), (1, None))
        self.assertEqual(json.loads(stderr)["error"]["code"], "invalid_request")


class ProcessTest(unittest.TestCase):
    def test_results_go_to_stdout_and_diagnostics_to_stderr(self):
        with tempfile.TemporaryDirectory() as data_dir:
            # The developer's own NCI_SIP_* settings must not reach the subprocess.
            inherited = {
                key: value for key, value in os.environ.items() if not key.startswith("NCI_SIP_")
            }
            process = subprocess.run(
                [sys.executable, "-m", "nci_sip_mcp.cli", "lookup", "oops"],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
                env={
                    **inherited,
                    "PYTHONPATH": str(Path(nci_sip_mcp.__file__).parents[1]),
                    "NCI_SIP_DATA_DIR": data_dir,
                },
            )

        self.assertEqual(process.returncode, 1)
        self.assertEqual(json.loads(process.stdout)["error"]["code"], "invalid_request")
        records = [json.loads(line) for line in process.stderr.splitlines()]
        self.assertEqual([record["event"] for record in records], ["call_failed", "call_completed"])
        self.assertEqual(records[0]["responseCode"], "invalid_request")
        self.assertEqual(records[0]["level"], "WARNING")
        self.assertEqual(
            records[1]["correlationId"], json.loads(process.stdout)["error"]["correlationId"]
        )


class CommandValidationTest(ServerFixture):
    def test_invalid_environment_error_has_an_actionable_next_step(self):
        output = io.StringIO()
        with (
            patch.dict(os.environ, {"NCI_SIP_TIMEOUT_SECONDS": "0"}, clear=True),
            patch.object(sys, "argv", ["nci-sip-mcp", "release-info"]),
            redirect_stdout(output),
        ):
            status = cli.main()
        result = json.loads(output.getvalue())
        self.assertEqual(status, 1)
        self.assertEqual(result["error"]["details"]["parameter"], "NCI_SIP_TIMEOUT_SECONDS")
        self.assertTrue(
            result["error"]["message"].endswith("Fix the environment variable and rerun.")
        )

    def test_cli_requires_a_subcommand(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            cli.build_parser().parse_args([])
        self.assertEqual(raised.exception.code, 2)

    def test_cli_help_describes_each_registered_command(self):
        help_text = " ".join(cli.build_parser().format_help().split())
        for spec in SPECS:
            if spec.command:
                with self.subTest(command=spec.command):
                    self.assertIn(" ".join(spec.description.splitlines()[0].split()), help_text)


if __name__ == "__main__":
    unittest.main()
