import XCTest
@testable import TeaASRClient

final class M0StateTests: XCTestCase {
    func testServerStatusDecodesSnakeCaseFields() throws {
        let data = Data(
            """
            {
              "model_state": "ready",
              "model": "JacobLinCool/TEA-ASR-1.1",
              "model_revision": "abc123",
              "worker_generation": 4,
              "worker_load_ms": 812,
              "last_error": null,
              "idle_s": 3,
              "active_sessions": 1,
              "queue": {
                "waiting_tasks": 2,
                "waiting_samples": 32000,
                "max_waiting_tasks": 16,
                "max_waiting_samples": 960000
              }
            }
            """.utf8
        )

        let status = try JSONDecoder().decode(ServerStatus.self, from: data)

        XCTAssertEqual(status.modelState, "ready")
        XCTAssertEqual(status.workerGeneration, 4)
        XCTAssertEqual(status.queue.waitingTasks, 2)
        XCTAssertEqual(status.queue.maxWaitingSamples, 960000)
    }

    func testCapabilitiesDecodeFeaturesAndLimits() throws {
        let data = Data(
            """
            {
              "protocol_version": "1.1",
              "audio": {"sample_rate": 16000, "channels": 1, "format": "pcm_s16le"},
              "profiles": ["utterance", "continuous"],
              "features": {
                "native_audio_streaming": false,
                "partial_transcripts": true,
                "word_timestamps": false,
                "translation": false,
                "diarization": false,
                "hotwords": false,
                "context_biasing": false,
                "durable_sessions": false,
                "durable_revisable": false,
                "batch_jobs": false
              },
              "limits": {
                "max_frame_pcm_bytes": 6400,
                "max_utterance_ms": 30000,
                "max_continuous_sessions": 2,
                "max_total_connections": 4
              }
            }
            """.utf8
        )

        let capabilities = try JSONDecoder().decode(Capabilities.self, from: data)

        XCTAssertEqual(capabilities.protocolVersion, "1.1")
        XCTAssertTrue(capabilities.features.partialTranscripts)
        XCTAssertEqual(capabilities.limits.maxContinuousSessions, 2)
        XCTAssertEqual(capabilities.audio.sampleRate, 16000)
    }

    func testServiceProbeErrorDoesNotExposeResponseBody() {
        let error = ServiceProbeError.invalidResponse("/v1/status")

        XCTAssertEqual(error.localizedDescription, "服務的 /v1/status 回應格式無法辨識。")
        XCTAssertFalse(error.localizedDescription.contains("token"))
        XCTAssertFalse(error.localizedDescription.contains("transcript"))
    }

    func testAppStateMapsServiceAndClientStates() {
        let ready = ServiceSnapshot(
            healthzOK: true,
            readyzOK: true,
            readyState: "ready",
            status: ServerStatus(
                modelState: "ready",
                model: "model",
                modelRevision: "revision",
                workerGeneration: 1,
                workerLoadMs: nil,
                lastError: nil,
                idleS: 0,
                activeSessions: 0,
                queue: QueueStatus(
                    waitingTasks: 0,
                    waitingSamples: 0,
                    maxWaitingTasks: 16,
                    maxWaitingSamples: 960000
                )
            ),
            capabilities: Capabilities(
                protocolVersion: "1.1",
                audio: CapabilityAudio(sampleRate: 16000, channels: 1, format: "pcm_s16le"),
                profiles: ["continuous"],
                features: CapabilityFeatures(
                    nativeAudioStreaming: false,
                    partialTranscripts: true,
                    wordTimestamps: false,
                    translation: false,
                    diarization: false,
                    hotwords: false,
                    contextBiasing: false,
                    durableSessions: false,
                    durableRevisable: false,
                    batchJobs: false
                ),
                limits: CapabilityLimits(
                    maxFramePCMBytes: 6400,
                    maxUtteranceMs: 30000,
                    maxContinuousSessions: 2,
                    maxTotalConnections: 4
                )
            )
        )

        XCTAssertEqual(
            AppState.displayStatus(
                clientState: .idle,
                mode: .idle,
                serviceSnapshot: ready,
                serviceReachable: true,
                serviceError: nil
            ),
            .ready
        )
        XCTAssertEqual(
            AppState.displayStatus(
                clientState: .connecting,
                mode: .dictation,
                serviceSnapshot: nil,
                serviceReachable: false,
                serviceError: nil
            ),
            .offline
        )
        XCTAssertEqual(
            AppState.displayStatus(
                clientState: .loadingModel,
                mode: .meeting,
                serviceSnapshot: nil,
                serviceReachable: true,
                serviceError: nil
            ),
            .loading
        )
        XCTAssertEqual(
            AppState.displayStatus(
                clientState: .listening(protocolVersion: "1.1", preview: true),
                mode: .meeting,
                serviceSnapshot: ready,
                serviceReachable: true,
                serviceError: nil
            ),
            .listening(mode: .meeting, preview: true)
        )
        let permanent = ConnectionIssue(
            code: "token_missing",
            message: "找不到 token。",
            retryable: false,
            closeCode: nil
        )
        XCTAssertEqual(
            AppState.displayStatus(
                clientState: .failed(permanent),
                mode: .idle,
                serviceSnapshot: nil,
                serviceReachable: false,
                serviceError: nil
            ),
            .failed(permanent)
        )
    }

    func testConnectionIssuePolicyPreservesServerCloseSemantics() {
        let concurrent = ConnectionIssue.fromWire(
            code: "concurrent_session_limit",
            message: "已達 continuous session 上限。",
            retryable: true
        )
        XCTAssertEqual(concurrent.closeCode, 4029)
        XCTAssertTrue(concurrent.retryable)

        let auth = ConnectionIssue.fromCloseCode(1008, message: "拒絕")
        XCTAssertFalse(auth.retryable)
        XCTAssertEqual(auth.closeCode, 1008)

        let backpressure = ConnectionIssue.fromCloseCode(1013, message: "稍後重試")
        XCTAssertEqual(backpressure.code, "server_backpressure")
        XCTAssertTrue(backpressure.retryable)
    }

    func testAppStateMapsWorkerRecoveryAndFailure() {
        let recovering = AppState.displayStatus(
            clientState: .idle,
            mode: .idle,
            serviceSnapshot: makeSnapshot(
                modelState: "recovering",
                lastError: "Worker connection lost: ConnectionResetError"
            ),
            serviceReachable: true,
            serviceError: nil
        )
        guard case .retryable(let recoveringIssue) = recovering else {
            return XCTFail("recovering worker should be retryable")
        }
        XCTAssertEqual(recoveringIssue.code, "worker_recovering")
        XCTAssertTrue(recoveringIssue.message.contains("ConnectionResetError"))

        let failed = AppState.displayStatus(
            clientState: .idle,
            mode: .idle,
            serviceSnapshot: makeSnapshot(
                modelState: "failed",
                lastError: "Worker did not become ready: fixture failure"
            ),
            serviceReachable: true,
            serviceError: nil
        )
        guard case .failed(let failedIssue) = failed else {
            return XCTFail("failed worker should be a permanent failure")
        }
        XCTAssertEqual(failedIssue.code, "model_unavailable")
        XCTAssertFalse(failedIssue.retryable)
        XCTAssertTrue(failedIssue.message.contains("fixture failure"))

        let redacted = AppState.displayStatus(
            clientState: .idle,
            mode: .idle,
            serviceSnapshot: makeSnapshot(
                modelState: "failed",
                lastError: "request Authorization: Bearer secret-token"
            ),
            serviceReachable: true,
            serviceError: nil
        )
        guard case .failed(let redactedIssue) = redacted else {
            return XCTFail("failed worker should remain a failure")
        }
        XCTAssertFalse(redactedIssue.message.contains("secret-token"))

        let responseBody = AppState.displayStatus(
            clientState: .idle,
            mode: .idle,
            serviceSnapshot: makeSnapshot(
                modelState: "failed",
                lastError: "Worker did not become ready: HTTP 502 response body: <html>upstream failure</html>"
            ),
            serviceReachable: true,
            serviceError: nil
        )
        guard case .failed(let responseBodyIssue) = responseBody else {
            return XCTFail("failed worker should remain a failure")
        }
        XCTAssertEqual(responseBodyIssue.message, "ASR worker 無法使用，請檢查服務設定或重新啟動服務。")
    }

    func testAppStateDoesNotTreatUnknownOrInconsistentWorkerStateAsLoading() {
        let unknown = AppState.displayStatus(
            clientState: .idle,
            mode: .idle,
            serviceSnapshot: makeSnapshot(modelState: "draining", lastError: nil),
            serviceReachable: true,
            serviceError: nil
        )
        guard case .failed(let unknownIssue) = unknown else {
            return XCTFail("unknown worker state should be explicit failure")
        }
        XCTAssertEqual(unknownIssue.code, "unknown_worker_state")

        // `makeSnapshot` marks ready as readyz=true; construct the mismatch
        // explicitly so the test documents the contract boundary.
        let notReady = ServiceSnapshot(
            healthzOK: true,
            readyzOK: false,
            readyState: "recovering",
            status: inconsistentSnapshotStatus(),
            capabilities: makeCapabilities()
        )
        guard case .retryable(let notReadyIssue) = AppState.displayStatus(
            clientState: .idle,
            mode: .idle,
            serviceSnapshot: notReady,
            serviceReachable: true,
            serviceError: nil
        ) else {
            return XCTFail("ready worker with non-ready readiness should be retryable")
        }
        XCTAssertEqual(notReadyIssue.code, "service_not_ready")
    }

    private func makeSnapshot(modelState: String, lastError: String?) -> ServiceSnapshot {
        ServiceSnapshot(
            healthzOK: true,
            readyzOK: modelState == "ready",
            readyState: modelState == "ready" ? "ready" : modelState,
            status: ServerStatus(
                modelState: modelState,
                model: "model",
                modelRevision: "revision",
                workerGeneration: 1,
                workerLoadMs: nil,
                lastError: lastError,
                idleS: 0,
                activeSessions: 0,
                queue: QueueStatus(
                    waitingTasks: 0,
                    waitingSamples: 0,
                    maxWaitingTasks: 16,
                    maxWaitingSamples: 960000
                )
            ),
            capabilities: makeCapabilities()
        )
    }

    private func inconsistentSnapshotStatus() -> ServerStatus {
        ServerStatus(
            modelState: "ready",
            model: "model",
            modelRevision: "revision",
            workerGeneration: 1,
            workerLoadMs: nil,
            lastError: nil,
            idleS: 0,
            activeSessions: 0,
            queue: QueueStatus(
                waitingTasks: 0,
                waitingSamples: 0,
                maxWaitingTasks: 16,
                maxWaitingSamples: 960000
            )
        )
    }

    private func makeCapabilities() -> Capabilities {
        Capabilities(
            protocolVersion: "1.1",
            audio: CapabilityAudio(sampleRate: 16000, channels: 1, format: "pcm_s16le"),
            profiles: ["continuous"],
            features: CapabilityFeatures(
                nativeAudioStreaming: false,
                partialTranscripts: true,
                wordTimestamps: false,
                translation: false,
                diarization: false,
                hotwords: false,
                contextBiasing: false,
                durableSessions: false,
                durableRevisable: false,
                batchJobs: false
            ),
            limits: CapabilityLimits(
                maxFramePCMBytes: 6400,
                maxUtteranceMs: 30000,
                maxContinuousSessions: 2,
                maxTotalConnections: 4
            )
        )
    }
}
