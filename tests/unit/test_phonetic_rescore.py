"""Synthetic checks for the phonetic rescoring benchmark (no private text, audio or model)."""

import pytest

from benchmarks.phonetic_rescore import (
    Evaluator,
    Params,
    Run,
    accepted_candidates,
    apply_accepted,
    classify_change,
    make_terms,
    mine_names,
    parameter_grid,
    term_recall,
)


def _candidate(start: int, end: int, term: str, text: str, cost: float = 0.1, src: str = "dict") -> dict:
    return {"s": start, "e": end, "term": term, "src": src, "cost": cost, "cost_tl": cost, "text": text}


def test_find_candidates_recovers_homophone_names() -> None:
    pytest.importorskip("pypinyin")
    from benchmarks.phonetic_rescore import find_candidates

    terms = make_terms([("孟恩哥", "dict"), ("住棚節", "dict")])
    found = {c.text: c for c in find_candidates("今天毛文哥說祝鵬節到了", terms)}
    assert "今天孟恩哥說祝鵬節到了" in found
    assert "今天毛文哥說住棚節到了" in found
    assert found["今天孟恩哥說祝鵬節到了"].cost < 0.3
    assert found["今天毛文哥說住棚節到了"].cost == pytest.approx(0.0)
    assert found["今天毛文哥說住棚節到了"].cost_toneless == pytest.approx(0.0)


def test_find_candidates_skips_exact_terms_and_unrelated_text() -> None:
    pytest.importorskip("pypinyin")
    from benchmarks.phonetic_rescore import find_candidates

    terms = make_terms([("孟恩哥", "dict")])
    assert find_candidates("孟恩哥來了", terms) == []
    assert find_candidates("我們一起唱歌", terms) == []


def test_find_candidates_never_spans_punctuation_and_caps_results() -> None:
    pytest.importorskip("pypinyin")
    from benchmarks.phonetic_rescore import find_candidates

    terms = make_terms([("孟恩哥", "dict")])
    assert find_candidates("毛文，哥來了", terms) == []
    many = "".join("毛文哥，" for _ in range(20))
    assert len(find_candidates(many, terms, cap=8)) == 8


def test_make_terms_filters_short_and_non_han() -> None:
    pytest.importorskip("pypinyin")
    terms = make_terms([("孟恩哥", "dict"), ("神", "dict"), ("AMEN", "dict"), ("孟恩哥", "hot")])
    assert [t.text for t in terms] == ["孟恩哥"]
    assert terms[0].source == "dict"


SCORES = {
    "一二三四": [-10.0, 5],
    "一二五六": [-8.0, 5],  # window 2-4 -> 五六: gain +2.0
    "七八三四": [-7.0, 5],  # window 0-2 -> 七八: gain +3.0
    "一九十四": [-9.5, 4],  # window 1-3 -> 九十: gain +0.5, overlaps both
}
BASE = "一二三四"


def _decision_candidates() -> list[dict]:
    return [
        _candidate(2, 4, "五六", "一二五六"),
        _candidate(1, 3, "九十", "一九十四"),
        _candidate(0, 2, "七八", "七八三四"),
    ]


def test_decision_rule_margin_and_overlap() -> None:
    accepted = accepted_candidates(BASE, _decision_candidates(), SCORES, Params(margin=1.0))
    assert [c["term"] for c in accepted] == ["七八", "五六"]  # best first, non-overlapping
    assert apply_accepted(BASE, accepted) == "七八五六"
    only_best = accepted_candidates(BASE, _decision_candidates(), SCORES, Params(margin=2.5))
    assert [c["term"] for c in only_best] == ["七八"]
    assert accepted_candidates(BASE, _decision_candidates(), SCORES, Params(margin=3.0)) == []
    assert accepted_candidates("沒有分數", _decision_candidates(), SCORES, Params(margin=0.0)) == []


def test_decision_rule_filters_by_cost_length_source() -> None:
    far = _candidate(2, 4, "五六", "一二五六", cost=0.35)
    named = _candidate(0, 2, "七八", "七八三四", src="name")
    assert accepted_candidates(BASE, [far], SCORES, Params(0.0, cost_max=0.3)) == []
    assert accepted_candidates(BASE, [far], SCORES, Params(0.0, cost_max=0.4))
    assert accepted_candidates(BASE, [named], SCORES, Params(0.0)) == []
    assert accepted_candidates(BASE, [named], SCORES, Params(0.0, names=True))
    assert accepted_candidates(BASE, [named], SCORES, Params(0.0, names=True, min_term_len=3)) == []
    toneless_only = {**far, "cost": 0.9, "cost_tl": 0.1}
    assert accepted_candidates(BASE, [toneless_only], SCORES, Params(0.0)) == []
    assert accepted_candidates(BASE, [toneless_only], SCORES, Params(0.0, toneless=True))


def test_decision_rule_normalisation_changes_the_verdict() -> None:
    shorter = [_candidate(1, 3, "九十", "一九十四")]
    assert accepted_candidates(BASE, shorter, SCORES, Params(0.0, "sum"))  # +0.5 summed
    assert not accepted_candidates(BASE, shorter, SCORES, Params(0.0, "tok"))  # -9.5/4 < -10/5
    assert accepted_candidates(BASE, shorter, SCORES, Params(0.0, "char"))  # same length: +0.125/char
    assert accepted_candidates(BASE, shorter, SCORES, Params(0.2, "char")) == []


def test_classify_change_false_good_worse() -> None:
    reference = "今天孟恩哥來分享"
    wrong_on_correct = _candidate(2, 5, "孟恩姐", "今天孟恩姐來分享")
    assert classify_change(reference, "今天孟恩哥來分享", wrong_on_correct) == "false"
    fix = _candidate(2, 5, "孟恩哥", "今天孟恩哥來分享")
    assert classify_change(reference, "今天毛文哥來分享", fix) == "good"
    worse = _candidate(3, 5, "想姐", "今天蒙想姐來分享")
    assert classify_change(reference, "今天孟恩姐來分享", worse) == "worse"
    same = _candidate(3, 5, "恩姐", "今天孟恩姐來分享")
    assert classify_change(reference, "今天孟恩姐來分享", same) == "neutral"


def test_term_recall_counts_occurrences() -> None:
    correct, total = term_recall(["孟恩哥說孟恩哥", "沒有"], ["孟恩哥說毛文哥", "沒有"], ["孟恩哥"])
    assert (correct, total) == (1, 2)


def test_mine_names_uses_titles_and_frequency() -> None:
    refs = ["孟恩哥來了"] * 3 + ["貝甄姐在這"] * 2 + ["我哥說"]
    assert mine_names(refs, min_count=3) == ["孟恩哥"]
    assert "貝甄姐" in mine_names(refs, min_count=2)


def test_grid_is_conservative_first_and_tune_prefers_fewer_false_changes() -> None:
    grid = parameter_grid()
    assert grid[0].margin == max(m for m in (p.margin for p in grid if p.norm == "sum"))
    reference = "孟恩哥來了"
    run = Run(
        name="syn",
        items=[{"id": "x", "reference": reference, "start_s": 0.0, "end_s": 3.0, "service": "syn"}],
        rows=[{
            "id": "x", "A": "毛文哥來了", "B": "毛文哥來了",
            "cands": {"A": [_candidate(0, 3, "孟恩哥", "孟恩哥來了")], "B": [_candidate(0, 3, "孟恩哥", "孟恩哥來了")]},
            "scores": {"毛文哥來了": [-20.0, 5], "孟恩哥來了": [-5.0, 5]},
        }],
        terms=["孟恩哥"],
        hours=3.0 / 3600,
    )
    evaluator = Evaluator([run])
    params, info = evaluator.tune([run], "C")
    assert info["delta_errors"] == -2 and info["false"] == 0
    outputs, changes = evaluator.texts(run, "C", params)
    assert outputs == ["孟恩哥來了"] and changes[0][2] == "good"
    assert evaluator.texts(run, "B", None)[0] == ["毛文哥來了"]
