"""Evaluation input errors must not silently shrink or weaken a regression gate."""

import json
import tempfile
import unittest
from pathlib import Path

from nci_sip_mcp.evaluation_sets import load_set, parse_set, production_set


def sample_set_payload():
    return {
        "version": "test-only-v1",
        "queries": [{"query": "Neoplasm", "expected_codes": ["C3262"], "category": "name"}],
        "thresholds": {
            "semantic": {"hit_at_5": 1, "mrr_at_10": 0.95},
            "hybrid": {"hit_at_5": 0.8, "mrr_at_10": 0.75},
        },
        "embedding_provider": "hashing",
        "embedding_model": "hashing-128",
        "embedding_dimensions": 128,
        "release": "26.09d",
        "test_only": True,
    }


class EvaluationSetTests(unittest.TestCase):
    def test_shipped_floors_use_full_corpus_measurement_and_agreed_margins(self):
        result = production_set()
        evidence = json.loads(
            (
                Path(__file__).resolve().parents[1] / "docs/evaluation/ncit-v1-full-corpus.json"
            ).read_text()
        )
        measured = {row["mode"]: row for row in evidence["results"]}
        self.assertFalse(result.test_only)
        self.assertEqual(len(result.queries), 12)
        self.assertEqual(result.embedding_dimensions, 768)
        self.assertEqual(evidence["build"]["manifest"]["concept_count"], 213524)
        self.assertEqual(result.semantic.hit_at_5, 10 / 12)
        self.assertEqual(result.hybrid.hit_at_5, 11 / 12)
        self.assertAlmostEqual(
            result.semantic.mrr_at_10, measured["vector"]["mean_reciprocal_rank"] - 0.05
        )
        self.assertAlmostEqual(
            result.hybrid.mrr_at_10, measured["hybrid"]["mean_reciprocal_rank"] - 0.05
        )

    def test_versioned_set_preserves_judgments_floors_and_measurement_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "set.json"
            path.write_text(json.dumps(sample_set_payload()), encoding="utf-8")
            result = load_set(path)
        self.assertEqual(result.version, "test-only-v1")
        self.assertEqual(result.queries[0].query, "Neoplasm")
        self.assertEqual(result.queries[0].expected_codes, ("C3262",))
        self.assertEqual(result.queries[0].category, "name")
        self.assertEqual((result.semantic.hit_at_5, result.semantic.mrr_at_10), (1, 0.95))
        self.assertEqual((result.hybrid.hit_at_5, result.hybrid.mrr_at_10), (0.8, 0.75))
        self.assertEqual(
            (result.embedding_provider, result.embedding_model), ("hashing", "hashing-128")
        )
        self.assertEqual(result.release, "26.09d")
        self.assertEqual(result.embedding_dimensions, 128)
        self.assertTrue(result.test_only)

    def test_empty_queries_and_missing_expected_codes_cannot_change_denominator(self):
        for invalid in ([], None, {}, "queries"):
            with self.subTest(invalid=invalid):
                payload = sample_set_payload()
                payload["queries"] = invalid
                with self.assertRaisesRegex(ValueError, "queries"):
                    parse_set(payload)
        for invalid in ([], ["C3262", "C3262"], ["not-a-code"], [True], "C3262"):
            with self.subTest(codes=invalid):
                payload = sample_set_payload()
                payload["queries"][0]["expected_codes"] = invalid
                with self.assertRaisesRegex(ValueError, "expected_codes"):
                    parse_set(payload)

    def test_duplicate_queries_are_rejected_instead_of_reweighting_one_scenario(self):
        payload = sample_set_payload()
        payload["queries"] *= 2
        with self.assertRaisesRegex(ValueError, "distinct"):
            parse_set(payload)

    def test_floors_reject_nonfinite_out_of_range_and_boolean_values(self):
        for key in ("hit_at_5", "mrr_at_10"):
            for invalid in (float("nan"), float("inf"), -0.1, 1.01, 10**400, True, "0.9", None):
                with self.subTest(key=key, invalid=invalid):
                    payload = sample_set_payload()
                    payload["thresholds"]["semantic"][key] = invalid
                    with self.assertRaisesRegex(ValueError, "Evaluation"):
                        parse_set(payload)

    def test_missing_unknown_or_unmeasured_thresholds_are_rejected(self):
        for invalid in ({}, {"semantic": {}}, {"semantic": None, "hybrid": None}):
            with self.subTest(invalid=invalid):
                payload = sample_set_payload()
                payload["thresholds"] = invalid
                with self.assertRaisesRegex(ValueError, "Evaluation"):
                    parse_set(payload)

    def test_measurement_identity_and_test_label_must_be_explicit(self):
        for key in ("version", "embedding_provider", "embedding_model", "release"):
            with self.subTest(key=key):
                payload = sample_set_payload()
                payload[key] = " "
                with self.assertRaisesRegex(ValueError, "nonempty text"):
                    parse_set(payload)
        payload = sample_set_payload()
        payload["test_only"] = "false"
        with self.assertRaisesRegex(ValueError, "boolean"):
            parse_set(payload)

    def test_missing_and_unknown_fields_are_rejected(self):
        payload = sample_set_payload()
        del payload["version"]
        with self.assertRaisesRegex(ValueError, "exactly"):
            parse_set(payload)
        payload = sample_set_payload() | {"typo": True}
        with self.assertRaisesRegex(ValueError, "exactly"):
            parse_set(payload)

    def test_calibrated_dimensions_must_be_a_positive_integer(self):
        for value in (0, -1, True, 128.0, "128", None):
            with self.subTest(value=value):
                payload = sample_set_payload() | {"embedding_dimensions": value}
                with self.assertRaisesRegex(ValueError, "positive integer"):
                    parse_set(payload)

    def test_malformed_file_does_not_become_an_empty_set(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "set.json"
            path.write_text("{broken", encoding="utf-8")
            with self.assertRaises(json.JSONDecodeError):
                load_set(path)

    def test_duplicate_json_keys_cannot_silently_replace_a_floor(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "set.json"
            path.write_text('{"hit_at_5": 1, "hit_at_5": 0}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "repeats key"):
                load_set(path)


class CalibrationReleaseTest(unittest.TestCase):
    def test_shipped_calibration_names_the_measured_release(self):
        self.assertEqual(production_set().release, "26.09d")
