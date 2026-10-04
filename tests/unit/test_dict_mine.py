import json
from pathlib import Path

import pytest

from benchmarks import dict_mine


def make_observation(
    reference: str, hypothesis: str, *, set_name: str = "sample", item_id: str = "item"
) -> dict_mine.Observation:
    normalized_reference = dict_mine.normalize_with_spans(reference)
    normalized_hypothesis = dict_mine.normalize_with_spans(hypothesis)
    return dict_mine.Observation(
        set_name=set_name,
        item_id=item_id,
        reference=reference,
        hypothesis=hypothesis,
        normalized_reference=normalized_reference,
        normalized_hypothesis=normalized_hypothesis,
        steps=dict_mine.align_characters(
            normalized_reference.value, normalized_hypothesis.value
        ),
    )


def candidate(
    *,
    from_: str = "祝鵬傑",
    to: str = "住棚節",
    count: int = 2,
    support: int = 2,
    precision_proxy: float = 1.0,
    harm: int = 0,
    occurrences: int = 2,
    hotword_exempt: bool = False,
) -> dict_mine.Candidate:
    return dict_mine.Candidate(
        from_=from_,
        to=to,
        count=count,
        support=support,
        precision_proxy=precision_proxy,
        harm=harm,
        occurrences=occurrences,
        contexts=["前文⟦祝鵬傑→住棚節⟧後文"],
        hotword_exempt=hotword_exempt,
    )


def test_normalization_ignores_punctuation_but_preserves_the_original_surface():
    value = dict_mine.normalize_with_spans("ＡＢ，Ｃ")

    assert value.value == "abc"
    assert value.surface("ＡＢ，Ｃ", 0, 3) == "ＡＢ，Ｃ"


def test_alignment_extracts_cjk_substitutions_and_whole_latin_words():
    cjk_reference = dict_mine.normalize_with_spans("前住棚節後")
    cjk_hypothesis = dict_mine.normalize_with_spans("前祝、鵬傑後")
    cjk_spans = dict_mine.extract_substitution_spans(cjk_reference, cjk_hypothesis)
    assert [
        (
            cjk_hypothesis.value[span.hypothesis_start : span.hypothesis_end],
            cjk_reference.value[span.reference_start : span.reference_end],
        )
        for span in cjk_spans
    ] == [("祝鵬傑", "住棚節")]
    assert cjk_hypothesis.surface("前祝、鵬傑後", 1, 4) == "祝、鵬傑"

    latin_reference = dict_mine.normalize_with_spans("He plays")
    latin_hypothesis = dict_mine.normalize_with_spans("He prays")
    latin_spans = dict_mine.extract_substitution_spans(latin_reference, latin_hypothesis)
    assert [
        (
            latin_hypothesis.value[span.hypothesis_start : span.hypothesis_end],
            latin_reference.value[span.reference_start : span.reference_end],
        )
        for span in latin_spans
    ] == [("prays", "plays")]


def test_cjk_span_expands_until_its_source_is_unambiguous():
    reference = dict_mine.normalize_with_spans("甲乙正丙甲乙錯丙")
    hypothesis = dict_mine.normalize_with_spans("甲乙錯丙甲乙錯丙")
    spans = dict_mine.extract_substitution_spans(reference, hypothesis)

    assert len(spans) == 1
    span = spans[0]
    assert hypothesis.value[span.hypothesis_start : span.hypothesis_end] == "甲乙錯丙甲"
    assert reference.value[span.reference_start : span.reference_end] == "甲乙正丙甲"


def test_precision_proxy_uses_every_hypothesis_occurrence_and_keeps_original_harm():
    observations = [
        make_observation("前住棚節後", "前祝鵬傑後", item_id="one"),
        make_observation("後住棚節前", "後祝鵬傑前", item_id="two"),
        make_observation("前祝朋傑後", "前祝鵬傑後", item_id="three"),
    ]
    candidates, _ = dict_mine.mine_candidates(observations)
    mined = next(item for item in candidates if item.from_ == "祝鵬傑" and item.to == "住棚節")

    assert (mined.count, mined.support, mined.occurrences, mined.harm) == (2, 2, 3, 0)
    assert mined.precision_proxy == pytest.approx(2 / 3)
    safe, review = dict_mine.filter_candidates([mined])
    assert mined not in safe
    assert mined not in review


def test_review_context_keeps_at_most_ten_source_characters_each_side():
    observations = [
        make_observation(
            "0123456789住棚節ABCDEFGHIJK",
            "0123456789祝鵬傑ABCDEFGHIJK",
        )
    ]

    candidates, _ = dict_mine.mine_candidates(observations)
    mined = next(item for item in candidates if item.from_ == "祝鵬傑")
    context = mined.contexts[0]
    before, after = context.split("⟦", 1)[0], context.split("⟧", 1)[1]

    assert len(before) <= 10
    assert len(after) <= 10


def test_harm_filter_rejects_a_rule_that_would_break_correct_text():
    observations = [
        make_observation("住棚節", "祝鵬傑", item_id="one"),
        make_observation("住棚節", "祝鵬傑", item_id="two"),
        make_observation("祝鵬傑", "祝鵬傑", item_id="three"),
    ]
    candidates, _ = dict_mine.mine_candidates(observations)
    mined = next(item for item in candidates if item.from_ == "祝鵬傑" and item.to == "住棚節")

    assert (mined.count, mined.support, mined.precision_proxy, mined.harm) == (
        2,
        2,
        pytest.approx(2 / 3),
        1,
    )
    assert mined not in dict_mine.filter_candidates([mined])[0]

    harm_only = candidate(
        count=10,
        support=2,
        precision_proxy=10 / 11,
        harm=1,
        occurrences=11,
    )
    assert dict_mine.filter_candidates([harm_only]) == ([], [])


def test_count_and_support_failures_are_review_only_and_hotwords_exempt_them():
    low_count = candidate(count=1, support=1)
    low_support = candidate(count=2, support=1)
    hotword = candidate(count=1, support=1, hotword_exempt=True)

    safe, review = dict_mine.filter_candidates(
        [low_count, low_support, hotword]
    )

    assert safe == [hotword]
    assert review == [low_support, low_count]


def test_low_precision_is_rejected():
    low_precision = candidate(precision_proxy=0.89)

    assert dict_mine.filter_candidates([low_precision]) == ([], [])


def test_minimum_source_length_rejects_single_character_cjk_and_latin_rules():
    short_cjk = candidate(from_="你", to="祢")
    short_latin = candidate(from_="a", to="b")

    assert not dict_mine.source_length_is_allowed(short_cjk.from_)
    assert not dict_mine.source_length_is_allowed(short_latin.from_)
    assert dict_mine.filter_candidates([short_cjk, short_latin]) == ([], [])


def test_builtin_and_file_stoplists_block_common_words(tmp_path: Path):
    built_in = candidate(from_="因為", to="因果")
    custom = candidate(from_="講道", to="講到")
    stoplist_path = tmp_path / "stoplist.txt"
    stoplist_path.write_text("# local exclusions\n講道\n", encoding="utf-8")
    stoplist = dict_mine.load_wordlist(stoplist_path, keys=("stoplist", "words"))

    safe, review = dict_mine.filter_candidates([built_in, custom], stoplist=stoplist)

    assert safe == []
    assert review == []


def test_hotword_file_can_exempt_a_low_support_domain_term(tmp_path: Path):
    path = tmp_path / "hotwords.toml"
    path.write_text('hotwords = ["住棚節"]\n', encoding="utf-8")
    hotwords = dict_mine.load_wordlist(path, keys=("hotwords", "words"))
    observations = [make_observation("住棚節", "祝鵬傑")]

    mined, _ = dict_mine.mine_candidates(observations, hotwords=hotwords)
    safe, review = dict_mine.filter_candidates(mined)

    assert len(safe) == 1
    assert safe[0].hotword_exempt
    assert (safe[0].count, safe[0].support) == (1, 1)
    assert review == []


def test_style_conventions_are_reported_separately_and_never_mined_as_rules():
    observations = [
        make_observation("主愛祢", "主愛你", item_id="you"),
        make_observation("耶穌祂說", "耶穌他說", item_id="he"),
        make_observation("為祂", "為他", item_id="context-dependent-he"),
        make_observation("AMEN", "阿門", item_id="amen1"),
        make_observation("AMEN", "阿們", item_id="amen2"),
        make_observation("你可以", "你可以", item_id="ordinary-you"),
    ]

    mined, style_counts = dict_mine.mine_candidates(observations)

    assert style_counts["你→祢"] == 1
    assert style_counts["他→祂"] == 2
    assert style_counts["阿門→AMEN"] == 1
    assert style_counts["阿們→AMEN"] == 1
    assert all(len(dict_mine.normalize(item.from_)) > 1 for item in mined)
    assert all("阿門" not in item.from_ and "阿們" not in item.from_ for item in mined)


def test_number_style_rule_definition_and_safe_word_boundaries():
    assert dict_mine.is_number_rule("一百萬", "00萬")
    assert dict_mine.is_number_rule("五七", "57")
    assert dict_mine.is_number_rule("anything", "２０２６")
    assert dict_mine.is_number_rule("千國", "天一")
    assert not dict_mine.is_number_rule("千國", "天國")
    assert not dict_mine.is_number_rule("五道招", "五大呼召")
    assert dict_mine._is_number_formatting_style("一百", "營業額")
    # A source-side Arabic digit is blocked by the broader policy even though
    # the precise paired-ratio definition does not classify it as a number rule.
    assert dict_mine._is_number_formatting_style("12abc", "twelve")


def test_number_formatting_candidates_only_appear_in_the_style_review():
    number_candidates = [
        candidate(from_="一百萬", to="00萬"),
        candidate(from_="四十", to="40"),
        candidate(from_="五七", to="57"),
        candidate(from_="到八", to="5-8"),
        candidate(from_="分之九", to="9/10"),
    ]
    legitimate_candidates = [
        candidate(from_="五道招", to="五大呼召"),
        candidate(from_="千國", to="天國"),
    ]
    all_candidates = number_candidates + legitimate_candidates

    safe, review = dict_mine.filter_candidates(all_candidates)
    style_only = [
        item
        for item in all_candidates
        if dict_mine._is_number_formatting_style(item.from_, item.to)
    ]
    markdown = dict_mine.render_review_markdown(
        safe,
        review,
        {},
        answer_paths=[],
        hypothesis_paths=[],
        number_formatting=style_only,
    )
    toml = dict_mine.render_candidates_toml(safe)

    assert safe == legitimate_candidates
    assert review == []
    assert style_only == number_candidates
    assert "## Number formatting (style)" in markdown
    style_section = markdown.split("## Number formatting (style)", 1)[1].split(
        "## Safe to auto-suggest", 1
    )[0]
    for item in number_candidates:
        assert f"| {item.from_} | {item.to} |" in style_section
    assert "00萬" not in toml
    assert "40" not in toml
    assert "57" not in toml
    assert "五道招" in toml
    assert "千國" in toml


def test_miner_routes_numeric_alignment_candidates_to_style_review(tmp_path: Path):
    answers_path = tmp_path / "answers.json"
    hypothesis_path = tmp_path / "hypothesis.json"
    candidates_path = tmp_path / "candidates.toml"
    review_path = tmp_path / "review.md"
    answers_path.write_text(
        json.dumps(
            {
                "set": "test",
                "items": [
                    {
                        "id": item_id,
                        "start_s": 0,
                        "end_s": 1,
                        "reference": "營業額一百萬",
                    }
                    for item_id in ("one", "two")
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    hypothesis_path.write_text(
        json.dumps({"one": "營業額00萬", "two": "營業額00萬"}, ensure_ascii=False),
        encoding="utf-8",
    )

    assert dict_mine.main(
        [
            "--answers",
            str(answers_path),
            "--hypothesis",
            str(hypothesis_path),
            "--candidates-toml",
            str(candidates_path),
            "--review-md",
            str(review_path),
        ]
    ) == 0
    markdown = review_path.read_text(encoding="utf-8")
    toml = candidates_path.read_text(encoding="utf-8")

    assert "## Number formatting (style)" in markdown
    assert "| 00 | 一百 |" in markdown
    assert "00" not in toml


def test_candidates_toml_loads_through_the_server_dictionary_loader(tmp_path: Path):
    from tea_asr.context import ContextDictionaryStore

    path = tmp_path / "candidates.toml"
    path.write_text(dict_mine.render_candidates_toml([candidate()]), encoding="utf-8")

    loaded = ContextDictionaryStore(tmp_path).load("candidates")

    assert [(rule.source, rule.target) for rule in loaded.replacements] == [
        ("祝鵬傑", "住棚節")
    ]


def test_merge_comparison_reports_existing_rules_and_conflicts(tmp_path: Path):
    existing = tmp_path / "existing.toml"
    existing.write_text(
        'domain = "test"\nhotwords = []\n'
        '[[replacements]]\nfrom = "祝鵬傑"\nto = "住棚節"\n'
        '[[replacements]]\nfrom = "軍中的祭司"\nto = "軍中的祭司"\n',
        encoding="utf-8",
    )
    already_present = candidate()
    conflict = candidate(from_="軍中的祭司", to="君尊的祭司")

    present, conflicts = dict_mine.inspect_merge([already_present, conflict], existing)

    assert present == [already_present]
    assert conflicts == [(conflict, "軍中的祭司")]


def test_collect_observations_aggregates_sets_and_skips_unclear_by_default(tmp_path: Path):
    answer_a = tmp_path / "a.json"
    answer_b = tmp_path / "b.json"
    hypothesis = tmp_path / "hyp.json"
    hypothesis_second = tmp_path / "hyp-second.json"
    for path, set_name, item_id, unclear in (
        (answer_a, "set-a", "shared", False),
        (answer_b, "set-b", "shared", False),
    ):
        path.write_text(
            json.dumps(
                {
                    "set": set_name,
                    "items": [
                        {
                            "id": item_id,
                            "start_s": 0,
                            "end_s": 1,
                            "reference": "住棚節",
                            "unclear": unclear,
                        },
                        {
                            "id": f"{item_id}-unclear",
                            "start_s": 1,
                            "end_s": 2,
                            "reference": "住棚節",
                            "unclear": True,
                        },
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    hypothesis_value = json.dumps(
        {
            "shared": "祝鵬傑",
            "shared-unclear": "祝鵬傑",
        },
        ensure_ascii=False,
    )
    hypothesis.write_text(hypothesis_value, encoding="utf-8")
    hypothesis_second.write_text(hypothesis_value, encoding="utf-8")

    observations, missing = dict_mine.collect_observations(
        [answer_a, answer_b], [hypothesis, hypothesis_second]
    )
    mined, _ = dict_mine.mine_candidates(observations)

    assert len(observations) == 4
    assert missing == 0
    assert any(
        item.from_ == "祝鵬傑"
        and item.count == 4
        and item.support == 2
        for item in mined
    )
