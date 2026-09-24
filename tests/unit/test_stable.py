"""Append-only stable prefix (`tea_asr.stable`), docs/07「穩定前綴」."""

from __future__ import annotations

import itertools
import random

import pytest

from tea_asr.stable import (
    StablePrefixTracker,
    is_grapheme_boundary,
    stable_cut,
)

FAMILY = "\U0001F468\u200d\U0001F469\u200d\U0001F467"  # man, woman, girl joined by ZWJ
FLAG_TW = "\U0001F1F9\U0001F1FC"
THUMBS_DARK = "\U0001F44D\U0001F3FF"
KEYCAP_ONE = "1\ufe0f\u20e3"
E_ACUTE = "e\u0301"  # e + combining acute
HANGUL_JAMO = "\u1100\u1161\u11a8"  # 각 as conjoining jamo


def feed(tracker: StablePrefixTracker, *hypotheses: str) -> list[str]:
    """Observe partials in order and return every committed value."""

    values = []
    for hypothesis in hypotheses:
        update = tracker.observe(hypothesis)
        if update is not None:
            values.append(update.text)
    return values


# -- append-only ----------------------------------------------------------------


def test_prefix_is_committed_only_after_n_partials_agree() -> None:
    tracker = StablePrefixTracker(2)
    assert tracker.observe("我們需要權限") is None
    update = tracker.observe("我們需要全線停駛")
    assert update is not None and update.text == "我們需要"
    assert update.state == "open"

    three = StablePrefixTracker(3)
    assert feed(three, "我們需要權限", "我們需要全線停駛") == []
    assert feed(three, "我們需要全線停駛，才能") == ["我們需要"]


def test_committed_text_never_shrinks_or_changes() -> None:
    tracker = StablePrefixTracker(2)
    values = feed(
        tracker,
        "今天天氣",
        "今天天氣很好",
        "今天天器很好我們",  # disagrees with what is already committed
        "今天天氣很好我們去",
        "",
        "今天",
        "今天天氣很好我們去爬山",
        "今天天氣很好我們去爬山吧",
    )
    assert values, "something must be committed"
    for older, newer in itertools.pairwise(values):
        assert newer.startswith(older) and len(newer) > len(older)
    assert tracker.text == values[-1]


def test_random_hypotheses_never_violate_append_only() -> None:
    rng = random.Random(7)
    alphabet = "我們需要全線停駛才能進行維修，。 abc👍\u0301"
    for _ in range(300):
        tracker = StablePrefixTracker(rng.choice((2, 3)))
        committed = ""
        base = "".join(rng.choice(alphabet) for _ in range(20))
        for _ in range(12):
            cut = rng.randint(0, len(base))
            noise = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 3)))
            update = tracker.observe(base[:cut] + noise)
            if update is not None:
                assert update.text.startswith(committed)
                assert len(update.text) > len(committed)
                committed = update.text
        final = tracker.finalize(base)
        assert final.text.startswith(committed)
        assert tracker.observe(base) is None, "no update after the segment closed"


def test_trailing_punctuation_is_held_back() -> None:
    # A truncated snapshot often ends in 。 that later becomes ，.
    tracker = StablePrefixTracker(2)
    update = feed(tracker, "你好。", "你好。")
    assert update == ["你好"]
    assert feed(tracker, "你好，我是", "你好，我是誰") == ["你好，我是"]


# -- grapheme and word safety ------------------------------------------------------


@pytest.mark.parametrize(
    ("hypotheses", "expected"),
    [
        # ZWJ family: the shorter hypothesis ends on a bare 👨.
        ((f"大家{FAMILY[:1]}", f"大家{FAMILY}好"), "大家"),
        ((f"大家{FAMILY}好", f"大家{FAMILY}好嗎"), f"大家{FAMILY}好"),
        # Regional-indicator flag must not be halved.
        ((f"來自{FLAG_TW[:1]}", f"來自{FLAG_TW}的"), "來自"),
        # Skin-tone modifier belongs to the thumb.
        (("讚\U0001F44D", f"讚{THUMBS_DARK}啦"), "讚"),
        # Keycap: 1 + VS16 + U+20E3.
        (("第1", f"第{KEYCAP_ONE}名"), "第"),
        # Base letter + combining mark.
        (("café", f"caf{E_ACUTE} 很好"), ""),
        ((f"我說 caf{E_ACUTE}", f"我說 caf{E_ACUTE} 很好"), f"我說 caf{E_ACUTE}"),
        # Conjoining Hangul jamo.
        (("\u1100\u1161", f"{HANGUL_JAMO}다"), ""),
    ],
)
def test_cut_never_splits_a_grapheme_cluster(hypotheses: tuple[str, ...], expected: str) -> None:
    length = stable_cut(hypotheses)
    assert hypotheses[0][:length] == expected
    for text in hypotheses:
        assert is_grapheme_boundary(text, length)


@pytest.mark.parametrize(
    ("hypotheses", "expected"),
    [
        (("我用 iPh", "我用 iPhone 拍照"), "我用"),
        (("我用 iPhone 拍", "我用 iPhone 拍照"), "我用 iPhone 拍"),
        (("在 2026", "在 2027 年"), "在"),
        (("I don", "I don't know"), "I"),
        # Every hypothesis ends in a Latin word: it may still be growing.
        (("我用 iPhone", "我用 iPhone"), "我用"),
        # CJK needs no word boundary; each character stands alone.
        (("中英混合 OK 的", "中英混合 OK 的句子"), "中英混合 OK 的"),
    ],
)
def test_cut_keeps_latin_words_and_numbers_whole(
    hypotheses: tuple[str, ...], expected: str
) -> None:
    assert hypotheses[0][: stable_cut(hypotheses)] == expected


def test_utf8_bytes_of_every_commit_are_a_prefix_of_the_next() -> None:
    """The OBS plugin handles UTF-8 bytes; each value must extend the last bytewise."""

    tracker = StablePrefixTracker(2)
    partials = [
        "直播",
        f"直播開始{FAMILY[:1]}",
        f"直播開始{FAMILY}歡迎",
        f"直播開始{FAMILY}歡迎 caf",
        f"直播開始{FAMILY}歡迎 caf{E_ACUTE}",
        f"直播開始{FAMILY}歡迎 caf{E_ACUTE} 的{FLAG_TW}朋友",
        f"直播開始{FAMILY}歡迎 caf{E_ACUTE} 的{FLAG_TW}朋友們",
    ]
    values = [b""] + [text.encode() for text in feed(tracker, *partials)]
    values.append(tracker.finalize(partials[-1] + "。").text.encode())
    for older, newer in itertools.pairwise(values):
        assert newer.startswith(older)
        newer.decode("utf-8")  # a byte prefix that is valid UTF-8 on its own


def test_boundary_check_is_no_looser_than_uax29() -> None:
    """Every cut we allow is also a UAX #29 extended grapheme boundary."""

    regex = pytest.importorskip("regex")
    samples = [
        FAMILY,
        FLAG_TW + FLAG_TW + "\U0001F1EF",
        THUMBS_DARK,
        KEYCAP_ONE,
        E_ACUTE + "\u0302",
        HANGUL_JAMO + "\u1100\uac00\u11a8",
        "\u0915\u094d\u0937",  # ksha: consonant + virama + consonant
        "\u0600\u0661",  # prepend + digit
        "\u0e01\u0e33",  # Thai SARA AM
        "a\r\nb",
        "\U0001F3F3\ufe0f\u200d\U0001F308",  # rainbow flag
        "\U0001F3F4\U000E0067\U000E0062\U000E0073\U000E0063\U000E0074\U000E007F",
        "中文，English 混合😀！",
    ]
    rng = random.Random(3)
    pool = "".join(samples)
    samples += ["".join(rng.choice(pool) for _ in range(12)) for _ in range(300)]
    for text in samples:
        allowed = set(
            itertools.accumulate((len(c) for c in regex.findall(r"\X", text)), initial=0)
        )
        for index in range(len(text) + 1):
            if is_grapheme_boundary(text, index):
                assert index in allowed, (text, index)


# -- final rules ---------------------------------------------------------------------


def test_final_that_extends_the_prefix_completes_it_exactly() -> None:
    tracker = StablePrefixTracker(2)
    feed(tracker, "我們需要全線", "我們需要全線停駛")
    update = tracker.finalize("我們需要全線停駛，才能進行維修。")
    assert update.state == "final"
    assert update.text == "我們需要全線停駛，才能進行維修。"
    assert update.diverged_chars == 0


def test_diverged_final_keeps_committed_text_and_appends_the_aligned_tail() -> None:
    tracker = StablePrefixTracker(2)
    feed(tracker, "我們需要權限", "我們需要權限停")
    assert tracker.text == "我們需要權限"
    update = tracker.finalize("我們需要全線停駛，才能進行維修。")
    assert update.state == "diverged"
    # 權限 stays on screen (it cannot be retracted); the rest of the final follows.
    assert update.text == "我們需要權限停駛，才能進行維修。"
    assert update.diverged_chars == 2


def test_diverged_final_that_drops_committed_text_appends_nothing_twice() -> None:
    tracker = StablePrefixTracker(2)
    feed(tracker, "嗯我覺得", "嗯我覺得可以")
    update = tracker.finalize("我覺得可以。")
    assert update.state == "diverged"
    assert update.text.startswith(tracker.text[: len("嗯我覺得")])
    assert update.text.count("我覺得") == 1


def test_diverged_tail_starts_on_a_grapheme_boundary() -> None:
    tracker = StablePrefixTracker(2)
    feed(tracker, "讚讚", "讚讚")
    update = tracker.finalize(f"好{THUMBS_DARK}啦")
    assert update.state == "diverged"
    # The aligned end falls between 👍 and its skin tone; the tail starts after it.
    assert update.text == "讚讚啦"


def test_abandon_keeps_text_and_closes() -> None:
    tracker = StablePrefixTracker(2)
    feed(tracker, "測試一下", "測試一下喔")
    update = tracker.abandon()
    assert update.state == "abandoned"
    assert update.text == "測試一下"
    assert tracker.observe("測試一下喔喔") is None


def test_each_segment_has_its_own_tracker_state() -> None:
    first, second = StablePrefixTracker(2), StablePrefixTracker(2)
    feed(first, "第一段文字", "第一段文字很長")
    feed(second, "第二段", "第二段也")
    assert first.text == "第一段文字"
    assert second.text == "第二段"


def test_agreement_below_two_is_refused() -> None:
    with pytest.raises(ValueError):
        StablePrefixTracker(1)
