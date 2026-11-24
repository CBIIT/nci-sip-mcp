import json
import sqlite3
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from fakes import concept, release, terminology_row
from nci_sip_mcp.errors import correlated
from nci_sip_mcp.evaluation import evaluate_build
from nci_sip_mcp.evaluation_sets import parse_set
from nci_sip_mcp.evs import EVSClient, EVSReleaseMismatchError, EVSResponseError
from nci_sip_mcp.index_adapter import manifest_record
from nci_sip_mcp.indexing import full_build
from nci_sip_mcp.registry import invoke
from nci_sip_mcp.release import ReleaseContext
from ncit_index import index as index_module
from ncit_index.errors import IndexBuildError, IndexCompatibilityError
from test_audit import captured, records
from test_evaluation_sets import sample_set_payload
from test_evs_client import FakeResponse
from test_index import normalized
from test_server import ServerFixture


def fixture_calibration(code, name):
    payload = sample_set_payload() | {
        "release": "26.06e",
        "queries": [{"query": name, "expected_codes": [code], "category": "name"}],
    }
    return parse_set(payload)


class FullBuildTest(ServerFixture):
    def setUp(self):
        super().setUp()
        invoke(self.context, "index_codes", ["C3262"])
        self.active = self.context.index.get_active_manifest()
        self.context.evs = EVSClient("https://evs.invalid", max_attempts=1)
        self.release = ReleaseContext("ncit", "monthly", "26.06e", None, "ncit_26.06e")

    def build_pages(self, pages):
        replies = [FakeResponse(json.dumps(page).encode()) for page in pages]
        with patch("nci_sip_mcp.http_client._open", side_effect=replies) as opened:
            result = full_build(self.context, self.release)
        return result, [call.args[0].full_url for call in opened.call_args_list]

    def test_all_pages_reconcile_before_inactive_build_and_progress_is_aggregated(self):
        first = [concept(f"C{number}") for number in range(1, 1001)]
        last = concept("C1001", active=False)
        with captured() as stream:
            built, urls = self.build_pages(
                [
                    {"total": 1001, "concepts": first},
                    {"total": 1001, "concepts": [last]},
                ]
            )
        self.assertEqual((built.concept_count, built.active), (1001, False))
        self.assertEqual(self.context.index.get_active_manifest(), self.active)
        self.assertEqual(len(urls), 2)
        self.assertTrue(all("/ncit_26.06e/search?" in url for url in urls))
        self.assertIn("pageSize=1000&fromRecord=1000", urls[1])
        self.assertIn("include=summary%2Cdefinitions%2Csynonyms%2Cproperties", urls[0])
        progress = records(stream, "index_download_progress")
        self.assertEqual([(row["pages"], row["concepts"]) for row in progress], [(2, 1001)])
        self.assertEqual(built.build_kind, "production")
        with self.assertRaisesRegex(IndexBuildError, "passing evaluation"):
            self.local_index.activate(built.build_id)
        evaluate_build(
            self.local_index,
            self.context.embedding_provider,
            fixture_calibration("C1001", last["name"]),
            built.build_id,
        )
        self.local_index.activate(built.build_id)
        self.assertFalse(self.context.index.get_concept_snapshot("C1001")[1].raw["active"])

    def test_shifted_page_duplicate_and_missing_concept_reject_before_database_write(self):
        first = [concept(f"C{number}") for number in range(1, 1001)]
        with self.assertRaisesRegex(EVSResponseError, "duplicate"):
            self.build_pages(
                [
                    {"total": 1001, "concepts": first},
                    {"total": 1001, "concepts": [first[-1]]},
                ]
            )
        self.assertEqual(
            [m.to_dict() for m in self.local_index.list_builds()], [self.active.to_dict()]
        )

    def test_changing_total_rejects_build(self):
        first = [concept(f"C{number}") for number in range(1, 1001)]
        with self.assertRaisesRegex(EVSResponseError, "total changed"):
            self.build_pages(
                [
                    {"total": 1001, "concepts": first},
                    {"total": 1002, "concepts": [concept("C1001")]},
                ]
            )
        self.assertEqual(
            [m.to_dict() for m in self.local_index.list_builds()], [self.active.to_dict()]
        )

    def test_invalid_totals_and_page_shapes_leave_active_untouched(self):
        pages = [
            {"total": value, "concepts": [concept("C1")]} for value in (None, True, 0, -1, "1")
        ]
        pages.extend(
            [
                {"total": 1},
                {"total": 1, "concepts": [None]},
                {"total": 2, "concepts": [concept("C1")]},
                {"total": 1, "concepts": [concept("C1"), concept("C2")]},
                {"total": 1, "concepts": [concept("")]},
                {"total": 1, "concepts": [concept("invalid")]},
                {"total": 1, "concepts": [concept("C1", " ")]},
            ]
        )
        for page in pages:
            with self.subTest(page=page), self.assertRaises(EVSResponseError):
                self.build_pages([page])
        self.assertEqual(
            [m.to_dict() for m in self.local_index.list_builds()], [self.active.to_dict()]
        )

    def test_every_page_requires_pinned_payload_release(self):
        for raw in (concept("C1", version="old"), concept("C1", version="")):
            with self.subTest(raw=raw), self.assertRaises(EVSReleaseMismatchError):
                self.build_pages([{"total": 1, "concepts": [raw]}])
        self.assertEqual(self.context.index.get_active_manifest(), self.active)

    def test_offline_rebuild_and_activation_commands_need_no_upstream(self):
        with patch("nci_sip_mcp.http_client._open", side_effect=AssertionError("offline")):
            result = invoke(self.context, "index_rebuild", self.active.build_id)
            listed = invoke(self.context, "index_builds")
            self.assertEqual(len(listed["builds"]), 2)
            self.assertEqual(result["manifest"]["version"], "26.06e")
            self.assertNotEqual(result["buildId"], self.active.build_id)
            activated = invoke(self.context, "index_activate", result["buildId"])
        self.assertEqual(activated["buildId"], self.context.index.get_active_manifest().build_id)

    def test_full_build_operator_command_selects_release_once_and_does_not_activate(self):
        replies = [
            FakeResponse(json.dumps([terminology_row()]).encode()),
            FakeResponse(json.dumps({"total": 1, "concepts": [concept("C1")]}).encode()),
        ]
        with (
            patch("nci_sip_mcp.http_client._open", side_effect=replies) as opened,
            patch(
                "nci_sip_mcp.handlers.production_set", return_value=fixture_calibration("C1", "C1")
            ),
        ):
            result = invoke(self.context, "index_build")
        self.assertEqual(opened.call_count, 2)
        self.assertEqual(result["manifest"]["concepts"], 1)
        self.assertTrue(result["evaluation"]["passed"])
        self.assertEqual(result["evaluation"]["build_id"], result["buildId"])
        self.assertEqual(self.context.index.get_active_manifest(), self.active)

    def test_unmatched_full_build_calibration_names_candidate_and_blocks_activation(self):
        replies = [
            FakeResponse(json.dumps([terminology_row()]).encode()),
            FakeResponse(json.dumps({"total": 1, "concepts": [concept("C1")]}).encode()),
        ]
        with patch("nci_sip_mcp.http_client._open", side_effect=replies):
            result = invoke(self.context, "index_build")
        candidate = next(b for b in self.local_index.list_builds() if not b.active)
        self.assertIn(candidate.build_id, result["error"]["message"])
        self.assertIn("calibration does not match", result["error"]["message"])
        with self.assertRaisesRegex(IndexBuildError, "passing evaluation"):
            self.local_index.activate(candidate.build_id)
        self.assertEqual(self.context.index.get_active_manifest(), self.active)

    def test_production_rebuild_reports_fresh_failed_evaluation_and_cannot_activate(self):
        candidate = self.local_index.build(
            normalized([concept("C1", "Target")]),
            None,
            self.context.embedding_provider,
            build_kind="production",
        )
        evaluate_build(
            self.local_index,
            self.context.embedding_provider,
            fixture_calibration("C1", "Target"),
            candidate.build_id,
        )
        with patch(
            "nci_sip_mcp.handlers.production_set",
            return_value=fixture_calibration("C2", "Missing"),
        ):
            result = invoke(self.context, "index_rebuild", candidate.build_id)
        self.assertFalse(result["evaluation"]["passed"])
        self.assertEqual(result["evaluation"]["gold_codes_not_indexed"], ["C2"])
        self.assertNotEqual(result["buildId"], candidate.build_id)
        with self.assertRaisesRegex(IndexBuildError, "passing evaluation"):
            self.local_index.activate(result["buildId"])
        self.assertEqual(self.context.index.get_active_manifest(), self.active)

    def test_evaluate_command_records_candidate_pass_without_activation(self):
        unavailable = invoke(self.context, "evaluate", build_id="missing")
        self.assertEqual(unavailable["error"]["code"], "internal_error")
        self.assertIn("unavailable", unavailable["error"]["message"])
        candidate = self.local_index.build(
            normalized([concept("C1", "Target")]),
            None,
            self.context.embedding_provider,
            build_kind="production",
        )
        with patch(
            "nci_sip_mcp.handlers.production_set",
            return_value=fixture_calibration("C1", "Target"),
        ):
            result = invoke(self.context, "evaluate", build_id=candidate.build_id)
        self.assertTrue(result["passed"])
        self.assertEqual(result["build_id"], candidate.build_id)
        self.assertEqual(self.context.index.get_active_manifest(), self.active)
        self.local_index.activate(candidate.build_id)
        self.assertEqual(self.context.index.get_active_manifest().evaluation_report, result)


class IndexOperatorResultTest(ServerFixture):
    def candidate(self):
        return self.local_index.build(
            normalized([concept("C1", "One", active=True)]), None, self.context.embedding_provider
        )

    def test_activate_returns_selected_build_and_its_manifest(self):
        candidate = self.candidate()
        result = invoke(self.context, "index_activate", candidate.build_id)
        active = self.context.index.get_active_manifest()
        with correlated(result["manifest"]["provenance"]["correlationId"]):
            self.assertEqual(
                result,
                {
                    "buildId": candidate.build_id,
                    "manifest": active.to_result(),
                    "pendingRemoval": [],
                },
            )
        self.assertEqual(active.build_id, candidate.build_id)

    def test_sample_rebuild_has_exact_ungated_build_shape(self):
        candidate = self.candidate()
        result = invoke(self.context, "index_rebuild", candidate.build_id)
        rebuilt = next(
            item for item in self.local_index.list_builds() if item.build_id == result["buildId"]
        )
        with correlated(result["manifest"]["provenance"]["correlationId"]):
            self.assertEqual(
                result,
                {"buildId": rebuilt.build_id, "manifest": manifest_record(rebuilt).to_result()},
            )
        self.assertIsNone(rebuilt.evaluation_version)


class SupersededBuildRemovalTest(ServerFixture):
    """Activation commits first; the builds it superseded are removed afterwards (#552)."""

    def setUp(self):
        super().setUp()
        self.active = self.candidate()
        self.local_index.activate(self.active.build_id)
        self.stale = sorted(self.candidate().build_id for _ in range(2))
        self.replacement = self.candidate()

    def candidate(self):
        return self.local_index.build(
            normalized([concept("C1", "One", active=True)]), None, self.context.embedding_provider
        )

    def listed(self):
        return {item["buildId"] for item in invoke(self.context, "index_builds")["builds"]}

    def rows(self, build_id):
        with sqlite3.connect(self.local_index.db_path) as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM manifests WHERE build_id = ?", (build_id,)
            ).fetchone()[0]

    def fail_second_removal(self):
        """The second superseded build cannot be deleted: the database is busy."""

        real = index_module.delete_build
        removed = []

        def flaky(conn, build_id):
            removed.append(build_id)
            if len(removed) > 1:
                busy = sqlite3.OperationalError("database is locked")
                busy.sqlite_errorcode = sqlite3.SQLITE_BUSY
                raise busy
            real(conn, build_id)

        return patch.object(index_module, "delete_build", flaky)

    def test_a_failed_removal_is_reported_and_leaves_the_activation_and_the_rollback_target(self):
        with captured() as stream, self.fail_second_removal():
            result = invoke(self.context, "index_activate", self.replacement.build_id)

        active = self.context.index.get_active_manifest()
        with correlated(result.get("manifest", {}).get("provenance", {}).get("correlationId")):
            self.assertEqual(
                result,
                {
                    "buildId": self.replacement.build_id,
                    "manifest": active.to_result(),
                    "pendingRemoval": [self.stale[1]],
                },
            )
        self.assertEqual(active.build_id, self.replacement.build_id)
        self.assertEqual(self.listed(), {self.active.build_id, self.replacement.build_id})
        self.assertEqual(self.rows(self.stale[0]), 0)
        pending = records(stream, "index_build_removal_pending")
        self.assertEqual(
            [(item["level"], item["build_ids"]) for item in pending],
            [("WARNING", [self.stale[1]])],
        )

    def test_the_next_activation_removes_what_is_left_and_the_rollback_target_survives(self):
        with self.fail_second_removal():
            invoke(self.context, "index_activate", self.replacement.build_id)

        hidden = invoke(self.context, "index_activate", self.stale[1])
        again = invoke(self.context, "index_activate", self.replacement.build_id)
        rollback = invoke(self.context, "index_activate", self.active.build_id)
        forward = invoke(self.context, "index_activate", self.replacement.build_id)

        self.assertEqual(hidden.get("error", {}).get("code"), "internal_error")
        self.assertEqual(again.get("pendingRemoval"), [])
        self.assertEqual(self.rows(self.stale[1]), 0)
        self.assertEqual(rollback.get("buildId"), self.active.build_id)
        self.assertEqual(forward.get("buildId"), self.replacement.build_id)
        self.assertEqual(self.listed(), {self.active.build_id, self.replacement.build_id})

    def test_a_removal_failure_that_is_not_a_storage_failure_propagates_after_activation(self):
        with sqlite3.connect(self.local_index.db_path) as conn:
            conn.execute(
                "CREATE TRIGGER refuse_delete BEFORE DELETE ON manifests "
                "BEGIN SELECT RAISE(ABORT, 'refused'); END"
            )

        with self.assertRaises(sqlite3.IntegrityError):
            invoke(self.context, "index_activate", self.replacement.build_id)

        self.assertEqual(
            self.context.index.get_active_manifest().build_id, self.replacement.build_id
        )
        self.assertEqual(self.listed(), {self.active.build_id, self.replacement.build_id})


class FullBuildInputValidationTest(ServerFixture):
    def setUp(self):
        super().setUp()
        self.context.evs = EVSClient("https://example.invalid")

    def test_downloaded_build_must_match_the_requested_release(self):
        with (
            patch.object(
                self.context.evs,
                "get_index_page",
                return_value=(1, [concept("C1", version="wrong")]),
            ),
            self.assertRaises(IndexCompatibilityError),
        ):
            full_build(self.context, release())
        self.assertEqual(self.local_index.list_builds(), [])

    def test_full_build_rejects_null_nontext_and_whitespace_names(self):
        for name in (None, 7, " \t\n "):
            with (
                self.subTest(name=name),
                patch.object(
                    self.context.evs,
                    "get_index_page",
                    return_value=(
                        1,
                        [concept("C1", synonyms=[{"name": "Searchable alias"}]) | {"name": name}],
                    ),
                ),
                self.assertRaisesRegex(EVSResponseError, "preferred name"),
            ):
                full_build(self.context, release())
        self.assertEqual(self.local_index.list_builds(), [])


class FullBuildRequestTest(unittest.TestCase):
    def setUp(self):
        self.client = EVSClient("https://evs.invalid", max_attempts=1)
        self.release = release()

    def reply(self, value):
        return patch(
            "nci_sip_mcp.http_client._open", return_value=FakeResponse(json.dumps(value).encode())
        )

    def test_full_build_requests_all_indexed_fields_and_rejects_zero_total(self):
        with self.reply({"total": 1, "concepts": [concept("C1")]}) as opened:
            total, rows = self.client.get_index_page(self.release, 0)
        self.assertEqual((total, rows[0]["code"]), (1, "C1"))
        params = parse_qs(urlsplit(opened.call_args.args[0].full_url).query)
        self.assertEqual(params["include"], ["summary,definitions,synonyms,properties"])
        with self.reply({"total": 0, "concepts": []}), self.assertRaises(EVSResponseError):
            self.client.get_index_page(self.release, 0)
