# Singing detector evaluation

**Status:** experimental; the detector misses the requested recall target and stays disabled by default. The implementation emits labels only after `singing_detection_enabled` or `TEA_ASR_SINGING_DETECTION=1` is set. ASR continues to transcribe every segment.

## Method

`benchmarks/singing_eval.py` reads the labelled 16 kHz WAVs and answer item spans directly from the private `.soak` directory. It writes no audio, transcript text, or derived private samples into the repository. Windows are 1.5 s with 0.5 s hop, labelled by item-span midpoint; segment evaluation uses non-overlapping 2 s windows. The full 29 min `church-30m-59m.wav` is labelled speech. The eight NumPy features feed an L2 logistic model fitted with per-file class-balanced weights. Cross-validation leaves out each WAV in turn; coefficients checked into `src/tea_asr/singing.py` are the fit on all labelled files.

Reproduce with the repository environment:

```bash
PYTHONPATH=src python benchmarks/singing_eval.py
```

Or pass `--data-root` / `TEA_ASR_SINGING_DATA` if the read-only private data is elsewhere.

## Operating point

The target was speech false-positive rate below 1% on both speech files and singing recall at least 80%. The LOFO trade-off did not contain such an operating point. Threshold `0.995` is selected to prioritize avoiding false singing labels; it produced no positive calls in held-out windows. Precision is therefore undefined because there were no predicted singing windows, and singing recall is 0%. The detector is not suitable for automatic caption hiding yet.

| Threshold | Singing precision | Singing recall | Worst speech-file FPR |
|---:|---:|---:|---:|
| 0.500 | 0.254 | 60.1% | 39.033% |
| 0.750 | 0.191 | 18.7% | 15.303% |
| 0.850 | 0.047 | 2.5% | 9.897% |
| 0.900 | 0.010 | 0.3% | 5.749% |
| 0.950 | 0.000 | 0.0% | 1.630% |
| 0.980 | 0.000 | 0.0% | 0.257% |
| 0.990 | 0.000 | 0.0% | 0.029% |
| **0.995 (chosen)** | — (no positive calls) | **0.0%** | **0.000%** |

## LOFO per-file confusion at the chosen threshold

Confusion matrix order is `(TP, FP, FN, TN)`. “—” precision means the detector predicted no singing windows.

### 1.5 s windows, 0.5 s hop

| Held-out file | Matrix (TP, FP, FN, TN) | Singing precision | Singing recall | Speech FPR |
|---|---:|---:|---:|---:|
| `music-0123` singing | (0, 0, 329, 0) | — | 0.0% | — |
| `music-0934` singing | (0, 0, 61, 0) | — | 0.0% | — |
| `music-1339` singing | (0, 0, 300, 0) | — | 0.0% | — |
| `music-8900` speech over music | (0, 0, 0, 269) | — | — | 0.0% |
| `church-30m-59m` speech | (0, 0, 0, 3496) | — | — | 0.0% |

### Non-overlapping 2 s segments

| Held-out file | Matrix (TP, FP, FN, TN) |
|---|---:|
| `music-0123` singing | (0, 0, 84, 0) |
| `music-0934` singing | (0, 0, 17, 0) |
| `music-1339` singing | (0, 0, 75, 0) |
| `music-8900` speech over music | (0, 0, 0, 68) |
| `church-30m-59m` speech | (0, 0, 0, 874) |

At threshold 0.995 the full 29 min church speech file produced **0 contiguous false singing calls** (0 positive overlapping windows). At threshold 0.900, LOFO windows yielded 201 false positives in the church file (5.749% FPR) and only 2 true positives across the three singing files (0.3% recall); relaxing the threshold is not a viable trade.

## Decision latency

Offline streaming simulation feeds 200 ms chunks and decides `speech` by the 1.4 s deadline when singing evidence remains below threshold. All measured per-file median and p95 first-decision latency were 1.40 s:

| File | Clips | Median | p95 |
|---|---:|---:|---:|
| `music-0123` | 19 | 1.40 s | 1.40 s |
| `music-0934` | 14 | 1.40 s | 1.40 s |
| `music-1339` | 26 | 1.40 s | 1.40 s |
| `music-8900` | 21 | 1.40 s | 1.40 s |
| `church-30m-59m` | 350 five-second clips | 1.40 s | 1.40 s |

The classifier's first label can later change once after 2.5 s evidence. Latency measures the first label, not a revision.
