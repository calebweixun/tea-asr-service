"""P0 品質量測：對固定語料跑一次辨識並算 CER／WER／MER。

語料為 adi-gov-tw/Taiwan-Tongues-ASR-CE-dataset-zhtw 的 test split
（Common Voice zh-TW 衍生，TRAIL-D 授權允許自由使用與再散布）。音訊只留在
Hugging Face cache，不進 Git。

    uv run python benchmarks/quality_eval.py --limit 100

輸出 docs/05 要求的兩組數字：保留繁體與標點的 raw 品質，以及內容正規化後的
品質；另外統計私用區字元的出現率，那是 P0 的封鎖問題。
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import subprocess
import sys
import tarfile
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tea_asr.backend import TeaMlxBackend
from tea_asr.model_manager import locate_prepared_model
from tea_asr.model_spec import TEA_ASR_1_1_MLX_4BIT

DATASET_ID = "adi-gov-tw/Taiwan-Tongues-ASR-CE-dataset-zhtw"
DATASET_REVISION = "ff0e8047bdce71c881c6d26eec7b2bbab6381ac1"
DATASET_SHARD = "test/test-000000.tar"

PRIVATE_USE = re.compile(r"[-]")
ASCII_WORD = re.compile(r"[a-z0-9]+(?:['’][a-z]+)?")


@dataclass(slots=True)
class Sample:
    key: str
    audio: bytes
    reference: str


@dataclass(slots=True)
class Totals:
    errors: int = 0
    length: int = 0

    def add(self, errors: int, length: int) -> None:
        self.errors += errors
        self.length += length

    @property
    def rate(self) -> float:
        return self.errors / self.length if self.length else 0.0


@dataclass(slots=True)
class Report:
    raw_mer: Totals = field(default_factory=Totals)
    norm_mer: Totals = field(default_factory=Totals)
    cer: Totals = field(default_factory=Totals)
    wer: Totals = field(default_factory=Totals)


def edit_distance(reference: list[str], hypothesis: list[str]) -> int:
    previous = list(range(len(hypothesis) + 1))
    for i, ref in enumerate(reference, start=1):
        current = [i]
        for j, hyp in enumerate(hypothesis, start=1):
            current.append(
                min(
                    previous[j] + 1,
                    current[j - 1] + 1,
                    previous[j - 1] + (ref != hyp),
                )
            )
        previous = current
    return previous[-1]


def normalize(text: str) -> str:
    """docs/05 的正規化規則：全形轉半形、去標點與空白、英文轉小寫。

    繁簡不做轉換：那會把真正的字形差異藏起來。
    """

    text = unicodedata.normalize("NFKC", text).lower()
    return "".join(
        char
        for char in text
        if not unicodedata.category(char).startswith(("P", "Z", "C"))
    )


def tokenize_mixed(text: str) -> list[str]:
    """中文逐字、英文逐詞，混合語料的 MER 才有意義。"""

    tokens: list[str] = []
    for match in re.finditer(r"[a-z0-9]+(?:['’][a-z]+)?|\S", text):
        tokens.append(match.group())
    return tokens


def chinese_chars(text: str) -> list[str]:
    return [char for char in text if "一" <= char <= "鿿"]


def english_words(text: str) -> list[str]:
    return ASCII_WORD.findall(text)


def load_samples(limit: int) -> list[Sample]:
    from huggingface_hub import hf_hub_download

    shard = hf_hub_download(
        DATASET_ID, DATASET_SHARD, repo_type="dataset", revision=DATASET_REVISION
    )
    samples: list[Sample] = []
    pending: dict[str, dict[str, bytes]] = {}
    with tarfile.open(shard) as archive:
        for member in archive:
            if not member.isfile():
                continue
            key, _, suffix = member.name.partition(".")
            if suffix not in {"mp3", "txt"}:
                continue
            handle = archive.extractfile(member)
            if handle is None:
                continue
            pending.setdefault(key, {})[suffix] = handle.read()
            entry = pending[key]
            if {"mp3", "txt"} <= entry.keys():
                samples.append(
                    Sample(
                        key=key,
                        audio=entry["mp3"],
                        reference=entry["txt"].decode("utf-8").strip(),
                    )
                )
                del pending[key]
                if len(samples) >= limit:
                    break
    return samples


def decode_mp3(data: bytes) -> np.ndarray:
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
         "-ac", "1", "-ar", "16000", "-f", "s16le", "pipe:1"],
        input=data,
        capture_output=True,
        check=True,
    )
    return np.frombuffer(result.stdout, dtype="<i2").astype(np.float32) / 32768.0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--out", type=Path, default=Path("benchmarks/results/quality.json"))
    args = parser.parse_args()

    samples = load_samples(args.limit)
    print(f"樣本 {len(samples)} 筆，語料 {DATASET_ID}@{DATASET_REVISION[:12]}")

    backend = TeaMlxBackend(locate_prepared_model(TEA_ASR_1_1_MLX_4BIT))
    backend.load()

    report = Report()
    rtfs: list[float] = []
    leaks = 0
    rows: list[dict[str, object]] = []
    for index, sample in enumerate(samples, start=1):
        audio = decode_mp3(sample.audio)
        started = time.perf_counter()
        result = backend.transcribe(audio)
        elapsed = time.perf_counter() - started
        duration = audio.size / 16_000
        rtfs.append(elapsed / duration if duration else 0.0)

        hypothesis = result.text
        if PRIVATE_USE.search(hypothesis):
            leaks += 1

        raw_ref, raw_hyp = tokenize_mixed(sample.reference), tokenize_mixed(hypothesis)
        report.raw_mer.add(edit_distance(raw_ref, raw_hyp), len(raw_ref))

        norm_reference, norm_hypothesis = normalize(sample.reference), normalize(hypothesis)
        norm_ref, norm_hyp = tokenize_mixed(norm_reference), tokenize_mixed(norm_hypothesis)
        report.norm_mer.add(edit_distance(norm_ref, norm_hyp), len(norm_ref))

        ref_chars, hyp_chars = chinese_chars(norm_reference), chinese_chars(norm_hypothesis)
        report.cer.add(edit_distance(ref_chars, hyp_chars), len(ref_chars))

        ref_words, hyp_words = english_words(norm_reference), english_words(norm_hypothesis)
        if ref_words:
            report.wer.add(edit_distance(ref_words, hyp_words), len(ref_words))

        rows.append(
            {
                "key": sample.key,
                "reference": sample.reference,
                "hypothesis": hypothesis,
                "private_use": bool(PRIVATE_USE.search(hypothesis)),
                "audio_s": round(duration, 3),
                "inference_s": round(elapsed, 3),
            }
        )
        if index % 20 == 0:
            print(f"  {index}/{len(samples)} …")

    summary = {
        "dataset": DATASET_ID,
        "dataset_revision": DATASET_REVISION,
        "model": TEA_ASR_1_1_MLX_4BIT.repo_id,
        "model_revision": TEA_ASR_1_1_MLX_4BIT.revision,
        "samples": len(samples),
        "raw_mer": round(report.raw_mer.rate, 4),
        "normalized_mer": round(report.norm_mer.rate, 4),
        "cer": round(report.cer.rate, 4),
        "wer": round(report.wer.rate, 4) if report.wer.length else None,
        "english_reference_tokens": report.wer.length,
        "private_use_rate": round(leaks / len(samples), 4) if samples else 0.0,
        "rtf_p50": round(statistics.median(rtfs), 4) if rtfs else None,
        "rtf_p95": round(sorted(rtfs)[int(len(rtfs) * 0.95)], 4) if len(rtfs) > 1 else None,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps({"summary": summary, "samples": rows}, ensure_ascii=False, indent=2) + "\n"
    )
    print(f"明細寫入 {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
