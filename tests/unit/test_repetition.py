from __future__ import annotations

import asyncio
from collections import deque
from typing import Any

import pytest

from tea_asr.api.stream import ClosedSegment, StreamSession
from tea_asr.config import ServiceConfig
from tea_asr.repetition import trim_repetitions
from tea_asr.stable import StablePrefixTracker


@pytest.mark.parametrize(
    ("unit", "limit"),
    [("哈", 3), ("ab", 3), ("abc", 3), ("abcd", 3), ("abcde", 3), ("abcdef", 3)],
)
def test_trims_each_supported_unit_length(unit: str, limit: int) -> None:
    text, trims = trim_repetitions(unit * (limit + 2))

    assert text == unit * limit
    assert len(trims) == 1
    assert trims[0].unit_length == len(unit)
    assert trims[0].removed_chars == len(unit) * 2


@pytest.mark.parametrize(
    "sample",
    (
        "1000000元",
        "0999999999",
        "電話0999999999",
        "2000年",
        "3.14159",
        "０００",
        "一九九九年",
        "零零七",
        "\u0661" * 7,
        "\uff10" * 6,
        "a1" * 6,
        "x\u0661" * 6,
    ),
)
def test_preserves_numbers_exactly(sample: str) -> None:
    assert trim_repetitions(sample) == (sample, ())


@pytest.mark.parametrize(
    "sample",
    ("1" + "餘" * 6, "餘" * 6 + "1", "\u0661" + "餘" * 6, "餘" * 6 + "\u0661"),
)
def test_does_not_trim_runs_adjacent_to_digits(sample: str) -> None:
    assert trim_repetitions(sample) == (sample, ())


@pytest.mark.parametrize("sample", ("x" + "a" * 6 + "y", "prefix-" + "a" * 6 + "suffix"))
def test_does_not_trim_ascii_letter_runs_inside_words(sample: str) -> None:
    assert trim_repetitions(sample) == (sample, ())


def test_trims_ascii_letter_runs_delimited_by_non_letters() -> None:
    assert trim_repetitions("好 " + "a" * 6 + "。")[0] == "好 aaa。"


def test_preserves_repeated_cjk_numerals() -> None:
    for numeral in "〇零一二三四五六七八九十百千萬億":
        sample = numeral * 6
        assert trim_repetitions(sample) == (sample, ())
    assert trim_repetitions("一九" * 6) == ("一九" * 6, ())


def test_still_trims_real_character_and_two_character_loops() -> None:
    assert trim_repetitions("餘" * 60)[0] == "餘餘餘"
    assert trim_repetitions("ab" * 6)[0] == "ab" * 3


def test_keeps_common_short_emphasis_and_repeated_phrase() -> None:
    for text in ("哈哈哈", "對對對", "是的，是的，是的"):
        assert trim_repetitions(text) == (text, ())


def test_ignores_punctuation_and_spaces_between_repeats() -> None:
    text, trims = trim_repetitions("早安，早安、早安；早安")

    assert text == "早安，早安、早安"
    assert trims[0].unit_length == 2
    assert trims[0].removed_chars == 3


def test_does_not_change_normal_text() -> None:
    samples = (
        "今天下午討論 API 設計，明天再確認。",
        "mixed text with 2 numbers and punctuation!",
        "",
    )
    for sample in samples:
        assert trim_repetitions(sample) == (sample, ())


def test_limits_are_applied_to_the_matching_unit_type() -> None:
    assert trim_repetitions("哈哈哈哈", single_char_limit=2)[0] == "哈哈"
    assert trim_repetitions("abababab", multi_char_limit=2)[0] == "abab"


def test_limits_must_be_positive() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        trim_repetitions("abc", multi_char_limit=0)


def test_stable_prefix_does_not_shrink_when_a_later_trimmed_partial_is_shorter() -> None:
    tracker = StablePrefixTracker(agreement=2)
    first, _ = trim_repetitions("餘" * 10)
    shorter, _ = trim_repetitions("餘餘")

    assert tracker.observe(first) is None
    committed = tracker.observe(first)
    assert committed is not None and committed.text == "餘餘餘"
    assert tracker.observe(shorter) is None
    assert tracker.text == committed.text


class ScriptedScheduler:
    def __init__(self, texts: list[str]) -> None:
        self.texts = deque(texts)

    async def transcribe(
        self,
        pcm: bytes,
        *,
        language: str = "Chinese",
        kind: str = "interactive",
        is_stale: Any = None,
    ) -> tuple[dict[str, Any], int]:
        return {"text": self.texts.popleft(), "total_time_s": 0.01}, 0


class RecordingSocket:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_json(self, payload: dict[str, Any]) -> None:
        self.sent.append(payload)


def _run(session: StreamSession, *steps: Any) -> list[dict[str, Any]]:
    async def scenario() -> None:
        writer = asyncio.create_task(session.writer.run())
        for step in steps:
            await step()
            await session.writer.drain(timeout=1.0)
        writer.cancel()

    asyncio.run(scenario())
    return session._websocket.sent  # type: ignore[attr-defined]


def test_final_text_is_trimmed_but_raw_text_is_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log_calls: list[tuple[str, str, dict[str, Any]]] = []

    def capture_log(
        logger: Any,
        message: str,
        *,
        level: str = "info",
        **fields: object,
    ) -> None:
        log_calls.append((message, level, fields))

    monkeypatch.setattr("tea_asr.api.stream.log_event", capture_log)
    socket = RecordingSocket()
    session = StreamSession(
        socket,  # type: ignore[arg-type]
        ScriptedScheduler(["餘" * 8]),
        config=ServiceConfig(),
        model_state="ready",
    )
    segment = session._open_segment(0)
    pcm = b"\0\0" * 8000
    events = _run(
        session,
        lambda: session._transcribe_segment(ClosedSegment(segment, pcm, 8000, "stop")),
    )
    final = next(event for event in events if event["type"] == "transcript.final")

    assert final["text"] == "餘餘餘"
    assert final["raw_text"] == "餘" * 8
    assert final["warnings"] == ["repetition_trimmed"]
    assert log_calls == [
        (
            "stream.repetition_trimmed",
            "info",
            {
                "session_id": session._state.session_id,
                "segment_id": segment.segment_id,
                "segment_index": segment.index,
                "kind": "final",
                "unit_length": 1,
                "removed_chars": 5,
            },
        )
    ]


def test_stream_stable_events_remain_append_only_across_trimmed_partials() -> None:
    socket = RecordingSocket()
    session = StreamSession(
        socket,  # type: ignore[arg-type]
        ScriptedScheduler(["餘" * 8, "餘" * 8, "餘餘", "餘餘"]),
        config=ServiceConfig(),
        model_state="ready",
    )
    session._transcript_mode = "revisable"
    session._stable_agreement = 2
    segment = session._open_segment(0)
    pcm = b"\0\0" * 8000
    events = _run(
        session,
        lambda: session._run_preview(pcm, 12800, segment),
        lambda: session._run_preview(pcm, 25600, segment),
        lambda: session._run_preview(pcm, 38400, segment),
        lambda: session._transcribe_segment(ClosedSegment(segment, pcm, 48000, "stop")),
    )
    stable_texts = [event["text"] for event in events if event["type"] == "transcript.stable"]

    assert stable_texts == ["餘餘餘", "餘餘餘"]
    assert stable_texts[1].startswith(stable_texts[0])


def test_keeps_letter_runs_inside_identifiers_and_urls() -> None:
    for sample in ("user_aaaa_id", "www.aaaa.com", "ID-bbbb-01", "a@cccc.tw"):
        assert trim_repetitions(sample) == (sample, ())
