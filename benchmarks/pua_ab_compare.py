"""PUA A/B 第三步：比較 MLX 4bit 與上游 BF16 兩輪的結果。

輸入是 pua_ab_mlx_pass.py 與 pua_ab_bf16_pass.py 各自產生的 JSON。
這支腳本只做純文字統計，不需要 mlx 或 torch，用專案 .venv 跑即可：

    uv run python benchmarks/pua_ab_compare.py \
        --mlx benchmarks/results/pua_ab_mlx.json \
        --bf16 benchmarks/results/pua_ab_bf16.json
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path


def load(path: Path) -> dict:
    return json.loads(path.read_text())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mlx", type=Path, default=Path("benchmarks/results/pua_ab_mlx.json"))
    parser.add_argument("--bf16", type=Path, default=Path("benchmarks/results/pua_ab_bf16.json"))
    args = parser.parse_args()

    mlx = load(args.mlx)
    bf16 = load(args.bf16)

    mlx_rows = {r["key"]: r for r in mlx["rows"]}
    bf16_rows = {r["key"]: r for r in bf16["rows"]}
    common_keys = sorted(set(mlx_rows) & set(bf16_rows))

    print("== 摘要 ==")
    for name, summary in (("MLX 4bit", mlx["summary"]), ("BF16 上游", bf16["summary"])):
        n = summary["samples"]
        with_pua = summary["sentences_with_pua"]
        occ = summary["pua_occurrences"]
        pct = (with_pua / n * 100) if n else 0.0
        print(f"{name}: {with_pua}/{n} 句含PUA ({pct:.1f}%), 總出現次數={occ}")

    print("\n== 逐句比較（共同樣本）==")
    mlx_cp: Counter[str] = Counter()
    bf16_cp: Counter[str] = Counter()
    diff_text_count = 0
    for key in common_keys:
        m, b = mlx_rows[key], bf16_rows[key]
        mlx_cp.update(m["pua_codepoints"])
        bf16_cp.update(b["pua_codepoints"])
        if m["hypothesis"] != b["hypothesis"]:
            diff_text_count += 1
        print(f"- {key}")
        print(f"  REF : {m['reference']}")
        print(f"  MLX : {m['hypothesis']}  (pua={m['pua_count']} {m['pua_codepoints']})")
        print(f"  BF16: {b['hypothesis']}  (pua={b['pua_count']} {b['pua_codepoints']})")

    print(f"\n共同樣本數: {len(common_keys)}；文字不同的句數: {diff_text_count}")
    print(f"MLX 碼位分布: {dict(mlx_cp)}")
    print(f"BF16 碼位分布: {dict(bf16_cp)}")

    overlap = set(mlx_cp) & set(bf16_cp)
    print(f"兩輪共同出現的PUA碼位: {sorted(overlap)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
