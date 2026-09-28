"""Pure metrics and threshold helpers for the real-audio live-subtitle soak."""

from __future__ import annotations

import itertools
import json
import math
import re
import statistics
from collections import Counter, defaultdict
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

SAMPLE_RATE = 16_000
TEXT_EVENTS = {"transcript.partial", "transcript.stable", "transcript.final"}
_FOLDS = {
    0x7232: 0x70BA,
    0x88CF: 0x88E1,
    0x9EBD: 0x9EBC,
    0x8846: 0x773E,
    0x7DAB: 0x7DDA,
    0x7FA3: 0x7FA4,
    0x5553: 0x555F,
    0x7740: 0x8457,
}


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def percentile(values: Iterable[float], fraction: float) -> float | None:
    ordered = sorted(values)
    if not ordered:
        return None
    index = min(len(ordered) - 1, max(0, math.ceil(len(ordered) * fraction) - 1))
    return round(ordered[index], 1)


def distribution(values: Iterable[float]) -> dict[str, float | int | None]:
    data = list(values)
    return {
        "n": len(data),
        "median": round(statistics.median(data), 1) if data else None,
        "p95": percentile(data, 0.95),
        "max": round(max(data), 1) if data else None,
    }


def _normalise_for_duplicate(text: str) -> tuple[int, ...]:
    out: list[int] = []
    for char in text:
        code = ord(char)
        if code <= 0x20 or code == 0x7F:
            continue
        if code < 0x80:
            if not ("0" <= char <= "9" or "a" <= char <= "z" or "A" <= char <= "Z"):
                continue
            code = ord(char.lower())
        elif (
            0x2000 <= code <= 0x206F
            or 0x3000 <= code <= 0x303F
            or 0xFE10 <= code <= 0xFE1F
            or 0xFE30 <= code <= 0xFE6F
            or 0xFF01 <= code <= 0xFF0F
            or 0xFF1A <= code <= 0xFF20
            or 0xFF3B <= code <= 0xFF40
            or 0xFF5B <= code <= 0xFF65
            or code in {0x00A0, 0x00B7}
        ):
            continue
        elif 0xFF21 <= code <= 0xFF3A:
            code = code - 0xFF21 + ord("a")
        elif 0xFF41 <= code <= 0xFF5A:
            code = code - 0xFF41 + ord("a")
        elif 0xFF10 <= code <= 0xFF19:
            code = code - 0xFF10 + ord("0")
        else:
            code = _FOLDS.get(code, code)
        out.append(code)
    return tuple(out)


def _non_overlapping_run_count(text: tuple[int, ...], run: tuple[int, ...]) -> int:
    count = 0
    index = 0
    width = len(run)
    while index + width <= len(text):
        if text[index : index + width] == run:
            count += 1
            index += width
        else:
            index += 1
    return count


def has_new_repeated_run(display: str, final: str, minimum: int = 4) -> bool:
    """Mirror the plugin replay guard: a repeated run absent from the final."""
    shown = _normalise_for_duplicate(display)
    reference = _normalise_for_duplicate(final)
    for start in range(max(0, len(shown) - minimum + 1)):
        run = shown[start : start + minimum]
        if _non_overlapping_run_count(shown, run) < 2:
            continue
        if _non_overlapping_run_count(reference, run) < _non_overlapping_run_count(shown, run):
            return True
    return False


def duplicate_segments_from_trace(rows: list[dict[str, Any]]) -> list[str]:
    """Find trace text snapshots with a repeated run missing from that segment's final."""
    finals: dict[str, str] = {}
    candidates: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        event = row.get("event", {})
        segment_id = event.get("segment_id")
        if not segment_id:
            continue
        kind = event.get("type")
        if kind == "transcript.final":
            finals[segment_id] = str(event.get("text", ""))
        elif kind in {"transcript.partial", "transcript.stable"}:
            candidates[segment_id].append(str(event.get("text", "")))
    return sorted(
        segment_id
        for segment_id, final in finals.items()
        if any(has_new_repeated_run(text, final) for text in candidates[segment_id])
    )


def _log_record(raw: dict[str, Any]) -> dict[str, Any]:
    fields = raw.get("fields")
    record = dict(fields) if isinstance(fields, dict) else {}
    record.update(
        {
            "ts": raw.get("ts") or raw.get("timestamp"),
            "level": raw.get("level"),
            "message": raw.get("message") or raw.get("event"),
        }
    )
    return record


def _epoch_seconds(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value) / 1000 if value > 100_000_000_000 else float(value)
    if not isinstance(value, str) or not value:
        return None
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.astimezone()
    return stamp.timestamp()


def _load_meta(trace_path: Path) -> dict[str, Any]:
    meta_path = trace_path.with_suffix(".meta.json")
    if not meta_path.exists():
        return {}
    value = json.loads(meta_path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else {}


def _load_sections(manifest: Path | dict[str, Any] | None, duration_s: float) -> list[dict[str, Any]]:
    if isinstance(manifest, Path):
        data = json.loads(manifest.read_text(encoding="utf-8"))
    else:
        data = manifest or {}
    sections = data.get("sections", []) if isinstance(data, dict) else []
    if not sections:
        return [{"name": "all", "start_s": 0.0, "end_s": duration_s}]
    return [
        {
            "name": str(section["name"]),
            "start_s": float(section["start_s"]),
            "end_s": float(section["end_s"]),
        }
        for section in sections
    ]


def _merge_intervals(intervals: list[tuple[float, float]]) -> list[tuple[float, float]]:
    merged: list[tuple[float, float]] = []
    for start, end in sorted(intervals):
        if end <= start:
            continue
        if merged and start <= merged[-1][1] + 100:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _longest_gap_without_text(
    intervals: list[tuple[float, float]], text_times: list[float]
) -> float:
    longest = 0.0
    for start, end in _merge_intervals(intervals):
        cuts = [start] + sorted(t for t in text_times if start < t < end) + [end]
        longest = max(longest, *(right - left for left, right in itertools.pairwise(cuts)))
    return round(longest, 1)


def _metric_scope(
    *,
    rows: list[dict[str, Any]],
    logs: list[dict[str, Any]],
    segment_ids: set[str],
    start_ms: float,
    end_ms: float,
    wall_t0_epoch_s: float,
    include_unscoped: bool,
) -> dict[str, Any]:
    selected = []
    for row in rows:
        event = row["event"]
        segment_id = event.get("segment_id")
        if segment_id in segment_ids or (
            not segment_id
            and (
                include_unscoped
                or start_ms <= float(row["t_ms"]) <= end_ms
            )
        ):
            selected.append(row)

    errors: Counter[str] = Counter()
    segments: dict[str, dict[str, Any]] = {}
    stable_states: Counter[str] = Counter()
    for index, row in enumerate(selected):
        event = row["event"]
        kind = event.get("type", "")
        segment_id = event.get("segment_id") or f"unscoped-{index}"
        segment = segments.setdefault(
            segment_id,
            {
                "stable_times": [],
                "partials": [],
                "speech_started_ms": None,
                "queued_ms": None,
                "start_sample": None,
                "end_sample": None,
                "final_ms": None,
                "has_final": False,
                "skipped": False,
                "error": False,
            },
        )
        if kind == "speech.started":
            segment["speech_started_ms"] = float(row["t_ms"])
            segment["start_sample"] = event.get("start_sample")
        elif kind == "segment.queued":
            segment["queued_ms"] = float(row["t_ms"])
            segment["start_sample"] = event.get("start_sample", segment["start_sample"])
            segment["end_sample"] = event.get("end_sample", segment["end_sample"])
        elif kind == "transcript.partial":
            segment["partials"].append((float(row["t_ms"]), str(event.get("text", ""))))
        elif kind == "transcript.stable":
            segment["stable_times"].append(float(row["t_ms"]))
            state = str(event.get("state", "unknown"))
            stable_states[state] += 1
        elif kind == "transcript.final":
            segment["final_ms"] = float(row["t_ms"])
            segment["has_final"] = True
            segment["start_sample"] = event.get("start_sample", segment["start_sample"])
            segment["end_sample"] = event.get("end_sample", segment["end_sample"])
        elif kind == "segment.skipped":
            segment["skipped"] = True
        elif kind == "segment.error":
            segment["error"] = True
            errors[str(event.get("code") or "unknown")] += 1
        elif kind == "error":
            errors[str(event.get("code") or "unknown")] += 1

    durations_s: list[float] = []
    first_partial_ms: list[float] = []
    stable_gaps_ms: list[float] = []
    final_latencies_ms: list[float] = []
    rewrite_count = 0
    rewrite_transitions = 0
    open_intervals: list[tuple[float, float]] = []
    for segment in segments.values():
        start_sample = segment["start_sample"]
        end_sample = segment["end_sample"]
        if start_sample is not None and end_sample is not None and end_sample >= start_sample:
            durations_s.append((int(end_sample) - int(start_sample)) / SAMPLE_RATE)
        speech_at = segment["speech_started_ms"]
        if speech_at is not None:
            if segment["partials"]:
                first_partial_ms.append(max(0.0, segment["partials"][0][0] - speech_at))
            close_at = segment["queued_ms"]
            if close_at is not None:
                open_intervals.append((speech_at, close_at))
        stable_times = sorted(segment["stable_times"])
        stable_gaps_ms.extend(b - a for a, b in itertools.pairwise(stable_times))
        if segment["has_final"] and segment["queued_ms"] is not None:
            final_latencies_ms.append(max(0.0, segment["final_ms"] - segment["queued_ms"]))
        partials = segment["partials"]
        for (_, previous), (_, current) in itertools.pairwise(partials):
            rewrite_transitions += 1
            if current != previous and not current.startswith(previous):
                rewrite_count += 1

    parsed_logs = [_log_record(row) for row in logs]
    heartbeat_windows: list[tuple[float, float, float | None, float | None, dict[str, Any]]] = []
    warning_counts: Counter[str] = Counter()
    server_errors: Counter[str] = Counter()
    for record in parsed_logs:
        epoch = _epoch_seconds(record.get("ts"))
        relative_ms = (epoch - wall_t0_epoch_s) * 1000 if epoch is not None else None
        record["relative_ms"] = relative_ms
        message = str(record.get("message") or "")
        level = str(record.get("level") or "").upper()
        if (
            level == "WARNING"
            and message.startswith("stream.")
            and (relative_ms is None or start_ms <= relative_ms <= end_ms)
        ):
            warning_counts[message] += 1
        if level == "ERROR" and (
            relative_ms is None or start_ms <= relative_ms <= end_ms
        ):
            code = record.get("code") or message
            if code:
                server_errors[str(code)] += 1
        if message == "stream.heartbeat":
            window_ms = float(record.get("window_ms") or 0)
            end = relative_ms
            start = end - window_ms if end is not None else None
            speech_fraction = record.get("vad_speech_frac")
            busy = record.get("worker_busy")
            heartbeat_windows.append(
                (
                    start if start is not None else float("nan"),
                    end if end is not None else float("nan"),
                    float(speech_fraction) if speech_fraction is not None else None,
                    float(busy) if busy is not None else None,
                    record,
                )
            )

    active_vad = [
        (start, end)
        for start, end, speech_fraction, _, _ in heartbeat_windows
        if math.isfinite(start) and math.isfinite(end) and speech_fraction is not None and speech_fraction > 0
    ]
    speech_source = "vad-heartbeat" if active_vad else "open-segments"
    activity = active_vad or open_intervals
    activity = [
        (max(start, start_ms), min(end, end_ms))
        for start, end in activity
        if end > start_ms and start < end_ms
    ]
    text_times = [
        float(row["t_ms"])
        for row in selected
        if row["event"].get("type") in TEXT_EVENTS
    ]
    longest_no_text_ms = _longest_gap_without_text(activity, text_times)

    busy_duration = 0.0
    busy_seconds = 0.0
    for start, end, _, busy, _ in heartbeat_windows:
        if busy is None or not (math.isfinite(start) and math.isfinite(end)):
            continue
        overlap = max(0.0, min(end, end_ms) - max(start, start_ms))
        busy_duration += overlap
        busy_seconds += overlap * min(1.0, max(0.0, busy))

    stable_closes = {
        "diverged": stable_states.get("diverged", 0),
        "abandoned": stable_states.get("abandoned", 0),
    }
    errors_by_code = dict(errors)
    for code, count in server_errors.items():
        errors_by_code[code] = max(errors_by_code.get(code, 0), count)
    transitions = rewrite_transitions
    return {
        "segments": {
            "count": len([s for key, s in segments.items() if not key.startswith("unscoped-")]),
            "queued": sum(s["queued_ms"] is not None for s in segments.values()),
            "final": sum(s["has_final"] for s in segments.values()),
            "skipped": sum(s["skipped"] for s in segments.values()),
            "errors": sum(s["error"] for s in segments.values()),
        },
        "segment_duration_s": distribution(durations_s),
        "speech_to_first_partial_ms": distribution(first_partial_ms),
        "stable_gap_ms": distribution(stable_gaps_ms),
        "final_latency_ms": distribution(final_latencies_ms),
        "longest_speech_no_text_ms": longest_no_text_ms,
        "speech_activity_source": speech_source,
        "partial_rewrite": {
            "rewrites": rewrite_count,
            "transitions": transitions,
            "rate": round(rewrite_count / transitions, 4) if transitions else 0.0,
        },
        "stable_closes": stable_closes,
        "worker_busy_fraction": round(busy_seconds / busy_duration, 4) if busy_duration else None,
        "errors_by_code": dict(sorted(errors_by_code.items())),
        "server_errors_by_code": dict(sorted(server_errors.items())),
        "warnings_by_message": dict(sorted(warning_counts.items())),
        "warning_count": sum(warning_counts.values()),
    }


def analyze_trace(
    trace_path: Path,
    manifest: Path | dict[str, Any] | None = None,
    server_log_path: Path | None = None,
) -> dict[str, Any]:
    rows = read_jsonl(trace_path)
    events = [row for row in rows if isinstance(row.get("event"), dict) and "t_ms" in row]
    events.sort(key=lambda row: float(row["t_ms"]))
    meta = _load_meta(trace_path)
    if manifest is None and meta.get("manifest"):
        manifest = Path(meta["manifest"])
    if server_log_path is None:
        candidate = meta.get("server_log_capture")
        if candidate:
            server_log_path = Path(candidate)
        else:
            candidate_path = trace_path.with_suffix(".server.jsonl")
            server_log_path = candidate_path if candidate_path.exists() else None
    logs = read_jsonl(server_log_path) if server_log_path and server_log_path.exists() else []
    session_id = next(
        (
            row["event"].get("session_id")
            for row in events
            if row["event"].get("type") == "session.started"
        ),
        None,
    )
    if session_id:
        logs = [
            row
            for row in logs
            if _log_record(row).get("session_id") == session_id
        ]
    sample_ends = [
        int(event.get("end_sample", 0))
        for row in events
        if (event := row["event"]).get("end_sample") is not None
    ]
    duration_s = float(meta.get("wav_duration_s") or (max(sample_ends, default=0) / SAMPLE_RATE))
    sections = _load_sections(manifest, duration_s)
    if isinstance(manifest, Path):
        manifest_data = json.loads(manifest.read_text(encoding="utf-8"))
    else:
        manifest_data = manifest or {}
    source_start_s = float(manifest_data.get("source_start_s", 0.0)) if isinstance(manifest_data, dict) else 0.0
    audio_t0_ms = float(meta.get("audio_t0_ms") or 0.0)

    segment_samples: dict[str, list[int]] = defaultdict(list)
    for row in events:
        event = row["event"]
        segment_id = event.get("segment_id")
        if not segment_id:
            continue
        for key in ("start_sample", "end_sample"):
            if event.get(key) is not None:
                segment_samples[segment_id].append(int(event[key]))

    section_results = []
    for section in sections:
        start_s, end_s = section["start_s"], section["end_s"]
        selected_segments = set()
        for segment_id, samples in segment_samples.items():
            midpoint_s = (min(samples) + max(samples)) / (2 * SAMPLE_RATE)
            if start_s <= midpoint_s < end_s or (
                section is sections[-1] and math.isclose(midpoint_s, end_s)
            ):
                selected_segments.add(segment_id)
        section_start_ms = audio_t0_ms + start_s * 1000
        section_end_ms = audio_t0_ms + end_s * 1000
        section_results.append(
            {
                **section,
                "metrics": _metric_scope(
                    rows=events,
                    logs=logs,
                    segment_ids=selected_segments,
                    start_ms=section_start_ms,
                    end_ms=section_end_ms,
                    wall_t0_epoch_s=float(meta.get("wall_t0_epoch_s") or 0.0),
                    include_unscoped=len(sections) == 1,
                ),
            }
        )
    overall = _metric_scope(
        rows=events,
        logs=logs,
        segment_ids=set(segment_samples),
        start_ms=audio_t0_ms,
        end_ms=audio_t0_ms + duration_s * 1000,
        wall_t0_epoch_s=float(meta.get("wall_t0_epoch_s") or 0.0),
        include_unscoped=True,
    )
    return {
        "trace": str(trace_path),
        "duration_s": round(duration_s, 2),
        "source_start_s": source_start_s,
        "sections": section_results,
        "overall": overall,
    }


def hard_failures(analysis: dict[str, Any], replay: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    scopes = [("overall", analysis.get("overall", {}), replay.get("overall", {}))]
    replay_sections = {item["name"]: item for item in replay.get("sections", [])}
    for section in analysis.get("sections", []):
        scopes.append(
            (
                section["name"],
                section.get("metrics", {}),
                replay_sections.get(section["name"], {}),
            )
        )
    for name, metrics, caption in scopes:
        errors = metrics.get("errors_by_code", {})
        if errors.get("invalid_ipc", 0):
            failures.append(f"{name}: invalid_ipc={errors['invalid_ipc']}")
        if caption.get("layout_moves", 0):
            failures.append(f"{name}: layout_moves={caption['layout_moves']}")
        if caption.get("mid_speech_fade_outs", 0):
            failures.append(f"{name}: mid_speech_fade_outs={caption['mid_speech_fade_outs']}")
        if caption.get("duplication_lines", 0):
            failures.append(f"{name}: duplication_lines={caption['duplication_lines']}")
        gap = metrics.get("longest_speech_no_text_ms", 0.0)
        if gap > 10_000:
            failures.append(f"{name}: speech_without_text_s={gap / 1000:.1f}")
    return failures


def soft_failures(analysis: dict[str, Any]) -> list[str]:
    failures = []
    overall = analysis.get("overall", {})
    stable_p95 = (overall.get("stable_gap_ms") or {}).get("p95")
    final_p95 = (overall.get("final_latency_ms") or {}).get("p95")
    if stable_p95 is not None and stable_p95 > 3000:
        failures.append(f"stable_gap_p95_ms={stable_p95:.1f} (limit 3000)")
    if final_p95 is not None and final_p95 > 1500:
        failures.append(f"final_latency_p95_ms={final_p95:.1f} (limit 1500)")
    return failures


def parse_replay_output(output: str) -> tuple[dict[str, Any], dict[int, dict[str, str]]]:
    metrics: dict[str, Any] = {
        "mid_speech_fade_outs": 0,
        "row_limit_losses": 0,
        "tail_retractions": 0,
        "tail_rewrites": 0,
        "layout_moves": 0,
        "largest_burst": 0,
        "duplication_lines": 0,
    }
    closes: dict[int, dict[str, str]] = {}
    summary = re.search(
        r"# summary: open-line shrink events: fade-out=(\d+) row-limit=(\d+) "
        r"retract\(tail hidden/shortened\)=(\d+) tail-rewrite=(\d+).*?largest burst=(\d+)",
        output,
    )
    if summary:
        metrics.update(
            {
                "mid_speech_fade_outs": int(summary.group(1)),
                "row_limit_losses": int(summary.group(2)),
                "tail_retractions": int(summary.group(3)),
                "tail_rewrites": int(summary.group(4)),
                "largest_burst": int(summary.group(5)),
            }
        )
    rows = re.search(r"# rows:.*?layout moves=(\d+)", output)
    if rows:
        metrics["layout_moves"] = int(rows.group(1))
    duplication = re.search(r"# duplication:.*?=(\d+)", output)
    if duplication:
        metrics["duplication_lines"] = int(duplication.group(1))
    for line in output.splitlines():
        match = re.search(
            r"CLOSE transcript\.final\s+seg\s+(\d+).*?: shown \[(.*)\] server \[.*\] -> now \[(.*)\]",
            line,
        )
        if match:
            closes[int(match.group(1))] = {
                "shown_before_close": match.group(2),
                "screen_at_close": match.group(3),
            }
    return metrics, closes
