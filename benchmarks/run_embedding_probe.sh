#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
W="${W:-$ROOT/.soak/church-eval/work}"
MODEL_DIR="${MODEL_DIR:-$ROOT/models/embeddinggemma-2}"
PY="${PY:-.venv-emb/bin/python}"
OUTPUT_DIR="$W/embedding-probe"
DICTIONARY_DIR="$HOME/Library/Application Support/TEA ASR/dictionaries"

mkdir -p "$OUTPUT_DIR"
cd "$ROOT"
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1
export PYTHONPATH="$ROOT/src:$ROOT${PYTHONPATH:+:$PYTHONPATH}"
exec > >(tee -a "$OUTPUT_DIR/run.log") 2>&1

printf 'EmbeddingGemma 2 probe started: %s\n' "$(date -u '+%Y-%m-%d %H:%M:%S UTC')"
for service in 20260704 20260822 20260912; do
  "$PY" benchmarks/embedding_asr_probe.py embed \
    --service "$service" --device mps --data-root "$W" \
    --model-dir "$MODEL_DIR" --dictionary-dir "$DICTIONARY_DIR" \
    --output-dir "$OUTPUT_DIR"
  "$PY" benchmarks/embedding_asr_probe.py evaluate \
    --service "$service" --device mps --data-root "$W" \
    --model-dir "$MODEL_DIR" --dictionary-dir "$DICTIONARY_DIR" \
    --output-dir "$OUTPUT_DIR"
done

"$PY" benchmarks/embedding_asr_probe.py report \
  --services 20260704 20260822 20260912 --output-dir "$OUTPUT_DIR"
printf 'EmbeddingGemma 2 probe finished: %s\n' "$(date -u '+%Y-%m-%d %H:%M:%S UTC')"
