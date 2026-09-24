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

R2T2 評估（docs/benchmarks/r2t2-eval-report.md）另外加兩個選項：

- `--source r2t2`：來源換成本機已下載的 netease-youdao/Confucius4-R2T2 BF16。
- `--recipe audio8`：重現生產用 Alkd/TEA-ASR-1.1-MLX-4bit 的配方——文字解碼器
  4bit/g64，audio_tower 的 linear 層 8bit/g64（逐層寫進 config.json 的
  `quantization`），讓 R2T2 與現有模型在「同樣的量化設定」下比較。
- R2T2 的 NetEase Model Use License 把量化列為衍生作品（1.5(iii)），必須隨附原
  授權：`--license-file` 指向官方 MODEL_LICENSE，會被複製進輸出資料夾。

    uv run python benchmarks/convert_quant.py --source r2t2 --recipe audio8 \
        --bits 4 --out models/r2t2-mlx-4bit-audio8 --license-file MODEL_LICENSE
"""

from __future__ import annotations

import argparse
import shutil
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

R2T2_BF16 = ModelSpec(
    repo_id="netease-youdao/Confucius4-R2T2",
    revision="185ce639118ad1362d049ca0d8ed04b6ec5cd6c9",
    mlx_audio_version="0.4.5",
)

SOURCES = {"tea-asr-1.1": UPSTREAM_BF16, "r2t2": R2T2_BF16}


def _audio8_predicate(model, quant_predicate_name=None):
    """Alkd 配方：audio_tower linear 8bit/g64，其餘照 mlx-audio 的基本條件量化。

    回傳 dict 時 mlx_lm.quantize_model 會把它寫成該層的逐層覆寫，與
    Alkd/TEA-ASR-1.1-MLX-4bit 的 config.json 同一種格式。
    """

    del model, quant_predicate_name

    def predicate(path, module):
        if not (
            hasattr(module, "weight")
            and module.weight.shape[-1] % 64 == 0
            and hasattr(module, "to_quantized")
        ):
            return False
        if path.startswith("audio_tower"):
            return {"bits": 8, "group_size": 64}
        return True

    return predicate


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
    parser.add_argument("--source", choices=sorted(SOURCES), default="tea-asr-1.1")
    parser.add_argument(
        "--recipe",
        choices=["standard", "audio8"],
        default="standard",
        help="standard=audio_tower 不量化（mlx-audio 預設）；audio8=Alkd 配方",
    )
    parser.add_argument(
        "--license-file",
        type=Path,
        default=None,
        help="要隨附在輸出資料夾的原始模型授權（--source r2t2 必填）",
    )
    args = parser.parse_args()
    if args.source == "r2t2" and args.license_file is None:
        parser.error("--source r2t2 需要 --license-file（NetEase Model Use License 1.5(iii)／4.1）")

    src_path = locate_prepared_model(SOURCES[args.source])
    print(f"[INFO] 來源（本機已下載，不重新下載）: {src_path}")

    import mlx_audio.convert as mlx_convert
    from mlx_audio.convert import convert

    if args.recipe == "audio8":
        mlx_convert.build_quant_predicate = _audio8_predicate

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
    if args.license_file is not None:
        shutil.copyfile(args.license_file, args.out / args.license_file.name)
        print(f"[INFO] 已隨附授權 {args.out / args.license_file.name}")
    print(f"[INFO] 完成，輸出於 {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
