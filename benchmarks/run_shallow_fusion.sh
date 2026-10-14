#!/usr/bin/env bash
set -euo pipefail
set -x

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY=${PY:-"$ROOT/.venv/bin/python"}
WORK=${SHALLOW_FUSION_WORK:-"$ROOT/.soak/church-eval/work"}
LIVE="$HOME/Library/Application Support/TEA ASR/dictionaries/church.toml"
MODEL=${SHALLOW_FUSION_MODEL:-"$ROOT/models/mlx-8bit-selfconv"}
LOG="$WORK/shallow-fusion/run.log"

mkdir -p "$WORK/shallow-fusion"
exec >> "$LOG" 2>&1
cd "$ROOT"
export PYTHONPATH="$ROOT/src:$ROOT"
export HF_HUB_OFFLINE=1

"$PY" benchmarks/shallow_fusion.py targets \
  --work "$WORK" --model-path "$MODEL" --live-dictionary "$LIVE"

# Materialize A from current-code beta=0 decodes. Complete item caches resume in place.
for service in 20260704 20260822 20260912; do
  "$PY" benchmarks/shallow_fusion.py decode \
    --work "$WORK" --model-path "$MODEL" \
    --service "$service" --fold "$service" --beta 0 \
    --system F --subset full
done

"$PY" benchmarks/shallow_fusion.py tune \
  --work "$WORK" --model-path "$MODEL" --random-controls 300

RESULTS="$WORK/shallow-fusion/tune/results.json"
for service in 20260704 20260822 20260912; do
  for system in F FB; do
    "$PY" benchmarks/shallow_fusion.py decode \
      --work "$WORK" --model-path "$MODEL" \
      --service "$service" --fold "$service" --tuned "$RESULTS" \
      --system "$system" --subset full
  done
done

"$PY" benchmarks/shallow_fusion.py report \
  --work "$WORK" --results "$RESULTS" \
  --markdown "$ROOT/docs/benchmarks/shallow-fusion-2026-10.md"
