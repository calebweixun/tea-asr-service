import XCTest
@testable import TeaASRClient

/// Covers the pieces of the Logs page that don't need AppKit: the
/// query-string/header assembly for `GET /v1/logs`, JSON decoding of the
/// documented response shape, and the pure presentation strings the section
/// renders for loading/empty/has-more states.
final class LogsClientTests: XCTestCase {
    // MARK: - Request assembly

    func testBuildRequestAssemblesLevelAndLimitQueryParameters() {
        let result = LogsClient.buildRequest(host: "127.0.0.1", port: 8327, token: "abc123", level: .warning, limit: 150)
        guard case .success(let request) = result, let url = request.url else {
            XCTFail("expected a valid request")
            return
        }
        let components = URLComponents(url: url, resolvingAgainstBaseURL: false)
        XCTAssertEqual(components?.path, "/v1/logs")
        let queryItems = components?.queryItems ?? []
        XCTAssertTrue(queryItems.contains(URLQueryItem(name: "level", value: "warning")))
        XCTAssertTrue(queryItems.contains(URLQueryItem(name: "limit", value: "150")))
        XCTAssertEqual(request.httpMethod, "GET")
        XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer abc123")
    }

    func testBuildRequestUsesEachLevelsRawValue() {
        for level in LogLevel.allCases {
            guard
                case .success(let request) = LogsClient.buildRequest(
                    host: "127.0.0.1", port: 8327, token: nil, level: level, limit: 50
                ),
                let url = request.url,
                let queryItems = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems
            else {
                XCTFail("expected a valid request for \(level)")
                continue
            }
            XCTAssertTrue(queryItems.contains(URLQueryItem(name: "level", value: level.rawValue)))
        }
    }

    func testBuildRequestOmitsAuthorizationHeaderWithoutToken() {
        guard case .success(let request) = LogsClient.buildRequest(
            host: "127.0.0.1", port: 8327, token: nil, level: .error, limit: 100
        ) else {
            XCTFail("expected a valid request")
            return
        }
        XCTAssertNil(request.value(forHTTPHeaderField: "Authorization"))
    }

    func testBuildRequestRejectsBlankHost() {
        switch LogsClient.buildRequest(host: "   ", port: 8327, token: nil, level: .info, limit: 100) {
        case .failure(.invalidEndpoint):
            break
        default:
            XCTFail("expected .invalidEndpoint for a blank host")
        }
    }

    func testBuildRequestRejectsOutOfRangePort() {
        switch LogsClient.buildRequest(host: "127.0.0.1", port: 0, token: nil, level: .info, limit: 100) {
        case .failure(.invalidEndpoint):
            break
        default:
            XCTFail("expected .invalidEndpoint for an out-of-range port")
        }
        switch LogsClient.buildRequest(host: "127.0.0.1", port: 70_000, token: nil, level: .info, limit: 100) {
        case .failure(.invalidEndpoint):
            break
        default:
            XCTFail("expected .invalidEndpoint for an out-of-range port")
        }
    }

    // MARK: - Decoding the documented response shape

    func testDecodesDocumentedResponseShape() throws {
        let json = """
        {
          "items": [
            {"ts":"2026-09-20T10:00:00Z","level":"ERROR","logger":"tea_asr.api","message":"worker.restart_failed","fields":{"code":"E1","attempt":3,"fatal":false}}
          ],
          "count": 1,
          "limit": 100,
          "has_more": true
        }
        """
        let response = try JSONDecoder().decode(LogsResponse.self, from: Data(json.utf8))
        XCTAssertEqual(response.count, 1)
        XCTAssertEqual(response.limit, 100)
        XCTAssertTrue(response.hasMore)
        XCTAssertEqual(response.items.count, 1)
        let entry = response.items[0]
        XCTAssertEqual(entry.level, "ERROR")
        XCTAssertEqual(entry.logger, "tea_asr.api")
        XCTAssertEqual(entry.message, "worker.restart_failed")
        XCTAssertEqual(entry.fields?["code"], .string("E1"))
        XCTAssertEqual(entry.fields?["attempt"], .number(3))
        XCTAssertEqual(entry.fields?["fatal"], .bool(false))
    }

    func testDecodesEntryWithoutFields() throws {
        let json = """
        {"ts":"2026-09-20T10:00:00Z","level":"INFO","logger":"tea_asr.api","message":"worker.ready"}
        """
        let entry = try JSONDecoder().decode(LogEntry.self, from: Data(json.utf8))
        XCTAssertNil(entry.fields)
    }

    // MARK: - Presentation

    func testLineIncludesTimestampLevelLoggerMessageAndSortedFields() {
        let entry = LogEntry(
            ts: "2026-09-20T10:00:00Z",
            level: "ERROR",
            logger: "tea_asr.api",
            message: "worker.restart_failed",
            fields: ["b": .string("2"), "a": .number(1)]
        )
        let line = LogsPresentation.line(for: entry)
        XCTAssertTrue(line.contains("2026-09-20T10:00:00Z"))
        XCTAssertTrue(line.contains("[ERROR]"))
        XCTAssertTrue(line.contains("tea_asr.api"))
        XCTAssertTrue(line.contains("worker.restart_failed"))
        // Fields render in a stable (sorted) order so the same entry always
        // renders identically.
        XCTAssertTrue(line.contains("a=1 b=2"))
    }

    func testLineWithoutFieldsOmitsTrailingFieldsText() {
        let entry = LogEntry(ts: "t", level: "INFO", logger: "l", message: "m", fields: nil)
        XCTAssertEqual(LogsPresentation.line(for: entry), "t  [INFO]  l  m")
        let entryWithEmptyFields = LogEntry(ts: "t", level: "INFO", logger: "l", message: "m", fields: [:])
        XCTAssertEqual(LogsPresentation.line(for: entryWithEmptyFields), "t  [INFO]  l  m")
    }

    func testHasMoreNoticeIsDistinctFromEmptyNotice() {
        XCTAssertNotEqual(LogsPresentation.hasMoreNotice(shown: 5), LogsPresentation.emptyNotice())
        XCTAssertTrue(LogsPresentation.hasMoreNotice(shown: 5).contains("還有"))
        XCTAssertTrue(LogsPresentation.hasMoreNotice(shown: 5).contains("5"))
    }

    func testFailureDescriptionsAreDistinctFromEachOtherAndFromEmptyNotice() {
        let messages: [String] = [
            LogsFetchError.unreachable("Connection refused").errorDescription!,
            LogsFetchError.unauthorized.errorDescription!,
            LogsFetchError.invalidResponse.errorDescription!,
            LogsFetchError.forbidden.errorDescription!,
            LogsFetchError.unexpectedStatus(500).errorDescription!,
        ]
        XCTAssertEqual(Set(messages).count, messages.count, "each failure must explain itself distinctly")
        for message in messages {
            XCTAssertNotEqual(message, LogsPresentation.emptyNotice(), "a fetch failure must never read like an empty result")
        }
        XCTAssertTrue(LogsFetchError.unreachable("").errorDescription!.contains("無法連到"))
        XCTAssertTrue(LogsFetchError.unauthorized.errorDescription!.contains("token"))
        XCTAssertTrue(LogsFetchError.invalidResponse.errorDescription!.contains("回應格式"))
    }
}
