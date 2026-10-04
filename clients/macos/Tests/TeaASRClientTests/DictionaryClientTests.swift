import XCTest
@testable import TeaASRClient

/// `DictionaryClient` end to end through a `URLProtocol` stub: the real
/// `URLSession` path (request method, path, auth header, JSON body) and the
/// status-code mapping, with no server and no network.
final class DictionaryClientTests: XCTestCase {
    private let endpoint = DictionaryEndpoint(host: "127.0.0.1", port: 8452, token: "secret")

    override func tearDown() {
        DictionaryStubProtocol.reset()
        super.tearDown()
    }

    // MARK: - Request assembly

    func testBuildRequestEncodesNameAsOnePathSegment() throws {
        let request = try DictionaryClient.buildRequest(method: "GET", path: ["a b/c"], endpoint: endpoint).get()
        XCTAssertEqual(request.url?.absoluteString, "http://127.0.0.1:8452/v1/dictionaries/a%20b%2Fc")
        XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer secret")
    }

    func testBuildRequestRejectsBadEndpoint() {
        let bad = DictionaryEndpoint(host: " ", port: 8452, token: nil)
        guard case .failure(.invalidEndpoint) = DictionaryClient.buildRequest(method: "GET", path: [], endpoint: bad) else {
            return XCTFail("expected .invalidEndpoint")
        }
    }

    // MARK: - Routes through the stub

    func testListHitsTheCollectionRouteWithTheToken() throws {
        DictionaryStubProtocol.respond(status: 200, body: #"[{"name":"church","domain":"","hotwords_count":0,"replacements_count":3,"error":null,"revision":"aa11","updated_at":"2026-10-01T00:00:00.5+00:00"}]"#)
        let result = try wait { self.client().list(endpoint: self.endpoint, completion: $0) }
        XCTAssertEqual(try result.get().map(\.name), ["church"])
        let request = try XCTUnwrap(DictionaryStubProtocol.requests.first)
        XCTAssertEqual(request.httpMethod, "GET")
        XCTAssertEqual(request.url?.path, "/v1/dictionaries")
        XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer secret")
    }

    func testPutSendsBaseRevisionAndDecodesTheSavedDocument() throws {
        DictionaryStubProtocol.respond(status: 200, body: #"{"name":"church","domain":"","hotwords":[],"replacements":[{"from":"a","to":"b"}],"revision":"bb22","updated_at":"2026-10-04T15:22:53.669347+00:00"}"#)
        let content = DictionaryContent(domain: "", hotwords: [], replacements: [ReplacementRule(from: "a", to: "b")])
        let result = try wait {
            self.client().put(name: "church", content: content, condition: .ifRevision("aa11"), endpoint: self.endpoint, completion: $0)
        }
        XCTAssertEqual(try result.get().revision, "bb22")
        let request = try XCTUnwrap(DictionaryStubProtocol.requests.first)
        XCTAssertEqual(request.httpMethod, "PUT")
        XCTAssertEqual(request.url?.path, "/v1/dictionaries/church")
        let body = try XCTUnwrap(DictionaryStubProtocol.bodies.first)
        let object = try JSONSerialization.jsonObject(with: body) as? [String: Any]
        XCTAssertEqual(object?["base_revision"] as? String, "aa11")
    }

    func testPutConflictMapsTo409Case() throws {
        DictionaryStubProtocol.respond(status: 409, body: #"{"error":{"code":"conflict","message":"Dictionary changed since it was read.","retryable":false,"request_id":null,"current_revision":"cc33"}}"#)
        let result = try wait {
            self.client().put(name: "church", content: .empty, condition: .ifRevision("old"), endpoint: self.endpoint, completion: $0)
        }
        guard case .failure(.conflict(let current)) = result else { return XCTFail("expected .conflict, got \(result)") }
        XCTAssertEqual(current, "cc33", "the 409 carries the server's current revision")
    }

    func testPutInvalidCarriesFieldErrors() throws {
        DictionaryStubProtocol.respond(status: 422, body: """
        {"error":{"code":"invalid","message":"bad","retryable":false,"request_id":null,"details":[{"field":"replacements.from","index":1,"message":"duplicate source; first used at index 0"}]}}
        """)
        let result = try wait {
            self.client().put(name: "church", content: .empty, condition: .createOnly, endpoint: self.endpoint, completion: $0)
        }
        guard case .failure(.invalid(let message, let fields)) = result else { return XCTFail("expected .invalid") }
        XCTAssertEqual(message, "bad")
        XCTAssertEqual(fields, [DictionaryFieldError(field: "replacements.from", index: 1, message: "duplicate source; first used at index 0")])
    }

    func testDeleteAccepts204AndPreviewPostsTheDraft() throws {
        DictionaryStubProtocol.respond(status: 204, body: "")
        let deleted = try wait { self.client().delete(name: "church", endpoint: self.endpoint, completion: $0) }
        XCTAssertNoThrow(try deleted.get())
        XCTAssertEqual(DictionaryStubProtocol.requests.last?.httpMethod, "DELETE")

        DictionaryStubProtocol.respond(status: 200, body: #"{"text":"b","applied":[{"from":"a","to":"b","count":1}]}"#)
        let draft = DictionaryContent(domain: "", hotwords: [], replacements: [ReplacementRule(from: "a", to: "b")])
        let preview = try wait {
            self.client().preview(name: "church", text: "a", draft: draft, endpoint: self.endpoint, completion: $0)
        }
        XCTAssertEqual(try preview.get().text, "b")
        XCTAssertEqual(DictionaryStubProtocol.requests.last?.httpMethod, "POST")
        XCTAssertEqual(DictionaryStubProtocol.requests.last?.url?.path, "/v1/dictionaries/church/preview")
        let body = try JSONSerialization.jsonObject(with: XCTUnwrap(DictionaryStubProtocol.bodies.last)) as? [String: Any]
        XCTAssertNotNil(body?["dictionary"], "preview must send the unsaved draft")
    }

    func testStatusMapping() {
        func status(_ code: Int, _ body: String = "") -> DictionaryAPIError? {
            let response = HTTPURLResponse(url: URL(string: "http://x")!, statusCode: code, httpVersion: nil, headerFields: nil)
            if case .failure(let error) = DictionaryClient.check(data: Data(body.utf8), response: response, error: nil) {
                return error
            }
            return nil
        }
        XCTAssertNil(status(200))
        XCTAssertEqual(status(401), .unauthorized)
        XCTAssertEqual(
            status(403, #"{"error":{"code":"forbidden","message":"Dictionary writes are allowed only from loopback clients.","retryable":false}}"#),
            .forbidden("Dictionary writes are allowed only from loopback clients.")
        )
        XCTAssertTrue(DictionaryAPIError.forbidden("x").localizedDescription.contains("這台電腦"))
        XCTAssertEqual(status(404), .notFound)
        XCTAssertEqual(status(409), .conflict(currentRevision: nil))
        XCTAssertEqual(status(500, #"{"error":{"code":"internal_error","message":"boom"}}"#), .unexpectedStatus(500, "boom"))
        let unreachable = DictionaryClient.check(data: nil, response: nil, error: URLError(.cannotConnectToHost))
        guard case .failure(let error) = unreachable else { return XCTFail("expected failure") }
        XCTAssertTrue(error.isUnreachable)
    }

    // MARK: - Helpers

    private func client() -> DictionaryClient {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [DictionaryStubProtocol.self]
        return DictionaryClient(session: URLSession(configuration: configuration))
    }

    private func wait<T>(_ call: (@escaping (T) -> Void) -> Void) throws -> T {
        let expectation = expectation(description: "completion")
        var value: T?
        call { result in
            value = result
            expectation.fulfill()
        }
        wait(for: [expectation], timeout: 5)
        return try XCTUnwrap(value)
    }
}

/// Answers every request with the canned response and records what was sent.
final class DictionaryStubProtocol: URLProtocol {
    private static let lock = NSLock()
    private static var status = 200
    private static var body = Data()
    private(set) static var requests: [URLRequest] = []
    private(set) static var bodies: [Data] = []

    static func respond(status: Int, body: String) {
        lock.lock(); defer { lock.unlock() }
        self.status = status
        self.body = Data(body.utf8)
    }

    static func reset() {
        lock.lock(); defer { lock.unlock() }
        requests = []
        bodies = []
        status = 200
        body = Data()
    }

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        let sentBody = request.httpBody ?? request.httpBodyStream.map(Self.read) ?? Data()
        Self.lock.lock()
        Self.requests.append(request)
        Self.bodies.append(sentBody)
        let status = Self.status
        let body = Self.body
        Self.lock.unlock()
        let response = HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: "HTTP/1.1", headerFields: ["Content-Type": "application/json"])!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: body)
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}

    private static func read(_ stream: InputStream) -> Data {
        stream.open()
        defer { stream.close() }
        var data = Data()
        var buffer = [UInt8](repeating: 0, count: 4096)
        while stream.hasBytesAvailable {
            let count = stream.read(&buffer, maxLength: buffer.count)
            guard count > 0 else { break }
            data.append(buffer, count: count)
        }
        return data
    }
}
