# Church service evaluation (2026-10-03/04): parameters measured on three full services

Metrics only: no transcript text, audio or caption text is in this file. Method and exact commands:
[docs/09](../09-testing-guide.md#整場主日-srt-評估church-eval2026-10). Private inputs live in the
git-excluded `.soak/church-eval/` (Sunday recordings `20260704`, `20260822`, `20260912`, ~2 h each, with
the user's human-corrected SRT).

## Summary of decisions

| Parameter | Decision | Evidence (pooled, 3 services) |
|---|---|---|
| Replacements (dictionary, `context_hints_enabled`) | **keep on** | -0.10 pp CER [-0.15, -0.05], significant |
| Mined dictionary (+99 rules, review first) | **adopt after review** | cross-validated -0.29 pp [-0.36, -0.22] on top of the 13 current rules |
| Model prompt | **keep off** | 4-bit: +0.04 pp [-0.13, +0.25]; 8-bit: -0.19 pp [-0.31, -0.08] but 8-bit is not selectable in the server |
| `carry_context_s` | **keep 3 (weak)** | offline -0.05 pp [-0.29, +0.14] (n.s.; 2 and 4 s the same); streaming carry 3 vs 0 errors -3%, same sign in 4 of 4 paired runs, +0.05-0.3 s final latency |
| Model bit width | **stay 4-bit** | 8-bit -0.27 pp [-0.44, -0.10] with carry, not available in the server, +92% size |
| Sentence break (`end_silence_ms`) | **870** | 870 vs 600: CER 7.14 vs 7.41%, better in 6 of 9 windows; 1200 / 1500: no further gain, more delay |
| OBS rows | **3** (optional) | row-limit losses 417 -> 157 in replay |
| Singing hide ("auto") | **user decision** | false hides 3.1 per captioned hour (target ~0, not met); coverage 81% of worship segments; no threshold fixes it |

## Corpus and reference

SRT cues are merged into chunks of at most 15 s, split at gaps of 0.6 s or more, and each chunk is widened
by 0.25 s on both sides (never past the midpoint to its neighbour); the padding lowers CER by 0.5-0.6 pp
on a 1-in-8 sample because the cue times are tight around the speech. Gaps of 20 s or more without a cue
are not references; they are kept as "uncaptioned" spans (mostly worship singing, but also MC and video audio).

| Service | Cues | Items | Referenced min | Reference chars | Uncaptioned spans / min | Audio min |
|---|---:|---:|---:|---:|---:|---:|
| 20260704 | 3419 | 658 | 102.5 | 28452 | 8 / 20.1 | 127.8 |
| 20260822 | 2811 | 720 | 92.8 | 24034 | 8 / 23.6 | 125.9 |
| 20260912 | 2838 | 715 | 93.4 | 24068 | 8 / 23.1 | 126.3 |

**SRT/audio alignment.** For 30 items per service the audio span was shifted by delta and decoded;
CER by delta (-0.25 / 0 / +0.25 s): 5.9 / 5.7 / 10.6% (0704), 7.4 / 7.5 / 8.9% (0822), 6.2 / 6.3 / 8.8% (0912).
No constant offset: the optimum lies within about 0.1 s of zero (all minima at 0 or -0.25 on a 0.25 s grid
with a flat bottom), so no correction was applied.

## Step 2: offline accuracy (SRT chunks, not VAD segments)

Offline items come from SRT chunks, not from the server's VAD segmentation, so carry (which matters when
segmentation cuts mid-sentence) is probably understated here; Step 3 is the realistic check. CER is the
cer_eval normalisation (NFKC, no punctuation, Latin lower-cased); "folded" also maps the God pronouns.
Intervals are 95% bootstrap CIs resampling 5-minute blocks within each service (5000 draws, seed 20261003);
deltas are paired. `m4`/`m8` = 4-bit (pinned snapshot) / 8-bit self-converted; `cN` = carry N s (max gap
1.5 s); `repl` = the 13 live replacements; `prompt` = domain + hotwords sent to the model.

### CER (%) per service and pooled

| variant | 20260704 | 20260822 | 20260912 | pooled |
|---|---:|---:|---:|---:|
| m4-base | 6.85 [5.46, 8.79] | 6.47 [5.44, 7.82] | 6.82 [5.82, 8.02] | 6.72 [5.99, 7.59] |
| m4-c2 | 6.72 [5.46, 8.50] | 6.58 [5.60, 7.82] | 6.85 [5.89, 7.98] | 6.72 [6.03, 7.52] |
| m4-c3 | 6.67 [5.36, 8.62] | 6.54 [5.57, 7.80] | 6.80 [5.84, 7.88] | 6.67 [5.97, 7.52] |
| m4-c4 | 6.65 [5.36, 8.61] | 6.73 [5.74, 7.98] | 6.77 [5.87, 7.78] | 6.71 [6.02, 7.56] |
| m8-base | 6.57 [5.19, 8.62] | 5.92 [4.96, 7.21] | 6.99 [5.98, 8.15] | 6.50 [5.77, 7.39] |
| m8-c3 | 6.41 [5.11, 8.35] | 5.94 [5.10, 7.09] | 6.84 [6.00, 7.77] | 6.40 [5.74, 7.23] |
| m4-base-repl | 6.82 [5.43, 8.77] | 6.33 [5.30, 7.68] | 6.72 [5.69, 7.92] | 6.64 [5.90, 7.51] |
| m4-c3-repl | 6.59 [5.27, 8.56] | 6.42 [5.44, 7.68] | 6.72 [5.75, 7.81] | 6.58 [5.87, 7.42] |
| m4-c3-repl-prompt | 6.63 [5.28, 8.62] | 6.45 [5.42, 7.76] | 6.77 [5.79, 7.91] | 6.62 [5.90, 7.49] |
| m8-c3-repl | 6.39 [5.09, 8.33] | 5.89 [5.06, 7.04] | 6.78 [5.92, 7.73] | 6.36 [5.69, 7.18] |
| m8-c3-repl-prompt | 6.36 [4.96, 8.43] | 5.61 [4.72, 6.82] | 6.48 [5.57, 7.53] | 6.16 [5.44, 7.06] |

### CER (%), God pronouns folded

| variant | 20260704 | 20260822 | 20260912 | pooled |
|---|---:|---:|---:|---:|
| m4-base | 6.51 [5.08, 8.47] | 5.85 [4.90, 7.03] | 6.38 [5.43, 7.53] | 6.26 [5.55, 7.12] |
| m4-c2 | 6.38 [5.10, 8.17] | 5.95 [5.03, 7.09] | 6.42 [5.47, 7.54] | 6.26 [5.57, 7.06] |
| m4-c3 | 6.36 [5.02, 8.31] | 5.90 [4.99, 7.04] | 6.36 [5.46, 7.40] | 6.22 [5.52, 7.06] |
| m4-c4 | 6.33 [5.01, 8.26] | 6.13 [5.22, 7.28] | 6.31 [5.45, 7.26] | 6.26 [5.58, 7.10] |
| m8-base | 6.28 [4.85, 8.33] | 5.35 [4.48, 6.52] | 6.57 [5.60, 7.70] | 6.08 [5.36, 6.96] |
| m8-c3 | 6.12 [4.79, 8.06] | 5.35 [4.57, 6.39] | 6.41 [5.60, 7.31] | 5.97 [5.31, 6.78] |
| m4-base-repl | 6.49 [5.05, 8.44] | 5.71 [4.75, 6.92] | 6.28 [5.31, 7.45] | 6.18 [5.45, 7.03] |
| m4-c3-repl | 6.28 [4.93, 8.24] | 5.78 [4.84, 6.94] | 6.27 [5.37, 7.32] | 6.12 [5.42, 6.96] |
| m4-c3-repl-prompt | 6.32 [4.94, 8.33] | 5.80 [4.83, 6.99] | 6.32 [5.39, 7.42] | 6.16 [5.45, 7.03] |
| m8-c3-repl | 6.10 [4.78, 8.04] | 5.30 [4.51, 6.34] | 6.35 [5.52, 7.27] | 5.93 [5.27, 6.74] |
| m8-c3-repl-prompt | 6.08 [4.64, 8.16] | 5.01 [4.14, 6.14] | 6.05 [5.18, 7.07] | 5.73 [5.02, 6.62] |

### Paired differences in CER (percentage points; negative = first-named is better; * = CI excludes 0)

| comparison | 20260704 | 20260822 | 20260912 | pooled |
|---|---:|---:|---:|---:|
| m4-c2 minus m4-base | -0.13 [-0.53, +0.19] | +0.10 [-0.22, +0.37] | +0.03 [-0.30, +0.30] | -0.01 [-0.21, +0.17] |
| m4-c3 minus m4-base | -0.18 [-0.64, +0.16] | +0.07 [-0.38, +0.38] | -0.03 [-0.28, +0.21] | -0.05 [-0.29, +0.14] |
| m4-c4 minus m4-base | -0.20 [-0.64, +0.17] | +0.26 [-0.09, +0.52] | -0.06 [-0.39, +0.24] | -0.01 [-0.24, +0.18] |
| m8-base minus m4-base | -0.28 [-0.56, +0.00] | -0.56 [-0.83, -0.33]* | +0.17 [-0.26, +0.76] | -0.23 [-0.43, +0.01] |
| m8-c3 minus m4-base | -0.44 [-0.96, -0.02]* | -0.53 [-0.94, -0.20]* | +0.01 [-0.41, +0.37] | -0.33 [-0.59, -0.09]* |
| m4-base-repl minus m4-base | -0.03 [-0.08, +0.01] | -0.14 [-0.30, -0.03]* | -0.10 [-0.20, -0.02]* | -0.09 [-0.15, -0.04]* |
| m4-c3-repl minus m4-base | -0.26 [-0.72, +0.07] | -0.05 [-0.55, +0.30] | -0.11 [-0.35, +0.12] | -0.15 [-0.39, +0.05] |
| m4-c3-repl-prompt minus m4-base | -0.22 [-0.73, +0.30] | -0.03 [-0.32, +0.24] | -0.06 [-0.40, +0.27] | -0.11 [-0.35, +0.12] |
| m8-c3-repl minus m4-base | -0.46 [-0.99, -0.03]* | -0.58 [-1.02, -0.24]* | -0.05 [-0.47, +0.30] | -0.37 [-0.64, -0.13]* |
| m8-c3-repl-prompt minus m4-base | -0.49 [-1.00, -0.07]* | -0.86 [-1.38, -0.46]* | -0.34 [-0.72, -0.02]* | -0.56 [-0.83, -0.32]* |
| m8-c3 minus m4-c3 | -0.26 [-0.46, -0.03]* | -0.60 [-0.93, -0.28]* | +0.04 [-0.32, +0.36] | -0.27 [-0.44, -0.10]* |
| m4-c3-repl minus m4-c3 | -0.09 [-0.18, -0.01]* | -0.12 [-0.24, -0.03]* | -0.08 [-0.19, +0.01] | -0.10 [-0.15, -0.05]* |
| m4-c3-repl-prompt minus m4-c3-repl | +0.05 [-0.20, +0.42] | +0.03 [-0.30, +0.42] | +0.05 [-0.27, +0.38] | +0.04 [-0.13, +0.25] |
| m8-c3-repl minus m8-c3 | -0.02 [-0.07, +0.03] | -0.05 [-0.11, +0.00] | -0.06 [-0.15, +0.03] | -0.04 [-0.08, -0.01]* |
| m8-c3-repl-prompt minus m8-c3-repl | -0.03 [-0.23, +0.20] | -0.29 [-0.47, -0.11]* | -0.30 [-0.51, -0.10]* | -0.19 [-0.31, -0.08]* |
| m8-c3-repl minus m4-c3-repl | -0.20 [-0.39, +0.04] | -0.53 [-0.86, -0.20]* | +0.06 [-0.31, +0.38] | -0.22 [-0.39, -0.04]* |

The pronoun-folded deltas are within 0.05 pp of these in every cell (`church_eval_report.py` prints both).

### Cost and carry behaviour

| variant | decode RTF* | decodes/item | carry used | carry fallback | dup-prefix items |
|---|---:|---:|---:|---:|---:|
| m4-base | 0.076 | 1.00 | 0% | 0% | 10 |
| m4-c2 | 0.088 | 1.25 | 88% | 25% | 10 |
| m4-c3 | 0.158 | 1.10 | 88% | 10% | 6 |
| m4-c4 | 0.141 | 1.11 | 88% | 11% | 10 |
| m8-base | 0.253 | 1.00 | 0% | 0% | 7 |
| m8-c3 | 0.206 | 1.08 | 88% | 8% | 6 |
| m4-base-repl | 0.181 | 1.00 | 0% | 0% | 10 |
| m4-c3-repl | 0.083 | 1.10 | 88% | 10% | 6 |
| m4-c3-repl-prompt | 0.102 | 1.09 | 88% | 9% | 6 |
| m8-c3-repl | 0.099 | 1.08 | 88% | 8% | 6 |
| m8-c3-repl-prompt | 0.127 | 1.09 | 88% | 9% | 8 |

\* Decode RTF was measured while several decodes and other host jobs shared the GPU (host load 6-100), so it is
indicative only; the decodes-per-item column is exact: carry re-runs a segment alone when no trustworthy
overlap is found (8-25% of items).

### Replacement rules (13 live rules, identified by dictionary order; effect on the un-replaced 4-bit output)

| rule | hits | items | net error change |
|---:|---:|---:|---:|
| 1 | 0 | 0 | +0 |
| 2 | 1 | 1 | -2 |
| 3 | 0 | 0 | +0 |
| 4 | 0 | 0 | +0 |
| 5 | 0 | 0 | +0 |
| 6 | 3 | 3 | -9 |
| 7 | 0 | 0 | +0 |
| 8 | 0 | 0 | +0 |
| 9 | 0 | 0 | +0 |
| 10 | 0 | 0 | +0 |
| 11 | 4 | 4 | -8 |
| 12 | 5 | 5 | -13 |
| 13 | 7 | 7 | -35 |

Five rules fire, none hurts; the other eight never matched in 6 h of audio.

### Dictionary mining, cross-validated

`benchmarks/dict_mine.py` was run on un-replaced 4-bit + carry 3 output. For each service in turn, candidates
were mined from the other two, merged with the live dictionary (about 65-75 safe rules per fold) and scored
on the held-out service (CER %, pooled over the three held-out folds):

| dictionary (held-out service) | 20260704 | 20260822 | 20260912 | pooled |
|---|---:|---:|---:|---:|
| none | 6.67 | 6.54 | 6.80 | 6.67 |
| current | 6.59 | 6.42 | 6.71 | 6.57 |
| merged | 6.35 | 6.07 | 6.43 | 6.29 |
| Δ current minus none (pp) | -0.08 [-0.17, -0.00] | -0.12 [-0.24, -0.03] | -0.09 [-0.20, +0.00] | -0.10 [-0.15, -0.05] |
| Δ merged minus current (pp) | -0.24 [-0.34, -0.16] | -0.35 [-0.46, -0.25] | -0.28 [-0.43, -0.14] | -0.29 [-0.35, -0.22] |
| Δ merged minus none (pp) | -0.32 [-0.47, -0.19] | -0.47 [-0.63, -0.32] | -0.37 [-0.58, -0.18] | -0.38 [-0.49, -0.29] |

Mining all three services gives 99 safe candidates (112 rules merged) and 691 lower-confidence ones.
The 99 are **not reviewed**: they include generic orthography rules, and the held-out gain only shows they
generalise across services of the same church, not that each rule is right.

## Step 3: streaming windows (real model, port 8431, isolated HOME)

Nine 20-minute windows (start of service, sermon 50:00-70:00, closing 105:00-125:00 per service; the opening and
closing windows contain captioned/uncaptioned boundaries), streamed at 1x through the real pipeline with
`end_silence_ms` 600 and 870 (two concurrent sessions, one per value, same audio), server: 4-bit, carry 3 s,
`church` profile, defaults otherwise. CER is concatenated over the window against the SRT cues starting in it;
segments mostly inside an uncaptioned span are not scored (counted as "shown in uncaptioned" when they carry
text and are not hidden as singing). "Shown" CER also drops segments labelled singing, as the plugin would.
OBS replay: fade-out 1500 ms, fade 200 ms, 2 rows, 3 sentences, 1800 px, comma breaks min 8, singing auto.

| window | end_silence | CER asr / shown % | speech segs | false hides | shown in uncaptioned | mid-speech fades | row-limit losses | tail retract | layout moves | dup lines | largest burst | 1st text s | commit s | final s p50 / p95 | carry strip / uncertain |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 0704 open | 600 | 9.1 / 9.1 | 50 | 0 | 26 | 0 | 47 | 0 | 0 | 0 | 43 | 0.9 | 0.2 | 0.4 / 1.4 | 49 / 20 |
| 0704 open | 870 | 8.8 / 8.8 | 45 | 0 | 24 | 0 | 42 | 0 | 0 | 0 | 38 | 0.8 | 0.1 | 0.4 / 1.5 | 48 / 13 |
| 0704 sermon | 600 | 6.1 / 6.1 | 224 | 0 | 0 | 0 | 30 | 0 | 0 | 0 | 63 | 1.1 | 0.3 | 0.6 / 4.0 | 164 / 51 |
| 0704 sermon | 870 | 5.8 / 5.8 | 161 | 0 | 0 | 0 | 53 | 0 | 0 | 0 | 63 | 1.2 | 0.3 | 0.9 / 5.4 | 125 / 26 |
| 0704 sermon | 1200 | 5.8 / 5.8 | 128 | 0 | 0 | 0 | 65 | 0 | 0 | 0 | 62 | 2.1 | 0.5 | 1.3 / 6.1 | 107 / 12 |
| 0704 sermon | 1500 | 5.9 / 5.9 | 115 | 0 | 0 | 0 | 62 | 0 | 0 | 0 | 70 | 2.4 | 0.5 | 1.6 / 6.5 | 97 / 11 |
| 0704 close | 600 | 6.5 / 8.5 | 143 | 2 | 8 | 0 | 34 | 0 | 0 | 0 | 44 | 0.8 | 0.1 | 0.2 / 0.7 | 117 / 49 |
| 0704 close | 870 | 6.5 / 8.6 | 114 | 2 | 7 | 7 | 33 | 0 | 0 | 0 | 68 | 1.9 | 0.9 | 4.0 / 42.0 | 96 / 37 |
| 0822 open | 600 | 7.1 / 7.1 | 48 | 0 | 16 | 0 | 27 | 0 | 0 | 0 | 82 | 4.0 | 1.7 | 7.3 / 89.5 | 51 / 27 |
| 0822 open | 870 | 7.0 / 7.0 | 46 | 0 | 15 | 0 | 27 | 0 | 0 | 0 | 63 | 4.9 | 1.8 | 6.7 / 92.9 | 45 / 29 |
| 0822 sermon | 600 | 6.3 / 6.3 | 221 | 0 | 0 | 0 | 41 | 0 | 0 | 0 | 42 | 1.4 | 0.6 | 0.9 / 2.7 | 151 / 48 |
| 0822 sermon | 870 | 5.8 / 5.8 | 170 | 0 | 0 | 0 | 61 | 0 | 0 | 0 | 55 | 1.5 | 0.6 | 1.2 / 3.2 | 129 / 20 |
| 0822 sermon | 1200 | 6.3 / 6.3 | 133 | 0 | 0 | 0 | 79 | 0 | 0 | 0 | 34 | 1.0 | 0.1 | 0.6 / 1.2 | 102 / 17 |
| 0822 sermon | 1500 | 6.1 / 6.1 | 120 | 0 | 0 | 0 | 87 | 0 | 0 | 0 | 41 | 1.3 | 0.2 | 0.7 / 1.7 | 94 / 9 |
| 0822 close | 600 | 7.2 / 10.8 | 87 | 4 | 8 | 0 | 53 | 0 | 0 | 0 | 30 | 1.1 | 0.5 | 1.0 / 4.9 | 65 / 43 |
| 0822 close | 870 | 6.8 / 10.5 | 66 | 4 | 8 | 0 | 52 | 0 | 0 | 0 | 46 | 1.4 | 0.5 | 1.1 / 9.2 | 53 / 37 |
| 0912 open | 600 | 4.9 / 4.9 | 47 | 0 | 21 | 0 | 42 | 0 | 0 | 0 | 89 | 1.4 | 0.4 | 1.0 / 14.2 | 58 / 48 |
| 0912 open | 870 | 5.2 / 5.2 | 39 | 0 | 20 | 0 | 53 | 0 | 0 | 0 | 43 | 1.6 | 0.4 | 1.1 / 7.8 | 54 / 38 |
| 0912 sermon | 600 | 8.6 / 8.6 | 195 | 0 | 11 | 0 | 54 | 0 | 0 | 0 | 36 | 1.1 | 0.3 | 0.6 / 2.0 | 152 / 37 |
| 0912 sermon | 870 | 8.6 / 8.6 | 151 | 0 | 11 | 0 | 50 | 0 | 0 | 0 | 25 | 1.1 | 0.3 | 0.8 / 2.1 | 118 / 29 |
| 0912 sermon | 1200 | 7.8 / 7.8 | 122 | 0 | 11 | 0 | 74 | 0 | 0 | 0 | 28 | 1.1 | 0.2 | 0.6 / 1.5 | 94 / 23 |
| 0912 sermon | 1500 | 7.7 / 7.7 | 111 | 0 | 12 | 0 | 73 | 0 | 0 | 0 | 27 | 1.1 | 0.1 | 0.6 / 1.4 | 93 / 13 |
| 0912 close | 600 | 12.0 / 18.6 | 110 | 10 | 3 | 0 | 35 | 0 | 0 | 0 | 33 | 1.1 | 0.3 | 0.7 / 2.2 | 81 / 57 |
| 0912 close | 870 | 11.0 / 17.2 | 88 | 10 | 3 | 0 | 46 | 0 | 0 | 0 | 38 | 1.4 | 0.3 | 0.9 / 2.3 | 70 / 45 |

Aggregates over the nine windows (600 / 870): CER as ASR 7.41 / 7.14%, as shown 8.59 / 8.29%; speech segments
1125 / 880; false hides 16 / 16 (all in the three closing windows); captions shown inside uncaptioned spans
93 / 88 segments (a mix of missed singing and uncaptioned speech); mid-speech fades 0 / 7 (the 7 are all in
0704 closing, a window with 42 s p95 final latency); tail retractions 0 / 0; layout moves 0 / 0; plugin-origin
duplication lines 0 / 0; largest burst 89 / 68 characters. 1200 / 1500 were run on the three sermon windows
only (rows above).

**Latency caveat.** The two sessions share one worker, and the host was heavily loaded by unrelated jobs during
parts of the run, so the latency columns are an upper bound and the 600-vs-870 and 1200/1500 latency
differences are only trustworthy between runs that were concurrent in the same window. Two windows show
worship-song backlog under that load: 0822 opening (finals up to 110 s late) and 0704 closing (one session died
with `session_limit` at 8.5 min; rerun). The same windows streamed alone: 0822 opening at 870 ms: final latency
p50 0.47 s / p95 1.8 s, first text 0.83 s, commit 0.10 s, CER 7.03% (vs 7.0% concurrent); 0704 closing at
600 ms: final p50 0.2 s / p95 0.7 s. One earlier attempt of 0822 opening was discarded after an
`inference_timeout` and worker restart (host overload); it is kept out of the tables.

**Carry in streaming.** Server restarted with `carry_context_s = 0`, sermon windows 0704 and 0912, 600 and 870 ms
(concurrent pairs, same conditions as the carry-3 runs): errors 319 / 312 / 350 / 351 vs 309 / 294 / 344 / 345
with carry 3 (-3.1% in total); median final latency 0.38 / 0.54 / 0.51 / 0.61 s vs 0.63 / 0.85 / 0.55 / 0.80 s.
About 30% of carry attempts fall back to a second decode (`carry_overlap_uncertain`).

### OBS display settings, replayed on the nine 870 ms traces

| display setting (end_silence 870, 9 windows) | mid-speech fades | row-limit losses | tail rewrites | tail retractions | layout moves | dup lines | largest burst | singing lines hidden |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| user settings | 7 | 417 | 1763 | 0 | 0 | 40 | 68 | 112 |
| rows 3 | 4 | 157 | 1764 | 0 | 0 | 40 | 74 | 112 |
| rows 1 | 19 | 976 | 1738 | 0 | 0 | 40 | 38 | 112 |
| sentences 2 | 5 | 417 | 1763 | 0 | 0 | 40 | 68 | 112 |
| sentences 4 | 7 | 418 | 1765 | 0 | 0 | 40 | 68 | 112 |
| punct off | 7 | 28 | 1764 | 0 | 0 | 40 | 132 | 112 |
| punct sentence | 7 | 112 | 1764 | 0 | 0 | 40 | 146 | 112 |
| comma min 12 | 7 | 271 | 1763 | 0 | 0 | 40 | 68 | 112 |
| tail off | 1 | 290 | 0 | 0 | 0 | 6 | 72 | 60 |
| fade-out 1000 ms | 6 | 417 | 1763 | 0 | 0 | 40 | 68 | 112 |
| fade-out 2500 ms | 7 | 417 | 1763 | 0 | 0 | 40 | 68 | 112 |
| singing show | 9 | 469 | 2336 | 0 | 0 | 56 | 68 | 0 |

## Step 4: singing detection over the full services

YAMNet frames + Silero VAD segments + the shipped `tea_asr.singing` labeller, run over the full recordings
(`church_eval_singing.py`). Captioned speech (VAD segments at least half covered by cues) is the reliable
truth; segments mostly inside uncaptioned spans are "likely singing". 600 ms and 870 ms segmentations give the
same false-call count.

| Service | Captioned h | Speech segs (870) | Hidden early / final / ever | Uncaptioned segs (not speech-like) | Called singing (final) | Seconds called / seconds |
|---|---:|---:|---:|---:|---:|---:|
| 20260704 | 1.80 | 786 | 2 / 2 / 2 | 97 | 69 | 432 / 539 |
| 20260822 | 1.71 | 766 | 4 / 5 / 5 | 103 | 88 | 632 / 765 |
| 20260912 | 1.72 | 792 | 9 / 7 / 9 | 92 | 80 | 764 / 863 |
| **Pooled** | **5.22** | **2344** | **15 / 14 / 16** | **292** | **237 (81%)** | **1827 / 2166 (84%)** |

- **False singing calls inside captioned speech: 16 per 5.22 h = 3.1 per captioned hour** (not near 0). They
  cluster in the closing worship (all after 107 min): the leader speaks over live music; YAMNet scores them
  Music about 0.5-0.95 with speech about 0 and vocal about 0.01-0.08. Decoding the 16 segments gives a median
  CER of 12% (870) / 20% (600) against their cues, so a hide removes real captions (about 370-390 correct
  characters, 81% of the characters in those segments).
- 70 more uncaptioned segments have a mean YAMNet speech score of at least 0.5 and are excluded from the coverage
  above (real talking: MC, video). Of the rest, spot checks by decoding sampled segments showed song lyrics
  (Chinese and English) in the segments called singing, and a mix of MC speech over music and missed song
  lyrics in the ones not called.
- Sweep (enter frames 4-24, enter threshold 0.98-0.9995, local 0.5-0.99, exit 0.4-0.8; 270 settings) on the same
  data: the best coverage for each number of false calls (segments called singing in uncaptioned spans):
  0 false: 0%; 1: 19%; 2: 32%; 3: 27%; 4: 55%; 5: 63%; shipped (16): 81%. No threshold or hysteresis removes the
  false calls without removing the detector. The strongest enter-window mean seen in captioned speech is
  0.986-0.993, above the 0.98 threshold and overlapping the true singing range.
- In the streaming windows the same effect shows as 16 false hides (870 ms: 2 + 4 + 10) in the three closing
  windows and 0 in the six others.

## Limitations and not verified

- One church, three services, one speaker mix; the SRT was made from live captions, so it follows the user's
  captioning habits (what counts as caption-worthy), pronoun and AMEN conventions, and may contain its own errors.
- Offline items are SRT chunks, not VAD segments; carry is probably understated offline.
- Streaming: 9 windows of 20 min (about 3 of 6 hours; one window type per service part), two concurrent sessions,
  host load not controlled, so latency is an upper bound; only single-session reruns of two windows.
- Not run: a streaming comparison with the mined dictionary or the 8-bit model (the server cannot load it);
  streaming prompt on/off; `end_silence` values below 600; preview cadence; full 6 h at 1x.
- The OBS replay is the plugin state machine on saved traces, with a font-width model; it is not the OBS screen.
  **Needs the user's eyes:** the 3-row layout, how the opening/closing windows look with singing hidden, and
  whether captions over music (the 88 shown in uncaptioned spans) are acceptable.
- The uncaptioned spans are not a singing ground truth (they contain MC speech and video audio).
