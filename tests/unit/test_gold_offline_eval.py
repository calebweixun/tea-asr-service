from __future__ import annotations

import json
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest

from benchmarks import gold_offline_eval


def _write_wav(path: Path, duration_s: int = 5) -> None:
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(16_000)
        wav_file.writeframes(b"\0\0" * (duration_s * 16_000))


def _write_answers(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "set": "synthetic",
                "items": [
                    {"id": "first", "start_s": 1, "end_s": 2, "reference": "新"},
                    {"id": "second", "start_s": 3, "end_s": 4, "reference": "新"},
                ],
            }
        ),
        encoding="utf-8",
    )


def test_parse_args_accepts_span_padding_and_requires_dictionary_for_prompt() -> None:
    args = gold_offline_eval.parse_args(
        [
            "--answers",
            "answers.json",
            "--wav",
            "audio.wav",
            "--model-path",
            "model",
            "--mode",
            "span",
            "--pad-s",
            "0.5",
            "--dictionary",
            "church.toml",
            "--prompt",
        ]
    )
    assert args.mode == "span"
    assert args.pad_s == 0.5
    assert args.prompt is True

    with pytest.raises(SystemExit):
        gold_offline_eval.parse_args(
            ["--answers", "answers.json", "--wav", "audio.wav", "--model-path", "model", "--prompt"]
        )
    with pytest.raises(SystemExit):
        gold_offline_eval.parse_args(
            [
                "--answers",
                "answers.json",
                "--wav",
                "audio.wav",
                "--model-path",
                "model",
                "--pad-s",
                "0.5",
            ]
        )
    with pytest.raises(SystemExit):
        gold_offline_eval.parse_args(
            [
                "--answers",
                "answers.json",
                "--wav",
                "audio.wav",
                "--model-path",
                "model",
                "--dictionary",
                "church.json",
            ]
        )


def test_main_uses_mock_backend_context_filters_and_writes_timings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    answers = tmp_path / "answers.json"
    wav_path = tmp_path / "audio.wav"
    dictionary = tmp_path / "church.toml"
    output = tmp_path / "eval" / "hypothesis.json"
    _write_answers(answers)
    _write_wav(wav_path)
    dictionary.write_text(
        'domain = "敬拜"\nhotwords = ["祢"]\n\n[[replacements]]\nfrom = "舊"\nto = "新"\n',
        encoding="utf-8",
    )
    instances: list[FakeBackend] = []

    class FakeBackend:
        def __init__(self, model_path: Path) -> None:
            self.model_path = model_path
            self.calls: list[tuple[int, str | None]] = []
            self.closed = False
            instances.append(self)

        def load(self) -> None:
            return None

        def transcribe(self, audio, *, system_prompt=None):
            self.calls.append((audio.size, system_prompt))
            return SimpleNamespace(text="舊好好好好祢\ue000", total_time_s=0.125)

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(gold_offline_eval, "TeaMlxBackend", FakeBackend)
    result = gold_offline_eval.main(
        [
            "--answers",
            str(answers),
            "--wav",
            str(wav_path),
            "--model-path",
            str(tmp_path / "model"),
            "--output",
            str(output),
            "--mode",
            "span",
            "--pad-s",
            "0.5",
            "--dictionary",
            str(dictionary),
            "--prompt",
        ]
    )

    assert result == 0
    assert json.loads(output.read_text(encoding="utf-8")) == {
        "first": "新好好好祢",
        "second": "新好好好祢",
    }
    timing_path = output.with_name("hypothesis.timings.json")
    timings = json.loads(timing_path.read_text(encoding="utf-8"))
    assert timings["first"]["audio_s"] == 2.0
    assert timings["first"]["model_time_s"] == 0.125
    backend = instances[0]
    assert [sample_count for sample_count, _ in backend.calls] == [32_000, 32_000]
    assert all("Domain: 敬拜" in prompt and "Terms: 祢" in prompt for _, prompt in backend.calls)
    assert backend.closed is True

    no_prompt_output = tmp_path / "eval" / "no-prompt.json"
    assert gold_offline_eval.main(
        [
            "--answers",
            str(answers),
            "--wav",
            str(wav_path),
            "--model-path",
            str(tmp_path / "model"),
            "--dictionary",
            str(dictionary),
            "--output",
            str(no_prompt_output),
        ]
    ) == 0
    assert json.loads(no_prompt_output.read_text(encoding="utf-8"))["first"] == "新好好好祢"
    assert instances[1].calls == [(16_000, None), (16_000, None)]
    assert instances[1].closed is True
    assert "decode-time first:" in capsys.readouterr().err


def test_segment_mode_keeps_the_exact_item_span() -> None:
    import numpy as np

    samples = np.zeros(5 * 16_000, dtype=np.float32)
    audio = gold_offline_eval._item_audio(
        samples, {"id": "one", "start_s": 1, "end_s": 2}, mode="segment", pad_s=0
    )
    assert audio.size == 16_000
