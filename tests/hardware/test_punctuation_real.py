"""Real CT-Transformer asset: verifies the pinned contract. Skipped when absent.

    uv run pytest -m hardware tests/hardware/test_punctuation_real.py

The asset comes from `tea-asr model-prepare`; CI never downloads it.
"""

from __future__ import annotations

import pytest

from tea_asr.punctuation import (
    PUNCTUATION_MODEL_SHA256,
    PunctuationModel,
    locate_punctuation,
    sha256,
)

pytestmark = pytest.mark.hardware


@pytest.fixture(scope="module")
def real() -> PunctuationModel:
    try:
        path = locate_punctuation()
    except Exception as exc:  # noqa: BLE001 - any lookup failure means "not prepared"
        pytest.skip(f"punctuation model is not prepared: {exc}")
    assert sha256(path) == PUNCTUATION_MODEL_SHA256
    return PunctuationModel(path)


def test_inserts_marks_into_a_long_unpunctuated_run(real: PunctuationModel) -> None:
    text = "我相信當我們這樣子不斷不斷的練習今年faithful steward管家我相信這會是對我們當中又忠心又良善的好管家"
    result = real.restore(text, terminal=True)
    assert result.changed and result.text.endswith("。")
    stripped = result.text
    for index, _mark in reversed(result.inserted):
        stripped = stripped[:index] + stripped[index + 1 :]
    assert stripped == text
    assert result.elapsed_ms < 500


def test_existing_marks_survive(real: PunctuationModel) -> None:
    text = "是祢在感動我，把我的國先放下來把我所在意的事情先放下來聖靈我知道祢在邀請我"
    result = real.restore(text, terminal=True)
    assert "我，把" in result.text
    assert result.text.replace("。", "").replace("，", "").replace("、", "").replace(
        "？", ""
    ) == text.replace("，", "")


def test_english_and_short_text_are_left_alone(real: PunctuationModel) -> None:
    assert real.restore("hello world how are you today").text == "hello world how are you today"
    assert real.restore("好").text == "好"
