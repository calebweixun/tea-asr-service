"""Real Confucius4-T3PO MLX 4bit through the production translation worker.

    TEA_ASR_TRANSLATION_MODEL_PATH=/Volumes/DigiFusion/tea-asr-models/t3po-mlx-4bit \\
        uv run pytest -m hardware tests/hardware/test_real_translation.py
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from tea_asr.translation.supervisor import TranslationSupervisor

pytestmark = pytest.mark.hardware

MODEL = os.environ.get("TEA_ASR_TRANSLATION_MODEL_PATH", "")


@pytest.mark.skipif(not MODEL or not Path(MODEL).is_dir(), reason="translation model not present")
def test_real_model_translates_zh_to_en_and_reuses_the_kv_prefix() -> None:
    async def scenario() -> list[dict]:
        translation = TranslationSupervisor(Path(MODEL), max_memory_gib=12)
        await translation.start()
        await translation.start_session("zh2en", "native")
        results = [
            await translation.translate(text, force=True)
            for text in ("遲遲未定的原因。", "然後利用深度學習演算法預測。")
        ]
        await translation.close()
        return results

    first, second = asyncio.run(scenario())
    assert first["action"] == second["action"] == "TRANS"
    assert first["text"].isascii() and first["text"].strip()
    assert second["reused_tokens"] > 0.8 * second["prompt_tokens"]
    assert second["peak_memory_bytes"] < 12 * 2**30
