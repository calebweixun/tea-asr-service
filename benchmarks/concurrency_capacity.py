"""量測「同時開幾個 continuous session」在單一 MLX worker 下還撐得住。

    uv run tea-asr serve
    uv run python benchmarks/concurrency_capacity.py --wav benchmarks/generated/taiwan.wav \\
        --max-n 4 --seconds 90

docs/wire.py 的 `CapabilityLimits.max_continuous_sessions` 一直是文件推導出來的
「1」，從未被量測，`/v1/stream` 也從未真正拒絕過超額連線（見
src/tea_asr/api/app.py 的 `/v1/stream`：只有 `activity.sessions += 1`，之後沒有
任何拒絕邏輯）。

worker/supervisor.py 只有一個 MLX 子行程、一把 asyncio.Lock，所以推論永遠是
序列化的；「並行上限」量的其實是：N 個 continuous session 同時把音訊灌進
scheduler（realtime 優先權），加上每隔幾秒一次的 HTTP interactive 請求
（interactive 優先權高於 realtime，interactive 優先權高於 preview），在
這個單一 worker 上還能不能：
  1. 不讓任何 session 的「已送出音訊 - 已定稿音訊」(backlog) 持續成長；
  2. 端到端延遲 (speech end -> transcript.final 抵達) 的 p95 還在可接受範圍；
  3. HTTP interactive 請求不被 realtime 流量餓死（golden path：docs/03 的
     優先權設計就是為了保證這件事）；
  4. RSS 沒有隨 N 不受控成長；
  5. 辨識結果的段落數/內容在不同 N 下一致（同一份音訊、同一個 worker，
     序列化推論理論上不會因為並行 session 數而改變任何一段的辨識結果，
     只會改變它要等多久才被處理）。

為了在合理時間內量測，同一段錄音會被迴圈播放到 --seconds 秒，並以真實時間
（real-time pacing）送出，這樣 backlog 成長趨勢才有意義（累積佇列，而不是
一次性倒完就結束）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request
import wave
from dataclasses import dataclass, field
from pathlib import Path

from websockets.asyncio.client import connect

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tea_asr.config import AppPaths

FRAME_BYTES = 3200
HTTP_PROBE_INTERVAL_S = 4.0


def read_wav(path: Path) -> bytes:
    with wave.open(str(path), "rb") as wav:
        if (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) != (16_000, 1, 2):
            raise ValueError("WAV 必須是 16 kHz mono PCM16")
        return wav.readframes(wav.getnframes())


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(len(ordered) * fraction))
    return round(ordered[index], 3)


def rss_mb() -> dict[str, float]:
    out = subprocess.run(
        ["ps", "-Ao", "rss,command"], capture_output=True, text=True, check=False
    ).stdout
    totals: dict[str, float] = {}
    for line in out.splitlines():
        if ("tea_asr" in line or "tea-asr serve" in line) and "grep" not in line:
            parts = line.split(None, 1)
            if len(parts) != 2:
                continue
            key = "worker" if "worker.entry" in parts[1] else "service"
            totals[key] = totals.get(key, 0.0) + int(parts[0]) / 1024
    return totals


def http_transcribe(base: str, token: str, pcm: bytes) -> tuple[int, float]:
    request = urllib.request.Request(
        f"{base}/v1/transcriptions?sample_rate=16000&channels=1&format=pcm_s16le",
        data=pcm,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/octet-stream",
        },
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=60) as response:  # fixed localhost URL
            response.read()
            return response.status, time.monotonic() - started
    except urllib.error.HTTPError as exc:
        exc.read()
        return exc.code, time.monotonic() - started


@dataclass
class SessionResult:
    index: int
    finals: int = 0
    errors: list[str] = field(default_factory=list)
    lag_samples: list[float] = field(default_factory=list)
    backlog_series: list[tuple[float, float]] = field(default_factory=list)  # (t, backlog_s)


async def run_continuous_session(
    index: int, ws_url: str, token: str, pcm: bytes, seconds: float
) -> SessionResult:
    result = SessionResult(index=index)
    finalized_samples = 0
    sample = 0

    async with connect(
        ws_url, additional_headers={"Authorization": f"Bearer {token}"}, max_queue=256
    ) as socket:
        await socket.recv()  # hello
        await socket.send(
            json.dumps(
                {
                    "type": "session.start",
                    "request_id": f"cap-{index}",
                    "profile": "continuous",
                    "audio": {"sample_rate": 16_000, "channels": 1, "format": "pcm_s16le"},
                    "language": "Chinese",
                    "durable": False,
                    "transcript_mode": "final_only",
                }
            )
        )
        started = json.loads(await socket.recv())
        if started.get("type") != "session.started":
            result.errors.append(f"start_failed:{started}")
            return result
        window = started["send_until_sample"]
        clock_origin = time.monotonic()

        async def receive() -> None:
            nonlocal finalized_samples
            async for message in socket:
                event = json.loads(message)
                kind = event["type"]
                if kind == "flow.control":
                    nonlocal_window(event["send_until_sample"])
                elif kind == "transcript.final":
                    finalized_samples = max(finalized_samples, event["end_sample"])
                    result.finals += 1
                    spoken_at = clock_origin + event["end_sample"] / 16_000
                    result.lag_samples.append(time.monotonic() - spoken_at)
                elif kind in {"segment.error", "error"}:
                    result.errors.append(f"{kind}:{event.get('code')}")
                    if kind == "error":
                        return
                elif kind == "session.stopped":
                    return

        def nonlocal_window(value: int) -> None:
            nonlocal window
            window = max(window, value)

        receiver = asyncio.create_task(receive())
        deadline = clock_origin + seconds
        seq = 0
        next_sample_report = clock_origin + 10
        try:
            while time.monotonic() < deadline:
                for offset in range(0, len(pcm), FRAME_BYTES):
                    now = time.monotonic()
                    if now >= deadline:
                        break
                    chunk = pcm[offset : offset + FRAME_BYTES]
                    end = sample + len(chunk) // 2
                    while end > window and time.monotonic() < deadline:
                        await asyncio.sleep(0.02)
                    await socket.send(struct.pack("<QQ", seq, sample) + chunk)
                    seq += 1
                    sample = end
                    ahead = (clock_origin + sample / 16_000) - time.monotonic()
                    if ahead > 0:
                        await asyncio.sleep(ahead)
                    if time.monotonic() >= next_sample_report:
                        next_sample_report += 10
                        backlog_s = (sample - finalized_samples) / 16_000
                        result.backlog_series.append(
                            (round(time.monotonic() - clock_origin, 1), round(backlog_s, 2))
                        )
        finally:
            try:
                await socket.send(
                    json.dumps(
                        {
                            "type": "session.stop",
                            "request_id": f"cap-{index}-stop",
                            "through_seq": seq - 1 if seq else None,
                        }
                    )
                )
                await asyncio.wait_for(receiver, timeout=60)
            except Exception as exc:  # noqa: BLE001 - best-effort drain, report is what matters
                result.errors.append(f"drain_failed:{type(exc).__name__}")
    return result


async def run_trial(
    n: int, ws_url: str, http_base: str, pcm: bytes, seconds: float, token: str
) -> dict[str, object]:
    probe = pcm[: 5 * 16_000 * 2]
    http_results: list[tuple[int, float]] = []
    stop_probe = asyncio.Event()

    async def probe_loop() -> None:
        loop = asyncio.get_running_loop()
        while not stop_probe.is_set():
            await asyncio.sleep(HTTP_PROBE_INTERVAL_S)
            if stop_probe.is_set():
                break
            status, elapsed = await loop.run_in_executor(
                None, http_transcribe, http_base, token, probe
            )
            http_results.append((status, elapsed))

    probe_task = asyncio.create_task(probe_loop())
    sessions = await asyncio.gather(
        *(run_continuous_session(i, ws_url, token, pcm, seconds) for i in range(n))
    )
    stop_probe.set()
    probe_task.cancel()

    all_lag = [lag for s in sessions for lag in s.lag_samples]
    all_errors = [f"session[{s.index}]:{e}" for s in sessions for e in s.errors]
    # Backlog growth: compare each session's last two samples to its first two,
    # per session, then take the worst (max) growth across sessions.
    growths = []
    for s in sessions:
        if len(s.backlog_series) >= 2:
            growths.append(s.backlog_series[-1][1] - s.backlog_series[0][1])
    ok_http = sum(1 for status, _ in http_results if status == 200)
    busy_http = sum(1 for status, _ in http_results if status == 429)

    return {
        "n": n,
        "seconds": seconds,
        "finals_per_session": [s.finals for s in sessions],
        "lag_p50_s": _percentile(all_lag, 0.5),
        "lag_p95_s": _percentile(all_lag, 0.95),
        "lag_max_s": round(max(all_lag), 3) if all_lag else None,
        "backlog_growth_s_max": round(max(growths), 2) if growths else None,
        "backlog_series_by_session": [s.backlog_series for s in sessions],
        "errors": all_errors,
        "http_probe_count": len(http_results),
        "http_ok": ok_http,
        "http_queue_full": busy_http,
        "http_other": len(http_results) - ok_http - busy_http,
        "http_p50_s": _percentile([e for _, e in http_results], 0.5),
        "http_p95_s": _percentile([e for _, e in http_results], 0.95),
        "rss_after_mb": rss_mb(),
    }


async def run(wav: Path, max_n: int, seconds: float, ws_url: str, http_base: str, out: Path) -> int:
    pcm = read_wav(wav)
    token = AppPaths.macos_default().token_file.read_text().strip()
    trials = []
    for n in range(1, max_n + 1):
        print(f"=== N={n} 個並行 continuous session，跑 {seconds:.0f} 秒 ===")
        trial = await run_trial(n, ws_url, http_base, pcm, seconds, token)
        trials.append(trial)
        print(json.dumps({k: v for k, v in trial.items() if k != "backlog_series_by_session"},
                          ensure_ascii=False, indent=2))
        # Let the worker settle (idle, RSS) between trials so N=k+1 doesn't
        # inherit N=k's backlog.
        await asyncio.sleep(5)

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"trials": trials}, ensure_ascii=False, indent=2) + "\n")
    print(f"\n寫入 {out}")
    any_hard_error = any(
        e for t in trials for e in t["errors"] if "queue_full" not in e and "session_limit" not in e
    )
    return 1 if any_hard_error else 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wav", type=Path, required=True)
    parser.add_argument("--max-n", type=int, default=4)
    parser.add_argument("--seconds", type=float, default=90)
    parser.add_argument("--ws", default="ws://127.0.0.1:8327/v1/stream")
    parser.add_argument("--http", default="http://127.0.0.1:8327")
    parser.add_argument("--out", type=Path, default=Path("benchmarks/results/concurrency-capacity.json"))
    args = parser.parse_args()
    return asyncio.run(run(args.wav, args.max_n, args.seconds, args.ws, args.http, args.out))


if __name__ == "__main__":
    raise SystemExit(main())
