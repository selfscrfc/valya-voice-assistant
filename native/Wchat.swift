import AppKit
import SwiftUI

struct Job: Identifiable, Decodable {
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
        switch kind {
        case "snapshot":
            if let raw = event["jobs"], let data = try? JSONSerialization.data(withJSONObject: raw),
               let value = try? JSONDecoder().decode([Job].self, from: data) { jobs = value }
            if let raw = event["sessions"], let data = try? JSONSerialization.data(withJSONObject: raw),
               let value = try? JSONDecoder().decode([Conversation].self, from: data) { sessions = value }
            active = event["active"] as? String ?? active
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
                jobs[index].response = event["response"] as? String ?? jobs[index].response + text
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
        revision += 1
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
                    let code = chunk.contains("\n") ? String(chunk.drop(while: { $0 != "\n" }).dropFirst()) : chunk
                    ScrollView(.horizontal) {
                        Text(code).font(.system(.body, design: .monospaced)).padding(12).textSelection(.enabled)
                    }.background(Color.black.opacity(0.18)).clipShape(RoundedRectangle(cornerRadius: 8))
                } else if !chunk.isEmpty {
                    Text((try? AttributedString(markdown: chunk, options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace)))
                         ?? AttributedString(chunk)).textSelection(.enabled).lineSpacing(4)
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
            HStack {
                Spacer(minLength: 70)
                VStack(alignment: .leading, spacing: 8) {
                    Label(job.source == "voice" ? "Вы · голос" : "Вы", systemImage: job.source == "voice" ? "mic" : "person")
                        .font(.caption).foregroundStyle(.secondary)
                    Text(job.prompt).textSelection(.enabled).lineSpacing(4)
                }.padding(16).background(Color.accentColor.opacity(0.18)).clipShape(RoundedRectangle(cornerRadius: 16))
            }
            VStack(alignment: .leading, spacing: 10) {
                HStack(spacing: 8) {
                    Image(systemName: "sparkle").foregroundStyle(Color.accentColor)
                    Text("wchat").fontWeight(.semibold)
                    Text(status).font(.caption).foregroundStyle(.secondary)
                    Spacer()
                    if !job.response.isEmpty {
                        Button { NSPasteboard.general.clearContents(); NSPasteboard.general.setString(job.response, forType: .string) }
                            label: { Image(systemName: "doc.on.doc") }.buttonStyle(.plain).help("Скопировать ответ")
                    }
                }
                if job.response.isEmpty && ["queued", "starting", "running"].contains(job.status) {
                    ProgressView().controlSize(.small)
                } else { MarkdownMessage(text: job.response) }
                if let error = job.error, !error.isEmpty { Text(error).font(.caption).foregroundStyle(.red).textSelection(.enabled) }
            }.padding(.trailing, 35)
        }.padding(.vertical, 16)
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
                Button("Подтвердить") { reply("accept") }.buttonStyle(.borderedProminent)
            }
        }.padding(16).background(Color.orange.opacity(0.08)).clipShape(RoundedRectangle(cornerRadius: 12))
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
    override func keyDown(with event: NSEvent) {
        if [36, 76].contains(event.keyCode) && !event.modifierFlags.contains(.shift) {
            submit?()
        } else { super.keyDown(with: event) }
    }
}

struct Composer: NSViewRepresentable {
    @Binding var text: String
    let submit: () -> Void
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
        view.font = .systemFont(ofSize: 14); view.textColor = .labelColor
        view.drawsBackground = false; view.textContainerInset = NSSize(width: 0, height: 3)
        view.isVerticallyResizable = true; view.isHorizontallyResizable = false
        view.autoresizingMask = [.width]; view.textContainer?.widthTracksTextView = true
        view.textContainer?.containerSize = NSSize(width: 0, height: CGFloat.greatestFiniteMagnitude)
        view.minSize = .zero; view.maxSize = NSSize(width: CGFloat.greatestFiniteMagnitude, height: CGFloat.greatestFiniteMagnitude)
        view.delegate = context.coordinator; view.submit = submit
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
    func submit() {
        guard model.connected, !draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return }
        model.send(["action": "send", "text": draft, "session": model.active]); draft = ""
    }
    var body: some View {
        HStack(spacing: 0) {
            VStack(alignment: .leading, spacing: 20) {
                HStack { Image(systemName: "waveform").foregroundStyle(Color.accentColor); Text("wchat").font(.title2.bold()) }.padding(.top, 12)
                Button { showNew = true } label: { Label("Новый диалог", systemImage: "plus") }.buttonStyle(.bordered)
                ScrollView {
                    VStack(spacing: 5) {
                        ForEach(model.sessions) { session in
                            Button { model.send(["action": "select", "name": session.name]) } label: {
                                HStack { Image(systemName: "bubble.left"); Text(session.name).lineLimit(1); Spacer() }
                                    .padding(10).background(model.active == session.name ? Color.accentColor.opacity(0.15) : .clear)
                                    .clipShape(RoundedRectangle(cornerRadius: 8))
                            }.buttonStyle(.plain)
                        }
                    }
                }
                Button { showContext = true } label: { Label("Общий контекст", systemImage: "doc.text") }.buttonStyle(.plain)
                Label(model.connection, systemImage: model.connected ? "circle.fill" : "circle")
                    .font(.caption).foregroundStyle(.secondary)
                Text("История сохраняется на этом Mac").font(.caption2).foregroundStyle(.tertiary)
            }.padding(18).frame(width: 205).background(Color(nsColor: .windowBackgroundColor))
            Divider()
            VStack(spacing: 0) {
                HStack {
                    VStack(alignment: .leading, spacing: 4) {
                        Text(model.active).font(.headline)
                        Text(model.cwd).font(.caption).foregroundStyle(.secondary).lineLimit(1).truncationMode(.middle)
                    }
                    Spacer()
                    Label(model.microphone, systemImage: model.microphone.contains("выключен") ? "mic.slash" : "mic")
                        .font(.caption).foregroundStyle(.secondary).lineLimit(2).frame(maxWidth: 300)
                }.padding(20)
                Divider()
                if let error = model.error {
                    HStack { Text(error).textSelection(.enabled); Spacer(); Button { model.error = nil } label: { Image(systemName: "xmark") } }
                        .font(.callout).padding(12).background(Color.red.opacity(0.12))
                }
                ScrollViewReader { proxy in
                    ScrollView {
                        LazyVStack(alignment: .leading, spacing: 8) {
                            if model.jobs.filter({ $0.session == model.active }).isEmpty {
                                VStack(alignment: .leading, spacing: 12) {
                                    Text("Чем помочь?").font(.largeTitle.bold())
                                    Text("Напишите поручение или скажите «чат», затем задачу.").foregroundStyle(.secondary)
                                }.padding(.vertical, 60)
                            }
                            ForEach(model.jobs.filter { $0.session == model.active }) { job in TurnView(job: job) }
                            ForEach(model.permissions) { permission in PermissionCard(permission: permission, model: model) }
                            Color.clear.frame(height: 1).id("bottom")
                        }.padding(.horizontal, 28).padding(.bottom, 16).frame(maxWidth: 900).frame(maxWidth: .infinity)
                    }
                    .onChange(of: model.revision) { _, _ in proxy.scrollTo("bottom", anchor: .bottom) }
                }
                VStack(spacing: 10) {
                    HStack(alignment: .bottom, spacing: 12) {
                        ZStack(alignment: .topLeading) {
                            if draft.isEmpty { Text("Сообщение…").foregroundStyle(.tertiary).padding(.top, 3).padding(.leading, 5) }
                            Composer(text: $draft, submit: submit).frame(height: 58)
                        }
                        Button(action: submit) { Image(systemName: "arrow.up").fontWeight(.bold).padding(4) }
                            .buttonStyle(.borderedProminent).disabled(!model.connected || draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                            .help("Отправить")
                    }.padding(14).background(Color(nsColor: .controlBackgroundColor)).clipShape(RoundedRectangle(cornerRadius: 14))
                    HStack {
                        Text("Enter - отправить · Shift+Enter - новая строка").font(.caption2).foregroundStyle(.tertiary)
                        Spacer()
                        if model.busy {
                            Button("Очистить очередь") { model.send(["action": "clear"]) }.buttonStyle(.plain)
                            Button("Остановить") { model.send(["action": "cancel"]) }.buttonStyle(.bordered)
                        }
                    }.font(.caption)
                }.padding(20)
            }.background(Color(nsColor: .textBackgroundColor))
        }.frame(minWidth: 800, minHeight: 560)
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
    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Общий контекст").font(.title2.bold())
            Text("Расскажите о проектах и укажите пути к нужным файлам. Этот текст сохраняется на Mac и передаётся Codex с каждым новым поручением во всех диалогах. Файлы не загружаются автоматически.")
                .foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            TextEditor(text: $context).font(.body).padding(8)
                .background(Color(nsColor: .textBackgroundColor)).clipShape(RoundedRectangle(cornerRadius: 8))
                .frame(minHeight: 220)
            Text("Рабочая папка для новых диалогов").font(.headline)
            TextField("~/Documents/jack", text: $cwd).textFieldStyle(.roundedBorder)
            Text("Сохранённые диалоги сохраняют свои рабочие папки. Изменения контекста применяются при запуске следующего поручения; прежние сообщения остаются в истории.")
                .font(.caption).foregroundStyle(.secondary)
            if let error = model.error { Text(error).foregroundStyle(.red).font(.caption) }
            HStack {
                Text("\(context.count) / 64000").font(.caption).foregroundStyle(.secondary)
                Spacer()
                Button("Отмена") { dismiss() }
                Button("Сохранить") {
                    model.error = nil
                    model.send(["action": "save_context", "context": context, "cwd": cwd])
                }.buttonStyle(.borderedProminent).disabled(context.count > 64000 || !model.connected)
            }
        }.padding(24).frame(width: 620, height: 530)
        .onAppear { context = model.commonContext; cwd = model.defaultCwd }
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
        window.contentView = NSHostingView(rootView: ChatView(model: model).tint(.indigo))
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

@main struct WchatMain {
    static func main() {
        let app = NSApplication.shared
        let delegate = AppDelegate()
        app.delegate = delegate; app.setActivationPolicy(.regular); app.run()
    }
}
