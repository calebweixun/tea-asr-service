from benchmarks.gold_kit import (
    ClipSpec,
    Section,
    group_finals,
    make_items,
)


def final(start_s: float, end_s: float, text: str = "字") -> dict[str, object]:
    return {
        "type": "transcript.final",
        "start_sample": round(start_s * 16_000),
        "end_sample": round(end_s * 16_000),
        "text": text,
    }


def test_group_finals_merges_short_neighbors_and_keeps_long_finals() -> None:
    events = [
        final(0.0, 0.8, "甲"),
        final(1.0, 2.3, "乙"),
        final(2.8, 5.2, "丙"),
        final(6.0, 6.7, "丁"),
        final(7.0, 8.0, "戊"),
        final(11.0, 12.0, "己"),
        final(15.0, 16.0, "庚"),
    ]

    groups = group_finals(events)

    assert [[event["text"] for event in group] for group in groups] == [
        ["甲", "乙"],
        ["丙"],
        ["丁", "戊"],
        ["己"],
        ["庚"],
    ]


def test_group_finals_respects_group_duration_and_silence_gap() -> None:
    events = [
        final(0, 1.8, "甲"),
        final(2, 3.8, "乙"),
        final(4, 5.8, "丙"),
        final(12, 13.8, "丁"),
    ]

    groups = group_finals(events, max_group_seconds=5, max_gap_seconds=2)

    assert [[event["text"] for event in group] for group in groups] == [
        ["甲", "乙"],
        ["丙"],
        ["丁"],
    ]


def test_make_items_assigns_speaker_sections_and_keeps_unpadded_times() -> None:
    spec = ClipSpec(
        name="speakers",
        trace_name="unused.jsonl",
        wav_name="unused.wav",
        sections=(
            Section("first", "第一位", 0, 10),
            Section("second", "第二位", 10, 20),
        ),
        music=False,
    )

    items = make_items([final(2, 3, "甲"), final(11, 13, "乙")], spec, 30)

    assert len(items) == 2
    assert items[0].start_s == 2
    assert items[0].end_s == 3
    assert items[0].section == "第一位"
    assert items[1].section == "第二位"
    assert items[0].audio_path == "audio/speakers-0001.m4a"
