import Foundation

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
        try receive(["kind": "voice_waiting", "text": "Ожидаю микрофон"])
        try receive(["kind": "voice_discard", "text": ""])
        check(model.microphone == "Ожидаю микрофон", "discard must not claim a disconnected microphone is listening")
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
        print("Native regression checks passed")
    }
}
