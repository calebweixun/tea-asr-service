from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from tea_asr.api.app import create_app
from tea_asr.config import AppPaths, ServiceConfig
from tea_asr.model_spec import TEA_ASR_1_1_MLX_8BIT
from tea_asr.worker.supervisor import WorkerSupervisor

AUTH = {"Authorization": "Bearer variant-report-test"}
START = {
    "type": "session.start",
    "request_id": "start-variant-test",
    "profile": "utterance",
    "audio": {"sample_rate": 16_000, "channels": 1, "format": "pcm_s16le"},
    "language": "Chinese",
    "durable": False,
}


def test_status_and_session_started_report_selected_real_model(
    tmp_path: Path, monkeypatch: Any
) -> None:
    async def start_ready(self: WorkerSupervisor) -> None:
        self.state = "ready"
        self.load_ms = 7
        self.generation = 1

    async def stop_cleanly(self: WorkerSupervisor) -> None:
        return None

    monkeypatch.setattr(WorkerSupervisor, "start", start_ready)
    monkeypatch.setattr(WorkerSupervisor, "stop", stop_cleanly)

    worker = WorkerSupervisor(tmp_path / "model")
    app = create_app(
        tmp_path / "model",
        token="variant-report-test",
        supervisor=worker,
        config=ServiceConfig(keep_warm=True, revisable_preview=False),
        model_spec=TEA_ASR_1_1_MLX_8BIT,
        vad_model=None,
        singing_runtime=None,
        paths=AppPaths(support=tmp_path / "support", logs=tmp_path / "logs"),
    )

    with TestClient(app, base_url="http://127.0.0.1") as http:
        status = http.get("/v1/status", headers=AUTH).json()
        assert status["model"] == TEA_ASR_1_1_MLX_8BIT.repo_id
        assert status["model_revision"] == TEA_ASR_1_1_MLX_8BIT.revision
        assert status["model_variant"] == TEA_ASR_1_1_MLX_8BIT.variant

        with http.websocket_connect("/v1/stream", headers=AUTH) as socket:
            assert socket.receive_json()["type"] == "hello"
            socket.send_json(START)
            started = socket.receive_json()
            assert started["type"] == "session.started"
            assert started["model"] == TEA_ASR_1_1_MLX_8BIT.repo_id
            assert started["model_revision"] == TEA_ASR_1_1_MLX_8BIT.revision
            assert started["model_variant"] == TEA_ASR_1_1_MLX_8BIT.variant
