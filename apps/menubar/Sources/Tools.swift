// Finding and running the command-line tools recall needs (uv, pipx, recall itself). An app opened
// from Finder has no shell PATH, so every subprocess gets the folders those tools install into,
// added after the PATH the app inherited so an inherited PATH stays first. `recall install` writes
// absolute paths into the agents' settings only if it finds `recall` on PATH.

import AppKit

enum Tool {
    /// Where uv, pipx and recall are installed. `--tool-dirs a:b` (a test flag) replaces them, so a test
    /// of "no uv on this Mac" isn't undone by the real uv in /opt/homebrew/bin.
    static let fixed: [String] = {
        if let d = Probe.value("--tool-dirs") { return d.split(separator: ":").map(String.init) }
        return [API.home + "/.local/bin", "/opt/homebrew/bin", "/usr/local/bin"]
    }()

    /// The folders searched and handed to every subprocess: the inherited PATH first.
    static let folders: [String] = {
        var out = (ProcessInfo.processInfo.environment["PATH"] ?? "").split(separator: ":").map(String.init)
        for d in fixed + ["/usr/bin", "/bin"] where !out.contains(d) { out.append(d) }
        return out
    }()

    static var path: String { folders.joined(separator: ":") }

    /// The environment for a subprocess.
    static var env: [String: String] {
        var e = ProcessInfo.processInfo.environment
        e["PATH"] = path
        return e
    }

    static func find(_ name: String) -> String? {
        for d in folders {
            let p = d + "/" + name
            var isDir: ObjCBool = false
            if FileManager.default.isExecutableFile(atPath: p), FileManager.default.fileExists(atPath: p, isDirectory: &isDir), !isDir.boolValue { return p }
        }
        return nil
    }

    /// The installed recall, with its symlinks resolved: the folder it lives in says which tool installed it.
    static func resolved(_ path: String) -> String { (path as NSString).resolvingSymlinksInPath }

    enum Installer { case uv, pipx, other }

    /// Which tool installed `recall`, judged from its resolved path: uv keeps tools under .../uv/tools/,
    /// pipx under .../pipx/venvs/. Anything else (Homebrew, pip, a checkout) is upgraded by hand.
    static func installer(of recall: String) -> Installer {
        let real = resolved(recall)
        if real.contains("/uv/tools/") { return .uv }
        if real.contains("/pipx/venvs/") { return .pipx }
        return .other
    }

    /// The command that upgrades recall for its installer, and what the person types when there is no button.
    static func upgradeCommand(_ i: Installer) -> [String] {
        switch i {
        case .uv: return ["uv", "tool", "upgrade", "recall-agents"]
        case .pipx: return ["pipx", "upgrade", "recall-agents"]
        case .other: return ["uv", "tool", "upgrade", "recall-agents"]
        }
    }
}

struct Ran {
    let status: Int32
    let out: String
    let err: String
    var ok: Bool { status == 0 }
    /// The last lines of what it said, for a reason and for Copy details.
    var tail: String {
        let text = (err.isEmpty ? out : err + "\n" + out).trimmingCharacters(in: .whitespacesAndNewlines)
        return text.split(separator: "\n").suffix(12).joined(separator: "\n")
    }
    var lastLine: String { tail.split(separator: "\n").last.map(String.init) ?? "" }
}

enum Shell {
    /// `command` run to the end with its stdin closed. A bare name is looked up on Tool.path. `onLine` hears each
    /// line of output as it arrives (from another thread). Blocking: call it off the main thread. A command
    /// that runs past `timeout` seconds is stopped.
    static func run(_ command: [String], timeout: TimeInterval = 180, onLine: ((String) -> Void)? = nil) -> Ran {
        guard let first = command.first else { return Ran(status: 127, out: "", err: "nothing to run") }
        let exe = first.contains("/") ? first : (Tool.find(first) ?? first)
        let p = Process()
        p.executableURL = URL(fileURLWithPath: exe)
        p.arguments = Array(command.dropFirst())
        p.environment = Tool.env
        p.currentDirectoryURL = URL(fileURLWithPath: API.home)
        p.standardInput = FileHandle.nullDevice
        let out = Pipe(), err = Pipe()
        p.standardOutput = out
        p.standardError = err
        do { try p.run() } catch { return Ran(status: 127, out: "", err: "Couldn't start \(command[0])") }
        // Past the timeout: ask it to stop, and three seconds later make it.
        let stop = DispatchWorkItem { if p.isRunning { p.terminate() } }
        let kill9 = DispatchWorkItem { if p.isRunning { kill(p.processIdentifier, SIGKILL) } }
        DispatchQueue.global().asyncAfter(deadline: .now() + timeout, execute: stop)
        DispatchQueue.global().asyncAfter(deadline: .now() + timeout + 3, execute: kill9)
        let lock = NSLock()
        var outData = Data(), errData = Data()
        let drained = DispatchGroup()
        func pump(_ h: FileHandle, _ into: @escaping (Data) -> Void) {
            drained.enter()
            DispatchQueue.global().async {
                var pending = ""
                while true {
                    let d = h.availableData
                    if d.isEmpty { break }
                    lock.lock(); into(d); lock.unlock()
                    if let onLine {
                        pending += String(decoding: d, as: UTF8.self)
                        while let nl = pending.firstIndex(of: "\n") {
                            onLine(String(pending[..<nl]))
                            pending.removeSubrange(...nl)
                        }
                    }
                }
                if let onLine, !pending.isEmpty { onLine(pending) }
                drained.leave()
            }
        }
        pump(out.fileHandleForReading) { outData.append($0) }
        pump(err.fileHandleForReading) { errData.append($0) }
        // The process first (it ends at the latest 3 s after the timeout), then what it printed. A grandchild
        // that kept a pipe open can't hold this up for more than a few seconds.
        p.waitUntilExit()
        stop.cancel()
        kill9.cancel()
        _ = drained.wait(timeout: .now() + 5)
        lock.lock()
        defer { lock.unlock() }
        return Ran(status: p.terminationStatus, out: String(decoding: outData, as: UTF8.self), err: String(decoding: errData, as: UTF8.self))
    }

    /// Starts `command` and leaves it running, its output appended to `log` (a memory run's own log).
    /// `done` hears its exit status on the main thread. False when it couldn't start.
    @discardableResult
    static func detached(_ command: [String], log: String, done: @escaping (Int32) -> Void) -> Bool {
        guard let first = command.first else { return false }
        let exe = first.contains("/") ? first : (Tool.find(first) ?? first)
        let fm = FileManager.default
        try? fm.createDirectory(atPath: (log as NSString).deletingLastPathComponent, withIntermediateDirectories: true)
        if !fm.fileExists(atPath: log) { fm.createFile(atPath: log, contents: nil, attributes: [.posixPermissions: 0o600]) }
        guard let handle = FileHandle(forWritingAtPath: log) else { return false }
        handle.seekToEndOfFile()
        let p = Process()
        p.executableURL = URL(fileURLWithPath: exe)
        p.arguments = Array(command.dropFirst())
        p.environment = Tool.env
        p.currentDirectoryURL = URL(fileURLWithPath: API.home)
        p.standardInput = FileHandle.nullDevice
        p.standardOutput = handle
        p.standardError = handle
        p.terminationHandler = { proc in
            try? handle.close()
            DispatchQueue.main.async { done(proc.terminationStatus) }
        }
        do { try p.run() } catch { try? handle.close(); return false }
        return true
    }
}

/// Opens things for a person. In a test run nothing leaves the app: a browser or another app coming
/// forward would take the screen, so the call is logged instead.
enum Open {
    static func url(_ s: String) {
        guard let u = URL(string: s) else { return }
        if Probe.background { Probe.log("would open \(s)"); return }
        NSWorkspace.shared.open(u)
    }

    static func path(_ p: String) {
        if Probe.background { Probe.log("would open \(p)"); return }
        NSWorkspace.shared.open(URL(fileURLWithPath: p))
    }
}
