"""Replay saved real-audio traces and enforce the live-subtitle hard thresholds."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SOAK_ROOT = ROOT / ".soak"
PLUGIN_DIR = Path("/Users/c2leb/Codes/obs-plugins/tea-live-subtitle")
sys.path.insert(0, str(HERE))

from soak_metrics import analyze_trace, hard_failures, soft_failures
from soak_real_audio import _build_replay, run_replay


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--trace-dir",
        type=Path,
        default=Path(os.environ.get("TEA_SOAK_TRACES", SOAK_ROOT / "traces")),
    )
    parser.add_argument("--plugin-dir", type=Path, default=PLUGIN_DIR)
    parser.add_argument("--build-dir", type=Path, default=SOAK_ROOT / "build")
    args = parser.parse_args()
    trace_dir = args.trace_dir.expanduser().resolve()
    traces = sorted(
        path for path in trace_dir.glob("*.jsonl") if not path.name.endswith(".server.jsonl")
    )
    if not traces:
        print(f"no JSONL traces found in {trace_dir}", file=sys.stderr)
        return 2
    SOAK_ROOT.mkdir(parents=True, exist_ok=True)
    try:
        binary = _build_replay(args.plugin_dir, args.build_dir)
        failures: list[tuple[Path, list[str]]] = []
        soft_warnings: list[tuple[Path, list[str]]] = []
        print("| Trace | Segments | Layout moves | Fades | Duplications | Longest no-text (s) | Result |")
        print("|---|---:|---:|---:|---:|---:|---|")
        for trace in traces:
            analysis = analyze_trace(trace)
            replay, _ = run_replay(trace, plugin_dir=args.plugin_dir, build_dir=args.build_dir, binary=binary)
            trace_failures = hard_failures(analysis, replay)
            trace_soft = soft_failures(analysis)
            if trace_failures:
                failures.append((trace, trace_failures))
            if trace_soft:
                soft_warnings.append((trace, trace_soft))
            overall = analysis["overall"]
            caption = replay["overall"]
            status = "FAIL" if trace_failures else "PASS"
            print(
                f"| {trace.name} | {overall['segments']['queued']} | {caption['layout_moves']} | "
                f"{caption['mid_speech_fade_outs']} | {caption['duplication_lines']} | "
                f"{overall['longest_speech_no_text_ms'] / 1000:.2f} | {status} |"
            )
        for trace, items in failures:
            print(f"{trace.name}: " + "; ".join(items), file=sys.stderr)
        for trace, items in soft_warnings:
            print(f"soft threshold: {trace.name}: " + "; ".join(items))
        return 1 if failures else 0
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"replay regression: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
