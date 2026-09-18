from __future__ import annotations

import argparse
import json
import platform
import sys
import urllib.error
import urllib.request
import wave
from pathlib import Path

from huggingface_hub.errors import LocalEntryNotFoundError

from .config import AppPaths, ServiceConfig
from .logs import setup_logging
from .model_manager import locate_prepared_model, prepare_model
from .model_spec import TEA_ASR_1_1_MLX_4BIT
from .service import (
    ServiceAlreadyRunningError,
    SingletonLock,
    agent_status,
    install_agent,
    port_in_use,
    uninstall_agent,
)
from .vad import VAD_REVISION, VAD_SHA256, locate_vad, prepare_vad, sha256
from .wire import MAX_UTTERANCE_PCM_BYTES, SAMPLE_RATE, ws_event_schema

DEFAULT_BASE_URL = "http://127.0.0.1:8327"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tea-asr")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="檢查本機環境是否符合需求")
    commands.add_parser("model-prepare", help="下載並固定模型 snapshot")

    serve = commands.add_parser("serve", help="啟動本機服務")
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", default=None, type=int)

    service = commands.add_parser("service", help="管理登入時自動啟動的 LaunchAgent")
    actions = service.add_subparsers(dest="action", required=True)
    install = actions.add_parser("install", help="建立並載入 LaunchAgent")
    install.add_argument("--host", default=None)
    install.add_argument("--port", default=None, type=int)
    actions.add_parser("uninstall", help="卸載並移除 LaunchAgent")
    actions.add_parser("status", help="顯示 LaunchAgent 與執行中實例的狀態")

    status = commands.add_parser("status", help="查詢執行中服務的狀態")
    status.add_argument("--url", default=DEFAULT_BASE_URL)

    transcribe = commands.add_parser("transcribe", help="把 WAV 轉成 PCM 送進服務")
    transcribe.add_argument("wav", type=Path)
    transcribe.add_argument("--url", default=DEFAULT_BASE_URL)

    export = commands.add_parser("export-schemas", help="產生 OpenAPI 與 WS JSON Schema")
    export.add_argument("--out", type=Path, default=Path("docs/api"))
    return parser


def _read_pcm16_wav(path: Path) -> bytes:
    with wave.open(str(path), "rb") as wav:
        if (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) != (SAMPLE_RATE, 1, 2):
            raise ValueError("WAV 必須是 16 kHz mono signed 16-bit PCM")
        return wav.readframes(wav.getnframes())


def _request(url: str, *, data: bytes | None = None, headers: dict[str, str]) -> dict:
    request = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(request) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        body = exc.read()
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return {"error": {"code": "unknown", "message": body.decode(errors="replace")}}


def _token() -> str:
    token_file = AppPaths.macos_default().token_file
    if not token_file.exists():
        raise FileNotFoundError(f"找不到 token，請先啟動服務：{token_file}")
    return token_file.read_text().strip()


def _doctor() -> int:
    try:
        model_path: str | None = str(locate_prepared_model(TEA_ASR_1_1_MLX_4BIT))
    except (LocalEntryNotFoundError, OSError):
        model_path = None
    try:
        vad_path = locate_vad()
        vad_ok = sha256(vad_path) == VAD_SHA256
    except (LocalEntryNotFoundError, OSError):
        vad_ok = False
    checks = {
        "machine": platform.machine(),
        "macos": platform.mac_ver()[0],
        "python": platform.python_version(),
        "python_ok": (3, 12) <= sys.version_info[:2] < (3, 13),
        "apple_silicon": platform.machine() == "arm64",
        "model_revision": TEA_ASR_1_1_MLX_4BIT.revision,
        "model_prepared": model_path is not None,
        "model_path": model_path,
        "vad_revision": VAD_REVISION,
        "vad_prepared": vad_ok,
    }
    print(json.dumps(checks, ensure_ascii=False, indent=2))
    return 0 if checks["python_ok"] and checks["apple_silicon"] else 1


def _export_schemas(out: Path) -> int:
    from .api.app import create_app

    out.mkdir(parents=True, exist_ok=True)
    app = create_app(Path("unused"), token="schema-export-only", vad_model=None)
    (out / "openapi.json").write_text(
        json.dumps(app.openapi(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    (out / "ws-events.schema.json").write_text(
        json.dumps(ws_event_schema(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    print(out / "openapi.json")
    print(out / "ws-events.schema.json")
    return 0


def main() -> int:
    args = _parser().parse_args()
    if args.command == "doctor":
        return _doctor()
    if args.command == "export-schemas":
        return _export_schemas(args.out)
    if args.command == "status":
        body = _request(
            f"{args.url}/v1/status", headers={"Authorization": f"Bearer {_token()}"}
        )
        print(json.dumps(body, ensure_ascii=False, indent=2))
        return 1 if "error" in body else 0
    if args.command == "transcribe":
        pcm = _read_pcm16_wav(args.wav)
        if len(pcm) > MAX_UTTERANCE_PCM_BYTES:
            print("音訊超過 30 秒上限；長檔案要等 P4 的 batch job。", file=sys.stderr)
            return 2
        body = _request(
            f"{args.url}/v1/transcriptions?sample_rate=16000&channels=1&format=pcm_s16le",
            data=pcm,
            headers={
                "Authorization": f"Bearer {_token()}",
                "Content-Type": "application/octet-stream",
            },
        )
        print(json.dumps(body, ensure_ascii=False, indent=2))
        return 1 if "error" in body else 0
    if args.command == "service":
        paths = AppPaths.macos_default()
        settings = ServiceConfig.load(paths)
        if args.action == "status":
            print(json.dumps(agent_status(paths), ensure_ascii=False, indent=2))
            return 0
        if args.action == "uninstall":
            removed = uninstall_agent()
            print("已移除 LaunchAgent。" if removed else "沒有已安裝的 LaunchAgent。")
            return 0
        host = args.host or settings.host
        port = args.port or settings.port
        target = install_agent(paths, host=host, port=port)
        print(f"已安裝並載入：{target}")
        print("登入時會自動啟動；要移除請執行 tea-asr service uninstall。")
        return 0

    if args.command == "serve":
        import uvicorn

        from .api.app import create_app

        paths = AppPaths.macos_default()
        settings = ServiceConfig.load(paths)
        host = args.host or settings.host
        port = args.port or settings.port
        try:
            model_path = locate_prepared_model(TEA_ASR_1_1_MLX_4BIT)
        except (LocalEntryNotFoundError, OSError) as exc:
            print(f"模型尚未準備：{exc}\n請先執行：tea-asr model-prepare", file=sys.stderr)
            return 2

        lock = SingletonLock(paths.lock_file)
        try:
            lock.acquire(port)
        except ServiceAlreadyRunningError as exc:
            print(f"{exc}\n每台機器只跑一份服務與一份模型。", file=sys.stderr)
            return 3
        if port_in_use(host, port):
            lock.release()
            print(
                f"{host}:{port} 已被占用，可能有另一份服務在跑。",
                file=sys.stderr,
            )
            return 3
        log_file = setup_logging(paths)
        print(f"log：{log_file}")
        try:
            uvicorn.run(
                create_app(model_path, config=settings), host=host, port=port, workers=1
            )
        finally:
            lock.release()
        return 0
    if args.command == "model-prepare":
        print(prepare_model(TEA_ASR_1_1_MLX_4BIT))
        vad_path = prepare_vad()
        digest = sha256(vad_path)
        if digest != VAD_SHA256:
            print(f"VAD 資產 hash 不符 models.lock：{digest}", file=sys.stderr)
            return 2
        print(vad_path)
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
