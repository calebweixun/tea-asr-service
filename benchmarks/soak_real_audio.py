"""Extract, capture, replay and report a private real-audio subtitle soak."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import subprocess
import sys
import tempfile
import wave
from collections import defaultdict
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SOAK_ROOT = ROOT / ".soak"
DEFAULT_AUDIO = Path(
    "/Users/c2leb/Downloads/【天使即將出道】FIGHT.K Cloud Church _ 20260926_128k.m4a"
)
DEFAULT_PLUGIN = Path("/Users/c2leb/Codes/obs-plugins/tea-live-subtitle")
FFMPEG = Path("/opt/homebrew/bin/ffmpeg")
REPLAY_FLAGS = (
    "--fade-delay-ms",
    "1500",
    "--fade-ms",
    "200",
    "--max-rows",
    "2",
    "--max-lines",
    "3",
    "--width",
    "1800",
    "--punct",
    "comma",
    "--comma-min",
    "8",
)

sys.path.insert(0, str(HERE))

import capture_event_trace
from soak_metrics import (
    analyze_trace,
    hard_failures,
    parse_replay_output,
    read_jsonl,
    soft_failures,
)


def _soak_path(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    try:
        resolved.relative_to(SOAK_ROOT.resolve())
    except ValueError as exc:
        raise ValueError(f"derived output must stay under {SOAK_ROOT}") from exc
    return resolved


def _time_seconds(value: str) -> float:
    try:
        if ":" not in value:
            return float(value)
        pieces = [float(part) for part in value.split(":")]
        if len(pieces) == 2:
            minutes, seconds = pieces
            return minutes * 60 + seconds
        if len(pieces) == 3:
            hours, minutes, seconds = pieces
            return hours * 3600 + minutes * 60 + seconds
    except ValueError as exc:
        raise ValueError(f"invalid timestamp: {value}") from exc
    raise ValueError(f"invalid timestamp: {value}; use seconds, MM:SS or HH:MM:SS")


def _format_time(seconds: float) -> str:
    return f"{seconds:.3f}".rstrip("0").rstrip(".")


def _parse_sections(values: list[str], duration_s: float) -> list[dict[str, Any]]:
    if values:
        sections: list[dict[str, Any]] = []
        for value in values:
            parts = value.split(",", maxsplit=2)
            if len(parts) != 3:
                raise ValueError(f"section must be NAME,START,END, got {value!r}")
            name, start, end = parts
            start_s = _time_seconds(start)
            end_s = duration_s if end.lower() == "end" else _time_seconds(end)
            sections.append({"name": name.strip(), "start_s": start_s, "end_s": end_s})
    else:
        sections = [
            {"name": "young_woman", "start_s": 0.0, "end_s": min(120.0, duration_s)},
            {"name": "elderly_woman", "start_s": 120.0, "end_s": min(180.0, duration_s)},
            {"name": "man", "start_s": 180.0, "end_s": duration_s},
        ]
        sections = [section for section in sections if section["end_s"] > section["start_s"]]
    previous_end = 0.0
    for section in sections:
        if section["start_s"] < 0 or section["end_s"] <= section["start_s"]:
            raise ValueError(f"invalid section range for {section['name']}")
        if section["start_s"] < previous_end:
            raise ValueError("sections must be ordered and cannot overlap")
        if section["end_s"] > duration_s + 0.05:
            raise ValueError(f"section {section['name']} extends past the extracted WAV")
        previous_end = section["end_s"]
    return sections


def extract(args: argparse.Namespace) -> int:
    source = args.input.expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    start_s, end_s = _time_seconds(args.start), _time_seconds(args.end)
    if start_s < 0 or end_s <= start_s:
        raise ValueError("--end must be later than --start")
    output = _soak_path(args.out or SOAK_ROOT / "audio" / "church-30m-59m.wav")
    manifest_path = _soak_path(args.manifest or output.with_suffix(".sections.json"))
    output.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        str(FFMPEG),
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        _format_time(start_s),
        "-i",
        str(source),
        "-t",
        _format_time(end_s - start_s),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        str(output),
    ]
    subprocess.run(command, check=True)
    with wave.open(str(output), "rb") as wav:
        if (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) != (16000, 1, 2):
            raise ValueError("ffmpeg did not produce 16 kHz mono PCM16")
        duration_s = wav.getnframes() / wav.getframerate()
    sections = _parse_sections(args.section, duration_s)
    manifest = {
        "source_start_s": start_s,
        "source_end_s": end_s,
        "duration_s": round(duration_s, 3),
        "sections": sections,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"WAV: {output} ({duration_s:.1f}s); sections: {manifest_path}")
    return 0


def _session_id(trace_path: Path) -> str | None:
    for row in read_jsonl(trace_path):
        event = row.get("event", {})
        if event.get("type") == "session.started":
            return event.get("session_id")
    return None


def _server_log_snapshot(path: Path) -> tuple[int | None, int]:
    try:
        stat = path.stat()
        return stat.st_ino, stat.st_size
    except FileNotFoundError:
        return None, 0


def _capture_session_logs(
    path: Path, before: tuple[int | None, int], session_id: str, trace_path: Path
) -> Path:
    inode, size = before
    current_inode = path.stat().st_ino
    offset = size if inode is not None and inode == current_inode else 0
    with path.open("rb") as source:
        source.seek(offset)
        data = source.read().decode("utf-8", errors="replace")
    matched = []
    for line in data.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        fields = value.get("fields") if isinstance(value, dict) else None
        candidate = value.get("session_id") if isinstance(value, dict) else None
        if not candidate and isinstance(fields, dict):
            candidate = fields.get("session_id")
        if candidate == session_id:
            matched.append(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
    sidecar = _soak_path(trace_path.with_suffix(".server.jsonl"))
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text("\n".join(matched) + ("\n" if matched else ""), encoding="utf-8")
    return sidecar


def capture(args: argparse.Namespace) -> int:
    wav_path = args.wav.expanduser().resolve()
    output = _soak_path(args.out)
    manifest_path = _soak_path(args.manifest) if args.manifest else None
    server_log = args.server_log.expanduser().resolve() if args.server_log else None
    log_before = _server_log_snapshot(server_log) if server_log else None
    common = argparse.Namespace(
        wav=wav_path,
        token=getattr(args, "token", None),
        token_file=getattr(args, "token_file", None),
        url=args.url,
        out=output,
        agreement=2,
        end_silence_ms=args.end_silence,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    status = asyncio.run(capture_event_trace.capture(common))
    if status != 0:
        return status
    meta_path = output.with_suffix(".meta.json")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["manifest"] = str(manifest_path) if manifest_path else None
    if server_log is not None and log_before is not None:
        session_id = _session_id(output)
        if not session_id:
            return 1
        sidecar = _capture_session_logs(server_log, log_before, session_id, output)
        meta["server_log_source"] = str(server_log)
        meta["server_log_capture"] = str(sidecar)
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if server_log is not None:
        print(f"Session log lines: {sidecar}")
    return 0


def _build_replay(plugin_dir: Path, build_dir: Path) -> Path:
    plugin_dir = plugin_dir.expanduser().resolve()
    build_dir = _soak_path(build_dir)
    if not (plugin_dir / "tests/replay/README.md").is_file():
        raise FileNotFoundError(f"plugin replay README not found under {plugin_dir}")
    build_dir.mkdir(parents=True, exist_ok=True)
    object_file = build_dir / "caption-state.o"
    binary = build_dir / "caption-replay"
    commands = [
        [
            "cc",
            "-std=c11",
            "-c",
            "-Itests/stubs",
            "-Isrc",
            "src/caption-state.c",
            "-o",
            str(object_file),
        ],
        [
            "c++",
            "-std=c++17",
            "-Itests/stubs",
            "-Isrc",
            "tests/replay/caption-replay.cpp",
            str(object_file),
            "-o",
            str(binary),
        ],
    ]
    for command in commands:
        subprocess.run(command, cwd=plugin_dir, check=True)
    return binary


def _trace_sections(trace_path: Path, manifest: Path | None) -> list[dict[str, Any]]:
    meta_path = trace_path.with_suffix(".meta.json")
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    if manifest is None and meta.get("manifest"):
        manifest = Path(meta["manifest"])
    if manifest and manifest.is_file():
        data = json.loads(manifest.read_text(encoding="utf-8"))
        sections = data.get("sections", [])
    else:
        duration = float(meta.get("wav_duration_s", 0.0))
        sections = [{"name": "all", "start_s": 0.0, "end_s": duration}]
    return [
        {"name": str(section["name"]), "start_s": float(section["start_s"]), "end_s": float(section["end_s"])}
        for section in sections
    ]


def _section_trace_rows(
    rows: list[dict[str, Any]], section: dict[str, Any]
) -> list[dict[str, Any]]:
    samples: dict[str, list[int]] = defaultdict(list)
    for row in rows:
        event = row.get("event", {})
        segment_id = event.get("segment_id")
        if segment_id:
            for key in ("start_sample", "end_sample"):
                if event.get(key) is not None:
                    samples[segment_id].append(int(event[key]))
    start_sample = section["start_s"] * 16000
    end_sample = section["end_s"] * 16000
    segment_ids = set()
    for segment_id, values in samples.items():
        midpoint = (min(values) + max(values)) / 2
        if start_sample <= midpoint < end_sample:
            segment_ids.add(segment_id)
    return [
        row
        for row in rows
        if row.get("event", {}).get("type") == "session.started"
        or row.get("event", {}).get("segment_id") in segment_ids
    ]


def _run_replay(binary: Path, trace_path: Path) -> tuple[dict[str, Any], dict[int, dict[str, str]]]:
    command = [str(binary), str(trace_path), *REPLAY_FLAGS, "--quiet"]
    result = subprocess.run(command, check=True, capture_output=True, text=True, errors="replace")
    return parse_replay_output(result.stdout)


def run_replay(
    trace_path: Path,
    manifest: Path | None = None,
    plugin_dir: Path = DEFAULT_PLUGIN,
    build_dir: Path = SOAK_ROOT / "build",
    *,
    binary: Path | None = None,
    only_overall: bool = False,
) -> tuple[dict[str, Any], dict[int, dict[str, str]]]:
    trace_path = trace_path.expanduser().resolve()
    rows = read_jsonl(trace_path)
    replay_binary = binary or _build_replay(plugin_dir, build_dir)
    overall, closes = _run_replay(replay_binary, trace_path)
    result: dict[str, Any] = {
        "trace": str(trace_path),
        "trace_size_bytes": trace_path.stat().st_size,
        "trace_mtime_ns": trace_path.stat().st_mtime_ns,
        "overall": overall,
        "sections": [],
    }
    if only_overall:
        return result, closes
    for index, section in enumerate(_trace_sections(trace_path, manifest)):
        section_rows = _section_trace_rows(rows, section)
        if not section_rows or not any(
            row.get("event", {}).get("type") == "transcript.final" for row in section_rows
        ):
            metrics = {
                "mid_speech_fade_outs": 0,
                "row_limit_losses": 0,
                "tail_retractions": 0,
                "tail_rewrites": 0,
                "layout_moves": 0,
                "largest_burst": 0,
                "duplication_lines": 0,
            }
        else:
            with tempfile.TemporaryDirectory(prefix="soak-replay-", dir=SOAK_ROOT) as temp_dir:
                section_path = Path(temp_dir) / f"section-{index}.jsonl"
                section_path.write_text(
                    "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in section_rows),
                    encoding="utf-8",
                )
                metrics, _ = _run_replay(replay_binary, section_path)
        result["sections"].append({**section, **metrics})
    return result, closes


def _same_trace(saved: dict[str, Any], trace_path: Path) -> bool:
    return (
        saved.get("trace_size_bytes") == trace_path.stat().st_size
        and saved.get("trace_mtime_ns") == trace_path.stat().st_mtime_ns
    )


def _result_path(trace_path: Path, suffix: str) -> Path:
    digest = hashlib.sha256(str(trace_path.resolve()).encode("utf-8")).hexdigest()[:10]
    return SOAK_ROOT / "reports" / f"{trace_path.stem}-{digest}.{suffix}"


def _save_json(path: Path, value: dict[str, Any]) -> None:
    path = _soak_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _analysis_and_replay(
    trace_path: Path,
    manifest: Path | None,
    server_log: Path | None,
    plugin_dir: Path,
    build_dir: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    analysis = analyze_trace(trace_path, manifest, server_log)
    replay_path = _result_path(trace_path, "replay.json")
    replay_data: dict[str, Any] = {}
    if replay_path.exists():
        loaded = json.loads(replay_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict) and _same_trace(loaded, trace_path):
            replay_data = loaded
    if not replay_data:
        replay_data, _ = run_replay(trace_path, manifest, plugin_dir, build_dir)
        _save_json(replay_path, replay_data)
    analysis["hard_failures"] = hard_failures(analysis, replay_data)
    analysis["soft_failures"] = soft_failures(analysis)
    analysis["trace_size_bytes"] = trace_path.stat().st_size
    analysis["trace_mtime_ns"] = trace_path.stat().st_mtime_ns
    _save_json(_result_path(trace_path, "analysis.json"), analysis)
    return analysis, replay_data


def _metric_table_rows(analysis: dict[str, Any], replay: dict[str, Any]) -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    replay_sections = {item["name"]: item for item in replay.get("sections", [])}
    rows = [("overall", analysis["overall"], replay.get("overall", {}))]
    rows.extend(
        (
            section["name"],
            section["metrics"],
            replay_sections.get(section["name"], {}),
        )
        for section in analysis.get("sections", [])
    )
    return rows


def _seconds(value: Any) -> str:
    return "—" if value is None else f"{float(value) / 1000:.2f}"


def _dist(value: dict[str, Any], unit_scale: float = 1000.0) -> str:
    return "/".join("—" if value.get(key) is None else f"{value[key] / unit_scale:.2f}" for key in ("median", "p95", "max"))


def write_metric_report(
    report_path: Path,
    analysis: dict[str, Any],
    replay: dict[str, Any],
) -> None:
    failures = analysis.get("hard_failures", [])
    hard_scopes = {failure.split(":", 1)[0] for failure in failures}
    lines = [
        "# Real-audio live-subtitle soak metrics",
        "",
        f"Audio duration: {analysis['duration_s']:.1f}s; source offset: {analysis['source_start_s']:.1f}s.",
        "",
        "Times in the distribution columns are seconds (median / p95 / max). Worker busy is a fraction.",
        "",
        "| Scope | Segments q/f/s/e | Duration s | Speech to first partial s | Stable gap s | Final latency p95 s | Longest speech without text s | Partial rewrites | Stable diverged/abandoned | Worker busy | Warnings | Fade outs | Row losses | Tail retract/rewrite | Layout moves | Burst | Duplications | Status |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    row_data = _metric_table_rows(analysis, replay)
    for scope, metrics, caption in row_data:
        segments = metrics.get("segments", {})
        partial = metrics.get("partial_rewrite", {})
        closes = metrics.get("stable_closes", {})
        busy = metrics.get("worker_busy_fraction")
        counts = "/".join(str(segments.get(key, 0)) for key in ("queued", "final", "skipped", "errors"))
        rewrites = f"{partial.get('rewrites', 0)}/{partial.get('transitions', 0)} ({partial.get('rate', 0.0):.0%})"
        stable_closes = f"{closes.get('diverged', 0)}/{closes.get('abandoned', 0)}"
        tail = f"{caption.get('tail_retractions', 0)}/{caption.get('tail_rewrites', 0)}"
        status = "FAIL" if scope in hard_scopes else "PASS"
        lines.append(
            "| {scope} | {counts} | {duration} | {first} | {stable} | {final} | {quiet} | "
            "{rewrites} | {closes} | {busy} | {warnings} | {fade} | {rows} | {tail} | "
            "{moves} | {burst} | {dups} | {status} |".format(
                scope=scope,
                counts=counts,
                duration=_dist(metrics.get("segment_duration_s", {}), 1.0),
                first=_dist(metrics.get("speech_to_first_partial_ms", {})),
                stable=_dist(metrics.get("stable_gap_ms", {})),
                final=_seconds((metrics.get("final_latency_ms") or {}).get("p95")),
                quiet=_seconds(metrics.get("longest_speech_no_text_ms")),
                rewrites=rewrites,
                closes=stable_closes,
                busy="—" if busy is None else f"{busy:.2f}",
                warnings=metrics.get("warning_count", 0),
                fade=caption.get("mid_speech_fade_outs", 0),
                rows=caption.get("row_limit_losses", 0),
                tail=tail,
                moves=caption.get("layout_moves", 0),
                burst=caption.get("largest_burst", 0),
                dups=caption.get("duplication_lines", 0),
                status=status,
            )
        )
    lines.extend(["", "## Error codes and stream warnings", "", "| Scope | Errors by code | Stream warnings by message |", "|---|---|---|"])
    for scope, metrics, _ in row_data:
        lines.append(
            f"| {scope} | `{json.dumps(metrics.get('errors_by_code', {}), sort_keys=True)}` "
            f"| `{json.dumps(metrics.get('warnings_by_message', {}), sort_keys=True)}` |"
        )
    lines.extend(["", "## Hard thresholds", "", "| Scope | invalid_ipc=0 | layout moves=0 | mid-speech fades=0 | duplication lines=0 | no-text stretch ≤10s | Status |", "|---|---|---|---|---|---|---|"])
    for scope, metrics, caption in row_data:
        checks = [
            metrics.get("errors_by_code", {}).get("invalid_ipc", 0) == 0,
            caption.get("layout_moves", 0) == 0,
            caption.get("mid_speech_fade_outs", 0) == 0,
            caption.get("duplication_lines", 0) == 0,
            metrics.get("longest_speech_no_text_ms", 0) <= 10_000,
        ]
        state = "PASS" if all(checks) else "FAIL"
        lines.append(f"| {scope} | " + " | ".join("PASS" if ok else "FAIL" for ok in checks) + f" | {state} |")
    lines.extend(["", "## Soft thresholds", "", "| Metric | Observed p95 | Threshold | Status |", "|---|---:|---:|---|"])
    overall = analysis.get("overall", {})
    stable_p95 = (overall.get("stable_gap_ms") or {}).get("p95")
    final_p95 = (overall.get("final_latency_ms") or {}).get("p95")
    lines.append(f"| Stable commit gap | {_seconds(stable_p95)} | 3.00s | {'FAIL' if stable_p95 is not None and stable_p95 > 3000 else 'PASS'} |")
    lines.append(f"| Final latency after close | {_seconds(final_p95)} | 1.50s | {'FAIL' if final_p95 is not None and final_p95 > 1500 else 'PASS'} |")
    if analysis.get("hard_failures"):
        lines.extend(["", "Hard failures: " + "; ".join(analysis["hard_failures"])])
    if analysis.get("soft_failures"):
        lines.extend(["", "Soft failures: " + "; ".join(analysis["soft_failures"])])
    report_path = _soak_path(report_path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _markdown_cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def write_review(
    review_path: Path,
    trace_path: Path,
    manifest: Path | None,
    closes: dict[int, dict[str, str]],
) -> None:
    rows = read_jsonl(trace_path)
    meta_path = trace_path.with_suffix(".meta.json")
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    if manifest is None and meta.get("manifest"):
        manifest = Path(meta["manifest"])
    manifest_data = json.loads(manifest.read_text(encoding="utf-8")) if manifest and manifest.is_file() else {}
    source_start_s = float(manifest_data.get("source_start_s", 0.0))
    finals = []
    for row in rows:
        event = row.get("event", {})
        if event.get("type") != "transcript.final":
            continue
        sample = int(event.get("end_sample", event.get("start_sample", 0)))
        source_s = source_start_s + sample / 16000
        minute_start = int(source_s // 60) * 60
        index = int(event.get("segment_index", -1))
        close = closes.get(index, {})
        finals.append(
            (
                minute_start,
                source_s,
                index,
                str(event.get("text", "")),
                close.get("screen_at_close", ""),
            )
        )
    finals.sort(key=lambda item: item[1])
    lines = [
        "# Local subtitle review",
        "",
        "Private transcript excerpts for eyeballing only. This file belongs under `.soak/` and must not be committed.",
        "",
    ]
    grouped: dict[int, list[tuple[int, float, int, str, str]]] = defaultdict(list)
    for item in finals:
        grouped[item[0]].append(item)
    for minute in sorted(grouped):
        lines.extend(
            [
                f"## Recording minute {minute // 60:02d}:{minute % 60:02d}",
                "",
                "| Close time | Segment | Final | Screen at close |",
                "|---:|---:|---|---|",
            ]
        )
        for _, source_s, index, final, screen in grouped[minute]:
            clock = f"{int(source_s // 60):02d}:{source_s % 60:05.2f}"
            lines.append(
                f"| {clock} | {index} | {_markdown_cell(final)} | {_markdown_cell(screen)} |"
            )
        lines.append("")
    review_path = _soak_path(review_path)
    review_path.parent.mkdir(parents=True, exist_ok=True)
    review_path.write_text("\n".join(lines), encoding="utf-8")


def _cmd_analyze(args: argparse.Namespace) -> int:
    trace_path = args.trace.expanduser().resolve()
    manifest = args.manifest.expanduser().resolve() if args.manifest else None
    server_log = args.server_log.expanduser().resolve() if args.server_log else None
    analysis, replay = _analysis_and_replay(
        trace_path, manifest, server_log, args.plugin_dir, args.build_dir
    )
    print(
        json.dumps(
            {
                "overall": analysis["overall"],
                "hard_failures": analysis["hard_failures"],
                "soft_failures": analysis["soft_failures"],
                "replay": replay["overall"],
            },
            indent=2,
        )
    )
    return 1 if analysis["hard_failures"] else 0


def _cmd_replay(args: argparse.Namespace) -> int:
    trace_path = args.trace.expanduser().resolve()
    manifest = args.manifest.expanduser().resolve() if args.manifest else None
    metrics, _ = run_replay(trace_path, manifest, args.plugin_dir, args.build_dir)
    _save_json(_result_path(trace_path, "replay.json"), metrics)
    print(json.dumps({"overall": metrics["overall"], "sections": metrics["sections"]}, indent=2))
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    trace_path = args.trace.expanduser().resolve()
    manifest = args.manifest.expanduser().resolve() if args.manifest else None
    server_log = args.server_log.expanduser().resolve() if args.server_log else None
    analysis, replay = _analysis_and_replay(
        trace_path, manifest, server_log, args.plugin_dir, args.build_dir
    )
    _, closes = run_replay(
        trace_path,
        manifest,
        args.plugin_dir,
        args.build_dir,
        only_overall=True,
    )
    report_path = args.out or _result_path(trace_path, "md")
    review_path = args.review or SOAK_ROOT / f"review-{_result_path(trace_path, 'md').stem}.md"
    write_metric_report(report_path, analysis, replay)
    write_review(review_path, trace_path, manifest, closes)
    print(f"Metrics report: {_soak_path(report_path)}")
    print(f"Local review: {_soak_path(review_path)}")
    return 1 if analysis["hard_failures"] else 0


def _add_trace_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("trace", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--server-log", type=Path)
    parser.add_argument("--plugin-dir", type=Path, default=DEFAULT_PLUGIN)
    parser.add_argument("--build-dir", type=Path, default=SOAK_ROOT / "build")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)

    cut = commands.add_parser("extract", help="cut and normalize the private m4a to PCM16 WAV")
    cut.add_argument("--input", type=Path, default=DEFAULT_AUDIO)
    cut.add_argument("--start", default="00:30:00")
    cut.add_argument("--end", default="00:59:09")
    cut.add_argument("--out", type=Path)
    cut.add_argument("--manifest", type=Path)
    cut.add_argument("--section", action="append", default=[], metavar="NAME,START,END")
    cut.set_defaults(handler=extract)

    cap = commands.add_parser("capture", help="stream a WAV at 1x into a live service")
    cap.add_argument("--wav", type=Path, required=True)
    cap.add_argument("--url", default="ws://127.0.0.1:8421/v1/stream")
    token = cap.add_mutually_exclusive_group(required=True)
    token.add_argument("--token")
    token.add_argument("--token-file", type=Path)
    cap.add_argument("--end-silence", "--end-silence-ms", dest="end_silence", type=int, default=600)
    cap.add_argument("--server-log", type=Path)
    cap.add_argument("--manifest", type=Path)
    cap.add_argument("--out", type=Path, required=True)
    cap.set_defaults(handler=capture)

    analyze_parser = commands.add_parser("analyze", help="compute trace, server and replay metrics")
    _add_trace_options(analyze_parser)
    analyze_parser.set_defaults(handler=_cmd_analyze)

    replay_parser = commands.add_parser("replay", help="build and run the plugin caption replay tool")
    _add_trace_options(replay_parser)
    replay_parser.set_defaults(handler=_cmd_replay)

    report_parser = commands.add_parser("report", help="write metrics report and private transcript review")
    _add_trace_options(report_parser)
    report_parser.add_argument("--out", type=Path)
    report_parser.add_argument("--review", type=Path)
    report_parser.set_defaults(handler=_cmd_report)

    args = parser.parse_args()
    try:
        return args.handler(args)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"soak: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
