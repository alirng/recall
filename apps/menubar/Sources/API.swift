// The window's only door to recall: `recall api`, HTTP and JSON on a unix socket in
// ~/.agents/recall/running. The app never opens recall.db or the notes files itself.
// URLSession can't reach a unix socket (and would write caches under ~/Library), so this is a
// small POSIX client: one connection per request, read to the end, then a server-sent events
// stream read on its own thread, which blocks in read() and costs nothing while idle.

import AppKit

enum API {
    static let home = FileManager.default.homeDirectoryForCurrentUser.path
    /// recall's data folder: RECALL_HOME when the app was started with one (a scratch copy, as
    /// recall itself honours it), ~/.agents/recall otherwise. The recall api the app starts inherits it.
    static let recallHome: String = {
        if let h = ProcessInfo.processInfo.environment["RECALL_HOME"], !h.isEmpty { return (h as NSString).expandingTildeInPath }
        return home + "/.agents/recall"
    }()
    static let socketPath = recallHome + "/running/api.sock"
    static let minimumAPI = 1
    static let maxBody = 64 << 20  // the most a single answer may be
    static let maxBlock = 1 << 20  // the most of one server-sent event (or its header) that is held
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

    static func send(_ fd: Int32, _ path: String, method: String = "GET", body: Data? = nil) -> Bool {
        var head = "\(method) \(path) HTTP/1.1\r\nHost: recall\r\nX-Recall-Client: app\r\nConnection: close\r\n"
        if let body { head += "Content-Type: application/json\r\nContent-Length: \(body.count)\r\n" }
        var data = Array((head + "\r\n").utf8)
        if let body { data += Array(body) }
        var sent = 0
        while sent < data.count {
            let n = data.withUnsafeBytes { write(fd, $0.baseAddress! + sent, data.count - sent) }
            if n <= 0 { return false }
            sent += n
        }
        return true
    }

    /// One request, read to the end. Blocking: call it off the main thread. `timeout` is how long a
    /// read may wait: a model's test call takes longer than anything else here.
    static func raw(_ path: String, method: String = "GET", body: Data? = nil, timeout: Int = 30) -> Result<Data, Failure> {
        let started = Date()
        defer { if Probe.on { FileHandle.standardError.write(Data(String(format: "  %4.0f ms %@ %@\n", Date().timeIntervalSince(started) * 1000, method, path).utf8)) } }
        guard let fd = connect(timeout: timeout) else { return .failure(Failure(message: "recall isn't running")) }
        defer { close(fd) }
        guard send(fd, path, method: method, body: body) else { return .failure(Failure(message: "Couldn't ask recall")) }
        var all = Data()
        var buf = [UInt8](repeating: 0, count: 65536)
        // Each read waits at most `timeout`; the whole answer gets a deadline too, and a size cap, so a
        // server that trickles bytes or never stops can't hold the caller or fill memory.
        let deadline = Date().addingTimeInterval(Double(max(timeout, 30)) * 2)
        while true {
            let n = read(fd, &buf, buf.count)
            if n <= 0 { break }
            all.append(buf, count: n)
            if all.count > maxBody { return .failure(Failure(message: "recall's answer was too large")) }
            if Date() > deadline { return .failure(Failure(message: "recall took too long to answer")) }
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
            if chunks.failed { return .failure(Failure(message: "recall's answer was malformed")) }
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

    /// A write: `method` with a JSON body, decoded on the API queue, answered on the main thread.
    /// The failure carries recall's own code (busy, conflict, test_failed…).
    static func send<T: Decodable>(_ method: String, _ path: String, _ body: [String: Any], _ type: T.Type, timeout: Int = 30,
                                   _ done: @escaping (Result<T, Failure>) -> Void) {
        let data = try? JSONSerialization.data(withJSONObject: body)
        queue.async {
            let result = raw(path, method: method, body: data ?? Data("{}".utf8), timeout: timeout).flatMap { data -> Result<T, Failure> in
                do { return .success(try JSONDecoder().decode(T.self, from: data)) } catch {
                    return .failure(Failure(code: "decode", message: "Couldn't read recall's answer to \(path)"))
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
    static func commands(_ extra: [String] = []) -> [[String]] {
        var out: [[String]] = []
        if let dev = Bundle.main.object(forInfoDictionaryKey: "RecallAPICommand") as? [String], !dev.isEmpty { out.append(dev) }
        for dir in [home + "/.local/bin", "/opt/homebrew/bin", "/usr/local/bin"] {
            let path = dir + "/recall"
            if FileManager.default.isExecutableFile(atPath: path) { out.append([path, "api"]) }
        }
        return out.map { $0 + extra }
    }

    static let notInstalled = "recall isn't set up on this Mac. Install it with uv tool install 'recall-agents[embeddings]', then run recall install."

    /// The installed `recall`, found as commands() finds it; nil when there is none.
    static func installed() -> String? {
        [home + "/.local/bin", "/opt/homebrew/bin", "/usr/local/bin"].map { $0 + "/recall" }
            .first { FileManager.default.isExecutableFile(atPath: $0) }
    }

    /// `recall status --json`'s command, or its other subcommands once "status" is cut off: the dev
    /// checkout build.sh wrote into Info.plist (only when RECALL_DEV_PROJECT was set), else the installed one.
    static func recallCommand() -> [String]? {
        if let dev = Bundle.main.object(forInfoDictionaryKey: "RecallCommand") as? [String], !dev.isEmpty { return dev }
        return installed().map { [$0, "status", "--json"] }
    }

    static let starting = NSLock()

    /// Starts `recall api` if nothing answers on the socket, and waits up to 10 s for it. The server
    /// exits by itself once idle, so the app never stops it. `idle` is how long a server this call starts
    /// waits with no client before exiting (the server's own default when nil). Blocking: call it off the main thread.
    static func ensureRunning(idle: Int? = nil) -> Failure? {
        starting.lock()
        defer { starting.unlock() }
        if let fd = connect(timeout: 2) { close(fd); return nil }
        let commands = commands(idle.map { ["--idle", String($0)] } ?? [])
        if commands.isEmpty {
            return Failure(code: "not_installed", message: notInstalled)
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
    var failed = false  // a chunk size that is negative, unreadable or over maxChunk: the stream is dropped
    static let maxChunk = 16 << 20

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
                let text = String(line.split(separator: ";").first ?? "").trimmingCharacters(in: .whitespaces)
                guard let size = Int(text, radix: 16), size >= 0, size <= Chunks.maxChunk else {
                    failed = true
                    done = true
                    buffer.removeAll()
                    break
                }
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
                if pending.count > API.maxBlock { break }
                guard let split = pending.range(of: Data("\r\n\r\n".utf8)) else { continue }
                chunked = String(decoding: pending[..<split.lowerBound], as: UTF8.self).lowercased().contains("chunked")
                data = Data(pending[split.upperBound...])
                headerDone = true
            }
            if chunked { data = chunks.feed(data) }
            if chunks.failed { break }
            text += String(decoding: data, as: UTF8.self).replacingOccurrences(of: "\r\n", with: "\n")
            if text.utf8.count > API.maxBlock && !text.contains("\n\n") { break }
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
    /// What this server can change (notes, coordination, model, storage). Absent on a recall api from
    /// before these writes: the window then shows its controls read-only.
    let writes: [String]?
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
    let archived: Bool?
    let created: String?
    let updated: String?
    let last_seen: String?
    let body: String?
    let stamp: String?
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
    struct Item: Decodable, Identifiable {
        let label: String; let path: String; let bytes: Int; let kind: String; let exists: Bool?
        let version: String?; let date: String?  // a copy kept before an upgrade: what it was and when
        var id: String { path }
    }
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
    struct Preset: Decodable { let rules: [String: String]; let meaning: String? }
    let rules: [Rule]
    let preset: String?
    let actions: [String]?
    let presets: [String: Preset]?
    let was: [String: String]?  // after a change: the rules before it
}

// MARK: Writes (step 4)

/// What saving a note's text records, rule by rule: kept, reworded, removed, new, merged or restored.
struct NoteReading: Decodable {
    struct Change: Decodable, Identifiable {
        let kind: String; let rule_id: String; let text: String
        let said_by: String?; let was: String?; let into: String?
        var id: String { kind + rule_id }
    }
    let stamp: String?
    let empty: Bool?
    let changes: [Change]
    let counts: [String: Int]?
}

struct Edited: Decodable { let note_id: String; let changed: Bool; let commit: String?; let counts: [String: Int]? }
struct Forgotten: Decodable { let note_id: String; let title: String; let commit: String? }

struct Models: Decodable {
    struct Option: Decodable, Identifiable { let id: String; let label: String; let detail: String?; let now: Bool?; let provider: String? }
    let options: [Option]
    let fallback: String?
}

struct ModelSaved: Decodable { let label: String; let `private`: Bool?; let fallback: String? }
struct Removed: Decodable { struct Item: Decodable { let path: String; let bytes: Int }; let removed: [Item]; let bytes: Int }

struct Doctor: Decodable {
    struct Root: Decodable { let root: String?; let agent: String; let state: String?; let connected: Bool?; let indexed_at: Double? }
    struct Autopilot: Decodable { let on: Bool? }
    let roots: [Root]?
    let autopilot: Autopilot?
}
