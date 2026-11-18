import sys
import types
import unittest
from unittest.mock import patch

from nci_sip_mcp.errors import InputValidationError
from nci_sip_mcp.validation import (
    validate_ncit_code,
    validate_ncit_codes,
    validate_search,
    validate_traversal,
)
from ncit_index.embeddings import HashingEmbeddingProvider, create_embedding_provider


class ValidationTest(unittest.TestCase):
    def test_codes_are_normalized_and_deduplicated_in_order(self):
        self.assertEqual(validate_ncit_code(" c3262 "), "C3262")
        self.assertEqual(validate_ncit_codes(["c40704", " C3262 ", "C40704"]), ["C40704", "C3262"])

    def test_a_rejected_argument_is_named_with_the_reason(self):
        ok = {"max_depth": 1, "max_nodes": 10, "max_edges": 10, "edge_types": None}

        def traversal(start_codes=("C1",), direction="out", **changes):
            return lambda: validate_traversal(start_codes, direction, **{**ok, **changes})

        rejections = {
            "code": lambda: validate_ncit_code("oops"),
            "codes": lambda: validate_ncit_codes([]),
            "query": lambda: validate_search(" ", 10, "bm25"),
            "limit": lambda: validate_search("x", 0, "bm25"),
            "mode": lambda: validate_search("x", 10, "fuzzy"),
            "start_codes": traversal(["oops"]),
            "direction": traversal(direction="sideways"),
            "max_depth": traversal(max_depth=-1),
            "max_nodes": traversal(max_nodes=0),
            "max_edges": traversal(max_edges=0),
            "edge_types": traversal(edge_types=["bogus"]),
            "relationship_names": traversal(relationship_names=[" "]),
        }
        for parameter, call in rejections.items():
            with self.subTest(parameter), self.assertRaises(InputValidationError) as raised:
                call()
            self.assertEqual(
                raised.exception.details, {"parameter": parameter, "reason": str(raised.exception)}
            )

    def test_more_start_codes_than_the_node_limit_names_the_start_codes(self):
        with self.assertRaises(InputValidationError) as raised:
            validate_traversal(["C1", "C2"], "out", 1, 1, 10, None)

        self.assertEqual(raised.exception.details["parameter"], "start_codes")

    def test_code_must_be_c_followed_by_digits_only(self):
        arabic_digits = "C١٢"  # noqa: RUF001 - digits that are not ASCII must be rejected
        for code in (
            "",
            "C",
            "3262",
            "C12/children",
            "C12?x=1",
            "C1\n2",
            "C12 3",
            arabic_digits,
            None,
        ):
            with self.subTest(code=code), self.assertRaises(InputValidationError):
                validate_ncit_code(code)
        with self.assertRaises(InputValidationError):
            validate_ncit_codes([])

    def test_search_arguments_are_normalized(self):
        self.assertEqual(validate_search(" tumor ", 1, "BM25"), ("tumor", 1, "bm25"))
        self.assertEqual(validate_search("tumor", 100, "hybrid"), ("tumor", 100, "hybrid"))

    def test_search_rejects_blank_query_bad_limit_and_unknown_mode(self):
        for arguments in (
            (" ", 10, "hybrid"),
            ("tumor", 0, "hybrid"),
            ("tumor", 101, "hybrid"),
            ("tumor", -1, "hybrid"),
            ("tumor", True, "hybrid"),
            ("tumor", "10", "hybrid"),
            ("tumor", 10, "fuzzy"),
            ("tumor", 10, ""),
        ):
            with self.subTest(arguments=arguments), self.assertRaises(InputValidationError):
                validate_search(*arguments)

    def test_traversal_arguments_are_normalized(self):
        self.assertEqual(
            validate_traversal(
                ["c3262", "C3262"], "OUT", 0, 1, 1, ["Role", "role", " child "], [" Has_Finding "]
            ),
            (["C3262"], "out", ["role", "child"], ["Has_Finding"]),
        )
        self.assertEqual(
            validate_traversal(["C3262"], "both", 4, 10, 10, None), (["C3262"], "both", None, None)
        )
        self.assertEqual(
            validate_traversal(["C3262"], "in", 1, 10, 10, [], []), (["C3262"], "in", None, None)
        )

    def test_traversal_rejects_each_invalid_argument(self):
        valid = {
            "start_codes": ["C3262"],
            "direction": "out",
            "max_depth": 1,
            "max_nodes": 10,
            "max_edges": 10,
            "edge_types": None,
            "relationship_names": None,
        }
        for field, value in (
            ("start_codes", []),
            ("start_codes", ["oops"]),
            ("direction", "sideways"),
            ("max_depth", -1),
            ("max_depth", True),
            ("max_depth", 1.5),
            ("max_nodes", 0),
            ("max_nodes", True),
            ("max_edges", 0),
            ("edge_types", ["unknown"]),
            ("relationship_names", ["Has_Finding", " "]),
            ("relationship_names", [None]),
        ):
            with self.subTest(field=field, value=value), self.assertRaises(InputValidationError):
                validate_traversal(**dict(valid, **{field: value}))

    def test_start_codes_must_fit_the_effective_node_limit(self):
        def traversal(count, max_nodes):
            return validate_traversal(
                [f"C{number}" for number in range(1, count + 1)], "out", 1, max_nodes, 10, None
            )

        self.assertEqual(len(traversal(2, 2)[0]), 2)
        self.assertEqual(len(traversal(1000, 5000)[0]), 1000)
        with self.assertRaisesRegex(
            InputValidationError,
            r"^3 start codes exceed the node limit of 2 \(max_nodes, at most 1000\)$",
        ):
            traversal(3, 2)
        # Codes are counted after duplicates are removed.
        with self.assertRaisesRegex(InputValidationError, "^3 start codes"):
            validate_traversal(["C1", "c1", "C2", "C3"], "out", 1, 2, 10, None)
        # max_nodes is clamped to 1,000, and the message names that limit.
        with self.assertRaisesRegex(InputValidationError, "1001 start codes .* node limit of 1000"):
            traversal(1001, 5000)


class EmbeddingConfigurationTest(unittest.TestCase):
    def test_hashing_provider_is_the_default_pair(self):
        provider = create_embedding_provider(" Hashing ", "hashing")

        self.assertEqual((provider.name, provider.model), ("hashing", "hashing-128"))
        self.assertEqual(len(provider.embed(["tumor"])[0]), 128)

    def test_provider_and_model_must_agree(self):
        for provider, model in (
            ("hashing", "all-MiniLM-L6-v2"),
            ("sentence-transformers", "hashing"),
            ("sentence-transformers", " "),
            ("unknown", "hashing"),
        ):
            with (
                self.subTest(provider=provider, model=model),
                self.assertRaises(ValueError) as raised,
            ):
                create_embedding_provider(provider, model)
            self.assertIn("NCI_SIP_EMBEDDING_", str(raised.exception))

    def test_hashing_needs_at_least_one_dimension(self):
        with self.assertRaises(ValueError):
            HashingEmbeddingProvider(0)

    def test_blank_text_embeds_to_the_zero_vector(self):
        self.assertEqual(HashingEmbeddingProvider(4).embed(["  ", ""]), [[0.0] * 4] * 2)

    def test_sentence_transformers_provider_wraps_the_named_model(self):
        class FakeModel:
            def __init__(self, name):
                self.name = name

            def encode(self, texts, normalize_embeddings, show_progress_bar=True):
                # Like the library, it takes a list and returns rows that are not lists.
                if not isinstance(texts, list):
                    raise TypeError(type(texts))
                if show_progress_bar:
                    raise ValueError("Progress output would corrupt structured diagnostics")
                return [(len(text), int(normalize_embeddings), len(self.name)) for text in texts]

        library = types.SimpleNamespace(SentenceTransformer=FakeModel)
        with patch.dict(sys.modules, {"sentence_transformers": library}):
            provider = create_embedding_provider("Sentence-Transformers", "a-model")

        self.assertEqual((provider.name, provider.model), ("sentence-transformers", "a-model"))
        # The named model is loaded and asked for unit vectors.
        vectors = provider.embed(iter(["ab", "c"]))
        self.assertEqual([row.tolist() for row in vectors], [[2.0, 1.0, 7.0], [1.0, 1.0, 7.0]])
        self.assertEqual({row.dtype.str for row in vectors}, {"<f4"})

    def test_missing_embeddings_extra_is_explained(self):
        with (
            patch.dict(sys.modules, {"sentence_transformers": None}),
            self.assertRaises(RuntimeError) as raised,
        ):
            create_embedding_provider("sentence-transformers", "all-MiniLM-L6-v2")

        self.assertIn("'embeddings' extra", str(raised.exception))
        self.assertIn(f"Import failed: {raised.exception.__cause__}", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
