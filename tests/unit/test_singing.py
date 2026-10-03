from __future__ import annotations

import dataclasses

import numpy as np
import pytest

from tea_asr.singing import (
    DEFAULT_PARAMS,
    DEFAULT_SCORER,
    ClassGroups,
    ClassMapError,
    FrameScorer,
    SegmentLabeler,
    SingingParams,
    SingingTracker,
    design_matrix,
    frame_features,
)
from tea_asr.yamnet import HOP_SAMPLES, NUM_CLASSES, PATCH_SAMPLES, frame_end_sample
from tests.conftest import FAKE_MUSIC, FAKE_SINGING, FAKE_SPEECH, fake_class_names

SING = (0.0, 0.9, 0.1)  # speech, music, vocal: a worship band with vocals
SPEECH = (0.95, 0.0, 0.0)
SPEECH_OVER_BAND = (0.9, 0.7, 0.01)
INSTRUMENTAL_PAUSE = (0.0, 0.9, 0.005)


def feats(*rows: tuple[float, float, float]) -> np.ndarray:
    return np.asarray(rows, dtype=np.float64)


def repeated(row: tuple[float, float, float], count: int) -> np.ndarray:
    return feats(*([row] * count))


# -- class groups and features -------------------------------------------------


def test_class_groups_resolve_by_name_and_refuse_guessing() -> None:
    names = fake_class_names()
    groups = ClassGroups.from_names(names)
    assert FAKE_SPEECH in groups.speech and groups.music == (FAKE_MUSIC,)
    assert FAKE_SINGING in groups.vocal
    broken = list(names)
    broken[groups.music[0]] = "Renamed"
    with pytest.raises(ClassMapError, match="Music"):
        ClassGroups.from_names(broken)
    with pytest.raises(ClassMapError, match="521"):
        ClassGroups.from_names(names[:10])


def test_frame_features_take_the_strongest_class_in_each_group() -> None:
    groups = ClassGroups.from_names(fake_class_names())
    scores = np.zeros((2, NUM_CLASSES), dtype=np.float32)
    scores[0, groups.speech[1]] = 0.7  # Narration beats a silent Speech
    scores[0, groups.vocal[3]] = 0.2
    scores[1, groups.music[0]] = 0.5
    out = frame_features(scores, groups)
    assert out.shape == (2, 3)
    assert out[0].tolist() == pytest.approx([0.7, 0.0, 0.2])
    assert out[1].tolist() == pytest.approx([0.0, 0.5, 0.0])
    with pytest.raises(ValueError):
        frame_features(np.zeros((2, 5)), groups)


def test_design_matrix_is_finite_for_saturated_scores() -> None:
    out = design_matrix(feats((0.0, 1.0, 0.5)))
    assert np.isfinite(out).all()
    assert out[0, 2] == pytest.approx(0.0)


# -- score mapping ------------------------------------------------------------------


def test_scorer_maps_evidence_in_the_documented_direction() -> None:
    score = DEFAULT_SCORER.score(feats(SING, SPEECH, SPEECH_OVER_BAND, INSTRUMENTAL_PAUSE))
    sing, speech, over_band, instrumental = score
    assert sing > 0.95
    assert speech < 0.01
    assert over_band < 0.1  # a talker over a band is not singing
    assert instrumental < sing  # without vocal evidence it is less singing than singing


def test_scorer_is_monotone_in_each_feature() -> None:
    base = (0.2, 0.5, 0.05)
    for index, expected_sign in ((0, -1), (2, 1)):
        low = list(base)
        high = list(base)
        low[index] = 0.01
        high[index] = 0.9
        delta = float(np.diff(DEFAULT_SCORER.score(feats(tuple(low), tuple(high))))[0])
        assert delta * expected_sign > 0


def test_scorer_output_is_a_probability_even_for_extreme_weights() -> None:
    scorer = FrameScorer((-50.0, 50.0, 50.0), 100.0)
    out = scorer.score(feats(SING, SPEECH))
    assert ((out >= 0) & (out <= 1)).all() and np.isfinite(out).all()


# -- tracker hysteresis ---------------------------------------------------------------


def tracker(**overrides: object) -> SingingTracker:
    params = dataclasses.replace(DEFAULT_PARAMS, **overrides)  # type: ignore[arg-type]
    return SingingTracker(DEFAULT_SCORER, params, history_frames=1000)


def test_a_single_clearly_singing_frame_does_not_switch_the_state_on() -> None:
    t = tracker()
    t.observe(repeated(SING, DEFAULT_PARAMS.enter_frames - 1))
    assert not t.singing
    t.observe(repeated(SING, 1))
    assert t.singing


def test_enter_needs_the_whole_window_not_just_a_good_average() -> None:
    t = tracker()
    t.observe(repeated(SING, 3))
    t.observe(repeated(SPEECH, 1))
    t.observe(repeated(SING, DEFAULT_PARAMS.enter_frames))  # window refills
    assert t.singing
    u = tracker()
    for _ in range(20):  # alternating never reaches a 0.98 average
        u.observe(repeated(SING, 1))
        u.observe(repeated(SPEECH, 1))
    assert not u.singing


def test_speech_over_a_band_never_enters_singing() -> None:
    t = tracker()
    t.observe(repeated(SPEECH_OVER_BAND, 100))
    assert not t.singing
    t.observe(repeated(INSTRUMENTAL_PAUSE, 3))
    assert not t.singing


def test_state_stays_on_through_short_dips_and_leaves_after_sustained_speech() -> None:
    t = tracker()
    t.observe(repeated(SING, DEFAULT_PARAMS.enter_frames))
    assert t.singing
    t.observe(repeated(SPEECH, 1))  # a spoken "amen" inside the song
    t.observe(repeated(SING, 1))
    assert t.singing
    t.observe(repeated(SPEECH, DEFAULT_PARAMS.exit_frames))
    assert not t.singing


def test_state_history_is_addressable_by_frame_count() -> None:
    t = tracker()
    t.observe(repeated(SPEECH, 4))
    t.observe(repeated(SING, DEFAULT_PARAMS.enter_frames))
    assert t.state_after(0) is False
    assert t.state_after(4) is False
    assert t.state_after(4 + DEFAULT_PARAMS.enter_frames) is True
    with pytest.raises(ValueError, match="not processed"):
        t.state_after(t.frames_seen + 1)


def test_history_is_bounded_and_trimmed_frames_are_refused() -> None:
    t = SingingTracker(DEFAULT_SCORER, DEFAULT_PARAMS, history_frames=16)
    t.observe(repeated(SING, 40))
    assert t.state_after(40) is True
    with pytest.raises(ValueError, match="trimmed"):
        t.state_after(5)


def test_evaluate_requires_state_and_local_agreement() -> None:
    t = tracker()
    t.observe(repeated(SING, 10))
    assert t.singing
    assert t.evaluate(frame_end_sample(9)).audio_class == "singing"
    # Speech takes over; the hysteresis state lags (mean of 4) but the local
    # check on the last two frames already says no.
    t.observe(repeated(SPEECH, 2))
    assert t.singing
    assert t.evaluate(frame_end_sample(11)).audio_class == "speech"
    # An earlier sample is still judged by what was known then.
    assert t.evaluate(frame_end_sample(9)).audio_class == "singing"


def test_evaluate_before_any_frame_is_speech() -> None:
    decision = tracker().evaluate(PATCH_SAMPLES - 1)
    assert decision.audio_class == "speech" and decision.revision == 0


def test_ready_tracks_complete_frames() -> None:
    t = tracker()
    assert t.ready(PATCH_SAMPLES - 1)
    assert not t.ready(PATCH_SAMPLES)
    t.observe(repeated(SING, 1))
    assert t.ready(PATCH_SAMPLES)
    assert not t.ready(PATCH_SAMPLES + HOP_SAMPLES)


# -- segment decision policy -----------------------------------------------------------

#: A segment opening 10 s into the stream: its early point (11.5 s) has 22
#: complete frames behind it, its revision point (14 s) has 28.
START = 160_000


def singing_tracker(frames: int = 40) -> SingingTracker:
    t = tracker()
    t.observe(repeated(SING, frames))
    return t


def test_early_decision_comes_at_about_one_and_a_half_seconds() -> None:
    labeler = SegmentLabeler(singing_tracker(), start_sample=START)
    assert labeler.early_sample == START + 24_000
    assert labeler.update(labeler.early_sample - 1) is None
    decision = labeler.update(labeler.early_sample)
    assert decision is not None
    assert (decision.audio_class, decision.revision) == ("singing", 0)
    assert 0.0 <= decision.confidence <= 1.0


def test_the_first_seconds_of_a_stream_cannot_be_singing_yet() -> None:
    # Precision first: 8 frames (about 4 s) of evidence are required, so a
    # segment at the very start of the stream is labelled speech at 1.5 s.
    labeler = SegmentLabeler(singing_tracker(), start_sample=0)
    decision = labeler.update(labeler.early_sample)
    assert decision is not None and decision.audio_class == "speech"


def test_decision_does_not_depend_on_when_it_is_asked() -> None:
    early = SegmentLabeler(singing_tracker(), start_sample=START)
    late = SegmentLabeler(singing_tracker(), start_sample=START)
    on_time = early.update(early.early_sample)
    # The call arrives 3 s late (slow executor); the label is still the one
    # computed from the evidence at the due sample.
    delayed = late.update(late.early_sample + 48_000)
    assert on_time == delayed


def test_a_decision_waits_for_frames_that_have_not_been_scored() -> None:
    t = tracker()
    t.observe(repeated(SING, 2))
    labeler = SegmentLabeler(t, start_sample=START)
    assert labeler.update(10**6) is None  # frames up to the due sample are not in yet
    t.observe(repeated(SING, 40))
    assert labeler.update(10**6) is not None


def test_one_revision_at_most_and_only_when_the_label_changes() -> None:
    t = tracker()
    t.observe(repeated(SPEECH, 16))
    t.observe(repeated(SING, 6))  # the song starts, but too recently to enter
    labeler = SegmentLabeler(t, start_sample=START)
    first = labeler.update(labeler.early_sample)
    assert first is not None and first.audio_class == "speech"
    t.observe(repeated(SING, 20))
    revision = labeler.update(labeler.revision_sample)
    assert revision is not None
    assert (revision.audio_class, revision.revision) == ("singing", 1)
    t.observe(repeated(SPEECH, 30))
    assert labeler.update(10**7) is None  # no second revision
    assert labeler.finished


def test_confirming_the_revision_point_sends_nothing() -> None:
    labeler = SegmentLabeler(singing_tracker(), start_sample=START)
    labeler.update(labeler.early_sample)
    assert labeler.update(labeler.revision_sample) is None
    assert labeler.finished and labeler.decision is not None
    assert labeler.decision.revision == 0


def test_short_segment_is_decided_at_close() -> None:
    labeler = SegmentLabeler(singing_tracker(), start_sample=START)
    close = START + 10_000
    assert labeler.update(close) is None
    decision = labeler.update(close, closing=True)
    assert decision is not None and decision.audio_class == "singing"
    # Close also settles the revision slot with the evidence at the close sample.
    assert labeler.update(close, closing=True) is None
    assert labeler.finished


def test_closing_before_the_revision_point_can_revise_once() -> None:
    t = tracker()
    t.observe(repeated(SPEECH, 16))
    t.observe(repeated(SING, 6))
    labeler = SegmentLabeler(t, start_sample=START)
    first = labeler.update(labeler.early_sample)
    assert first is not None and first.audio_class == "speech"
    t.observe(repeated(SING, 20))
    revision = labeler.update(START + 40_000, closing=True)
    assert revision is not None and revision.audio_class == "singing"
    assert revision.revision == 1


def test_speech_segment_after_a_song_is_not_hidden() -> None:
    t = singing_tracker(30)
    t.observe(repeated(SPEECH_OVER_BAND, 16))  # the pastor starts over the band
    start = frame_end_sample(29)
    labeler = SegmentLabeler(t, start_sample=start)
    decision = labeler.update(labeler.early_sample)
    assert decision is not None and decision.audio_class == "speech"


def test_params_are_overridable_and_frozen() -> None:
    loose = SingingParams(enter_frames=2, enter_threshold=0.5)
    t = SingingTracker(DEFAULT_SCORER, loose)
    t.observe(repeated(SING, 2))
    assert t.singing
    with pytest.raises(dataclasses.FrozenInstanceError):
        loose.enter_frames = 5  # type: ignore[misc]
