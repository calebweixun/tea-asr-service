"""Synthetic fixtures only: no real caption text or audio."""

from benchmarks.srt_gold import (
    build_answers,
    build_chunks,
    join_text,
    parse_srt,
    shift_cues,
    uncaptioned_spans,
)

SRT = (
    "﻿1\r\n00:00:01,000 --> 00:00:02,500\r\nalpha\r\n\r\n"
    "2\r\n00:00:02,500 --> 00:00:04,000\r\n  beta line one\r\n   second line  \r\n\r\n"
    "3\r\n00:00:10,000 --> 00:00:11,000\r\n\r\n\r\n"  # empty cue is dropped
    "4\r\n00:00:20,250 --> 00:00:21,000\r\nOK\r\n\r\n"
    "5\r\n00:00:21,000 --> 00:00:22,000\r\nthank you\r\n"
)


def test_parse_srt_handles_bom_crlf_multiline_and_empty_cues() -> None:
    cues = parse_srt(SRT)
    assert [c.index for c in cues] == [1, 2, 4, 5]
    assert cues[0].start_s == 1.0 and cues[0].end_s == 2.5
    assert cues[1].text == "beta line one second line"
    assert cues[2].start_s == 20.25
    assert cues[3].text == "thank you"


def test_parse_srt_accepts_dot_milliseconds_and_missing_index() -> None:
    cues = parse_srt("00:01:02.5 --> 00:01:03.25\n你好\n")
    assert cues[0].start_s == 62.5
    assert cues[0].end_s == 63.25
    assert cues[0].text == "你好"


def test_join_text_spaces_only_between_ascii_words() -> None:
    assert join_text(["你好", " 世界 "]) == "你好世界"
    assert join_text(["thank", "you"]) == "thank you"
    assert join_text(["Amen", "好"]) == "Amen好"


def _cues(spans: list[tuple[float, float]]):
    text = "".join(f"{i}\n00:00:00,000 --> 00:00:01,000\nx\n\n" for i in range(len(spans)))
    cues = parse_srt(text)
    return [type(c)(c.index, s, e, f"c{i}") for i, (c, (s, e)) in enumerate(zip(cues, spans, strict=True))]


def test_chunker_splits_on_gap_and_on_max_length() -> None:
    cues = _cues([(0, 2), (2.1, 4), (4.7, 6), (6, 8), (8, 10), (10, 12)])
    chunks = build_chunks(cues, max_chunk_s=7.0, split_gap_s=0.6)
    # gap 0.7 between cue 2 and 3 splits; 4.7..12 would be 7.3 s, over 7, so cue 5 starts a new chunk.
    assert [[c.index for c in chunk] for chunk in chunks] == [[0, 1], [2, 3, 4], [5]]
    assert all(chunk[-1].end_s - chunk[0].start_s <= 7.0 for chunk in chunks)


def test_chunker_keeps_overlong_single_cue_and_tolerates_overlap() -> None:
    cues = _cues([(0, 20), (19.5, 21)])
    chunks = build_chunks(cues, max_chunk_s=15.0, split_gap_s=0.6)
    assert [len(c) for c in chunks] == [1, 1]


def test_uncaptioned_spans_include_head_gap_and_tail() -> None:
    cues = _cues([(30, 32), (33, 35), (60, 61)])
    spans = uncaptioned_spans(cues, min_gap_s=20.0, audio_duration_s=100.0)
    assert [(s["start_s"], s["end_s"], s["edge"]) for s in spans] == [
        (0.0, 30.0, "head"),
        (35.0, 60.0, "gap"),
        (61.0, 100.0, "tail"),
    ]
    assert spans[0]["duration_s"] == 30.0


def test_build_answers_ids_references_and_padding_clamped_to_neighbours() -> None:
    cues = _cues([(1, 3), (3.1, 4), (5, 7)])
    answers = build_answers(cues, "svc", pad_s=0.5, audio_duration_s=7.2)
    items = answers["items"]
    assert [i["id"] for i in items] == ["svc-0001", "svc-0002"]
    assert items[0]["reference"] == "c0 c1"
    assert items[0]["start_s"] == 0.5
    # padding stops at the midpoint of the 1 s gap between the two chunks
    assert items[0]["end_s"] == 4.5 and items[1]["start_s"] == 4.5
    assert items[1]["end_s"] == 7.2
    assert items[0]["set"] == "svc" and items[0]["music"] is False


def test_shift_cues_applies_constant_offset_and_drops_negative() -> None:
    cues = _cues([(0, 1), (5, 6)])
    shifted = shift_cues(cues, -2.0)
    assert [(c.start_s, c.end_s) for c in shifted] == [(3.0, 4.0)]
