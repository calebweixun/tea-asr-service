"""Singing detection on top of YAMNet frame scores.

Pipeline (pure and deterministic, so the offline evaluation in
``benchmarks/singing_eval.py`` replays exactly what the server runs):

1. ``frame_features``: 521 YAMNet class scores per 0.48 s frame are reduced to
   three numbers: the strongest speech-type class, ``Music``, and the strongest
   vocal-music class (Singing, Choir, Song, Christian music, ...).
2. ``FrameScorer``: a three-weight logistic over the log-odds of those numbers
   gives a per-frame singing probability.
3. ``SingingTracker``: session-level hysteresis over the recent frames. Worship
   songs last minutes, so one clearly singing stretch switches the state on and
   it only switches off after sustained non-singing evidence. Precision is the
   priority, so entering needs a long run of clearly singing frames.
4. ``SegmentLabeler``: labels one VAD segment ``speech`` or ``singing`` about
   1.5 s after it opened, with at most one revision. A segment is singing only
   if the session state is singing *and* the frames right at the segment agree,
   so a pastor speaking over a band is not hidden because a song just ended.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

import numpy as np

from .yamnet import NUM_CLASSES, SAMPLE_RATE, complete_frames

AudioClass = Literal["speech", "singing"]

#: Class groups, by AudioSet display name (resolved against the pinned class map).
SPEECH_CLASSES = (
    "Speech",
    "Narration, monologue",
    "Conversation",
    "Child speech, kid speaking",
)
MUSIC_CLASSES = ("Music",)
VOCAL_CLASSES = (
    "Singing",
    "Choir",
    "Chant",
    "Vocal music",
    "A capella",
    "Song",
    "Christian music",
    "Gospel music",
)

FEATURE_NAMES = ("speech", "music", "vocal")
_LOGIT_CLIP = 1e-4


class ClassMapError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ClassGroups:
    speech: tuple[int, ...]
    music: tuple[int, ...]
    vocal: tuple[int, ...]

    @classmethod
    def from_names(cls, names: Sequence[str]) -> ClassGroups:
        if len(names) != NUM_CLASSES:
            raise ClassMapError(f"expected {NUM_CLASSES} class names, got {len(names)}")
        lookup = {name: index for index, name in enumerate(names)}
        resolved: list[tuple[int, ...]] = []
        for group in (SPEECH_CLASSES, MUSIC_CLASSES, VOCAL_CLASSES):
            missing = [name for name in group if name not in lookup]
            if missing:
                raise ClassMapError(f"class map lacks {missing}; refusing to guess indices")
            resolved.append(tuple(lookup[name] for name in group))
        return cls(*resolved)


def frame_features(scores: np.ndarray, groups: ClassGroups) -> np.ndarray:
    """``[frames, 521]`` YAMNet scores to ``[frames, 3]`` (speech, music, vocal)."""

    scores = np.asarray(scores, dtype=np.float64)
    if scores.ndim != 2 or scores.shape[1] != NUM_CLASSES:
        raise ValueError(f"scores must be [frames, {NUM_CLASSES}]")
    return np.stack(
        [
            scores[:, list(groups.speech)].max(axis=1),
            scores[:, list(groups.music)].max(axis=1),
            scores[:, list(groups.vocal)].max(axis=1),
        ],
        axis=1,
    )


def design_matrix(features: np.ndarray) -> np.ndarray:
    """Log-odds of each feature, clipped so a saturated score stays finite."""

    clipped = np.clip(np.asarray(features, dtype=np.float64), _LOGIT_CLIP, 1.0 - _LOGIT_CLIP)
    return np.log(clipped / (1.0 - clipped))


@dataclass(frozen=True, slots=True)
class FrameScorer:
    """Logistic regression over the logit of (speech, music, vocal)."""

    coefficients: tuple[float, float, float]
    intercept: float

    def score(self, features: np.ndarray) -> np.ndarray:
        features = np.asarray(features, dtype=np.float64).reshape(-1, 3)
        logits = design_matrix(features) @ np.asarray(self.coefficients) + self.intercept
        return 1.0 / (1.0 + np.exp(-np.clip(logits, -40.0, 40.0)))


# Fitted offline on every labelled frame by ``benchmarks/singing_eval.py --fit``.
# Speech evidence pulls the score down, vocal-music evidence pushes it up, and
# ``Music`` alone barely matters: a speaker over a band stays speech. The
# held-out numbers are in docs/benchmarks/singing-eval-report.md.
DEFAULT_SCORER = FrameScorer(coefficients=(-0.6882, -0.1611, 0.9478), intercept=2.2381)


@dataclass(frozen=True, slots=True)
class SingingParams:
    """Hysteresis and decision knobs (see the evaluation report for the sweep)."""

    #: Enter singing when the mean score of the last ``enter_frames`` frames is
    #: at least ``enter_threshold`` (8 frames span about 4.2 s of audio). The
    #: precision-first selection on all labelled files picked this setting.
    enter_frames: int = 8
    enter_threshold: float = 0.98
    #: Leave singing when the mean of the last ``exit_frames`` frames drops
    #: below ``exit_threshold``.
    exit_frames: int = 4
    exit_threshold: float = 0.4
    #: The frames ending at the decision must also look singing: the mean of
    #: the last ``local_frames`` frames is at least ``local_threshold``.
    local_frames: int = 2
    local_threshold: float = 0.5
    #: Early decision about 1.5 s after the segment's first sample; the single
    #: revision is evaluated 4 s after it, or at close for shorter segments.
    early_decision_s: float = 1.5
    revision_decision_s: float = 4.0


DEFAULT_PARAMS = SingingParams()


@dataclass(frozen=True, slots=True)
class AudioClassDecision:
    audio_class: AudioClass
    confidence: float
    revision: int


class SingingTracker:
    """Per-session frame history and hysteresis state.

    Frames are fed in order. Frame ``i`` ends at a fixed sample, so a decision
    at sample ``t`` uses exactly ``complete_frames(t)`` frames however late the
    executor delivered them.
    """

    def __init__(
        self,
        scorer: FrameScorer = DEFAULT_SCORER,
        params: SingingParams = DEFAULT_PARAMS,
        *,
        history_frames: int = 512,
    ) -> None:
        self._scorer = scorer
        self._params = params
        self._scores: deque[float] = deque(maxlen=history_frames)
        self._states: deque[bool] = deque(maxlen=history_frames)
        self.frames_seen = 0
        self.singing = False
        #: Probability of the newest frame, for diagnostics only.
        self.last_score: float | None = None

    @property
    def params(self) -> SingingParams:
        return self._params

    def observe(self, features: np.ndarray) -> None:
        """Add ``[n, 3]`` features for the next ``n`` frames."""

        for probability in self._scorer.score(features):
            self._step(float(probability))

    def _step(self, probability: float) -> None:
        params = self._params
        self._scores.append(probability)
        self.last_score = probability
        self.frames_seen += 1
        if not self.singing:
            if self.frames_seen >= params.enter_frames and (
                self._recent_mean(params.enter_frames) >= params.enter_threshold
            ):
                self.singing = True
        elif self._recent_mean(params.exit_frames) < params.exit_threshold:
            self.singing = False
        self._states.append(self.singing)

    def _recent_mean(self, count: int) -> float:
        count = min(count, len(self._scores))
        return sum(self._scores[-k] for k in range(1, count + 1)) / count

    def _offset(self, frame_count: int, history: int) -> int:
        offset = self.frames_seen - frame_count
        if offset < 0:
            raise ValueError("frames not processed yet")
        if offset >= history:
            raise ValueError("frame history already trimmed")
        return offset

    def state_after(self, frame_count: int) -> bool:
        """Hysteresis state once ``frame_count`` frames were processed."""

        if frame_count <= 0:
            return False
        offset = self._offset(frame_count, len(self._states))
        return self._states[len(self._states) - 1 - offset]

    def local_score(self, frame_count: int) -> float:
        """Mean score of the ``local_frames`` frames ending at ``frame_count``."""

        if frame_count <= 0:
            return 0.0
        end = len(self._scores) - self._offset(frame_count, len(self._scores))
        start = max(0, end - self._params.local_frames)
        return sum(self._scores[i] for i in range(start, end)) / (end - start)

    def ready(self, sample: int) -> bool:
        """True when every frame completed by ``sample`` has been observed."""

        return self.frames_seen >= complete_frames(sample)

    def evaluate(self, sample: int) -> AudioClassDecision:
        """Label with the evidence of the frames complete at stream ``sample``."""

        count = complete_frames(sample)
        score = self.local_score(count)
        singing = self.state_after(count) and score >= self._params.local_threshold
        if singing:
            return AudioClassDecision("singing", round(min(1.0, score), 4), 0)
        return AudioClassDecision("speech", round(min(1.0, 1.0 - score), 4), 0)


class SegmentLabeler:
    """One early label per segment, at most one revision.

    Drive it with the current stream sample whenever audio or frames advance;
    each decision is computed *at its due sample*, not at the call time, so a
    slow executor delays the event but never changes the label.
    """

    def __init__(self, tracker: SingingTracker, start_sample: int) -> None:
        self._tracker = tracker
        self.start_sample = start_sample
        params = tracker.params
        self.early_sample = start_sample + round(params.early_decision_s * SAMPLE_RATE)
        self.revision_sample = start_sample + round(params.revision_decision_s * SAMPLE_RATE)
        self.decision: AudioClassDecision | None = None
        self.revised = False

    @property
    def finished(self) -> bool:
        return self.revised

    def update(self, stream_sample: int, *, closing: bool = False) -> AudioClassDecision | None:
        """Return a decision to emit (first label or the single revision).

        ``closing`` evaluates at the segment end when the next due point has not
        been reached yet. Returns ``None`` when nothing is due or when the
        frames required are not processed yet; call again after they arrive.
        """

        if self.revised:
            return None
        due = self.early_sample if self.decision is None else self.revision_sample
        at = stream_sample if closing and stream_sample < due else due
        if stream_sample < at or not self._tracker.ready(at):
            return None
        candidate = self._tracker.evaluate(at)
        if self.decision is None:
            self.decision = candidate
            return candidate
        if candidate.audio_class == self.decision.audio_class:
            self.revised = True  # evidence confirmed the label; nothing to send
            return None
        self.revised = True
        self.decision = AudioClassDecision(candidate.audio_class, candidate.confidence, 1)
        return self.decision
