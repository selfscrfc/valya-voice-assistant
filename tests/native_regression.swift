import Foundation
import AppKit

@main struct NativeRegression {
    @MainActor static func main() throws {
        let model = ChatModel()
        func receive(_ event: [String: Any]) throws {
            model.receive(try JSONSerialization.data(withJSONObject: event))
        }
        func check(_ ok: Bool, _ message: String) {
            if !ok { fputs("FAIL: \(message)\n", stderr); exit(1) }
        }
        let job: [String: Any] = ["id": 1, "session": "default", "prompt": "Alarm test", "source": "voice",
                                  "status": "running", "response": "Checking access…", "error": NSNull()]
        var snapshot: [String: Any] = ["kind": "snapshot", "active": "default", "sessions": [], "jobs": [job], "requests": [:]]
        try receive(snapshot)
        let initial = model.revision
        for _ in 0..<100 {
            try receive(["kind": "voice_wake", "text": ""])
            try receive(["kind": "voice_discard", "text": ""])
        }
        check(model.revision == initial, "microphone state changes must not request transcript layout/scroll")
        try receive(["kind": "voice_waiting", "text": "Waiting for a microphone"])
        try receive(["kind": "voice_discard", "text": ""])
        check(model.microphone == "Waiting for a microphone", "discard must not claim a disconnected microphone is listening")
        try receive(["kind": "voice_ready", "text": "Wireless Mic Rx"])
        try receive(["kind": "voice_prompt", "text": "test"])
        check(model.microphone.contains("Wireless Mic Rx"), "selected device must remain visible after a command")
        let params = try JSONSerialization.jsonObject(with: Data(contentsOf: URL(fileURLWithPath: "tests/fixtures/clock-permission.json")))
        snapshot["requests"] = ["clock": ["method": "mcpServer/elicitation/request", "params": params]]
        for _ in 0..<100 { try receive(snapshot) }
        check(model.revision == initial, "permission snapshots with unchanged messages must not request scrolling")
        check(model.permissions.count == 1, "Clock permission must remain available")
        try receive(["kind": "delta", "job": 1, "text": "more", "response": "Checking access…more"])
        check(model.revision == initial + 1, "new reply text must request one transcript update")
        check(model.jobs[0].response.hasSuffix("more"), "streamed reply must be retained")
        try receive(["kind": "voice_transcribing", "text": "Transcribing with Whisper…"])
        check(model.microphone == "Transcribing with Whisper…", "hybrid inference must be visible")
        try receive(["kind": "voice_discard", "text": ""])
        check(model.microphone.contains("Wireless Mic Rx"), "discarded transcription must restore device status")
        model.jobs = []
        try receive(["kind": "speech_started", "text": "Short reply"])
        check(model.speaking && model.busy, "playback must expose the Stop control")
        try receive(["kind": "speech_finished", "text": ""])
        check(!model.speaking && !model.busy, "playback completion must clear the Stop control")
        try receive(["kind": "speech_error", "text": "test"])
        check(model.error == "Speech playback: test", "playback errors must be visible")
        let application = NSApplication.shared
        let delegate = AppDelegate()
        check(!delegate.applicationShouldTerminateAfterLastWindowClosed(application),
              "closing chat must keep the background assistant running")
        delegate.model = model
        delegate.window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 200, height: 100),
                                   styleMask: [.titled, .closable], backing: .buffered, defer: false)
        delegate.installStatusMenu()
        check(delegate.statusItem.menu?.items.contains { $0.title == "Show Chat" && $0.target === delegate } == true,
              "menu bar must provide a chat reopen action")
        check(delegate.statusItem.menu?.items.contains { $0.title == "Quit Valya" && $0.target === application } == true,
              "menu bar must provide explicit quit")
        check(!delegate.windowShouldClose(delegate.window), "close must hide and retain the chat window")
        check(!delegate.window.isVisible, "hidden chat must not remain onscreen")
        NSStatusBar.system.removeStatusItem(delegate.statusItem)
        print("Native regression checks passed")
    }
}
