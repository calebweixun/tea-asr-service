import AppKit

/// Settings that are not per-session: where the service is, and what the client
/// is allowed to do with the recognised text.
final class PreferencesWindow: NSWindowController, NSWindowDelegate {
    private let settings: Settings
    private let hostField = NSTextField(string: "")
    private let portField = NSTextField(string: "")
    private let autoInsert = NSButton(checkboxWithTitle: "定稿後自動貼進前景 app", target: nil, action: nil)
    private let preview = NSButton(checkboxWithTitle: "會議記錄顯示即時預覽", target: nil, action: nil)
    private let serviceStatus = NSTextField(labelWithString: "尚未檢查")
    private let accessStatus = NSTextField(labelWithString: "")

    var onClose: (() -> Void)?

    init(settings: Settings) {
        self.settings = settings
        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 460, height: 330),
            styleMask: [.titled, .closable],
            backing: .buffered,
            defer: false
        )
        window.title = "TEA ASR 設定"
        window.center()
        super.init(window: window)
        window.delegate = self
        build()
        load()
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) {
        fatalError("not supported")
    }

    private func build() {
        guard let window else { return }
        let content = NSView()
        window.contentView = content

        let hostLabel = NSTextField(labelWithString: "服務位址")
        let portLabel = NSTextField(labelWithString: "Port")
        let tokenLabel = NSTextField(labelWithString: "Token")
        let tokenValue = NSTextField(labelWithString: settings.tokenFile.path)
        tokenValue.textColor = .secondaryLabelColor
        tokenValue.lineBreakMode = .byTruncatingMiddle
        tokenValue.toolTip = settings.tokenFile.path

        let check = NSButton(title: "測試連線", target: self, action: #selector(testConnection))
        let openAccess = NSButton(
            title: "開啟輔助使用設定", target: self, action: #selector(openAccessibility)
        )
        autoInsert.target = self
        autoInsert.action = #selector(save)
        preview.target = self
        preview.action = #selector(save)
        hostField.target = self
        hostField.action = #selector(save)
        portField.target = self
        portField.action = #selector(save)
        serviceStatus.textColor = .secondaryLabelColor
        accessStatus.textColor = .secondaryLabelColor

        let grid = NSGridView(views: [
            [hostLabel, hostField],
            [portLabel, portField],
            [tokenLabel, tokenValue],
            [NSGridCell.emptyContentView, autoInsert],
            [NSGridCell.emptyContentView, preview],
            [NSGridCell.emptyContentView, accessStatus],
            [NSGridCell.emptyContentView, openAccess],
            [NSGridCell.emptyContentView, check],
            [NSGridCell.emptyContentView, serviceStatus],
        ])
        grid.translatesAutoresizingMaskIntoConstraints = false
        grid.columnSpacing = 12
        grid.rowSpacing = 10
        grid.column(at: 0).xPlacement = .trailing
        content.addSubview(grid)

        NSLayoutConstraint.activate([
            grid.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 20),
            grid.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -20),
            grid.topAnchor.constraint(equalTo: content.topAnchor, constant: 20),
        ])
    }

    private func load() {
        hostField.stringValue = settings.host
        portField.stringValue = String(settings.port)
        autoInsert.state = settings.autoInsert ? .on : .off
        preview.state = settings.revisablePreview ? .on : .off
        refreshAccessibility()
    }

    private func refreshAccessibility() {
        accessStatus.stringValue = TextInjector.isTrusted
            ? "輔助使用：已允許，可以自動貼上"
            : "輔助使用：未允許，定稿只會放進剪貼簿"
    }

    @objc private func save() {
        settings.host = hostField.stringValue.trimmingCharacters(in: .whitespaces)
        if let port = Int(portField.stringValue), port > 0, port < 65_536 {
            settings.port = port
        } else {
            portField.stringValue = String(settings.port)
        }
        settings.autoInsert = autoInsert.state == .on
        settings.revisablePreview = preview.state == .on
        refreshAccessibility()
    }

    @objc private func openAccessibility() {
        TextInjector.requestTrust()
        let url = URL(
            string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
        )!
        NSWorkspace.shared.open(url)
    }

    @objc private func testConnection() {
        save()
        serviceStatus.stringValue = "檢查中…"
        let host = settings.host
        let port = settings.port
        guard let url = URL(string: "http://\(host):\(port)/v1/status") else { return }
        var request = URLRequest(url: url)
        request.timeoutInterval = 5
        if let token = try? settings.token() {
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        } else {
            serviceStatus.stringValue = "找不到 token：\(settings.tokenFile.path)"
            return
        }
        URLSession.shared.dataTask(with: request) { [weak self] data, response, error in
            DispatchQueue.main.async {
                guard let self else { return }
                if let error {
                    self.serviceStatus.stringValue = "連不上：\(error.localizedDescription)"
                    return
                }
                let code = (response as? HTTPURLResponse)?.statusCode ?? 0
                guard
                    code == 200,
                    let data,
                    let body = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                    let state = body["model_state"] as? String
                else {
                    self.serviceStatus.stringValue = "服務回應 HTTP \(code)"
                    return
                }
                self.serviceStatus.stringValue = "服務正常，模型狀態：\(state)"
            }
        }.resume()
    }

    func windowWillClose(_ notification: Notification) {
        save()
        onClose?()
    }
}
