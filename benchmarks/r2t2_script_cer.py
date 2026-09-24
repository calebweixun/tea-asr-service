"""R2T2 評估的補充指標：把「簡體字混入」與「辨識錯誤」拆開看。

docs/05 的 CER 規則刻意**不做**繁簡轉換（見 quality_eval.normalize），因為那會把
真正的字形差異藏起來；這支腳本不改那條規則，只在同一份逐筆明細上**另外**算：

- `cer_raw`：與 quality_eval 相同的規則（NFKC、去標點空白、只算 CJK 字）。
- `cer_s2tw`：參考與假設都先過 OpenCC `s2tw`（簡→台灣正體，逐字、不換詞）後再算。
  兩個模型套同一個轉換，差值只反映「字形」以外的錯誤。
- `samples_with_simplified`：假設文字過 `s2tw` 後會改變的句數，也就是含簡體字的句數。
- 兩模型逐句配對的 bootstrap 95% 信賴區間（CER 差值，百分點）。

OpenCC 不是專案依賴，也不打算變成依賴；這裡用任何裝了
`opencc-python-reimplemented==0.1.7` 的 Python 執行即可（本次用 homebrew
python3.11 既有的安裝，未另外下載）：

    python3.11 benchmarks/r2t2_script_cer.py \\
        benchmarks/results/r2t2_gate1_tea_full.json \\
        benchmarks/results/r2t2_gate1_r2t2_full.json \\
        benchmarks/results/r2t2_gate1_r2t2_ctx_full.json
"""

from __future__ import annotations

import json
import random
import sys
import unicodedata
from pathlib import Path

from opencc import OpenCC

BOOTSTRAP_ROUNDS = 5000


def normalize(text: str) -> str:
    # 與 benchmarks/quality_eval.py::normalize 同一條規則。
    text = unicodedata.normalize("NFKC", text).lower()
    return "".join(
        char for char in text if not unicodedata.category(char).startswith(("P", "Z", "C"))
    )


def chinese_chars(text: str) -> list[str]:
    return [char for char in text if "一" <= char <= "鿿"]


def edit_distance(reference: list[str], hypothesis: list[str]) -> int:
    previous = list(range(len(hypothesis) + 1))
    for i, ref in enumerate(reference, start=1):
        current = [i]
        for j, hyp in enumerate(hypothesis, start=1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ref != hyp)))
        previous = current
    return previous[-1]


def per_sample(rows: list[dict], convert) -> list[tuple[int, int]]:
    out = []
    for row in rows:
        reference = chinese_chars(normalize(convert(row["reference"])))
        hypothesis = chinese_chars(normalize(convert(row["hypothesis"])))
        out.append((edit_distance(reference, hypothesis), len(reference)))
    return out


def bootstrap(base: list[tuple[int, int]], other: list[tuple[int, int]]) -> tuple[float, float]:
    rng = random.Random(0)
    indices = range(len(base))
    diffs = []
    for _ in range(BOOTSTRAP_ROUNDS):
        pick = [rng.choice(indices) for _ in indices]
        chars = sum(base[i][1] for i in pick)
        diffs.append((sum(other[i][0] for i in pick) - sum(base[i][0] for i in pick)) / chars)
    diffs.sort()
    return (
        round(diffs[int(BOOTSTRAP_ROUNDS * 0.025)] * 100, 2),
        round(diffs[int(BOOTSTRAP_ROUNDS * 0.975)] * 100, 2),
    )


def main() -> int:
    paths = [Path(arg) for arg in sys.argv[1:]]
    if len(paths) < 2:
        raise SystemExit("用法：r2t2_script_cer.py BASELINE.json OTHER.json [OTHER.json ...]")
    s2tw = OpenCC("s2tw").convert
    runs = [json.loads(path.read_text()) for path in paths]
    keys = [row["key"] for row in runs[0]["samples"]]
    for run in runs[1:]:
        if [row["key"] for row in run["samples"]] != keys:
            raise SystemExit("各檔的樣本順序不同，不能配對比較")

    base_raw = per_sample(runs[0]["samples"], lambda text: text)
    base_s2tw = per_sample(runs[0]["samples"], s2tw)
    for path, run in zip(paths, runs, strict=True):
        raw = per_sample(run["samples"], lambda text: text)
        converted = per_sample(run["samples"], s2tw)
        chars = sum(n for _, n in raw)
        summary = {
            "file": str(path),
            "model": run["summary"]["model"],
            "system_prompt": run["summary"].get("system_prompt"),
            "samples": len(raw),
            "reference_chars": chars,
            "cer_raw": round(sum(e for e, _ in raw) / chars, 4),
            "cer_s2tw": round(sum(e for e, _ in converted) / chars, 4),
            "samples_with_simplified": sum(
                s2tw(row["hypothesis"]) != row["hypothesis"] for row in run["samples"]
            ),
        }
        if run is not runs[0]:
            summary["raw_diff_vs_baseline_pp_95ci"] = bootstrap(base_raw, raw)
            summary["s2tw_diff_vs_baseline_pp_95ci"] = bootstrap(base_s2tw, converted)
        print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
