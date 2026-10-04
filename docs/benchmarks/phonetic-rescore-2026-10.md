# Phonetic candidates + ASR-model rescoring (offline, 2026-10)

Metrics only: no transcript, caption or audio text is in this file. Commands: [docs/09](../09-testing-guide.md) (section on phonetic candidates + model rescoring). Code: `benchmarks/phonetic_rescore.py` (benchmark only; the live server never imports it).

## Verdict

**NO-GO as built** for live integration.

| criterion | result | met |
|---|---|---|
| CER improvement CI below 0 (C vs B) | -0.013 [-0.025, -0.003]* pp | yes |
| false changes <= 1 per audio hour (C) | 1 in 4.81 h = 0.21/h | yes |
| p95 added time <= 300 ms per final (C) | 677 ms standalone (296 ms if the audio/prompt prefill were shared with the decode) | no |

## Reading

* The scorer is precise: with the tuned margin, C made 9 replacements in 4.8 h, 7 of them good, 1 false. But the dictionary rules in B already take the easy wins, so on top of B the gain is a handful of characters (see the delta table); that is statistically below zero but far from useful.
* D (terms only, no mishearing rules) recovers only a small part of what the rules give: compare D-A with B-A. Rescoring does not replace the long rule list; it finds some extra cases the rules miss.
* Ceiling: even a perfect picker over the generated candidates could fix only the 'Ceiling' count below, so the phonetic candidate generator, not the scorer, bounds the gain.
* Cost: about one extra encode + prefill + batch per final that has a candidate; the p95 target fails standalone and sits at the limit even if the decode's prefill were shared.

## What was built

Targets: dictionary `to` values (Han, >= 2 chars) plus the 13 live hotwords, plus optional names with titles mined from the training services' references only. Candidates: pinyin (TONE3) syllable edit distance with initial/final confusion costs (zh/z, ch/c, sh/s, n/l, f/h, m/n, j/zh, en/eng, an/ang, in/ing, w/y/zero ...), windows of length |term| +-1 inside one Han run, best 8 per text by tonal cost (toneless cost also stored). Scorer: the 8-bit Qwen3-ASR model itself, teacher-forced log p(text | audio) on the item audio, audio-encoder and prompt prefill once, all candidates in one batch over a broadcast KV cache, `<|im_end|>` included. Decision: replace when score(candidate) - score(original) > margin; several non-overlapping windows may be replaced. Scoring implementation: `TeacherForcedScorer.prefix` `benchmarks/phonetic_rescore.py:487-504` and `.score` `benchmarks/phonetic_rescore.py:512-545`.

Cross-validation: for each held-out service the dictionary is `church.toml` (13 rules) + rules mined from the other two services (`dict-cv/<held>/fold-safe.toml`), hotwords are the live 13, names come from the other two services' references, and the decision parameters (norm, margin, phonetic cost limit, tone use, minimum term length, names on/off) are grid-searched on the other two services (objective: errors + 4 x false changes; feasible only if false changes <= training hours). The live 116-rule dictionary was mined from all three services, so it is not used for the CV numbers (it would leak); it is used for the separate hand-corrected set. Hypotheses are `eval/m8-c3` (8-bit, carry 3 s); scoring uses the item audio span without the carry audio; CER is pronoun-folded; CIs are the 5-minute-cluster bootstrap of `church_eval_report.py`.

Held-out audio: 4.81 h, 3 services pooled.

## CER, held-out services (end-to-end pipeline run, per-fold parameters)

| system | pooled | 20260704 | 20260822 | 20260912 |
|---|---:|---:|---:|---:|
| A baseline 8-bit + carry | 5.97 [5.32, 6.79] | 6.12 [4.80, 8.11] | 5.35 [4.59, 6.40] | 6.41 [5.58, 7.36] |
| B = A + dictionary replacements | 5.67 [5.03, 6.48] | 5.87 [4.58, 7.81] | 5.06 [4.29, 6.09] | 6.06 [5.20, 7.05] |
| C = B + rescoring (dictionary terms) | 5.66 [5.01, 6.47] | 5.86 [4.56, 7.81] | 5.04 [4.28, 6.05] | 6.04 [5.19, 7.02] |
| D = A + rescoring, terms only (no rules) | 5.91 [5.26, 6.73] | 6.06 [4.74, 8.06] | 5.29 [4.54, 6.33] | 6.33 [5.48, 7.28] |

Paired CER difference, percentage points (negative favours the first system; * = CI excludes 0):

| pair | pooled | 20260704 | 20260822 | 20260912 |
|---|---:|---:|---:|---:|
| C-B | -0.013 [-0.025, -0.003]* | -0.007 [-0.026, 0.007] | -0.017 [-0.040, 0.000] | -0.017 [-0.041, 0.000] |
| D-B | 0.233 [0.168, 0.300]* | 0.195 [0.118, 0.283]* | 0.239 [0.165, 0.324]* | 0.272 [0.108, 0.442]* |
| D-A | -0.062 [-0.086, -0.041]* | -0.053 [-0.098, -0.016]* | -0.055 [-0.082, -0.030]* | -0.079 [-0.130, -0.042]* |
| B-A | -0.295 [-0.363, -0.232]* | -0.248 [-0.344, -0.161]* | -0.293 [-0.380, -0.220]* | -0.351 [-0.519, -0.196]* |

Same comparison computed from the stage-1 cache (batch scores of all candidates together; shows the batch-composition noise is harmless):

| system | pooled | 20260704 | 20260822 | 20260912 |
|---|---:|---:|---:|---:|
| A baseline 8-bit + carry | 5.97 [5.32, 6.79] | 6.12 [4.80, 8.11] | 5.35 [4.59, 6.40] | 6.41 [5.58, 7.36] |
| B = A + dictionary replacements | 5.67 [5.03, 6.48] | 5.87 [4.58, 7.81] | 5.06 [4.29, 6.09] | 6.06 [5.20, 7.05] |
| C = B + rescoring (dictionary terms) | 5.66 [5.01, 6.47] | 5.86 [4.56, 7.81] | 5.04 [4.28, 6.05] | 6.04 [5.19, 7.02] |
| D = A + rescoring, terms only (no rules) | 5.91 [5.26, 6.73] | 6.06 [4.74, 8.06] | 5.29 [4.54, 6.32] | 6.34 [5.50, 7.29] |

Paired CER difference, percentage points (negative favours the first system; * = CI excludes 0):

| pair | pooled | 20260704 | 20260822 | 20260912 |
|---|---:|---:|---:|---:|
| C-B | -0.013 [-0.025, -0.003]* | -0.007 [-0.026, 0.007] | -0.017 [-0.040, 0.000] | -0.017 [-0.041, 0.000] |
| D-B | 0.234 [0.170, 0.301]* | 0.195 [0.118, 0.283]* | 0.235 [0.157, 0.323]* | 0.280 [0.121, 0.446]* |
| D-A | -0.061 [-0.082, -0.041]* | -0.053 [-0.098, -0.016]* | -0.059 [-0.088, -0.031]* | -0.071 [-0.106, -0.041]* |
| B-A | -0.295 [-0.363, -0.232]* | -0.248 [-0.344, -0.161]* | -0.293 [-0.380, -0.220]* | -0.351 [-0.519, -0.196]* |

## Changes made, target-term recall, candidates

| system | replacements | good | neutral | worse | false (correct -> wrong) | false / hour |
|---|---:|---:|---:|---:|---:|---:|
| C | 9 | 7 | 1 | 0 | 1 | 0.21 |
| D | 44 | 41 | 1 | 0 | 2 | 0.42 |

| system | recall, terms >= 3 chars | recall, all terms >= 2 chars |
|---|---:|---:|
| A | 53/163 = 32.52% | 1097/1611 = 68.09% |
| B | 104/163 = 63.80% | 1383/1611 = 85.85% |
| C | 108/163 = 66.26% | 1394/1611 = 86.53% |
| D | 63/163 = 38.65% | 1148/1611 = 71.26% |

| system | candidates generated / segment | passing phonetic filter / segment | accepted / segment | segments with >= 1 candidate |
|---|---:|---:|---:|---:|
| C | 4.29 | 0.92 | 0.0043 | 86.14% |
| D | 4.37 | 0.79 | 0.0210 | 86.62% |

Ceiling: if the scorer always picked the best generated candidate, errors fixable in C 103 of 4307; in D 281 of 4531 (one candidate per item).

## Added time per final (8-bit model, this Mac, nothing else decoding)

| system | finals | scored (>= 1 candidate) | candidates / final | median ms (all) | p95 ms (all) | p99 ms (all) | median ms (scored) | p95 ms (scored) | p95 ms (all) if prefill shared | added s per audio hour |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| C | 2093 | 1048 | 0.92 | 136 | 677 | 1089 | 388 | 816 | 296 | 94 |
| D | 2093 | 910 | 0.79 | 9 | 476 | 655 | 348 | 559 | 202 | 67 |

Standalone = candidate generation + audio encode + prompt prefill + batched candidate scoring. 'Prefill shared' drops the encode+prefill, assuming a live integration reuses the decode's audio embeddings and prompt cache (not implemented or measured).

## Normalisation, margin and parameters

Cross-validated pooled result per normalisation of the score difference (sum = log-likelihood ratio; tok = per token; char = per character):

| norm | C-B pp [CI] | D-A pp [CI] | C replacements (false) | D replacements (false) |
|---|---:|---:|---:|---:|
| sum | -0.013 [-0.023, -0.003]* | -0.061 [-0.082, -0.041]* | 9 (1) | 44 (2) |
| tok | -0.007 [-0.019, 0.007] | -0.066 [-0.092, -0.044]* | 10 (3) | 47 (2) |
| char | -0.013 [-0.023, -0.003]* | -0.067 [-0.091, -0.045]* | 9 (1) | 48 (2) |

Margin sweep with the final other parameters, pooled over the three services (in-sample, shows the trade-off only):

| system | margin | replacements | good | false | net error change |
|---|---:|---:|---:|---:|---:|
| C | 0.0 | 12 | 10 | 1 | -15 |
| C | 0.5 | 10 | 9 | 0 | -14 |
| C | 1.0 | 9 | 8 | 0 | -12 |
| C | 2.0 | 6 | 5 | 0 | -8 |
| C | 3.0 | 3 | 2 | 0 | -3 |
| C | 4.0 | 3 | 2 | 0 | -3 |
| C | 6.0 | 1 | 1 | 0 | -2 |
| D | 0.0 | 52 | 49 | 2 | -58 |
| D | 0.5 | 43 | 41 | 1 | -50 |
| D | 1.0 | 36 | 34 | 1 | -41 |
| D | 2.0 | 23 | 22 | 0 | -27 |
| D | 3.0 | 13 | 12 | 0 | -13 |
| D | 4.0 | 12 | 11 | 0 | -12 |
| D | 6.0 | 5 | 5 | 0 | -6 |

Tuned parameters (norm, margin, phonetic cost limit, toneless, min term length, names):

| fold | C | D |
|---|---|---|
| held out 20260704 | sum, 0.0, 0.25, False, 2, False | sum, 0.0, 0.25, False, 2, False |
| held out 20260822 | sum, 0.5, 0.25, False, 2, True | sum, 0.0, 0.25, False, 2, True |
| held out 20260912 | sum, 0.5, 0.25, False, 2, True | sum, 0.5, 0.15, False, 2, False |
| final (all three) | sum, 0.5, 0.25, False, 2, True | sum, 0.0, 0.25, False, 2, True |

## Hand-corrected set (speakers-answers.json, church-30m-59m.wav)

5.6 audio minutes of corrected items; live 116-rule dictionary; parameters = final (tuned on the three services). Independence of this audio from the three services was not checked, and 40 items are too few for a significance claim.

| system | gold |
|---|---:|
| A baseline 8-bit + carry | 10.88 [5.44, 11.94] |
| B = A + dictionary replacements | 9.51 [4.18, 10.55] |
| C = B + rescoring (dictionary terms) | 9.51 [4.18, 10.55] |
| D = A + rescoring, terms only (no rules) | 10.74 [5.44, 11.77] |

Paired CER difference, percentage points (negative favours the first system; * = CI excludes 0):

| pair | gold |
|---|---:|
| C-B | 0.000 [0.000, 0.000] |
| D-B | 1.231 [1.226, 1.255]* |
| D-A | -0.137 [-0.164, 0.000] |
| B-A | -1.368 [-1.390, -1.255]* |

Replacements: C 0 (false 0), D 2 (false 0).
