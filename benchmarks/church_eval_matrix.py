"""Run the offline accuracy matrix on the church services (decode step).

A variant label is ``m{4|8}-{base|c<L>[g<G>]}[-repl][-prompt]``:

* ``m4``/``m8``: the 4-bit snapshot the service ships / ``models/mlx-8bit-selfconv``;
* ``base``: segment decode; ``c3`` = carry 3 s with max gap 1.5 s; ``c3g5`` = max gap 5 s;
* ``-repl``: apply the dictionary's deterministic replacements;
* ``-prompt``: also send the dictionary domain and hotwords to the model (needs ``-repl``'s
  dictionary, so it implies it).

Decoding is resumable: an existing ``<out>/<label>/<service>.json`` is kept.  The services
run as parallel processes (one model copy each).  Scoring is ``church_eval_report.py``.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SERVICE_ROOT = Path("/Users/c2leb/Codes/tea-asr-service")
DEFAULT_WORK = SERVICE_ROOT / ".soak/church-eval/work"
MODELS = {
    "4": SERVICE_ROOT
    / "models/models--Alkd--TEA-ASR-1.1-MLX-4bit/snapshots/caee57a908b6d64be08a6462c7a21ececbd4d7cb",
    "8": SERVICE_ROOT / "models/mlx-8bit-selfconv",
}
LABEL_RE = re.compile(r"^m(?P<bits>[48])-(?P<mode>base|c(?P<carry>\d+(?:\.\d+)?)(?:g(?P<gap>\d+(?:\.\d+)?))?)(?P<repl>-repl)?(?P<prompt>-prompt)?$")


def parse_label(label: str) -> dict:
    match = LABEL_RE.match(label)
    if match is None:
        raise ValueError(f"bad variant label {label!r}")
    carry = match.group("carry")
    return {
        "bits": match.group("bits"),
        "carry_s": float(carry) if carry else None,
        "gap_s": float(match.group("gap") or 1.5) if carry else None,
        "repl": bool(match.group("repl") or match.group("prompt")),
        "prompt": bool(match.group("prompt")),
    }


def decode_command(label: str, service: str, work: Path, dictionary: Path | None) -> list[str]:
    spec = parse_label(label)
    out_dir = work / "eval" / label
    command = [
        sys.executable, str(HERE / "gold_offline_eval.py"),
        "--answers", str(work / "answers" / f"{service}-pad25.json"),
        "--wav", str(work / f"{service}.wav"),
        "--model-path", str(MODELS[spec["bits"]]),
        "--output", str(out_dir / f"{service}.json"),
        "--timings", str(out_dir / f"{service}.timings.json"),
    ]
    if spec["carry_s"] is not None:
        command += [
            "--mode", "carry", "--carry-s", str(spec["carry_s"]),
            "--carry-max-gap-s", str(spec["gap_s"]),
        ]
    if spec["repl"]:
        if dictionary is None:
            raise ValueError("--dictionary is required for -repl/-prompt variants")
        command += ["--dictionary", str(dictionary)]
    if spec["prompt"]:
        command.append("--prompt")
    return command


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("labels", nargs="+")
    parser.add_argument("--services", nargs="+", default=["20260704", "20260822", "20260912"])
    parser.add_argument("--work", type=Path, default=DEFAULT_WORK)
    parser.add_argument("--dictionary", type=Path, help="church.toml copy for -repl/-prompt")
    args = parser.parse_args(argv)
    failures = 0
    for label in args.labels:
        running: list[tuple[str, subprocess.Popen]] = []
        for service in args.services:
            if (args.work / "eval" / label / f"{service}.json").exists():
                print(f"skip {label} {service} (exists)", file=sys.stderr)
                continue
            command = decode_command(label, service, args.work, args.dictionary)
            print(f"decode {label} {service}", file=sys.stderr, flush=True)
            running.append(
                (service, subprocess.Popen(command, stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL))
            )
        for service, process in running:
            if process.wait() != 0:
                print(f"FAILED {label} {service}", file=sys.stderr)
                failures += 1
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
