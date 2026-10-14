# EmbeddingGemma 2 audio↔text probe

Generated: 2026-10-14 02:00 UTC

EmbeddingGemma 2 returns vectors; it does not generate a transcript. This report tests closed-set retrieval and similarity-based error discrimination.
The local Transformers checkpoint was loaded offline with the vision encoder disabled. Text was passed as displayed with the model's `SentenceSimilarity` prompt; audio was extracted at 16 kHz mono and wrapped in its model audio tokens. The shared transformer's last hidden states were mean-pooled by attention mask and L2-normalized, matching the checkpoint's SentenceTransformers pooling modules. CPU uses float32 and MPS uses bfloat16 when supported, otherwise float32. No fp16 was used.

## Dataset coverage

| Service | Usable items | Mismatching hypotheses | Dictionary-rule pairs | Device | Model RSS increase (MB, not measured on MPS) |
|---|---:|---:|---:|---|---:|
| 20260704 | 658 | 401 | 132 | mps | 0.0 |
| 20260822 | 720 | 372 | 134 | mps | 0.0 |
| 20260912 | 715 | 428 | 147 | mps | 0.0 |

## E1 · Closed-set retrieval

Each audio item ranks the usable human references from the same service. The random baseline accounts for duplicate normalized references.

| Dim | Split | Queries | Candidates | Top-1 | Random | Top-5 | Random | MRR | Random |
|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 768 | overall | 2093 | [658, 720] | 66.3% | 0.1% | 83.5% | 0.7% | 0.740 | 0.010 |
| 768 | short_lt_4s | 596 | [658, 720] | 51.2% | 0.2% | 72.0% | 0.8% | 0.608 | 0.011 |
| 768 | long_gte_4s | 1497 | [658, 720] | 72.3% | 0.1% | 88.0% | 0.7% | 0.792 | 0.010 |
| 256 | overall | 2093 | [658, 720] | 56.1% | 0.1% | 77.2% | 0.7% | 0.657 | 0.010 |
| 256 | short_lt_4s | 596 | [658, 720] | 45.8% | 0.2% | 67.8% | 0.8% | 0.561 | 0.011 |
| 256 | long_gte_4s | 1497 | [658, 720] | 60.3% | 0.1% | 80.9% | 0.7% | 0.695 | 0.010 |

## E2 · Pairwise discrimination

Accuracy is the strict fraction where cosine(audio, preferred text) > cosine(audio, alternative text); ties count as misses.

| Dim | Mismatch group | Pairs | Wins | Ties | Accuracy |
|---:|---|---:|---:|---:|---:|
| 768 | overall | 1201 | 585 | 0 | 48.7% |
| 768 | 1_char | 341 | 161 | 0 | 47.2% |
| 768 | 2_3_chars | 399 | 190 | 0 | 47.6% |
| 768 | 4_plus_chars | 461 | 234 | 0 | 50.8% |
| 768 | dictionary_rule_variants | 413 | 194 | 0 | 47.0% |
| 256 | overall | 1201 | 603 | 0 | 50.2% |
| 256 | 1_char | 341 | 168 | 0 | 49.3% |
| 256 | 2_3_chars | 399 | 189 | 0 | 47.4% |
| 256 | 4_plus_chars | 461 | 246 | 0 | 53.4% |
| 256 | dictionary_rule_variants | 413 | 200 | 0 | 48.4% |

## E3 · Cost

| Service | Device | Audio mean (ms/item) | Audio p95 (ms/item) | Text mean (ms/item) | Model RSS increase (MB, not measured on MPS) |
|---|---|---:|---:|---:|---:|
| 20260704 | mps | 122.1 | 168.3 | 7.3 | 0.0 |
| 20260822 | mps | 100.0 | 149.7 | 6.2 | 0.0 |
| 20260912 | mps | 99.0 | 145.9 | 6.1 | 0.0 |

## Verdict

**NO-GO for rescoring or replacing ASR.** Over 2,093 items (3 services), pairwise discrimination is at chance: 47–51% on 1-char, 2–3-char and dictionary-rule pairs (gate 70%). Closed-set retrieval works (top-1 66%, top-5 84% among ~700 same-service sentences; random 0.1%), so the model captures *what is said* in Chinese but not *which characters*. The only plausible follow-up is closed-set detection of known scripture/hymn texts, which needs a curated text list and a held-out evaluation first.

## What this can and cannot do for live subtitles

- **Cannot replace ASR:** an embedding maps audio to a fixed vector and cannot emit open-vocabulary words or a transcript.
- **Possible closed-set use:** detect a known scripture reading or hymn by ranking a curated reference list. Follow-up: evaluate held-out services; require top-1 ≥80% and at least 5× the random top-1 baseline before a live canary.
- **Possible error gate:** compare an ASR candidate with a reference-like correction. Follow-up: require ≥70% pairwise accuracy on 1–3 character and dictionary-rule pairs, with at least 50 pairs per group, before testing any live rescore path.
- **Possible audio classifier:** classify speech, singing, and silence from audio vectors. Follow-up: label a held-out set and require balanced accuracy ≥85% plus per-class recall ≥75%.
- **Otherwise:** no live use; keep the model offline for this service.

All outputs are aggregate metrics. No reference, hypothesis, item ID, or audio content is included in this report.
