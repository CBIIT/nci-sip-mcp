import json
import tempfile
import unittest

from fakes import concept
from nci_sip_mcp.evaluation import GoldQuery, evaluate_retrieval
from ncit_index.embeddings import HashingEmbeddingProvider
from ncit_index.index import LocalIndex
from test_index import IndexTestCase, normalized

# "sarcoma" matches seven concepts. BM25 ranks them by how often the term
# occurs, so C1 is first, C2 second and C7 seventh.
CONCEPTS = [
    concept(f"C{rank}", " ".join(["sarcoma"] * (8 - rank) + [f"filler{rank}"] * rank))
    for rank in range(1, 8)
] + [concept("C8", "unrelated melanoma")]


class EvaluationTest(unittest.TestCase):
    def evaluate(self, expected_codes):
        with tempfile.TemporaryDirectory() as tmp_path:
            index = LocalIndex(tmp_path)
            provider = HashingEmbeddingProvider()
            index.upsert_concepts(normalized(CONCEPTS, "2026-06-29"), "2026-06-29", provider)
            return evaluate_retrieval(
                index,
                provider,
                gold_queries=[GoldQuery("sarcoma", tuple(codes)) for codes in expected_codes],
                modes=["bm25"],
            )[0]

    def test_metrics_follow_the_rank_of_the_first_expected_code(self):
        cases = {
            "first": ([["C1"]], (1.0, 1.0, 1.0)),
            "second": ([["C2"]], (0.0, 1.0, 0.5)),
            "beyond five": ([["C7"]], (0.0, 0.0, 1 / 7)),
            "best of several": ([["C7", "C2"]], (0.0, 1.0, 0.5)),
            "missing": ([["C8"]], (0.0, 0.0, 0.0)),
            "averaged": ([["C1"], ["C2"], ["C8"], ["C7"]], (0.25, 0.5, (1 + 0.5 + 0 + 1 / 7) / 4)),
        }
        for label, (expected_codes, metrics) in cases.items():
            with self.subTest(label):
                result = self.evaluate(expected_codes)
                self.assertEqual(result.query_count, len(expected_codes))
                self.assertAlmostEqual(result.hit_at_1, metrics[0])
                self.assertAlmostEqual(result.hit_at_5, metrics[1])
                self.assertAlmostEqual(result.mean_reciprocal_rank, metrics[2])

    def test_every_mode_is_evaluated_by_default(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            index = LocalIndex(tmp_path)
            provider = HashingEmbeddingProvider()
            index.upsert_concepts(normalized(CONCEPTS, "2026-06-29"), "2026-06-29", provider)

            results = evaluate_retrieval(
                index, provider, gold_queries=[GoldQuery("melanoma", ("C8",))]
            )

        self.assertEqual([result.mode for result in results], ["bm25", "vector", "hybrid"])
        for result in results:
            self.assertEqual(result.hit_at_1, 1.0)
            self.assertEqual(result.to_dict()["mode"], result.mode)

    def test_query_report_exposes_rankings_misses_and_reciprocal_rank_denominator(self):
        result = self.evaluate([["C2", "C7"], ["C8"]])
        report = json.loads(json.dumps(result.to_dict()))
        self.assertEqual(report["query_count"], 2)
        self.assertEqual(report["limit"], 10)
        self.assertEqual(report["mean_reciprocal_rank"], 0.25)
        found, missing = report["per_query"]
        self.assertEqual(found["query"], "sarcoma")
        self.assertEqual(found["category"], "furnished")
        self.assertEqual(found["expected_codes"], ["C2", "C7"])
        self.assertEqual(found["ranked_codes"], [f"C{rank}" for rank in range(1, 8)])
        self.assertEqual(found["first_expected_rank"], 2)
        self.assertIsNone(missing["first_expected_rank"])
        self.assertEqual(missing["expected_codes"], ["C8"])

    def test_empty_evaluation_and_cutoff_below_five_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            index = LocalIndex(directory)
            provider = HashingEmbeddingProvider()
            with self.assertRaisesRegex(ValueError, "nonempty"):
                evaluate_retrieval(index, provider, gold_queries=[])
            with self.assertRaisesRegex(ValueError, "modes must be nonempty"):
                evaluate_retrieval(index, provider, modes=[])
            for limit in (4, 0, True, 5.0):
                with self.subTest(limit=limit), self.assertRaisesRegex(ValueError, "at least five"):
                    evaluate_retrieval(index, provider, limit=limit)

    def test_evaluation_can_target_an_inactive_build_without_changing_active_results(self):
        with tempfile.TemporaryDirectory() as directory:
            index = LocalIndex(directory)
            provider = HashingEmbeddingProvider()
            index.upsert_concepts(normalized([concept("C1", "sarcoma")]), None, provider)
            active = index.get_active_manifest()
            candidate = index.build(normalized([concept("C8", "melanoma")]), None, provider)
            results = evaluate_retrieval(
                index, provider, [GoldQuery("melanoma", ("C8",))], build_id=candidate.build_id
            )
            for result in results:
                self.assertEqual(result.hit_at_1, 1)
                self.assertEqual(result.per_query[0].ranked_codes, ("C8",))
            self.assertEqual(index.get_active_manifest(), active)
            self.assertIsNone(index.get_concept_snapshot("C8")[1])


class MetricBoundaryTest(IndexTestCase):
    def test_rank_five_counts_in_hit_at_five(self):
        index = self.build([concept(f"C{i}", "Same") for i in range(1, 7)])
        results = evaluate_retrieval(index, self.provider, [GoldQuery("Same", ("C5",))])
        for result in results:
            with self.subTest(mode=result.mode):
                self.assertEqual(result.per_query[0].first_expected_rank, 5)
                self.assertEqual(result.hit_at_5, 1.0)
                self.assertEqual(result.mean_reciprocal_rank, 0.2)


if __name__ == "__main__":
    unittest.main()
