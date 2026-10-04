"""Punctuation restoration: tokenizer, insert-only policy, tail margin, gating.

The ONNX session is mocked (CI has no asset): a fake whose "model" puts a mark
after chosen characters. The real asset is exercised by
`tests/hardware/test_punctuation_real.py`.
"""

from __future__ import annotations

import asyncio
import itertools
import threading
import time
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from tea_asr import punctuation as punct
from tea_asr.api import app as app_module
from tea_asr.api.stream import ClosedSegment, StreamSession
from tea_asr.config import ServiceConfig
from tea_asr.punctuation import (
    PunctuationModel,
    PunctuationRuntime,
    PunctuationUnavailableError,
    hold_back_tail,
    tokenize,
)

MARKS = ["<unk>", "_", "，", "。", "？", "、"]
COMMA, DOT, QUEST, PAUSE = 2, 3, 4, 5
CHARS = "我們今天來到這裡看見神的榮耀並且很高興你好嗎大家平安謝謝"


class FakeSession:
    """Predicts ``after[char]`` (a mark id) for each CJK character, else `_`."""

    def __init__(self, after: dict[str, int] | None = None) -> None:
        tokens = ["<blank>", "<s>", "</s>", "<unk>", *CHARS, "hello", "world", "iphone"]
        self.tokens = tokens
        self.after = after or {}
        self.calls: list[list[str]] = []
        self.meta = {
            "tokens": "|".join(tokens),
            "punctuations": "|".join(MARKS),
            "unk_symbol": "<unk>",
            "vocab_size": str(len(tokens)),
        }

    def get_modelmeta(self) -> Any:
        return SimpleNamespace(custom_metadata_map=self.meta)

    def get_inputs(self) -> list[Any]:
        return [SimpleNamespace(name="inputs"), SimpleNamespace(name="text_lengths")]

    def run(self, _outputs: Any, feed: dict[str, np.ndarray]) -> list[np.ndarray]:
        ids = feed["inputs"][0].tolist()
        assert feed["inputs"].dtype == np.int32 and feed["text_lengths"].tolist() == [len(ids)]
        words = [self.tokens[i] for i in ids]
        self.calls.append(words)
        logits = np.zeros((1, len(ids), len(MARKS)), dtype=np.float32)
        for index, word in enumerate(words):
            logits[0, index, self.after.get(word, 1)] = 1.0
        return [logits]


def model_with(after: dict[str, int] | None = None) -> PunctuationModel:
    return PunctuationModel(session=FakeSession(after))


def strip_inserted(result: punct.PunctuationResult) -> str:
    text = result.text
    for index, _mark in reversed(result.inserted):
        text = text[:index] + text[index + 1 :]
    return text


# -- tokenizer ----------------------------------------------------------------


def test_tokenizer_splits_chinese_by_character_and_english_by_word() -> None:
    tokens = tokenize("我們Hello World你好 iPhone")
    assert [t.text for t in tokens] == ["我", "們", "hello", "world", "你", "好", "iphone"]
    # Spans index the original text, so marks can be inserted in place.
    text = "我們Hello World"
    assert [text[t.start : t.end] for t in tokenize(text)] == ["我", "們", "Hello", "World"]


def test_tokenizer_ignores_marks_and_whitespace_but_keeps_apostrophes() -> None:
    tokens = tokenize("我，好。 don't stop-now！")
    assert [t.text for t in tokens] == ["我", "好", "don't", "stop", "now"]


def test_tokenizer_keeps_digits_in_words() -> None:
    assert [t.text for t in tokenize("第3章 covid19")] == ["第", "3", "章", "covid19"]


# -- insert-only policy ---------------------------------------------------------


def test_inserts_marks_after_predicted_characters_without_changing_anything_else() -> None:
    model = model_with({"裡": COMMA, "耀": DOT})
    text = "我們來到這裡看見神的榮耀並且很高興"
    result = model.restore(text)
    assert result.text == "我們來到這裡，看見神的榮耀。並且很高興"
    assert result.changed and result.skipped is None
    assert strip_inserted(result) == text
    assert [(result.text[i], m) for i, m in result.inserted] == [("，", "，"), ("。", "。")]


def test_existing_marks_are_never_replaced_or_removed() -> None:
    # The model would put a "。" after 裡, but the ASR already has a "，" there.
    model = model_with({"裡": DOT, "耀": COMMA})
    text = "我們來到這裡，看見神的榮耀並且很高興"
    result = model.restore(text, min_gap=0)
    assert "這裡，看見" in result.text
    assert "。" not in result.text
    assert result.text == "我們來到這裡，看見神的榮耀，並且很高興"
    assert strip_inserted(result) == text


def test_never_touches_characters_spaces_or_case() -> None:
    model = model_with({"天": COMMA})
    text = "我們今天 Hello  World 看見神的榮耀"
    result = model.restore(text)
    assert result.text == "我們今天， Hello  World 看見神的榮耀"
    assert strip_inserted(result) == text


def test_only_chinese_marks_are_inserted() -> None:
    # `<unk>` (id 0) and `_` (1) never insert; `、` and `？` are allowed.
    model = model_with({"我": 0, "們": PAUSE, "今": QUEST})
    result = model.restore("我們今天來到這裡看見神的榮耀", min_gap=0)
    assert result.text.startswith("我們、今？天")
    assert result.text.count("，") + result.text.count("。") == 0


def test_min_gap_keeps_inserted_marks_away_from_existing_and_each_other() -> None:
    model = model_with({"天": COMMA, "來": COMMA, "到": COMMA, "裡": COMMA})
    text = "我們今天來到這裡看見神的榮耀"
    spaced = model.restore(text, min_gap=3)
    assert spaced.text == "我們今天，來到這裡，看見神的榮耀"  # 來 and 到 are too close to 天
    positions = [i - n for n, (i, _m) in enumerate(spaced.inserted)]
    assert all(b - a >= 3 for a, b in itertools.pairwise(positions))
    near_existing = model.restore("我們今天來，到這裡看見神的榮耀", min_gap=3)
    assert strip_inserted(near_existing) == "我們今天來，到這裡看見神的榮耀"
    # 天 and 到 are within 3 tokens of the ASR's own mark; 裡 is far enough.
    assert near_existing.text == "我們今天來，到這裡，看見神的榮耀"
    assert [m for _i, m in near_existing.inserted] == ["，"]


def test_skips_short_and_mostly_english_text() -> None:
    model = model_with({"天": COMMA})
    assert model.restore("今天好").skipped == "short"
    assert model.restore("   今 ").skipped == "short"
    english = model.restore("hello world hello world 我")
    assert english.skipped == "english" and english.text == "hello world hello world 我"
    assert model.restore("我們今天來到這裡 hello").skipped is None


def test_skipped_text_does_not_call_the_model() -> None:
    session = FakeSession({"天": COMMA})
    model = PunctuationModel(session=session)
    model.restore("今天好")
    model.restore("hello world hello world 我")
    assert session.calls == []


def test_terminal_adds_a_closing_mark_only_when_missing() -> None:
    model = model_with({"耀": DOT, "嗎": QUEST})
    assert model.restore("我們來到這裡看見神的榮耀並且很高興", terminal=True).text.endswith("高興。")
    assert model.restore("我們來到這裡看見神的榮耀並且很高興。", terminal=True).text.endswith("高興。")
    assert model.restore("大家平安謝謝你好嗎", terminal=True).text.endswith("嗎？")
    # A comma predicted on the last token becomes a full stop, never a trailing comma.
    ends_comma = model_with({"興": COMMA}).restore("我們來到這裡看見神很高興", terminal=True)
    assert ends_comma.text.endswith("高興。")
    # Not terminal (a partial): nothing is added at the very end.
    assert not model.restore("我們來到這裡看見神的榮耀並且很高興").text.endswith("。")


def test_tail_margin_withholds_marks_near_the_end_of_a_partial() -> None:
    model = model_with({"耀": COMMA, "且": COMMA})
    text = "我們來到這裡看見神的榮耀並且很高興"  # 且 is 3 characters from the end
    assert "且，" in model.restore(text, tail_margin=0, min_gap=0).text
    withheld = model.restore(text, tail_margin=4, min_gap=0)
    assert "且，" not in withheld.text and "耀，" in withheld.text
    assert strip_inserted(withheld) == text
    # Exactly `margin` characters follow: allowed; one fewer: withheld.
    assert "且，" in model.restore(text, tail_margin=3, min_gap=0).text
    assert "且，" not in model.restore(text, tail_margin=4, min_gap=0).text


def test_long_text_uses_growing_windows_and_marks_every_token() -> None:
    session = FakeSession({"耀": DOT})
    model = PunctuationModel(session=session)
    text = "我們來到這裡看見神的榮耀" * 6  # 72 tokens
    result = model.restore(text, min_gap=0)
    assert strip_inserted(result) == text
    assert result.text.count("。") >= 5  # every sentence end is found in some window
    assert max(len(call) for call in session.calls) <= 40  # windows restart after each stop


def test_tokens_after_the_last_sentence_end_are_still_decoded() -> None:
    # The reference decoder drops tokens after the last stop of the final window;
    # we decode them in one more pass.
    session = FakeSession({"耀": DOT, "高": COMMA})
    model = PunctuationModel(session=session)
    text = "我們來到這裡看見神的榮耀並且很高興大家平安"
    result = model.restore(text, min_gap=0)
    assert "耀。" in result.text and "高，" in result.text
    assert strip_inserted(result) == text


# -- hold_back_tail -------------------------------------------------------------


def test_hold_back_tail_drops_the_last_characters_and_keeps_marks_at_the_cut() -> None:
    assert hold_back_tail("我們來到，這裡看見神", 0) == "我們來到，這裡看見神"
    assert hold_back_tail("我們來到，這裡看見神", 3) == "我們來到，這裡"
    assert hold_back_tail("我們來到，這裡看見神", 2) == "我們來到，這裡看"
    assert hold_back_tail("我們來到，這", 5) == ""
    assert hold_back_tail("我們來到，這", 5) == ""
    assert hold_back_tail("我們來到。", 1) == "我們來"


def test_hold_back_tail_is_a_prefix_so_stable_stays_append_only() -> None:
    full = "我們來到，這裡看見神的榮耀。"
    for count in range(8):
        assert full.startswith(hold_back_tail(full, count))


# -- load / hash / stats ------------------------------------------------------------


def test_load_refuses_a_missing_or_tampered_asset(tmp_path: Path) -> None:
    with pytest.raises(PunctuationUnavailableError, match="missing"):
        PunctuationModel(tmp_path / "nope.onnx")
    bad = tmp_path / "model.int8.onnx"
    bad.write_bytes(b"tampered")
    with pytest.raises(PunctuationUnavailableError, match="hash"):
        PunctuationModel(bad)


def test_load_refuses_changed_metadata_or_inputs() -> None:
    session = FakeSession()
    session.meta["vocab_size"] = "1"
    with pytest.raises(PunctuationUnavailableError):
        PunctuationModel(session=session)
    session = FakeSession()
    del session.meta["punctuations"]
    with pytest.raises(PunctuationUnavailableError):
        PunctuationModel(session=session)
    session = FakeSession()
    session.get_inputs = lambda: [SimpleNamespace(name="x")]  # type: ignore[method-assign]
    with pytest.raises(PunctuationUnavailableError):
        PunctuationModel(session=session)


def test_pinned_hashes_are_in_the_lock_file() -> None:
    import json

    lock = json.loads(Path(__file__).parents[2].joinpath("models.lock.json").read_text())
    entry = lock["punctuation"]
    assert entry["files"]["model.int8.onnx"] == punct.PUNCTUATION_MODEL_SHA256
    assert entry["archive"]["sha256"] == punct.PUNCTUATION_ARCHIVE_SHA256
    assert entry["archive"]["url"] == punct.PUNCTUATION_ARCHIVE_URL
    assert entry["license"] == "Apache-2.0"


def test_milliseconds_per_call_are_recorded() -> None:
    model = model_with({"天": COMMA})
    for _ in range(3):
        model.restore("我們今天來到這裡看見神的榮耀")
    model.restore("短")  # skipped calls are not model calls
    snapshot = model.stats.snapshot()
    assert snapshot["calls"] == 3 and snapshot["inserted_marks"] == 3
    assert snapshot["max_ms"] >= snapshot["p50_ms"] >= 0.0


# -- executor: never blocks the event loop ---------------------------------------


def test_restore_runs_off_the_event_loop() -> None:
    release = threading.Event()
    seen: dict[str, Any] = {}

    class SlowModel:
        def restore(self, text: str, **kwargs: Any) -> punct.PunctuationResult:
            seen["thread"] = threading.current_thread().name
            seen["kwargs"] = kwargs
            release.wait(5)
            return punct.PunctuationResult(text + "。", ((len(text), "。"),))

    runtime = PunctuationRuntime(SlowModel(), tail_margin=2)  # type: ignore[arg-type]

    async def scenario() -> tuple[int, str]:
        task = asyncio.create_task(runtime.restore("測試文字", final=False))
        ticks = 0
        for _ in range(20):  # the loop keeps turning while the model is busy
            await asyncio.sleep(0.005)
            ticks += 1
        assert not task.done()
        release.set()
        return ticks, (await task).text

    try:
        ticks, text = asyncio.run(scenario())
    finally:
        runtime.close()
    assert ticks == 20 and text == "測試文字。"
    assert seen["thread"].startswith("punct") and seen["thread"] != "MainThread"
    assert seen["kwargs"] == {"tail_margin": 2, "terminal": False}


def test_final_restore_uses_no_margin_and_terminal() -> None:
    seen: list[dict[str, Any]] = []

    class Spy:
        def restore(self, text: str, **kwargs: Any) -> punct.PunctuationResult:
            seen.append(kwargs)
            return punct.PunctuationResult(text)

    runtime = PunctuationRuntime(Spy(), tail_margin=5)  # type: ignore[arg-type]
    try:
        asyncio.run(runtime.restore("一二三四五六", final=True))
    finally:
        runtime.close()
    assert seen == [{"tail_margin": 0, "terminal": True}]


# -- config / capability gating ----------------------------------------------------------


def test_config_defaults_env_and_validation() -> None:
    assert ServiceConfig().punctuation_tail_margin == 2
    assert ServiceConfig.load(env={"TEA_ASR_PUNCTUATION": "1"}).punctuation_restore_enabled
    assert not ServiceConfig.load(env={"TEA_ASR_PUNCTUATION": "0"}).punctuation_restore_enabled
    with pytest.raises(ValueError, match="punctuation_tail_margin"):
        ServiceConfig(punctuation_tail_margin=-1)
    with pytest.raises(ValueError, match="punctuation_tail_margin"):
        ServiceConfig(punctuation_tail_margin=21)


def test_disabled_config_never_touches_the_asset(monkeypatch: pytest.MonkeyPatch) -> None:
    looked: list[bool] = []
    monkeypatch.setattr(app_module, "locate_punctuation", lambda: looked.append(True))
    assert app_module._load_punctuation(ServiceConfig(punctuation_restore_enabled=False)) is None
    assert looked == []  # must not even look for the asset when disabled


def test_missing_or_tampered_asset_means_no_runtime(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = ServiceConfig(punctuation_restore_enabled=True)

    def missing() -> None:
        raise FileNotFoundError("not prepared")

    monkeypatch.setattr(app_module, "locate_punctuation", missing)
    assert app_module._load_punctuation(config) is None
    bad = tmp_path / "model.int8.onnx"
    bad.write_bytes(b"tampered")
    monkeypatch.setattr(app_module, "locate_punctuation", lambda: bad)
    assert app_module._load_punctuation(config) is None


def test_verified_asset_builds_a_runtime_with_the_configured_margin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    model = tmp_path / "model.int8.onnx"
    model.write_bytes(b"model")
    monkeypatch.setattr(app_module, "locate_punctuation", lambda: model)

    class StubModel:
        def __init__(self, path: Path) -> None:
            self.path = path

    monkeypatch.setattr(app_module, "PunctuationModel", StubModel)
    runtime = app_module._load_punctuation(
        ServiceConfig(punctuation_restore_enabled=True, punctuation_tail_margin=5)
    )
    assert runtime is not None and runtime.tail_margin == 5
    assert runtime.model.path == model  # type: ignore[attr-defined]
    runtime.close()


def test_capability_is_reported_only_when_the_runtime_loaded() -> None:
    from tests.conftest import AUTH, FakeSupervisor, build_client

    with build_client(FakeSupervisor(), punctuation_runtime=None) as http:
        features = http.get("/v1/capabilities", headers=AUTH).json()["features"]
        assert "punctuation_restore" not in features
    runtime = PunctuationRuntime(model_with())
    try:
        with build_client(FakeSupervisor(), punctuation_runtime=runtime) as http:
            features = http.get("/v1/capabilities", headers=AUTH).json()["features"]
            assert features["punctuation_restore"] is True
    finally:
        runtime.close()


# -- stream integration: partial + final + stable ----------------------------------------------


class Scheduler:
    def __init__(self, texts: list[str]) -> None:
        self.texts = deque(texts)

    async def transcribe(self, pcm: bytes, **_kwargs: Any) -> tuple[dict[str, Any], int]:
        return {"text": self.texts.popleft(), "total_time_s": 0.01}, 0


class Socket:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_json(self, payload: dict[str, Any]) -> None:
        self.sent.append(payload)


def stream_session(
    texts: list[str], runtime: PunctuationRuntime | None, **config: Any
) -> StreamSession:
    session = StreamSession(
        Socket(),  # type: ignore[arg-type]
        Scheduler(texts),
        config=ServiceConfig(**config),
        model_state="ready",
        punctuation=runtime,
    )
    session._transcript_mode = "revisable"
    session._stable_agreement = 2
    return session


def run_steps(session: StreamSession, *steps: Any) -> list[dict[str, Any]]:
    async def scenario() -> None:
        writer = asyncio.create_task(session.writer.run())
        for step in steps:
            await step()
            await session.writer.drain(timeout=1.0)
        writer.cancel()

    asyncio.run(scenario())
    return session._websocket.sent  # type: ignore[attr-defined]


def test_stream_partial_final_and_stable_carry_punctuation_and_the_warning() -> None:
    runtime = PunctuationRuntime(model_with({"裡": COMMA, "耀": DOT}), tail_margin=2)
    first = "我們來到這裡看見神的榮耀並且很高興"
    texts = [first[:12], first[:14], first]
    session = stream_session(texts, runtime)
    pcm = b"\0\0" * 16_000
    segment = session._open_segment(0)

    async def close() -> None:
        segment.terminal = True

    try:
        events = run_steps(
            session,
            lambda: session._run_preview(pcm, 12_800, segment),
            lambda: session._run_preview(pcm, 25_600, segment),
            close,
            lambda: session._transcribe_segment(ClosedSegment(segment, pcm, 32_000, "silence")),
        )
    finally:
        runtime.close()
    partials = [e for e in events if e["type"] == "transcript.partial"]
    final = next(e for e in events if e["type"] == "transcript.final")
    stable = [e for e in events if e["type"] == "transcript.stable"]
    assert "裡，" in partials[1]["text"] and "punctuation_restored" in partials[1]["warnings"]
    # Tail margin: the last two characters of a partial are never followed by a mark.
    assert not partials[1]["text"].endswith(("。", "，"))
    assert final["text"] == "我們來到這裡，看見神的榮耀。並且很高興。"
    assert final["raw_text"] == first  # raw_text keeps the model's output as is
    assert "punctuation_restored" in final["warnings"]
    # Stable commits punctuation as text and stays append-only through the final.
    texts_seen = [e["text"] for e in stable]
    assert len(texts_seen) >= 2 and "，" in texts_seen[0] + texts_seen[1]
    for earlier, later in itertools.pairwise(texts_seen):
        assert later.startswith(earlier)
    assert texts_seen[-1] == final["text"] and stable[-1]["state"] == "final"


def test_stream_without_a_runtime_is_unchanged() -> None:
    session = stream_session(["我們來到這裡看見神", "我們來到這裡看見神的榮耀"], None)
    pcm = b"\0\0" * 16_000
    segment = session._open_segment(0)

    async def close() -> None:
        segment.terminal = True

    events = run_steps(
        session,
        lambda: session._run_preview(pcm, 12_800, segment),
        close,
        lambda: session._transcribe_segment(ClosedSegment(segment, pcm, 32_000, "silence")),
    )
    final = next(e for e in events if e["type"] == "transcript.final")
    assert final["text"] == final["raw_text"] == "我們來到這裡看見神的榮耀"
    assert "punctuation_restored" not in final["warnings"]


def test_stream_keeps_text_when_the_model_fails() -> None:
    class Broken:
        async def restore(self, text: str, *, final: bool) -> Any:
            raise RuntimeError("onnx exploded")

    session = stream_session(["我們來到這裡看見神的榮耀"], Broken())  # type: ignore[arg-type]
    pcm = b"\0\0" * 16_000
    segment = session._open_segment(0)
    events = run_steps(
        session,
        lambda: session._transcribe_segment(ClosedSegment(segment, pcm, 32_000, "stop")),
    )
    final = next(e for e in events if e["type"] == "transcript.final")
    assert final["text"] == "我們來到這裡看見神的榮耀"
    assert "punctuation_restored" not in final["warnings"]


def test_a_partial_whose_segment_closed_during_restoration_is_dropped() -> None:
    gate: dict[str, asyncio.Event] = {}

    class Slow:
        tail_margin = 2

        async def restore(self, text: str, *, final: bool) -> punct.PunctuationResult:
            gate["entered"].set()
            await gate["release"].wait()
            return punct.PunctuationResult(text + "，", ((len(text), "，"),))

    session = stream_session(["我們來到這裡看見神的榮耀"], Slow())  # type: ignore[arg-type]
    pcm = b"\0\0" * 16_000
    segment = session._open_segment(0)

    async def scenario() -> None:
        gate["entered"], gate["release"] = asyncio.Event(), asyncio.Event()
        writer = asyncio.create_task(session.writer.run())
        task = asyncio.create_task(session._run_preview(pcm, 12_800, segment))
        await gate["entered"].wait()
        segment.terminal = True  # the final won the race while the model ran
        gate["release"].set()
        await task
        await session.writer.drain(timeout=1.0)
        writer.cancel()

    asyncio.run(scenario())
    sent = session._websocket.sent  # type: ignore[attr-defined]
    assert [e for e in sent if e["type"] == "transcript.partial"] == []
    assert segment.revision == 0


def test_restore_is_slow_safe_final_waits_for_executor() -> None:
    """A slow model delays the final but the loop stays free (see executor test)."""

    runtime = PunctuationRuntime(model_with({"耀": DOT}))
    started = time.monotonic()
    try:
        result = asyncio.run(runtime.restore("我們來到這裡看見神的榮耀並且很高興", final=True))
    finally:
        runtime.close()
    assert result.changed and time.monotonic() - started < 5


def test_stable_tracker_sees_the_partial_without_its_uncertain_tail() -> None:
    """With punctuation on, `transcript.stable` holds back `tail_margin` characters."""

    runtime = PunctuationRuntime(model_with(), tail_margin=2)
    session = stream_session(["我們來到這裡看見神", "我們來到這裡看見神的"], runtime)
    pcm = b"\0\0" * 16_000
    segment = session._open_segment(0)
    try:
        events = run_steps(
            session,
            lambda: session._run_preview(pcm, 12_800, segment),
            lambda: session._run_preview(pcm, 25_600, segment),
        )
    finally:
        runtime.close()
    stable = [e for e in events if e["type"] == "transcript.stable"]
    # Both partials agree on all of 我們來到這裡看見神; the last 2 characters of each are held back.
    assert [e["text"] for e in stable] == ["我們來到這裡看"]
    assert [e["text"] for e in events if e["type"] == "transcript.partial"][-1] == (
        "我們來到這裡看見神的"
    )
