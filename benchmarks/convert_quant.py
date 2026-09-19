"""PUA A/B 第二輪：自己從上游 BF16 轉換出 8bit / 4bit MLX 量化模型。

目的：既有的 Alkd/TEA-ASR-1.1-MLX-4bit 這一版轉換，會把 mlx-audio 預設排除在
量化範圍外的 audio_tower（見 Qwen3ASRModel.model_quant_predicate：
`not p.startswith("audio_tower")`）也強制量化進去（見
src/tea_asr/backend.py 的 _mixed_quantization_loader monkey patch 註解）。

這支腳本用 mlx-audio 官方轉換工具（mlx_audio.convert.convert），對*本機已經
下載好*的上游 BF16 checkpoint（models/models--JacobLinCool--TEA-ASR-1.1/...），
以「標準流程」（沿用 model_quant_predicate 預設值，audio_tower 不量化）分別
轉出 8bit 與 4bit 兩份模型，藉此把「位元寬」與「Alkd 那版轉換選擇多量化
audio_tower」兩個變數分開。

不下載任何新東西、不動 ~/Library/Caches，輸出都在專案 models/ 底下
（已被 gitignore）。

用法（專案 .venv，需要 mlx_lm，已隨 mlx-audio 安裝）：

    uv run python benchmarks/convert_quant.py --bits 8 \
        --out models/mlx-8bit-selfconv
    uv run python benchmarks/convert_quant.py --bits 4 \
        --out models/mlx-4bit-selfconv
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tea_asr.model_manager import locate_prepared_model
from tea_asr.model_spec import ModelSpec

UPSTREAM_BF16 = ModelSpec(
    repo_id="JacobLinCool/TEA-ASR-1.1",
    revision="bda08df76d4fd6b487b4a1dd7f0bddf8541696f8",
    mlx_audio_version="0.4.5",
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bits", type=int, choices=[4, 8], required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--group-size",
        type=int,
        default=None,
        help="量化 group size，省略則用 mlx_lm 該模式的預設值",
    )
    args = parser.parse_args()

    src_path = locate_prepared_model(UPSTREAM_BF16)
    print(f"[INFO] 來源（本機已下載，不重新下載）: {src_path}")

    from mlx_audio.convert import convert

    args.out.mkdir(parents=True, exist_ok=True)
    convert(
        hf_path=str(src_path),
        mlx_path=str(args.out),
        quantize=True,
        q_bits=args.bits,
        q_group_size=args.group_size,
        q_mode="affine",
        model_domain="stt",
        # quant_predicate=None -> 用模型類別自己的 model_quant_predicate
        # 預設值（Qwen3ASRModel: 排除 audio_tower），也就是「標準流程」，
        # 跟 Alkd 那版「強制連 audio_tower 都量化」的做法不同。
    )
    print(f"[INFO] 完成，輸出於 {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
