# Real-audio soak run 2026-09-28 (church livestream, 30:00–59:09, three speakers)

Server main 308713f + plugin master 19c3f09 replay. Audio and transcripts stay local in `.soak/`; this file is metrics only.

Duplicate counts below were reclassified on 2026-09-28 by matching replay lines to server partial/final text. Columns are plugin-origin / model-origin / speech-origin; only plugin-origin is a hard failure.

## end_silence_ms = 300

Audio duration: 1749.0s; source offset: 1800.0s.

Times in the distribution columns are seconds (median / p95 / max). Worker busy is a fraction.

| Scope | Segments q/f/s/e | Duration s | Speech to first partial s | Stable gap s | Final latency p95 s | Longest speech without text s | Partial rewrites | Stable diverged/abandoned | Worker busy | Warnings | Fade outs | Row losses | Tail retract/rewrite | Layout moves | Burst | Plugin dup | Model dup | Speech dup | Status |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| overall | 444/444/0/0 | 2.70/9.80/14.00 | 0.08/0.26/1.37 | 0.41/1.96/11.69 | 0.44 | 3.40 | 2228/3190 (70%) | 115/0 | — | 0 | 1 | 40 | 0/755 | 0 | 43 | 1 | 1 | 7 | FAIL |
| young_woman | 12/12/0/0 | 11.10/14.00/14.00 | 0.42/0.84/0.84 | 0.44/2.92/11.69 | 0.71 | 1.15 | 162/238 (68%) | 6/0 | — | 0 | 0 | 11 | 0/35 | 0 | 15 | 0 | 0 | 1 | PASS |
| elderly_woman | 11/11/0/0 | 4.00/9.30/9.30 | 0.09/0.66/0.66 | 0.41/2.06/6.87 | 0.48 | 0.55 | 82/113 (73%) | 3/0 | — | 0 | 0 | 1 | 0/28 | 0 | 9 | 0 | 0 | 0 | PASS |
| man | 421/421/0/0 | 2.60/9.40/14.00 | 0.08/0.17/1.37 | 0.41/1.93/11.08 | 0.38 | 3.40 | 1984/2839 (70%) | 106/0 | — | 0 | 1 | 28 | 0/692 | 0 | 43 | 1 | 1 | 6 | FAIL |

## Error codes and stream warnings

| Scope | Errors by code | Stream warnings by message |
|---|---|---|
| overall | `{}` | `{}` |
| young_woman | `{}` | `{}` |
| elderly_woman | `{}` | `{}` |
| man | `{}` | `{}` |

## Hard thresholds

| Scope | invalid_ipc=0 | layout moves=0 | mid-speech fades=0 | plugin duplication lines=0 | no-text stretch ≤10s | Status |
|---|---|---|---|---|---|---|
| overall | PASS | PASS | FAIL | FAIL | PASS | FAIL |
| young_woman | PASS | PASS | PASS | PASS | PASS | PASS |
| elderly_woman | PASS | PASS | PASS | PASS | PASS | PASS |
| man | PASS | PASS | FAIL | FAIL | PASS | FAIL |

## Soft thresholds

| Metric | Observed p95 | Threshold | Status |
|---|---:|---:|---|
| Stable commit gap | 1.96 | 3.00s | PASS |
| Final latency after close | 0.44 | 1.50s | PASS |

## Soft duplication classifications

| Scope | Model-origin lines | Speech-origin lines |
|---|---:|---:|
| overall | 1 | 7 |
| young_woman | 0 | 1 |
| elderly_woman | 0 | 0 |
| man | 1 | 6 |

Hard failures: overall: mid_speech_fade_outs=1; overall: duplication_lines=1; man: mid_speech_fade_outs=1; man: duplication_lines=1

## end_silence_ms = 600

Audio duration: 1749.0s; source offset: 1800.0s.

Times in the distribution columns are seconds (median / p95 / max). Worker busy is a fraction.

| Scope | Segments q/f/s/e | Duration s | Speech to first partial s | Stable gap s | Final latency p95 s | Longest speech without text s | Partial rewrites | Stable diverged/abandoned | Worker busy | Warnings | Fade outs | Row losses | Tail retract/rewrite | Layout moves | Burst | Plugin dup | Model dup | Speech dup | Status |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| overall | 288/288/0/0 | 4.30/13.00/14.00 | 0.08/0.61/4.36 | 0.42/2.74/12.87 | 0.57 | 3.16 | 2379/3377 (70%) | 107/0 | — | 0 | 5 | 54 | 0/764 | 0 | 43 | 1 | 3 | 7 | FAIL |
| young_woman | 11/11/0/0 | 13.00/14.00/14.00 | 0.42/0.89/0.89 | 0.44/2.85/8.03 | 0.80 | 1.25 | 171/250 (68%) | 4/0 | — | 0 | 0 | 18 | 0/30 | 0 | 15 | 0 | 0 | 1 | PASS |
| elderly_woman | 9/9/0/0 | 4.00/12.70/12.70 | 0.09/0.79/0.79 | 0.42/2.74/10.32 | 0.52 | 1.09 | 84/111 (76%) | 2/0 | — | 0 | 0 | 0 | 0/26 | 0 | 17 | 0 | 0 | 1 | PASS |
| man | 268/268/0/0 | 4.10/12.50/14.00 | 0.08/0.57/4.36 | 0.42/2.71/12.87 | 0.57 | 3.16 | 2124/3016 (70%) | 101/0 | — | 0 | 5 | 36 | 0/708 | 0 | 43 | 1 | 3 | 5 | FAIL |

## Error codes and stream warnings

| Scope | Errors by code | Stream warnings by message |
|---|---|---|
| overall | `{}` | `{}` |
| young_woman | `{}` | `{}` |
| elderly_woman | `{}` | `{}` |
| man | `{}` | `{}` |

## Hard thresholds

| Scope | invalid_ipc=0 | layout moves=0 | mid-speech fades=0 | plugin duplication lines=0 | no-text stretch ≤10s | Status |
|---|---|---|---|---|---|---|
| overall | PASS | PASS | FAIL | FAIL | PASS | FAIL |
| young_woman | PASS | PASS | PASS | PASS | PASS | PASS |
| elderly_woman | PASS | PASS | PASS | PASS | PASS | PASS |
| man | PASS | PASS | FAIL | FAIL | PASS | FAIL |

## Soft thresholds

| Metric | Observed p95 | Threshold | Status |
|---|---:|---:|---|
| Stable commit gap | 2.74 | 3.00s | PASS |
| Final latency after close | 0.57 | 1.50s | PASS |

## Soft duplication classifications

| Scope | Model-origin lines | Speech-origin lines |
|---|---:|---:|
| overall | 3 | 7 |
| young_woman | 0 | 1 |
| elderly_woman | 0 | 1 |
| man | 3 | 5 |

Hard failures: overall: mid_speech_fade_outs=5; overall: duplication_lines=1; man: mid_speech_fade_outs=5; man: duplication_lines=1
