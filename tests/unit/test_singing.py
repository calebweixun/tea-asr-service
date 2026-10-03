from __future__ import annotations

import numpy as np

from tea_asr.singing import (
    EARLY_DECISION_DEADLINE_S,
    MAX_ANALYSIS_SECONDS,
    AudioFeatures,
    SingingDecisionPolicy,
    StreamingSingingClassifier,
    estimate_f0,
    extract_features,
)


def _tone(frequency: float, duration_s: float = 0.12, sample_rate: int = 16_000) -> np.ndarray:
    time = np.arange(round(duration_s * sample_rate)) / sample_rate
    return 0.4 * np.sin(2 * np.pi * frequency * time)


def test_f0_estimator_tracks_synthetic_tone() -> None:
    frequency, confidence = estimate_f0(_tone(220.0))
    assert frequency is not None
    assert abs(frequency - 220.0) < 4.0
    assert confidence > 0.7


def test_f0_estimator_tracks_harmonic_vowel_like_signal() -> None:
    time = np.arange(3_200) / 16_000
    vowel = sum(
        amplitude * np.sin(2 * np.pi * harmonic * 220.0 * time)
        for harmonic, amplitude in ((1, 1.0), (2, 0.55), (3, 0.3), (4, 0.15))
    )
    frequency, confidence = estimate_f0(vowel)
    assert frequency is not None
    assert abs(frequency - 220.0) < 4.0
    assert confidence > 0.7


def test_feature_extraction_is_finite_for_silence_and_voiced_signal() -> None:
    silence = extract_features(np.zeros(24_000, dtype=np.float64))
    voiced = extract_features(np.tile(_tone(196.0), 12))
    assert all(np.isfinite(silence.as_array()))
    assert all(np.isfinite(voiced.as_array()))
    assert silence.voiced_ratio == 0.0
    assert voiced.voiced_ratio > 0.8
    assert voiced.harmonicity > 0.5


def test_decision_policy_is_early_and_allows_only_one_revision() -> None:
    policy = SingingDecisionPolicy(threshold=0.8)
    first = policy.observe(0.95, 0.8)
    assert first is not None
    assert first.audio_class == "singing"
    assert first.revision == 0
    assert policy.observe(0.2, 1.4) is None
    revision = policy.observe(0.2, 2.5)
    assert revision is not None
    assert revision.audio_class == "speech"
    assert revision.revision == 1
    assert policy.observe(0.99, 3.0) is None
    assert EARLY_DECISION_DEADLINE_S <= 1.5


def test_streaming_pcm_history_is_bounded() -> None:
    classifier = StreamingSingingClassifier(0)
    chunk = np.zeros(1_600, dtype="<i2").tobytes()
    for index in range(30):
        classifier.append(chunk, index * 1_600)
    assert len(classifier._audio) <= round(MAX_ANALYSIS_SECONDS * 16_000) * 2
    assert classifier.elapsed_s == 3.0


def test_speech_decision_is_forced_by_early_deadline() -> None:
    policy = SingingDecisionPolicy(threshold=0.8)
    assert policy.observe(0.1, 1.4) == policy.decision
    assert policy.decision is not None
    assert policy.decision.audio_class == "speech"
    assert policy.decision.revision == 0


def test_audio_features_keep_expected_field_order() -> None:
    assert AudioFeatures(0, 0, 0, 0, 0, 0, 1, 0).as_array().shape == (8,)
