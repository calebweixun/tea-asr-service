import pytest

pytest.importorskip("pypinyin")  # optional benchmark dependency, absent in CI

from benchmarks.cer_eval import normalize_for_scoring
from benchmarks.error_phonetics import (
    analyze_normalized_item,
    classify_substitution_run,
)


def test_classifies_homophone_near_phone_non_phonetic_and_non_han_runs() -> None:
    homophone = classify_substitution_run("是", "事")
    assert homophone.name == "homophone_toneless"
    assert homophone.tone_identical_pairs == 1

    assert classify_substitution_run("知", "資").name == "near_phone"
    assert classify_substitution_run("安", "昂").name == "near_phone"
    assert classify_substitution_run("貓", "狗").name == "non_phonetic"
    assert classify_substitution_run("A", "1").name == "non_han"


def test_heteronym_reading_can_make_a_character_pair_homophonic() -> None:
    result = classify_substitution_run("重", "蟲")

    assert result.name == "homophone_toneless"
    assert result.tone_identical_pairs == 1


def test_alignment_splits_substitution_runs_at_matches() -> None:
    result = analyze_normalized_item("甲是乙知丙", "甲事乙資丙")

    assert (result.substitutions, result.deletions, result.insertions) == (2, 0, 0)
    assert result.class_counts == {
        "homophone_toneless": 1,
        "near_phone": 1,
    }
    assert len(result.examples["homophone_toneless"]) == 1
    assert len(result.examples["near_phone"]) == 1


def test_unequal_length_span_counts_edits_and_checks_concatenated_pinyin() -> None:
    result = analyze_normalized_item("甲先乙", "甲西安乙")

    assert result.substitutions == 1
    assert result.insertions == 1
    assert result.class_counts == {"span_mismatch": 2}
    assert result.span_mismatch_homophone_spans == 1


def test_scoring_normalization_uses_cer_pronoun_folding() -> None:
    assert normalize_for_scoring("祂它祢", fold_pronouns=True) == "他他你"
