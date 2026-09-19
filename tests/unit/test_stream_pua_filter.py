from __future__ import annotations

from tea_asr.api.stream import filter_private_use_characters, private_use_warnings


def test_filters_pua_characters_inserted_between_correct_text() -> None:
    # docs/benchmarks/pua-bf16-ab-report.md: PUA is inserted between correct
    # text by MLX 4bit quantization, never substituted for it.
    text = "下午跟 client 開會。"
    assert filter_private_use_characters(text) == "下午跟 client 開會。"


def test_filters_the_full_pua_range() -> None:
    text = ""
    assert filter_private_use_characters(text) == ""


def test_does_not_touch_normal_traditional_chinese_punctuation_or_mixed_text() -> None:
    samples = [
        "下午跟 client 開會。",
        "已經 merge 了，謝謝大家！",
        "這份 PR 的 review 意見：先看 comment。",
        "12:30 開始，記得帶筆電。",
        "",
    ]
    for text in samples:
        assert filter_private_use_characters(text) == text


def test_warnings_are_reported_from_the_original_unfiltered_text() -> None:
    raw = "測試文字"
    assert private_use_warnings(raw) == ["private_use_characters"]
    assert private_use_warnings(filter_private_use_characters(raw)) == []
