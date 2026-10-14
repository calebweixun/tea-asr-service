# Hotword shallow fusion (offline, 2026-10)

Metrics only: no transcript, caption, audio text, or target term is included. Commands are in [docs/09](../09-testing-guide.md#hotword-shallow-fusion-logit-bias-offline-experiment-2026-10).

## Verdict

**NO-GO** for live integration.

| criterion | result | met |
|---|---|---|
| FB − B pooled CER CI upper < 0 | +0.003 [-0.003, +0.009] pp | no |
| false changes ≤ 1 / audio hour | 208 / 4.81 h = 43.23/h | no |
| p95 added decode time ≤ 50 ms / final | 67.5 ms (p50 15.0 ms) | no |

## Cross-validation choices

| held-out service | β | names | training errors | false changes | objective |
|---|---:|---|---:|---:|---:|
| 20260704 | 0 | off | 2535 | 142 | 3103 |
| 20260822 | 0 | off | 2956 | 184 | 3692 |
| 20260912 | 0 | off | 2695 | 187 | 3443 |

## Pronoun-folded CER (%, 5-minute-cluster bootstrap 95% CI)

| system | 20260704 | 20260822 | 20260912 | pooled |
|---|---:|---:|---:|---:|
| A current-code β=0 decode | 6.11 [4.78, 8.06] | 5.35 [4.57, 6.39] | 6.41 [5.60, 7.31] | 5.97 [5.31, 6.78] |
| B = A + fold dictionary replacements | 5.86 [4.56, 7.74] | 5.06 [4.28, 6.09] | 6.06 [5.21, 7.03] | 5.67 [5.02, 6.45] |
| F(β) decode-time fusion | 6.11 [4.78, 8.06] | 5.35 [4.57, 6.39] | 6.41 [5.60, 7.31] | 5.97 [5.31, 6.78] |
| FB(β) fusion + dictionary replacements | 5.87 [4.56, 7.76] | 5.06 [4.28, 6.09] | 6.06 [5.21, 7.03] | 5.67 [5.02, 6.46] |

## Paired FB − B CER difference (percentage points; negative favours FB)

| 20260704 | 20260822 | 20260912 | pooled |
|---|---:|---:|---:|---:|
| FB − B | +0.004 [-0.010, +0.021] | +0.000 [+0.000, +0.000] | +0.004 [+0.000, +0.013] | +0.003 [-0.003, +0.009] |

## False changes and decode time

| held-out service | false changes | added decode p50 (ms) | added decode p95 (ms) |
|---|---:|---:|---:|
| 20260704 | 55 | 5.6 | 37.8 |
| 20260822 | 59 | 19.8 | 76.4 |
| 20260912 | 94 | 18.7 | 74.9 |

## Method

Targets are the held-out fold dictionary `to` values plus the 13 read-only live hotwords; optional title names are mined only from the other two services. Terms are Han-only and at least two characters. Tokenization uses the local 8-bit model tokenizer, with standalone and leading-space variants.

The processor boosts valid trie continuations by β. If a partial match is abandoned, the breaking transition pays back the accumulated partial reward; completed terms keep their reward. `Model.generate()` drops unknown kwargs in mlx-audio 0.4.5, so this offline benchmark calls Qwen3-ASR `_generate_single_chunk()` with the existing `stream_generate()` processor hook. No live server code changed.

A is the current-code β=0 full decode; B applies the held-out fold dictionary replacements to A. If the β=0 output is missing, the benchmark warns and falls back to `eval/m8-c3`. The β=0 diagnostic reports how many outputs differ from m8-c3 and prints only their item ids. U+FFFD is removed from every decoded hypothesis. Audio uses the pad25 answer spans, 3-second carry with the matrix's 1.5-second gap limit, shared overlap stripping, PUA filtering and repetition trim. CER follows `church_eval_report.py` with pronoun folding and paired 5-minute-cluster bootstrap (5000 draws, seed 20261003). False changes are counted per item; audio hours are the sum of scored answer spans. Added time is paired per-final `decode_s` versus β=0.
