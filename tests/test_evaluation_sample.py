"""CI exercises real ranking and reporting, without claiming semantic quality from hashing."""

import json
import tempfile
import unittest
from pathlib import Path

from fakes import concept
from nci_sip_mcp.evaluation import evaluate_build
from nci_sip_mcp.evaluation_sets import load_set, production_set
from nci_sip_mcp.registry import invoke
from ncit_index.embeddings import HashingEmbeddingProvider
from ncit_index.errors import IndexBuildError
from ncit_index.index import LocalIndex
from test_index import normalized
from test_server import ServerFixture

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/evaluation"


def recorded_sample():
    rows = [
        json.loads(path.read_text())["response"]["body"]
        for path in sorted((ROOT / "acceptance/fixtures/recorded/evs/concepts").glob("*.json"))
    ]
    return rows + [
        json.loads((FIXTURES / f"{code}.json").read_text())["body"]
        for code in ("C40704", "C116938")
    ]


class RecordedEvaluationTests(unittest.TestCase):
    def test_recorded_sample_runs_through_the_gate_with_explicit_test_only_calibration(self):
        dataset = load_set(FIXTURES / "test-only-v1.json")
        self.assertTrue(dataset.test_only)
        self.assertEqual(dataset.embedding_provider, "hashing")
        with tempfile.TemporaryDirectory() as directory:
            index = LocalIndex(directory)
            provider = HashingEmbeddingProvider()
            candidate = index.build(
                normalized(recorded_sample()), None, provider, build_kind="production"
            )
            with self.assertRaises(IndexBuildError):
                index.activate(candidate.build_id)
            evaluated = evaluate_build(index, provider, dataset, candidate.build_id)
            report = json.loads(json.dumps(evaluated.evaluation_report))
            self.assertTrue(report["passed"])
            self.assertEqual(report["gold_codes_not_indexed"], [])
            # Includes 19 cohort descendants and two source-specific status/history concepts.
            self.assertEqual(report["concept_count"], 167)
            self.assertEqual(report["evaluation_version"], "test-only-ncit-v1")
            self.assertEqual(report["release"], "26.09d")
            self.assertEqual(report["embedding_dimensions"], 128)
            self.assertIsNone(index.get_active_manifest())
            self.assertTrue(index.activate(candidate.build_id).active)
        bm25 = report["results"][0]
        self.assertEqual(bm25["mode"], "bm25")
        self.assertEqual(bm25["hit_at_1"], 11 / 12)
        self.assertEqual(bm25["hit_at_5"], 11 / 12)
        self.assertEqual(bm25["mean_reciprocal_rank"], 11 / 12)
        self.assert_exact_names(report)

    def assert_exact_names(self, report):
        for mode in report["results"]:
            for query in mode["per_query"]:
                if query["category"] == "preferred-name":
                    with self.subTest(mode=mode["mode"], query=query["query"]):
                        self.assertEqual(query["first_expected_rank"], 1)
                        self.assertIn(query["ranked_codes"][0], query["expected_codes"])


class LegacyEvaluationTest(ServerFixture):
    def candidate(self):
        return self.local_index.build(
            normalized([concept("C1", "One", active=True)]), None, self.context.embedding_provider
        )

    def test_legacy_build_is_scored_without_the_gate(self):
        # A snapshot written before builds were classified has no build_kind in its manifest.
        candidate = self.candidate()
        payload = candidate.to_dict()
        del payload["build_kind"]
        with self.local_index._connect() as conn:
            conn.execute("UPDATE manifests SET payload = ?", (json.dumps(payload),))
        result = invoke(self.context, "evaluate", candidate.build_id)
        self.assertEqual(result["build_id"], candidate.build_id)
        self.assertEqual(result["evaluation_version"], production_set().version)
        self.assertFalse(result["gate_applies"])
        self.assertEqual([item["mode"] for item in result["results"]], ["bm25", "vector", "hybrid"])
        self.assertIsNone(self.local_index.list_builds()[0].evaluation_report)
