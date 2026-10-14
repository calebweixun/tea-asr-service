from __future__ import annotations

import numpy as np
import pytest

from benchmarks.embedding_asr_probe import (
    ProbeItem,
    build_rule_variant_pairs,
    edit_distance,
    edit_distance_bucket,
    normalize_text,
    pairwise_accuracy,
    random_retrieval_baseline,
    retrieval_metrics,
)
from tea_asr.context import ReplacementRule


def test_retrieval_metrics_and_random_baseline() -> None:
    scores = np.tile(np.arange(10, 0, -1, dtype=np.float32), (3, 1))
    targets = [0, 2, 5]

    result = retrieval_metrics(scores, targets)

    assert result["query_count"] == 3
    assert result["top1"] == 1 / 3
    assert result["top5"] == 2 / 3
    assert result["mrr"] == (1 + 1 / 3 + 1 / 6) / 3
    assert result["random_baseline"]["top1"] == pytest.approx(0.1)
    assert result["random_baseline"]["top5"] == pytest.approx(0.5)
    assert result["random_baseline"]["mrr"] == pytest.approx(
        sum(1 / n for n in range(1, 11)) / 10
    )
    assert random_retrieval_baseline(0, [1])["mrr"] is None


def test_duplicate_references_are_all_relevant_for_ranking() -> None:
    result = retrieval_metrics(
        np.asarray([[0.2, 0.9, 0.1]], dtype=np.float32),
        [{0, 1}],
    )

    assert result["top1"] == 1.0
    assert result["random_baseline"]["top1"] == pytest.approx(2 / 3)


def test_pairwise_accuracy_counts_strict_wins_and_ties() -> None:
    audio = np.asarray([[1, 0], [1, 0], [1, 0]], dtype=np.float32)
    preferred = np.asarray([[1, 0], [0, 1], [1, 0]], dtype=np.float32)
    alternative = np.asarray([[0, 1], [1, 0], [1, 0]], dtype=np.float32)

    result = pairwise_accuracy(audio, preferred, alternative)

    assert result["count"] == 3
    assert result["wins"] == 1
    assert result["ties"] == 1
    assert result["accuracy"] == 1 / 3


def test_cer_pronoun_normalization_and_edit_distance_buckets() -> None:
    assert normalize_text("Ａ、I") == normalize_text("a i")
    assert edit_distance("甲乙", "甲丙") == 1
    assert edit_distance_bucket(1) == "1_char"
    assert edit_distance_bucket(2) == "2_3_chars"
    assert edit_distance_bucket(3) == "2_3_chars"
    assert edit_distance_bucket(4) == "4_plus_chars"
    assert edit_distance_bucket(0) is None


def test_rule_variant_pairs_require_applicable_correction() -> None:
    items = (
        ProbeItem("good", 0.0, 1.0, "we praise good", "we praise goof"),
        ProbeItem("target-missing", 1.0, 2.0, "we praise God", "we praise goof"),
        ProbeItem("already-correct", 2.0, 3.0, "we praise good", "we praise good"),
        ProbeItem("source-missing", 3.0, 4.0, "we praise good", "we praise great"),
    )
    rules = (ReplacementRule(source="goof", target="good"),)

    pairs = build_rule_variant_pairs(items, rules)

    assert len(pairs) == 1
    assert pairs[0].item_index == 0
    assert pairs[0].from_text == "we praise goof"
    assert pairs[0].to_text == "we praise good"
