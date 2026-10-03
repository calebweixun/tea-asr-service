"""Small, interpretable singing detector for 16 kHz mono PCM.

The estimator is deliberately CPU-only and uses no downloaded model.  See
``benchmarks/singing_eval.py`` for feature fitting and held-out evaluation.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

import numpy as np

SAMPLE_RATE = 16_000
FRAME_SAMPLES = 640  # 40 ms, enough to resolve a low sung fundamental.
HOP_SAMPLES = 320  # 20 ms.
FEATURE_NAMES = (
    "voiced_ratio",
    "stable_ratio",
    "longest_plateau_s",
    "pitch_range_st",
    "median_pitch_step_st",
    "harmonicity",
    "spectral_flatness",
    "syllable_rate_hz",
)

# Weighted logistic fit on the labelled 1.5 s windows. The evaluation and
# leave-one-file-out results are documented in docs/benchmarks/singing-eval-report.md.
MODEL_MEAN = np.asarray(
    [0.35739189, 0.67992097, 0.13535635, 13.86319629, 0.52991905, 0.68445204, 0.04320519, 2.40197803]
)
MODEL_SCALE = np.asarray(
    [0.22214998, 0.26926760, 0.11032763, 7.46111762, 1.53079010, 0.11568377, 0.04058072, 0.80932770]
)
MODEL_COEFFICIENTS = np.asarray(
    [-0.64305559, 0.45020013, -0.03853093, 0.00101658, 0.03103230, -0.00913680, 0.10898463, -0.28673772]
)
MODEL_INTERCEPT = 0.00600639
# The requested <1% sermon false-positive rate is prioritized over recall.
# This held-out threshold does not reach the 80% singing recall target; keep the
# capability opt-in until the detector can meet both targets.
SINGING_THRESHOLD = 0.995

EARLY_EVIDENCE_S = 0.8
EARLY_DECISION_DEADLINE_S = 1.4
REVISION_EVIDENCE_S = 2.5
MAX_ANALYSIS_SECONDS = 2.0


@dataclass(frozen=True, slots=True)
class AudioFeatures:
    voiced_ratio: float
    stable_ratio: float
    longest_plateau_s: float
    pitch_range_st: float
    median_pitch_step_st: float
    harmonicity: float
    spectral_flatness: float
    syllable_rate_hz: float

    def as_array(self) -> np.ndarray:
        return np.asarray([getattr(self, name) for name in FEATURE_NAMES], dtype=np.float64)


@dataclass(frozen=True, slots=True)
class AudioClassDecision:
    audio_class: Literal["speech", "singing"]
    confidence: float
    revision: int


def _parabolic_peak(values: np.ndarray, index: int) -> float:
    """Return a sub-sample peak location using a three-point parabola."""

    if index <= 0 or index >= values.size - 1:
        return float(index)
    left, middle, right = map(float, values[index - 1 : index + 2])
    denominator = left - 2.0 * middle + right
    if abs(denominator) < 1e-12:
        return float(index)
    return float(index) + 0.5 * (left - right) / denominator


def estimate_f0(frame: np.ndarray, sample_rate: int = SAMPLE_RATE) -> tuple[float | None, float]:
    """Estimate the fundamental with a YIN cumulative-mean autocorrelation.

    Returns ``(hz, confidence)``. ``hz`` is ``None`` when the frame is too
    short, silent, or insufficiently periodic.  The implementation uses NumPy
    FFTs and is intentionally independent of librosa or Torch.
    """

    signal = np.asarray(frame, dtype=np.float64).reshape(-1)
    if signal.size < sample_rate // 40:
        return None, 0.0
    signal = signal - float(np.mean(signal))
    energy = float(np.dot(signal, signal))
    if energy <= 1e-10:
        return None, 0.0

    # FFT autocorrelation gives the YIN difference function in O(n log n).
    signal = signal * np.hanning(signal.size)
    fft_size = 1 << (2 * signal.size - 1).bit_length()
    spectrum = np.fft.rfft(signal, n=fft_size)
    autocorrelation = np.fft.irfft(spectrum * np.conjugate(spectrum), n=fft_size)
    autocorrelation = autocorrelation[: signal.size]
    prefix = np.concatenate(([0.0], np.cumsum(signal * signal)))

    min_lag = max(2, int(sample_rate / 500))
    max_lag = min(signal.size // 2, int(sample_rate / 65))
    if max_lag <= min_lag:
        return None, 0.0
    lags = np.arange(1, max_lag + 1)
    overlap = signal.size - lags
    difference = (
        prefix[overlap]
        + (prefix[-1] - prefix[lags])
        - 2.0 * autocorrelation[lags]
    )
    cumulative = np.cumsum(difference)
    cmnd = np.ones(max_lag + 1, dtype=np.float64)
    cmnd[1:] = difference * lags / np.maximum(cumulative, 1e-12)

    # YIN uses the first clear period minimum to avoid selecting a multiple of
    # the true period.  If none crosses the cutoff, keep only a strong global
    # minimum; noisy frames remain unvoiced.
    selected: int | None = None
    cutoff = 0.22
    for lag in range(min_lag, max_lag):
        if cmnd[lag] < cutoff:
            selected = lag
            while selected + 1 < max_lag and cmnd[selected + 1] < cmnd[selected]:
                selected += 1
            break
    if selected is None:
        candidate = int(np.argmin(cmnd[min_lag : max_lag + 1])) + min_lag
        if cmnd[candidate] >= 0.42:
            return None, max(0.0, 1.0 - float(cmnd[candidate]))
        selected = candidate

    refined_lag = _parabolic_peak(-cmnd, selected)
    confidence = min(1.0, max(0.0, 1.0 - float(cmnd[selected])))
    if refined_lag <= 0 or confidence < 0.45:
        return None, confidence
    return float(sample_rate / refined_lag), confidence


def _runs(mask: np.ndarray) -> list[int]:
    lengths: list[int] = []
    current = 0
    for value in mask:
        if bool(value):
            current += 1
        elif current:
            lengths.append(current)
            current = 0
    if current:
        lengths.append(current)
    return lengths


def extract_features(audio: np.ndarray | bytes, sample_rate: int = SAMPLE_RATE) -> AudioFeatures:
    """Extract pitch, harmonicity, plateau, and syllabic-rate features."""

    if isinstance(audio, bytes):
        signal = np.frombuffer(audio, dtype="<i2").astype(np.float64) / 32768.0
    else:
        signal = np.asarray(audio, dtype=np.float64).reshape(-1)
        if signal.size and float(np.max(np.abs(signal))) > 1.5:
            signal = signal / 32768.0
    if signal.size < FRAME_SAMPLES:
        empty = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0)
        return AudioFeatures(*empty)

    starts = range(0, signal.size - FRAME_SAMPLES + 1, HOP_SAMPLES)
    pitches: list[float] = []
    confidences: list[float] = []
    flatness: list[float] = []
    rms: list[float] = []
    window = np.hanning(FRAME_SAMPLES)
    fft_size = 1024
    frequencies = np.fft.rfftfreq(fft_size, 1.0 / sample_rate)
    spectral_bins = (frequencies >= 100.0) & (frequencies <= 4_000.0)

    for start in starts:
        frame = signal[start : start + FRAME_SAMPLES]
        f0, confidence = estimate_f0(frame, sample_rate)
        pitches.append(float(f0 or 0.0))
        confidences.append(confidence)
        rms.append(float(np.sqrt(np.mean(frame * frame))))
        power = np.abs(np.fft.rfft(frame * window, n=fft_size))[spectral_bins] ** 2
        if power.size and float(np.mean(power)) > 1e-15:
            flatness.append(float(np.exp(np.mean(np.log(power + 1e-15))) / np.mean(power)))
        else:
            flatness.append(1.0)

    f0_track = np.asarray(pitches, dtype=np.float64)
    confidence_track = np.asarray(confidences, dtype=np.float64)
    voiced = (f0_track > 0.0) & (confidence_track >= 0.55)
    voiced_pitches = f0_track[voiced]
    voiced_ratio = float(np.mean(voiced)) if voiced.size else 0.0
    harmonicity = float(np.mean(confidence_track[voiced])) if np.any(voiced) else 0.0

    if voiced_pitches.size >= 2:
        semitones = np.zeros_like(f0_track)
        semitones[voiced] = 12.0 * np.log2(
            voiced_pitches / float(np.median(voiced_pitches))
        )
        adjacent_voiced = voiced[:-1] & voiced[1:]
        all_steps = np.abs(np.diff(semitones))
        adjacent_steps = all_steps[adjacent_voiced]
        stable_pairs = adjacent_voiced & (all_steps <= 0.65)
        stable_ratio = (
            float(np.mean(adjacent_steps <= 0.65)) if adjacent_steps.size else 0.0
        )
        longest_plateau_s = (
            (max(_runs(stable_pairs), default=0) + 1) * HOP_SAMPLES / sample_rate
        )
        voiced_semitones = semitones[voiced]
        pitch_range_st = float(
            np.percentile(voiced_semitones, 95) - np.percentile(voiced_semitones, 5)
        )
        median_pitch_step_st = float(np.median(adjacent_steps)) if adjacent_steps.size else 0.0
    else:
        stable_ratio = 0.0
        longest_plateau_s = 0.0
        pitch_range_st = 0.0
        median_pitch_step_st = 0.0

    energy = np.asarray(rms, dtype=np.float64)
    # A 100 ms moving average suppresses individual pitch cycles while
    # preserving syllable-scale rises. Count separated positive envelope peaks.
    if energy.size >= 7 and float(np.max(energy)) > 1e-5:
        smooth = np.convolve(energy, np.ones(5, dtype=np.float64) / 5.0, mode="same")
        threshold = max(float(np.percentile(smooth, 35)) * 1.18, 1e-5)
        peaks = [
            i
            for i in range(1, smooth.size - 1)
            if smooth[i - 1] < smooth[i] >= smooth[i + 1] and smooth[i] >= threshold
        ]
        min_gap = max(1, round(0.18 * sample_rate / HOP_SAMPLES))
        separated: list[int] = []
        for peak in peaks:
            if not separated or peak - separated[-1] >= min_gap:
                separated.append(peak)
        duration = signal.size / sample_rate
        syllable_rate = len(separated) / max(duration, 1e-6)
    else:
        syllable_rate = 0.0

    return AudioFeatures(
        voiced_ratio=voiced_ratio,
        stable_ratio=stable_ratio,
        longest_plateau_s=longest_plateau_s,
        pitch_range_st=pitch_range_st,
        median_pitch_step_st=median_pitch_step_st,
        harmonicity=harmonicity,
        spectral_flatness=float(np.median(flatness)) if flatness else 1.0,
        syllable_rate_hz=float(syllable_rate),
    )


def singing_probability(features: AudioFeatures) -> float:
    """Return the fitted singing probability from source-stored coefficients."""

    scaled = (features.as_array() - MODEL_MEAN) / MODEL_SCALE
    logit = MODEL_INTERCEPT + float(np.dot(MODEL_COEFFICIENTS, scaled))
    if logit >= 0:
        return 1.0 / (1.0 + math.exp(-min(logit, 60.0)))
    value = math.exp(max(logit, -60.0))
    return value / (1.0 + value)


class SingingDecisionPolicy:
    """Make one early decision and permit at most one evidence-based revision."""

    def __init__(self, threshold: float = SINGING_THRESHOLD) -> None:
        self.threshold = float(threshold)
        self.decision: AudioClassDecision | None = None
        self.revised = False

    def observe(
        self, score: float, elapsed_s: float, *, final: bool = False
    ) -> AudioClassDecision | None:
        score = min(1.0, max(0.0, float(score)))
        if self.decision is None:
            if elapsed_s >= EARLY_EVIDENCE_S and score >= self.threshold:
                self.decision = AudioClassDecision("singing", round(score, 4), 0)
            elif elapsed_s >= EARLY_DECISION_DEADLINE_S or final:
                self.decision = AudioClassDecision("speech", round(1.0 - score, 4), 0)
            else:
                return None
            return self.decision

        if self.revised or elapsed_s < REVISION_EVIDENCE_S:
            return None
        previous = self.decision
        next_class = "singing" if score >= self.threshold else "speech"
        if next_class == previous.audio_class:
            return None
        confidence = score if next_class == "singing" else 1.0 - score
        self.decision = AudioClassDecision(next_class, round(confidence, 4), 1)
        self.revised = True
        return self.decision


class StreamingSingingClassifier:
    """Per-segment bounded PCM history plus the early/revision decision policy."""

    def __init__(
        self,
        start_sample: int,
        *,
        initial_pcm: bytes = b"",
        initial_start_sample: int | None = None,
        threshold: float = SINGING_THRESHOLD,
    ) -> None:
        self.start_sample = start_sample
        audio_start_sample = start_sample if initial_start_sample is None else initial_start_sample
        self._audio = bytearray(initial_pcm)
        self._last_sample = audio_start_sample + len(self._audio) // 2
        self._policy = SingingDecisionPolicy(threshold)
        self._last_feature_samples = 0
        self.latest_score: float | None = None

    @property
    def elapsed_s(self) -> float:
        return max(0.0, (self._last_sample - self.start_sample) / SAMPLE_RATE)

    @property
    def decision(self) -> AudioClassDecision | None:
        return self._policy.decision

    def append(self, pcm: bytes, start_sample: int) -> AudioClassDecision | None:
        if len(pcm) % 2:
            raise ValueError("PCM byte length must be even")
        count = len(pcm) // 2
        end_sample = start_sample + count
        if end_sample <= self._last_sample:
            return None
        # The wire sample clock is continuous. If a caller nevertheless skips
        # a range, leave it absent instead of manufacturing samples.
        self._last_sample = max(self._last_sample, start_sample)
        overlap = max(0, self._last_sample - start_sample)
        self._audio.extend(pcm[overlap * 2 :])
        self._last_sample = end_sample
        max_samples = round(MAX_ANALYSIS_SECONDS * SAMPLE_RATE)
        if len(self._audio) // 2 > max_samples:
            trim_samples = len(self._audio) // 2 - max_samples
            del self._audio[: trim_samples * 2]
        return self._evaluate(final=False)

    def seed(self) -> AudioClassDecision | None:
        """Evaluate audio already present in the stream's bounded history."""

        return self._evaluate(final=False)

    def finish(self) -> AudioClassDecision | None:
        return self._evaluate(final=True)

    def _evaluate(self, *, final: bool) -> AudioClassDecision | None:
        samples = len(self._audio) // 2
        if samples < round(EARLY_EVIDENCE_S * SAMPLE_RATE) and not final:
            return None
        # Recompute only after a useful amount of new audio. Each analysis uses
        # at most 1.5 s, regardless of the segment's full duration.
        if not final and samples - self._last_feature_samples < round(0.18 * SAMPLE_RATE):
            return None
        max_window = round(EARLY_DECISION_DEADLINE_S * SAMPLE_RATE)
        if self._policy.decision is None:
            analysis = bytes(self._audio[: max_window * 2])
        else:
            window = min(samples, max_window)
            analysis = bytes(self._audio[-window * 2 :])
        features = extract_features(analysis)
        self.latest_score = singing_probability(features)
        self._last_feature_samples = samples
        return self._policy.observe(self.latest_score, self.elapsed_s, final=final)
