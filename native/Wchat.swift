import AppKit
import SwiftUI

enum Ink {
    static func native(_ hex: UInt32) -> NSColor {
        NSColor(srgbRed: CGFloat((hex >> 16) & 255) / 255,
                green: CGFloat((hex >> 8) & 255) / 255,
                blue: CGFloat(hex & 255) / 255, alpha: 1)
    }
    static let canvas = Color(nsColor: native(0x20211D))
    static let sidebar = Color(nsColor: native(0x181915))
    static let field = Color(nsColor: native(0x272822))
    static let text = Color(nsColor: native(0xE4E1D1))
    static let cream = Color(nsColor: native(0xECE4B4))
    static let muted = Color(nsColor: native(0xA4A594))
    static let line = Color(nsColor: native(0x42443A))
}

struct QuietButton: ButtonStyle {
    @Environment(\.isEnabled) private var enabled
    var outlined = false
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: 12, weight: .medium))
            .foregroundStyle(enabled ? Ink.cream : Ink.muted.opacity(0.55))
            .padding(.horizontal, outlined ? 12 : 4).padding(.vertical, 7)
            .background(configuration.isPressed ? Ink.line : Color.clear)
            .overlay { if outlined { RoundedRectangle(cornerRadius: 4).stroke(Ink.line, lineWidth: 1) } }
            .contentShape(Rectangle())
    }
}

struct Hairline: View {
    var body: some View { Rectangle().fill(Ink.line.opacity(0.65)).frame(height: 1) }
}

struct Job: Identifiable, Decodable, Equatable {
    let id: Int
    let session: String
    let prompt: String
    let source: String
    var status: String
    var response: String
    let error: String?
}
struct Conversation: Identifiable, Decodable {
    let name: String
    let cwd: String
    var id: String { name }
}
struct Permission: Identifiable {
    let id: String
    let method: String
    let params: [String: Any]
}

@MainActor final class ChatModel: ObservableObject {
    @Published var jobs: [Job] = []
    @Published var sessions: [Conversation] = []
    @Published var active = "default"
    @Published var permissions: [Permission] = []
    @Published var connection = "Подключение…"
    @Published var microphone = "Запуск микрофона…"
    @Published var error: String?
    @Published var connected = false
    @Published var revision = 0
    @Published var commonContext = ""
    @Published var defaultCwd = NSHomeDirectory() + "/Documents/jack"
    @Published var contextSaved = 0
    let process = Process()
    let input = Pipe()
    private var output: Pipe?
    private var stderr: FileHandle?
    var onExit: (() -> Void)?
    var busy: Bool { jobs.contains { ["queued", "starting", "running"].contains($0.status) } }
    var cwd: String { sessions.first { $0.name == active }?.cwd ?? "" }

    func start() {
        guard let root = Bundle.main.object(forInfoDictionaryKey: "WChatProjectRoot") as? String else {
            error = "Не задана папка проекта"; return
        }
        var args = Array(CommandLine.arguments.dropFirst())
        if let deviceIndex = args.firstIndex(of: "--device"), deviceIndex + 1 < args.count {
            UserDefaults.standard.set(args[deviceIndex + 1], forKey: "microphoneDevice")
        } else if let device = UserDefaults.standard.string(forKey: "microphoneDevice") {
            args += ["--device", device]
        }
        let dataIndex = args.firstIndex(of: "--data-dir")
        let dataPath = dataIndex.flatMap { $0 + 1 < args.count ? args[$0 + 1] : nil }
            ?? NSHomeDirectory() + "/Library/Application Support/wchat"
        do {
            try FileManager.default.createDirectory(atPath: dataPath, withIntermediateDirectories: true,
                attributes: [.posixPermissions: 0o700])
            let logPath = dataPath + "/desktop.log"
            if !FileManager.default.fileExists(atPath: logPath) {
                FileManager.default.createFile(atPath: logPath, contents: nil, attributes: [.posixPermissions: 0o600])
            }
            stderr = try FileHandle(forWritingTo: URL(fileURLWithPath: logPath))
            try stderr?.seekToEnd()
            process.executableURL = URL(fileURLWithPath: root + "/.venv/bin/python")
            process.currentDirectoryURL = URL(fileURLWithPath: root)
            var env = ProcessInfo.processInfo.environment
            env["PYTHONPATH"] = root
            env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:" + (env["PATH"] ?? "/usr/bin:/bin")
            process.environment = env
            process.arguments = ["-m", "wchat", "--ui"] + args
            process.standardInput = input
            let pipe = Pipe(); output = pipe
            process.standardOutput = pipe
            process.standardError = stderr
            process.terminationHandler = { [weak self] process in
                DispatchQueue.main.async {
                    guard let self else { return }
                    self.connected = false
                    self.connection = "Остановлен"
                    self.microphone = "Микрофон выключен"
                    if process.terminationStatus != 0 && self.error == nil {
                        self.error = "Клиент завершился. Подробности: \(logPath)"
                    }
                    self.onExit?()
                }
            }
            try process.run()
            // One reader owns the buffer; each JSON line is delivered in order.
            DispatchQueue.global(qos: .userInitiated).async { [weak self] in
                var pending = Data()
                while true {
                    let chunk = pipe.fileHandleForReading.availableData
                    if chunk.isEmpty { break }
                    pending.append(chunk)
                    while let newline = pending.firstIndex(of: 10) {
                        let line = Data(pending[..<newline]); pending.removeSubrange(...newline)
                        DispatchQueue.main.async { self?.receive(line) }
                    }
                }
            }
        } catch { self.error = error.localizedDescription; connection = "Ошибка запуска" }
    }
    func send(_ payload: [String: Any]) {
        guard process.isRunning else { return }
        do {
            var data = try JSONSerialization.data(withJSONObject: payload); data.append(10)
            try input.fileHandleForWriting.write(contentsOf: data)
        } catch { self.error = error.localizedDescription }
    }
    func receive(_ data: Data) {
        guard let event = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let kind = event["kind"] as? String else { return }
        let text = event["text"] as? String ?? ""
        var transcriptChanged = false
        switch kind {
        case "snapshot":
            if let raw = event["jobs"], let data = try? JSONSerialization.data(withJSONObject: raw),
               let value = try? JSONDecoder().decode([Job].self, from: data), value != jobs {
                jobs = value
                transcriptChanged = true
            }
            if let raw = event["sessions"], let data = try? JSONSerialization.data(withJSONObject: raw),
               let value = try? JSONDecoder().decode([Conversation].self, from: data) { sessions = value }
            if let name = event["active"] as? String, name != active {
                active = name
                transcriptChanged = true
            }
            commonContext = event["context"] as? String ?? ""
            defaultCwd = event["default_cwd"] as? String ?? defaultCwd
            permissions = (event["requests"] as? [String: [String: Any]] ?? [:]).sorted { $0.key < $1.key }.map {
                Permission(id: $0.key, method: $0.value["method"] as? String ?? "",
                           params: $0.value["params"] as? [String: Any] ?? [:])
            }
            if event["halted"] as? Bool == true { connected = false; connection = "Нужен перезапуск" }
        case "context_saved": contextSaved += 1
        case "connected": connected = true; connection = "Codex подключён"
        case "delta":
            if let id = event["job"] as? Int, let index = jobs.firstIndex(where: { $0.id == id }) {
                let response = event["response"] as? String ?? jobs[index].response + text
                if jobs[index].response != response {
                    jobs[index].response = response
                    transcriptChanged = true
                }
            }
        case "queued", "started", "finished", "requests_changed", "approval", "session":
            send(["action": "snapshot"])
        case "voice_ready": microphone = "Слушаю: «чат» · " + text
        case "voice_wake": microphone = "Слушаю поручение…"
        case "voice_prompt", "voice_discard": microphone = "Слушаю: «чат»"
        case "voice_disabled", "voice_end": microphone = "Микрофон выключен"
        case "voice_error": microphone = "Ошибка микрофона"; error = text
        case "error": error = text; send(["action": "snapshot"])
        default: break
        }
        if transcriptChanged { revision += 1 }
    }
    func stop() {
        guard process.isRunning else { onExit?(); return }
        send(["action": "quit"])
        DispatchQueue.main.asyncAfter(deadline: .now() + 6) { [weak self] in
            guard let self, self.process.isRunning else { return }
            self.process.terminate()
        }
    }
}

struct MarkdownMessage: View {
    let text: String
    var body: some View {
        let chunks = text.components(separatedBy: "```")
        VStack(alignment: .leading, spacing: 10) {
            ForEach(Array(chunks.enumerated()), id: \.offset) { index, chunk in
                if index % 2 == 1 {
                    let code = (chunk.contains("\n") ? String(chunk.drop(while: { $0 != "\n" }).dropFirst()) : chunk).trimmingCharacters(in: .newlines)
                    ScrollView(.horizontal) {
                        Text(code).font(.system(.body, design: .monospaced)).padding(12).textSelection(.enabled)
                    }.background(Ink.sidebar).overlay(alignment: .leading) { Rectangle().fill(Ink.line).frame(width: 2) }
                } else if !chunk.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                    let prose = chunk.trimmingCharacters(in: .whitespacesAndNewlines)
                    Text((try? AttributedString(markdown: prose, options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace)))
                         ?? AttributedString(prose)).textSelection(.enabled).lineSpacing(4)
                }
            }
        }
    }
}

struct TurnView: View {
    let job: Job
    var status: String {
        switch job.status {
        case "queued": return "В очереди"
        case "starting", "running": return "Выполняю…"
        case "completed": return "Ответ готов"
        case "cancelled", "interrupted": return "Остановлено"
        case "failed": return "Ошибка"
        default: return "Нужно проверить результат"
        }
    }
    var body: some View {
        VStack(alignment: .leading, spacing: 22) {
            HStack(alignment: .top, spacing: 18) {
                VStack(alignment: .leading, spacing: 4) {
                    Text("Вы").foregroundStyle(Ink.cream)
                    if job.source == "voice" { Text("голос").font(.system(size: 10)).foregroundStyle(Ink.muted) }
                }.font(.system(size: 12, weight: .medium)).frame(width: 48, alignment: .leading).padding(.top, 3)
                Text(job.prompt).font(.system(size: 15)).foregroundStyle(Ink.cream)
                    .lineSpacing(5).textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.leading, 14)
                    .overlay(alignment: .leading) { Rectangle().fill(Ink.cream.opacity(0.45)).frame(width: 2) }
            }
            HStack(alignment: .top, spacing: 18) {
                Text("wchat").font(.system(size: 12)).foregroundStyle(Ink.muted)
                    .frame(width: 48, alignment: .leading).padding(.top, 3)
                VStack(alignment: .leading, spacing: 10) {
                    if job.status != "completed" {
                        Text(status).font(.system(size: 12)).foregroundStyle(Ink.muted)
                    }
                    if job.response.isEmpty && ["queued", "starting", "running"].contains(job.status) {
                        ProgressView().controlSize(.small)
                    } else { MarkdownMessage(text: job.response).font(.system(size: 15)).foregroundStyle(Ink.text) }
                    if let error = job.error, !error.isEmpty {
                        Text(error).font(.caption).foregroundStyle(.orange).textSelection(.enabled)
                    }
                    if !job.response.isEmpty {
                        Button {
                            NSPasteboard.general.clearContents()
                            NSPasteboard.general.setString(job.response, forType: .string)
                        } label: { Image(systemName: "doc.on.doc").font(.system(size: 11)).foregroundStyle(Ink.muted) }
                            .buttonStyle(QuietButton()).help("Скопировать ответ")
                    }
                }.frame(maxWidth: .infinity, alignment: .leading)
            }
        }.padding(.vertical, 26)
        .overlay(alignment: .bottom) { Hairline() }
    }
}

struct PermissionCard: View {
    let permission: Permission
    @ObservedObject var model: ChatModel
    @State private var content = "{}"
    @State private var invalid: String?
    @State private var values: [String: String] = [:]
    var fields: [(String, [String: Any])] {
        if permission.method == "item/tool/requestUserInput" {
            return (permission.params["questions"] as? [[String: Any]] ?? []).compactMap { q in
                guard let id = q["id"] as? String else { return nil }
                var field: [String: Any] = ["type": "string", "title": q["question"] as? String ?? id]
                if let options = q["options"] as? [[String: Any]] {
                    field["description"] = options.map { ($0["label"] as? String ?? "") + ": " + ($0["description"] as? String ?? "") }.joined(separator: "\n")
                }
                return (id, field)
            }
        }
        let schema = permission.params["requestedSchema"] as? [String: Any] ?? [:]
        return (schema["properties"] as? [String: [String: Any]] ?? [:]).sorted { $0.key < $1.key }.map { ($0.key, $0.value) }
    }
    var simpleForm: Bool {
        fields.allSatisfy { ["string", "boolean", "integer", "number"].contains($0.1["type"] as? String ?? "") }
        && !(permission.params["requestedSchema"] as? [String: Any] ?? [:]).keys.contains("oneOf")
    }
    func formContent() throws -> Any {
        if needsForm && simpleForm {
            var result: [String: Any] = [:]
            for (key, field) in fields {
                guard let value = values[key], !value.isEmpty else { continue }
                if permission.method == "item/tool/requestUserInput" { result[key] = ["answers": [value]] }
                else {
                    switch field["type"] as? String {
                    case "boolean": result[key] = value == "true"
                    case "integer":
                        guard let n = Int(value) else { throw NSError(domain: "Введите целое число", code: 1) }; result[key] = n
                    case "number":
                        guard let n = Double(value), n.isFinite else { throw NSError(domain: "Введите число", code: 1) }; result[key] = n
                    default: result[key] = value
                    }
                }
            }
            return result
        }
        return try JSONSerialization.jsonObject(with: Data(content.utf8))
    }
    var needsForm: Bool { permission.method == "item/tool/requestUserInput" ||
        (permission.method == "mcpServer/elicitation/request" && permission.params["mode"] as? String != "url") }
    var details: String {
        let value = permission.params
        guard let data = try? JSONSerialization.data(withJSONObject: value, options: [.prettyPrinted, .sortedKeys]),
              let text = String(data: data, encoding: .utf8) else { return "" }
        return text
    }
    func reply(_ decision: String) {
        do {
            let value = try formContent()
            invalid = nil
            model.send(["action": "reply", "request": permission.id, "decision": decision, "content": value])
        } catch { invalid = "Проверьте заполнение полей: " + error.localizedDescription }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Label("Требуется ваше решение", systemImage: "hand.raised.fill").font(.headline).foregroundStyle(.orange)
            Text(permission.params["message"] as? String ?? permission.params["reason"] as? String ?? "Разрешить действие?")
                .textSelection(.enabled)
            if let command = permission.params["command"] as? String { Text(command).font(.system(.body, design: .monospaced)).textSelection(.enabled) }
            if let url = permission.params["url"] as? String { Text(url).textSelection(.enabled) }
            DisclosureGroup("Подробности запроса") {
                Text(details).font(.system(.caption, design: .monospaced)).textSelection(.enabled)
            }
            if needsForm {
                if simpleForm {
                    ForEach(fields, id: \.0) { key, field in
                        VStack(alignment: .leading, spacing: 6) {
                            Text(field["title"] as? String ?? key).fontWeight(.medium)
                            if let description = field["description"] as? String {
                                Text(description).font(.caption).foregroundStyle(.secondary).textSelection(.enabled)
                            }
                            let binding = Binding<String>(get: { values[key] ?? "" }, set: { values[key] = $0 })
                            if field["type"] as? String == "boolean" {
                                Picker("Ответ", selection: binding) {
                                    Text("Выберите…").tag("")
                                    Text("Да").tag("true"); Text("Нет").tag("false")
                                }
                            } else if let options = field["enum"] as? [String] {
                                Picker("Ответ", selection: binding) {
                                    Text("Выберите…").tag("")
                                    ForEach(options, id: \.self) { Text($0).tag($0) }
                                }
                            } else if let options = field["oneOf"] as? [[String: String]] {
                                Picker("Ответ", selection: binding) {
                                    Text("Выберите…").tag("")
                                    ForEach(options.indices, id: \.self) { i in
                                        Text(options[i]["title"] ?? options[i]["const"] ?? "").tag(options[i]["const"] ?? "")
                                    }
                                }
                            } else { TextField("Ответ", text: binding).textFieldStyle(.roundedBorder) }
                        }
                    }
                } else {
                    Text("Поля ответа (JSON по схеме в подробностях)").font(.caption).foregroundStyle(.secondary)
                    TextEditor(text: $content).font(.system(.body, design: .monospaced)).frame(height: 90)
                }
            }
            if let invalid { Text(invalid).foregroundStyle(.red) }
            HStack {
                Button("Отклонить") { model.send(["action": "reply", "request": permission.id, "decision": "decline"]) }
                Spacer()
                Button("Подтвердить") { reply("accept") }.buttonStyle(QuietButton(outlined: true))
            }
        }.padding(18).background(Ink.field).overlay(alignment: .leading) { Rectangle().fill(Ink.cream).frame(width: 2) }
        .onAppear {
            if permission.method == "item/tool/requestUserInput" {
                let questions = permission.params["questions"] as? [[String: Any]] ?? []
                let template = Dictionary(uniqueKeysWithValues: questions.compactMap { q -> (String, [String: [String]])? in
                    guard let id = q["id"] as? String else { return nil }; return (id, ["answers": [""]])
                })
                if let data = try? JSONSerialization.data(withJSONObject: template, options: [.prettyPrinted]),
                   let text = String(data: data, encoding: .utf8) { content = text }
            }
        }
    }
}

final class ComposerTextView: NSTextView {
    var submit: (() -> Void)?
    var focusChanged: ((Bool) -> Void)?
    override func becomeFirstResponder() -> Bool {
        let result = super.becomeFirstResponder()
        if result { focusChanged?(true) }; return result
    }
    override func resignFirstResponder() -> Bool {
        let result = super.resignFirstResponder()
        if result { focusChanged?(false) }; return result
    }
    override func keyDown(with event: NSEvent) {
        if [36, 76].contains(event.keyCode) && !event.modifierFlags.contains(.shift) {
            submit?()
        } else { super.keyDown(with: event) }
    }
}

struct Composer: NSViewRepresentable {
    @Binding var text: String
    let submit: () -> Void
    var focusChanged: (Bool) -> Void = { _ in }
    final class Coordinator: NSObject, NSTextViewDelegate {
        var parent: Composer
        init(_ parent: Composer) { self.parent = parent }
        func textDidChange(_ notification: Notification) {
            if let view = notification.object as? NSTextView { parent.text = view.string }
        }
    }
    func makeCoordinator() -> Coordinator { Coordinator(self) }
    func makeNSView(context: Context) -> NSScrollView {
        let scroll = NSScrollView()
        scroll.drawsBackground = false; scroll.hasVerticalScroller = true
        let view = ComposerTextView()
        view.isRichText = false; view.isAutomaticQuoteSubstitutionEnabled = false
        view.font = .systemFont(ofSize: 15); view.textColor = Ink.native(0xE4E1D1)
        view.insertionPointColor = Ink.native(0xECE4B4)
        view.selectedTextAttributes = [.backgroundColor: Ink.native(0x4F4D3C), .foregroundColor: Ink.native(0xECE4B4)]
        view.drawsBackground = false; view.textContainerInset = NSSize(width: 0, height: 3)
        view.isVerticallyResizable = true; view.isHorizontallyResizable = false
        view.autoresizingMask = [.width]; view.textContainer?.widthTracksTextView = true
        view.textContainer?.containerSize = NSSize(width: 0, height: CGFloat.greatestFiniteMagnitude)
        view.minSize = .zero; view.maxSize = NSSize(width: CGFloat.greatestFiniteMagnitude, height: CGFloat.greatestFiniteMagnitude)
        view.delegate = context.coordinator; view.submit = submit; view.focusChanged = focusChanged
        view.setAccessibilityLabel("Сообщение")
        scroll.documentView = view
        DispatchQueue.main.async { view.window?.makeFirstResponder(view) }
        return scroll
    }
    func updateNSView(_ scroll: NSScrollView, context: Context) {
        context.coordinator.parent = self
        guard let view = scroll.documentView as? ComposerTextView else { return }
        if view.string != text { view.string = text }
        view.submit = submit
    }
}

struct ChatView: View {
    @ObservedObject var model: ChatModel
    @State private var draft = ""
    @State private var newName = ""
    @State private var showNew = false
    @State private var showContext = false
    @State private var inputFocused = false
    @State private var historyLimit = 100
    var conversationJobs: [Job] { model.jobs.filter { $0.session == model.active } }
    var canSend: Bool { model.connected && !draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }
    func submit() {
        guard canSend else { return }
        model.send(["action": "send", "text": draft, "session": model.active]); draft = ""
    }
    var sidebar: some View {
        VStack(alignment: .leading, spacing: 0) {
            Text("wchat").font(.system(size: 20, weight: .regular)).tracking(-0.5)
                .foregroundStyle(Ink.cream).padding(.bottom, 34)
            HStack {
                Text("Диалоги").font(.system(size: 11)).foregroundStyle(Ink.muted)
                Spacer()
                Button { showNew = true } label: { Image(systemName: "plus") }
                    .buttonStyle(QuietButton()).help("Новый диалог").accessibilityLabel("Новый диалог")
            }.padding(.bottom, 8)
            ScrollView {
                VStack(alignment: .leading, spacing: 3) {
                    ForEach(model.sessions) { session in
                        Button { model.send(["action": "select", "name": session.name]) } label: {
                            HStack(spacing: 10) {
                                Rectangle().fill(model.active == session.name ? Ink.cream : .clear).frame(width: 2, height: 18)
                                Text(session.name).lineLimit(1).truncationMode(.middle)
                                    .foregroundStyle(model.active == session.name ? Ink.cream : Ink.muted)
                                Spacer(minLength: 0)
                            }.font(.system(size: 13)).padding(.vertical, 8).contentShape(Rectangle())
                        }.buttonStyle(.plain)
                    }
                }
            }
            Hairline().padding(.bottom, 18)
            Button { showContext = true } label: {
                VStack(alignment: .leading, spacing: 5) {
                    Text("Общий контекст").font(.system(size: 13)).foregroundStyle(Ink.cream)
                    Text(model.commonContext.isEmpty ? "Проекты и файлы" : "Добавлен для всех диалогов")
                        .font(.system(size: 11)).foregroundStyle(Ink.muted)
                }.frame(maxWidth: .infinity, alignment: .leading).contentShape(Rectangle())
            }.buttonStyle(.plain).padding(.bottom, 24)
            HStack(spacing: 6) {
                Circle().fill(model.connected ? Ink.cream : Ink.muted).frame(width: 4, height: 4)
                Text(model.connection).font(.system(size: 10)).foregroundStyle(Ink.muted)
            }
        }.padding(.horizontal, 20).padding(.top, 25).padding(.bottom, 20)
            .frame(width: 190).background(Ink.sidebar)
    }
    var header: some View {
        HStack(alignment: .center, spacing: 20) {
            VStack(alignment: .leading, spacing: 5) {
                Text(model.active).font(.system(size: 16)).foregroundStyle(Ink.cream)
                Text(model.cwd.replacingOccurrences(of: NSHomeDirectory(), with: "~"))
                    .font(.system(size: 11, design: .monospaced)).foregroundStyle(Ink.muted)
                    .lineLimit(1).truncationMode(.middle).help(model.cwd)
            }
            Spacer(minLength: 10)
            Label(model.microphone.components(separatedBy: " · ").first ?? model.microphone,
                  systemImage: model.microphone.contains("выключен") ? "mic.slash" : "mic")
                .font(.system(size: 11)).foregroundStyle(Ink.muted).lineLimit(1).help(model.microphone)
        }.padding(.horizontal, 30).padding(.vertical, 22)
    }
    var composer: some View {
        VStack(spacing: 10) {
            HStack(alignment: .bottom, spacing: 14) {
                ZStack(alignment: .topLeading) {
                    if draft.isEmpty {
                        Text("Напишите поручение…").font(.system(size: 15)).foregroundStyle(Ink.muted)
                            .padding(.top, 3).padding(.leading, 5)
                    }
                    Composer(text: $draft, submit: submit, focusChanged: { inputFocused = $0 }).frame(height: 52)
                }
                Button(action: submit) { Image(systemName: "arrow.up").frame(width: 15, height: 18) }
                    .buttonStyle(QuietButton(outlined: true)).disabled(!canSend).help("Отправить").accessibilityLabel("Отправить")
            }.padding(14).background(Ink.field)
                .overlay { RoundedRectangle(cornerRadius: 5).stroke(inputFocused ? Ink.cream.opacity(0.65) : Ink.line, lineWidth: 1) }
            HStack {
                Text("Enter - отправить · Shift+Enter - новая строка").font(.system(size: 10)).foregroundStyle(Ink.muted)
                Spacer(minLength: 0)
                if model.busy {
                    Button("Очистить очередь") { model.send(["action": "clear"]) }.buttonStyle(QuietButton())
                    Button("Остановить") { model.send(["action": "cancel"]) }.buttonStyle(QuietButton(outlined: true))
                }
            }
        }.padding(.horizontal, 30).padding(.top, 12).padding(.bottom, 18)
    }
    var body: some View {
        HStack(spacing: 0) {
            sidebar
            VStack(spacing: 0) {
                header
                Hairline()
                if let error = model.error {
                    HStack {
                        Text(error).textSelection(.enabled)
                        Spacer()
                        Button { model.error = nil } label: { Image(systemName: "xmark") }.buttonStyle(QuietButton())
                    }.font(.callout).padding(12).background(Ink.field).foregroundStyle(.orange)
                }
                ScrollViewReader { proxy in
                    ScrollView {
                        // Eager, bounded layout avoids estimating changing rows while scrolling.
                        VStack(alignment: .leading, spacing: 0) {
                            if conversationJobs.count > historyLimit {
                                Button("Показать предыдущие сообщения") { historyLimit += 100 }
                                    .buttonStyle(QuietButton()).padding(.vertical, 12)
                            }
                            if model.jobs.filter({ $0.session == model.active }).isEmpty {
                                VStack(alignment: .leading, spacing: 12) {
                                    Text("Пока нет сообщений").font(.system(size: 22)).foregroundStyle(Ink.cream)
                                    Text(model.microphone.hasPrefix("Слушаю")
                                         ? "Напишите поручение внизу или скажите «чат», затем задачу."
                                         : "Напишите поручение внизу, чтобы начать этот диалог.")
                                        .font(.system(size: 14)).foregroundStyle(Ink.muted).lineSpacing(5)
                                    Text("Ответы и выполненные поручения появятся здесь.")
                                        .font(.system(size: 12)).foregroundStyle(Ink.muted)
                                }.frame(maxWidth: 440, alignment: .leading).padding(.vertical, 65)
                            }
                            ForEach(Array(conversationJobs.suffix(historyLimit))) { job in TurnView(job: job) }
                            Color.clear.frame(height: 1).id("bottom")
                        }.padding(.horizontal, 30).padding(.bottom, 12).frame(maxWidth: .infinity, alignment: .leading)
                    }.task(id: model.revision) {
                        // Let the current layout transaction settle; collapse streaming bursts.
                        do { try await Task.sleep(for: .milliseconds(60)) } catch { return }
                        guard !Task.isCancelled else { return }
                        proxy.scrollTo("bottom", anchor: .bottom)
                    }
                }
                if !model.permissions.isEmpty {
                    Hairline()
                    ScrollView {
                        VStack(spacing: 12) {
                            ForEach(model.permissions) { permission in
                                PermissionCard(permission: permission, model: model)
                                    .frame(maxWidth: .infinity, alignment: .leading)
                            }
                        }.padding(.horizontal, 30).padding(.vertical, 12)
                    }.frame(height: 230)
                        .accessibilityLabel("Запросы разрешений")
                }
                composer
            }.background(Ink.canvas)
        }.foregroundStyle(Ink.text).frame(maxWidth: .infinity, maxHeight: .infinity)
        .alert("Новый диалог", isPresented: $showNew) {
            TextField("Имя диалога", text: $newName)
            Button("Создать") { model.send(["action": "new", "name": newName]); newName = "" }
            Button("Отмена", role: .cancel) {}
        } message: { Text("Буквы, цифры, дефис или подчёркивание. Каждый диалог сохраняет свой контекст Codex.") }
        .sheet(isPresented: $showContext) { ContextEditor(model: model) }
    }
}

struct ContextEditor: View {
    @ObservedObject var model: ChatModel
    @Environment(\.dismiss) private var dismiss
    @State private var context = ""
    @State private var cwd = ""
    @FocusState private var fieldFocused: Bool
    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(alignment: .firstTextBaseline) {
                Text("Общий контекст").font(.system(size: 22)).foregroundStyle(Ink.cream)
                Spacer()
                Text("Для всех диалогов").font(.system(size: 11)).foregroundStyle(Ink.muted)
            }.padding(.bottom, 12)
            Text("Опишите проекты и укажите пути к рабочим файлам.")
                .font(.system(size: 14)).foregroundStyle(Ink.text).padding(.bottom, 20)
            ZStack(alignment: .topLeading) {
                if context.isEmpty {
                    Text("Например:\nЛичные заметки - ~/Documents/jack\nПроект wchat - ~/wchat, голосовой ассистент для macOS.")
                        .font(.system(size: 14)).foregroundStyle(Ink.muted).lineSpacing(6).padding(12)
                }
                TextEditor(text: $context).font(.system(size: 14)).foregroundStyle(Ink.text)
                    .scrollContentBackground(.hidden).padding(8).focused($fieldFocused)
            }.background(Ink.field)
                .overlay { RoundedRectangle(cornerRadius: 4).stroke(fieldFocused ? Ink.cream.opacity(0.65) : Ink.line, lineWidth: 1) }
                .frame(minHeight: 210).padding(.bottom, 10)
            HStack {
                Text("Передаётся Codex с каждым новым поручением.")
                Spacer()
                Text("\(context.count) / 64000").monospacedDigit()
            }.font(.system(size: 11)).foregroundStyle(Ink.muted).padding(.bottom, 24)
            Text("Папка новых диалогов").font(.system(size: 13)).foregroundStyle(Ink.cream).padding(.bottom, 8)
            TextField("~/Documents/jack", text: $cwd).textFieldStyle(.plain)
                .font(.system(size: 12, design: .monospaced)).padding(10).background(Ink.field)
                .overlay { RoundedRectangle(cornerRadius: 4).stroke(Ink.line, lineWidth: 1) }.padding(.bottom, 10)
            Text("Файлы не загружаются автоматически. Существующие диалоги сохраняют свою папку и прежнюю переписку.")
                .font(.system(size: 11)).foregroundStyle(Ink.muted).fixedSize(horizontal: false, vertical: true).lineSpacing(3)
            if let error = model.error { Text(error).foregroundStyle(.orange).font(.caption).padding(.top, 10) }
            Spacer(minLength: 20)
            Hairline().padding(.bottom, 14)
            HStack {
                Text("Сохраняется на этом Mac").font(.system(size: 11)).foregroundStyle(Ink.muted)
                Spacer()
                Button("Отмена") { dismiss() }.buttonStyle(QuietButton())
                Button("Сохранить") {
                    model.error = nil
                    model.send(["action": "save_context", "context": context, "cwd": cwd])
                }.buttonStyle(QuietButton(outlined: true)).disabled(context.count > 64000 || !model.connected)
            }
        }.padding(28).frame(width: 640, height: 560).background(Ink.canvas).foregroundStyle(Ink.text)
        .onAppear { context = model.commonContext; cwd = model.defaultCwd; fieldFocused = true }
        .onChange(of: model.contextSaved) { _, _ in dismiss() }
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    var window: NSWindow!
    var model: ChatModel!
    func applicationDidFinishLaunching(_ notification: Notification) {
        model = ChatModel()
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1060, height: 760),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        window.title = "wchat"
        window.titleVisibility = .hidden
        window.titlebarAppearsTransparent = true
        window.backgroundColor = Ink.native(0x181915)
        window.appearance = NSAppearance(named: .darkAqua)
        let hosting = NSHostingView(rootView: ChatView(model: model).tint(Ink.cream).preferredColorScheme(.dark))
        // AppKit owns window geometry. Content changes must not renegotiate its bounds.
        hosting.sizingOptions = []
        window.contentMinSize = NSSize(width: 800, height: 560)
        window.contentView = hosting
        window.center(); window.makeKeyAndOrderFront(nil)
        let menu = NSMenu(); let item = NSMenuItem(); menu.addItem(item)
        let appMenu = NSMenu(); appMenu.addItem(withTitle: "Завершить wchat", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        item.submenu = appMenu
        let edit = NSMenuItem(); menu.addItem(edit); let editMenu = NSMenu(title: "Правка"); edit.submenu = editMenu
        for (title, action, key) in [("Вырезать", "cut:", "x"), ("Копировать", "copy:", "c"), ("Вставить", "paste:", "v"), ("Выделить всё", "selectAll:", "a")] {
            editMenu.addItem(withTitle: title, action: Selector(action), keyEquivalent: key)
        }
        NSApp.mainMenu = menu
        NSApp.activate(ignoringOtherApps: true)
        model.start()
    }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard model?.process.isRunning == true else { return .terminateNow }
        model.onExit = { sender.reply(toApplicationShouldTerminate: true) }; model.stop(); return .terminateLater
    }
}

#if !WCHAT_TEST
@main struct WchatMain {
    static func main() {
        let app = NSApplication.shared
        let delegate = AppDelegate()
        app.delegate = delegate; app.setActivationPolicy(.regular); app.run()
    }
}

#endif
