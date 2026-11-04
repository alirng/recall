// The window's only door to recall: `recall api`, HTTP and JSON on a unix socket in
// ~/.agents/recall/running. The app never opens recall.db or the notes files itself.
// URLSession can't reach a unix socket (and would write caches under ~/Library), so this is a
// small POSIX client: one connection per request, read to the end, then a server-sent events
// stream read on its own thread, which blocks in read() and costs nothing while idle.

import AppKit

enum API {
    static let home = FileManager.default.homeDirectoryForCurrentUser.path
    static let socketPath = home + "/.agents/recall/running/api.sock"
    static let minimumAPI = 1
    static let queue = DispatchQueue(label: "recall.api", qos: .userInitiated, attributes: .concurrent)

    struct Failure: Error {
        var status = 0
        var code = "unreachable"
        var message: String
    }

    // MARK: Connecting

    static func connect(timeout: Int = 30) -> Int32? {
        let fd = socket(AF_UNIX, SOCK_STREAM, 0)
        guard fd >= 0 else { return nil }
        var on: Int32 = 1
        setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &on, socklen_t(MemoryLayout<Int32>.size))
        if timeout > 0 {
            var tv = timeval(tv_sec: timeout, tv_usec: 0)
            setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, socklen_t(MemoryLayout<timeval>.size))
        }
        var addr = sockaddr_un()
        addr.sun_family = sa_family_t(AF_UNIX)
        let bytes = Array(socketPath.utf8)
        guard bytes.count < MemoryLayout.size(ofValue: addr.sun_path) else { close(fd); return nil }
        withUnsafeMutableBytes(of: &addr.sun_path) { raw in
            raw.copyBytes(from: bytes)
            raw[bytes.count] = 0
        }
        let ok = withUnsafePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                Darwin.connect(fd, $0, socklen_t(MemoryLayout<sockaddr_un>.size))
            }
        }
        if ok != 0 { close(fd); return nil }
        return fd
    }

    static func send(_ fd: Int32, _ path: String) -> Bool {
        let request = "GET \(path) HTTP/1.1\r\nHost: recall\r\nX-Recall-Client: app\r\nConnection: close\r\n\r\n"
        let data = Array(request.utf8)
        return data.withUnsafeBytes { write(fd, $0.baseAddress, data.count) } == data.count
    }

    /// One GET, read to the end. Blocking: call it off the main thread.
    static func raw(_ path: String) -> Result<Data, Failure> {
        let started = Date()
        defer { if Probe.on { FileHandle.standardError.write(Data(String(format: "  %4.0f ms %@\n", Date().timeIntervalSince(started) * 1000, path).utf8)) } }
        guard let fd = connect() else { return .failure(Failure(message: "recall isn't running")) }
        defer { close(fd) }
        guard send(fd, path) else { return .failure(Failure(message: "Couldn't ask recall")) }
        var all = Data()
        var buf = [UInt8](repeating: 0, count: 65536)
        while true {
            let n = read(fd, &buf, buf.count)
            if n <= 0 { break }
            all.append(buf, count: n)
        }
        guard let split = all.range(of: Data("\r\n\r\n".utf8)) else {
            return .failure(Failure(message: "recall gave no answer"))
        }
        let head = String(decoding: all[..<split.lowerBound], as: UTF8.self).lowercased()
        var body = all[split.upperBound...]
        let status = Int(head.split(separator: " ").dropFirst().first ?? "") ?? 0
        if head.contains("transfer-encoding: chunked") {
            var chunks = Chunks()
            body = chunks.feed(Data(body))[...]
        }
        if status >= 400 {
            struct Wrapped: Decodable { struct E: Decodable { let code: String; let message: String }; let error: E }
            let e = try? JSONDecoder().decode(Wrapped.self, from: Data(body))
            return .failure(Failure(status: status, code: e?.error.code ?? "http_\(status)", message: e?.error.message ?? "recall answered \(status)"))
        }
        return .success(Data(body))
    }

    /// GET and decode on the API queue; the answer comes back on the main thread.
    static func get<T: Decodable>(_ path: String, _ type: T.Type, _ done: @escaping (Result<T, Failure>) -> Void) {
        queue.async {
            let result = raw(path).flatMap { data -> Result<T, Failure> in
                do { return .success(try JSONDecoder().decode(T.self, from: data)) } catch {
                    return .failure(Failure(code: "decode", message: "Couldn't read recall's answer to \(path.split(separator: "?").first ?? "")"))
                }
            }
            DispatchQueue.main.async { done(result) }
        }
    }

    static func q(_ s: String) -> String {
        s.addingPercentEncoding(withAllowedCharacters: .alphanumerics.union(CharacterSet(charactersIn: "-._~"))) ?? s
    }

    // MARK: Starting recall api

    /// Where `recall api` can come from: a dev checkout written into Info.plist by build.sh, then the
    /// folders uv, pipx and Homebrew install into. An app opened from Finder has no shell PATH.
    static func commands() -> [[String]] {
        var out: [[String]] = []
        if let dev = Bundle.main.object(forInfoDictionaryKey: "RecallAPICommand") as? [String], !dev.isEmpty { out.append(dev) }
        for dir in [home + "/.local/bin", "/opt/homebrew/bin", "/usr/local/bin"] {
            let path = dir + "/recall"
            if FileManager.default.isExecutableFile(atPath: path) { out.append([path, "api"]) }
        }
        return out
    }

    static let starting = NSLock()

    /// Starts `recall api` if nothing answers on the socket, and waits up to 10 s for it. The server
    /// exits by itself once idle, so the app never stops it. Blocking: call it off the main thread.
    static func ensureRunning() -> Failure? {
        starting.lock()
        defer { starting.unlock() }
        if let fd = connect(timeout: 2) { close(fd); return nil }
        let commands = commands()
        if commands.isEmpty {
            return Failure(code: "not_installed", message: "recall isn't set up on this Mac. Install it with uv tool install 'recall-agents[embeddings]', then run recall install.")
        }
        var tooOld: String?
        for command in commands {
            let p = Process()
            p.executableURL = URL(fileURLWithPath: command[0])
            p.arguments = Array(command.dropFirst())
            p.currentDirectoryURL = URL(fileURLWithPath: home)
            p.standardInput = FileHandle.nullDevice
            p.standardOutput = FileHandle.nullDevice
            let err = Pipe()
            p.standardError = err
            do { try p.run() } catch { continue }
            var ready = false
            for _ in 0..<100 {  // only while starting: 10 s at most, then never again
                if let fd = connect(timeout: 2) { close(fd); ready = true; break }
                if !p.isRunning { break }
                usleep(100_000)
            }
            if ready {
                // Keep draining its stderr so a chatty server never blocks on a full pipe.
                err.fileHandleForReading.readabilityHandler = { h in
                    if h.availableData.isEmpty { h.readabilityHandler = nil }
                }
                return nil
            }
            if !p.isRunning {
                let text = String(decoding: err.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self)
                if text.contains("invalid choice") || text.contains("'api'") {
                    tooOld = version(command[0]) ?? "this release"
                }
            } else {
                p.terminate()
            }
        }
        if let tooOld {
            return Failure(code: "too_old", message: "This recall (\(tooOld)) is too old for the app: it has no recall api. Upgrade it with uv tool upgrade recall-agents.")
        }
        return Failure(message: "recall api didn't start. See ~/.agents/recall/logs/api.log.")
    }

    static func version(_ recall: String) -> String? {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: recall)
        p.arguments = ["--version"]
        let out = Pipe()
        p.standardOutput = out
        p.standardError = FileHandle.nullDevice
        guard (try? p.run()) != nil else { return nil }
        let text = String(decoding: out.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self)
        p.waitUntilExit()
        return text.split(separator: " ").last.map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
    }
}

/// Undoes HTTP chunked framing as bytes arrive.
struct Chunks {
    var buffer = Data()
    var need: Int?  // bytes left in the current chunk, nil while reading a size line
    var done = false

    mutating func feed(_ data: Data) -> Data {
        buffer.append(data)
        var out = Data()
        while !done {
            if let n = need {
                guard buffer.count >= n + 2 else { break }
                out.append(buffer.prefix(n))
                buffer.removeFirst(n + 2)
                need = nil
            } else {
                guard let eol = buffer.range(of: Data("\r\n".utf8)) else { break }
                let line = String(decoding: buffer[..<eol.lowerBound], as: UTF8.self)
                buffer.removeSubrange(..<eol.upperBound)
                let size = Int(line.split(separator: ";").first ?? "", radix: 16) ?? 0
                if size == 0 { done = true } else { need = size }
            }
        }
        return out
    }
}

/// `/v1/events` on its own thread. It blocks in read() until recall says something changed, so an
/// open window with nothing changing uses no CPU. `stop()` shuts the socket, which ends the read.
final class Events {
    private var fd: Int32 = -1
    private let lock = NSLock()
    private var stopped = false
    let onChange: ([String]) -> Void
    let onEnd: () -> Void

    init(onChange: @escaping ([String]) -> Void, onEnd: @escaping () -> Void) {
        self.onChange = onChange
        self.onEnd = onEnd
    }

    func start() {
        let t = Thread { [weak self] in self?.run() }
        t.name = "recall.events"
        t.qualityOfService = .utility
        t.start()
    }

    func stop() {
        lock.lock()
        stopped = true
        if fd >= 0 { shutdown(fd, SHUT_RDWR) }
        lock.unlock()
    }

    private func run() {
        guard let s = API.connect(timeout: 0), API.send(s, "/v1/events") else { finish(); return }
        lock.lock()
        fd = s
        let early = stopped
        lock.unlock()
        if early { close(s); return }
        var headerDone = false, chunked = false
        var pending = Data(), text = ""
        var chunks = Chunks()
        var buf = [UInt8](repeating: 0, count: 8192)
        while true {
            let n = read(s, &buf, buf.count)
            if n <= 0 { break }
            var data = Data(buf[0..<n])
            if !headerDone {
                pending.append(data)
                guard let split = pending.range(of: Data("\r\n\r\n".utf8)) else { continue }
                chunked = String(decoding: pending[..<split.lowerBound], as: UTF8.self).lowercased().contains("chunked")
                data = Data(pending[split.upperBound...])
                headerDone = true
            }
            if chunked { data = chunks.feed(data) }
            text += String(decoding: data, as: UTF8.self).replacingOccurrences(of: "\r\n", with: "\n")
            while let end = text.range(of: "\n\n") {
                let block = String(text[..<end.lowerBound])
                text.removeSubrange(..<end.upperBound)
                handle(block)
            }
        }
        lock.lock()
        close(s)
        fd = -1
        lock.unlock()
        finish()
    }

    private func handle(_ block: String) {
        var event = "message", payload = ""
        for line in block.split(separator: "\n") {
            if line.hasPrefix("event:") { event = line.dropFirst(6).trimmingCharacters(in: .whitespaces) }
            if line.hasPrefix("data:") { payload += line.dropFirst(5).trimmingCharacters(in: .whitespaces) }
        }
        guard event == "changed" else { return }
        struct Changed: Decodable { let topics: [String] }
        let topics = (try? JSONDecoder().decode(Changed.self, from: Data(payload.utf8)))?.topics ?? []
        DispatchQueue.main.async { self.onChange(topics) }
    }

    private func finish() {
        lock.lock()
        let wanted = !stopped
        lock.unlock()
        if wanted { DispatchQueue.main.async { self.onEnd() } }
    }
}

// MARK: - Shapes (docs/api.md). Optional wherever a field may be missing, so a newer server still reads.

struct Info: Decodable {
    struct Instance: Decodable { let name: String; let kind: String? }
    let recall: String
    let api: Int
    let instance: Instance?
    let live_updates: Bool?
    let home: String?
    let memory: String?
}

struct Stats: Decodable {
    struct Latest: Decodable { let chat_id: String; let title: String?; let project: String? }
    struct Agent: Decodable { let chats: Int; let messages: Int?; let latest: String?; let latest_chat: Latest? }
    let agents: [String: Agent]
    let chats: Int
    let messages: Int?
    let notes: Int?
}

struct NoteRow: Decodable, Identifiable, Equatable {
    let note_id: String
    let type: String
    let scope: String?
    let project: String?
    let title: String
    let gist: String?
    let confidence: String?
    let status: String?
    let created: String?
    let updated: String?
    let last_seen: String?
    let sources: Int?
    let rules: Int?
    var id: String { note_id }
}

struct NotesPage: Decodable {
    struct Facets: Decodable { let types: [String: Int]?; let projects: [String: Int]? }
    let total: Int
    let offset: Int
    let notes: [NoteRow]
    let facets: Facets?
}

struct Note: Decodable {
    struct Source: Decodable { let chat_id: String; let agent: String?; let title: String?; let date: String?; let messages: Int?; let found: Bool? }
    struct Rule: Decodable { let id: String; let text: String; let status: String?; let first_seen: String?; let reason: String? }
    struct Related: Decodable { let note_id: String; let title: String; let type: String?; let reason: String? }
    struct Change: Decodable { let kind: String; let rule_id: String?; let text: String? }
    struct Commit: Decodable { let commit: String; let date: String?; let subject: String?; let changes: [Change]? }
    let note_id: String
    let type: String
    let scope: String?
    let project: String?
    let title: String
    let confidence: String?
    let status: String?
    let created: String?
    let updated: String?
    let last_seen: String?
    let body: String?
    let path: String?
    let source_chats: [Source]?
    let rules_all: [Rule]?
    let related: [Related]?
    let history: [Commit]?
}

struct ChatRow: Decodable, Identifiable, Equatable {
    let chat_id: String
    let agent: String
    let title: String?
    let branch: String?
    let updated: String?
    let messages: Int?
    let project: String?
    let folder: String?
    var id: String { chat_id }
}

struct ChatsPage: Decodable {
    let total: Int
    let offset: Int
    let chats: [ChatRow]
}

struct Transcript: Decodable {
    struct Message: Decodable, Identifiable { let seq: Int; let role: String; let time: String?; let text: String; var id: Int { seq } }
    let chat_id: String
    let agent: String
    let title: String?
    let branch: String?
    let messages: Int?
    let next_offset: Int?
    let transcript: [Message]
    let project: String?
    let folder: String?
}

struct SearchResult: Decodable {
    struct Filter: Decodable { let key: String; let value: String }
    struct Chat: Decodable, Identifiable { let chat_id: String; let agent: String; let title: String?; let updated: String?; let snippet: String?; let project: String?; var id: String { chat_id } }
    struct NoteHit: Decodable, Identifiable { let note_id: String; let type: String?; let project: String?; let title: String; let gist: String?; let last_seen: String?; var id: String { note_id } }
    let query: String?
    let filters: [Filter]?
    let chats: [Chat]?
    let notes: [NoteHit]?
    let took_ms: Int?
}

struct Projects: Decodable {
    struct Project: Decodable, Identifiable { let name: String; let chats: Int; let notes: Int; let agents: [String]?; let latest: String?; var id: String { name } }
    let projects: [Project]
}

struct Runs: Decodable {
    struct Phase: Decodable { let name: String; let started: String?; let finished: String?; let result: String? }
    struct Run: Decodable {
        let id: Int
        let started: String?
        let finished: String?
        let seconds: Double?
        let backend: String?
        let chats_read: Int?
        let model_calls: Int?
        let cost_usd: Double?
        let batches: Int?
        let counts: [String: Int]?
        let error: String?
        let phases: [Phase]?
    }
    let latest: Run?
}

struct Storage: Decodable {
    struct Item: Decodable, Identifiable { let label: String; let path: String; let bytes: Int; let kind: String; let exists: Bool?; var id: String { path } }
    let items: [Item]
    let totals: [String: Int]?
    let total_bytes: Int?
    let kinds: [String: String]?
}

struct Config: Decodable {
    struct Key: Decodable { let configured: Bool?; let stored_in: String? }
    struct Model: Decodable { let provider: String?; let name: String?; let batch: String?; let fallback: String?; let key: Key?; let local: Bool? }
    struct Embeddings: Decodable { let provider: String?; let name: String?; let on: Bool? }
    struct Limits: Decodable { let max_minutes: Double?; let max_calls: Int?; let max_usd: Double? }
    struct Schedule: Decodable { let limits: Limits?; let kind: String?; let enabled: Bool?; let time: String?; let loaded: Bool? }
    struct Power: Decodable { let on_battery: String? }
    let model: Model?
    let embeddings: Embeddings?
    let schedule: Schedule?
    let power: Power?
}

struct Coordination: Decodable {
    struct Rule: Decodable, Identifiable { let situation: String; let meaning: String?; let action: String; let `default`: String?; var id: String { situation } }
    let rules: [Rule]
}

struct Doctor: Decodable {
    struct Root: Decodable { let root: String?; let agent: String; let state: String?; let connected: Bool?; let indexed_at: Double? }
    struct Autopilot: Decodable { let on: Bool? }
    let roots: [Root]?
    let autopilot: Autopilot?
}
