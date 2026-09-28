import json
from pathlib import Path

from benchmarks.cer_eval import (
    EditCounts,
    bootstrap_difference_ci,
    edit_counts,
    evaluate,
    fold_god_pronouns,
    make_item_counts,
    normalize,
    parse_args,
    summarize_breakdowns,
    trace_to_item,
)


def test_normalize_folds_fullwidth_removes_punctuation_and_lowers_latin() -> None:
    assert normalize("Ｔｅａ， ＡＳＲ！我愛你") == "teaasr我愛你"


def test_normalize_does_not_fold_simplified_to_traditional() -> None:
    assert normalize("后台") == "后台"
    assert normalize("后台") != normalize("後台")


def test_fold_god_pronouns_maps_only_the_requested_variants() -> None:
    assert fold_god_pronouns("祢祂它你他") == "你他他你他"


def test_edit_counts_reports_substitutions_deletions_and_insertions() -> None:
    assert edit_counts("cat", "cut").substitutions == 1
    assert edit_counts("cat", "ct").deletions == 1
    assert edit_counts("ct", "cat").insertions == 1


def test_trace_to_item_uses_strict_interval_overlap_and_joins_adjacent_chinese() -> None:
    events = [
        {"start_sample": 0, "end_sample": 16_000, "text": "不含"},
        {"start_sample": 8_000, "end_sample": 24_000, "text": "甲"},
        {"start_sample": 24_000, "end_sample": 32_000, "text": "乙"},
        {"start_sample": 32_000, "end_sample": 48_000, "text": "邊界外"},
    ]

    assert trace_to_item(events, 1, 2) == "甲乙"


def test_make_item_counts_excludes_unclear_and_groups_by_music() -> None:
    reference = {
        "set": "synthetic",
        "items": [
            {
                "id": "clear",
                "start_s": 0,
                "end_s": 1,
                "reference": "台灣",
                "music": False,
                "unclear": False,
            },
            {
                "id": "unclear",
                "start_s": 1,
                "end_s": 2,
                "reference": "禱告",
                "music": True,
                "unclear": True,
            },
        ],
    }

    rows, counts, excluded, missing = make_item_counts(
        reference, ("json", {"clear": "台灣", "unclear": "禱告"}), include_unclear=False
    )
    sets, music = summarize_breakdowns(rows)

    assert len(counts) == 1
    assert excluded == ["unclear"]
    assert missing == 0
    assert sets["synthetic"]["cer"] == 0
    assert music["false"]["items"] == 1
    assert music["true"]["items"] == 0


def test_paired_bootstrap_ci_is_reproducible_and_covers_observed_delta() -> None:
    better = [EditCounts(0, 0, 0, 4, 4), EditCounts(0, 0, 0, 4, 4)]
    worse = [EditCounts(1, 0, 0, 4, 4), EditCounts(0, 0, 0, 4, 4)]

    first = bootstrap_difference_ci(better, worse, iterations=200, seed=7)
    second = bootstrap_difference_ci(better, worse, iterations=200, seed=7)

    assert first == second
    assert first["delta_cer"] == -0.125
    assert first["ci95_low"] <= first["delta_cer"] <= first["ci95_high"]


def test_parse_args_enables_pronoun_fold_report() -> None:
    args = parse_args(["answers.json", "hypothesis.json", "--fold-pronouns"])

    assert args.fold_pronouns is True


def test_evaluate_reports_unfolded_and_folded_scores(tmp_path: Path) -> None:
    reference = {
        "set": "synthetic",
        "items": [
            {
                "id": "one",
                "start_s": 0,
                "end_s": 1,
                "reference": "祢甲",
                "unclear": False,
            },
            {
                "id": "two",
                "start_s": 1,
                "end_s": 2,
                "reference": "乙祂",
                "unclear": False,
            },
        ],
    }
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    first.write_text(json.dumps({"one": "你甲", "two": "乙他"}), encoding="utf-8")
    second.write_text(json.dumps({"one": "祢甲", "two": "乙祂"}), encoding="utf-8")

    report = evaluate(
        reference, [first, second], fold_pronouns=True, bootstrap_iterations=20, seed=4
    )
    first_source = report["sources"][0]

    assert first_source["overall"]["cer"] == 0.5
    assert first_source["pronoun_folded"]["overall"]["cer"] == 0
    assert report["pairwise_bootstrap_95_ci"][0]["pronoun_folded"]["iterations"] == 20


def test_evaluate_reports_per_set_concatenated_cer(tmp_path: Path) -> None:
    reference = {
        "set": "synthetic",
        "items": [
            {
                "id": "one",
                "start_s": 0,
                "end_s": 1,
                "reference": "甲乙",
                "unclear": False,
            },
            {
                "id": "two",
                "start_s": 1,
                "end_s": 2,
                "reference": "丙丁",
                "unclear": False,
            },
        ],
    }
    hypothesis = tmp_path / "hypothesis.json"
    hypothesis.write_text(json.dumps({"one": "甲", "two": "乙丙丁"}), encoding="utf-8")

    report = evaluate(reference, [hypothesis])
    source = report["sources"][0]

    assert source["per_set"]["synthetic"]["cer"] > 0
    assert source["per_set_concatenated"]["synthetic"]["cer"] == 0
