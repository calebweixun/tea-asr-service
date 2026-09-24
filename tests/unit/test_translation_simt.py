from __future__ import annotations

from itertools import pairwise

import pytest

from tea_asr.translation.simt import (
    LATENCY_MODES,
    STOP_TOKEN_IDS,
    VERIFIED_DIRECTIONS,
    LatencyMode,
    SimtEngine,
    build_user_message,
    parse_model_response,
    sanitize_history,
)


class ScriptedModel:
    """Returns queued raw completions and records every prompt it was sent."""

    def __init__(self, *outputs: str | Exception) -> None:
        self.outputs = list(outputs)
        self.prompts: list[str] = []
        self.forced: list[bool] = []

    def __call__(self, user_message: str, *, force: bool, mode: LatencyMode) -> str:
        self.prompts.append(user_message)
        self.forced.append(force)
        output = self.outputs.pop(0)
        if isinstance(output, Exception):
            raise output
        return output


def engine(model: ScriptedModel, **kwargs: object) -> SimtEngine:
    return SimtEngine(model, "zh2en", LATENCY_MODES["native"], **kwargs)  # type: ignore[arg-type]


def test_prompt_matches_the_official_interleaved_history_layout() -> None:
    message = build_user_message("zh2en", "你好¦Hello§", "今天")
    assert message.startswith("### Role\nYou are a professional Chinese-to-English")
    assert message.endswith("\n\n<STREAMING_HISTORY>\n你好¦Hello§\n\n<CURRENT_INPUT>\n今天")
    # Everything before the history is byte-identical between calls, which is
    # what makes the KV prefix reusable.
    assert build_user_message("zh2en", "", "x").split("<STREAMING_HISTORY>")[0] == (
        message.split("<STREAMING_HISTORY>")[0]
    )


def test_calibrated_modes_and_stop_tokens_are_the_published_ones() -> None:
    assert STOP_TOKEN_IDS == (151643, 151645)
    assert LATENCY_MODES["native"].tau == 0.0
    assert LATENCY_MODES["low"].tau == pytest.approx(0.9375009536743164)
    assert LATENCY_MODES["high"].tau == pytest.approx(-0.39)
    # Only what was exercised is offered (docs/06 #6).
    assert VERIFIED_DIRECTIONS == ("zh2en",)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", ("WAIT", "")),
        ("<|im_end|>", ("WAIT", "")),
        ("WAIT", ("WAIT", "")),
        ("TRANS: Hello", ("TRANS", "Hello")),
        ("Hello¦there§", ("TRANS", "Hello｜there；")),
    ],
)
def test_response_parsing(raw: str, expected: tuple[str, str]) -> None:
    assert parse_model_response(raw) == expected


def test_sanitize_keeps_history_framing_intact() -> None:
    assert sanitize_history(" a¦b§c ") == "a｜b；c"


def test_wait_keeps_the_buffer_and_trans_appends_history() -> None:
    model = ScriptedModel("", "The reason")
    simt = engine(model)
    first = simt.translate("遲遲", force=False)
    assert first.action == "WAIT"
    assert simt.buffer == "遲遲"
    second = simt.translate("未定的原因", force=True)
    assert second.action == "TRANS"
    assert second.source == "遲遲未定的原因"
    assert simt.history == [("遲遲未定的原因", "The reason")]
    assert simt.buffer == ""
    assert model.forced == [False, True]
    assert model.prompts[1].endswith("<CURRENT_INPUT>\n遲遲未定的原因")


def test_committed_history_is_append_only_until_the_window_trims() -> None:
    model = ScriptedModel(*[f"t{i}" for i in range(7)])
    simt = engine(model, history_window=4, history_keep=2)
    snapshots = []
    for i in range(7):
        simt.translate(f"s{i}", force=True)
        snapshots.append(list(simt.history))
    # Between trims each step only appends; nothing already committed changes.
    for before, after in pairwise(snapshots[:4]):
        assert after[: len(before)] == before
    assert snapshots[4] == [("s3", "t3"), ("s4", "t4")]


def test_failed_call_leaves_the_buffer_untouched_for_a_retry() -> None:
    model = ScriptedModel(RuntimeError("boom"), "ok")
    simt = engine(model)
    with pytest.raises(RuntimeError):
        simt.translate("一段", force=True)
    assert simt.buffer == ""
    assert simt.translate("一段", force=True).source == "一段"


def test_buffer_bound_forces_a_write() -> None:
    model = ScriptedModel("x")
    simt = engine(model, max_buffer_chars=4)
    simt.translate("一二三四五", force=False)
    assert model.forced == [True]
