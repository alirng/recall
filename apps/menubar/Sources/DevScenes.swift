// Test flags for the screens Recall 0.1.0 added, run off screen like the others in Dev.swift: nothing
// comes to the front, notifications and the login item are logged instead of done, and links are not opened.
//   --scene NAME --shots-to DIR   saves each state of a scene as PNGs, dark and light, then quits.
//     setup        (add --force: "Set up again" on a connected Mac) the real setup flow in a sandbox: finding agents, the model, install and connect, the end, Memory
//                  (add --drive to press the buttons; without it only the first step is shown)
//     move         "Move Recall to Applications?"
//     versions     This Mac's Versions panel (with --fake-update, --fake-latest, --fake-updated)
//     runfail      This Mac's run panel with a failed night (with --run-fixture)
//     consent      the model menu's consent sheet (needs a sandbox whose model is a local one)
//   --retry-fix PATH   (setup --drive) step 3 failed: remove PATH, press Try again twice, carry on
//   --run-fixture FILE   JSON {"run": …, "config": …, "models": …} standing in for /v1/runs/latest, /v1/config
//                        and /v1/models, for failures the sandbox can't make (a spend limit, five chats failing)
//   --fake-update V, --fake-latest V, --fake-updated "a,b"   what the Versions panel shows without Sparkle or PyPI
//   --tool-dirs a:b      where uv and recall are looked for, instead of the usual three folders

import AppKit
import SwiftUI

extension Probe {
    struct Fixture {
        var run: Runs.Run?
        var config: Config?
        var models: Models?
    }

    static let fixture: Fixture? = {
        guard let path = value("--run-fixture"), let data = FileManager.default.contents(atPath: path),
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return nil }
        func decode<T: Decodable>(_ key: String, _ type: T.Type) -> T? {
            guard let v = json[key], let d = try? JSONSerialization.data(withJSONObject: v) else { return nil }
            return try? JSONDecoder().decode(T.self, from: d)
        }
        return Fixture(run: decode("run", Runs.Run.self), config: decode("config", Config.self), models: decode("models", Models.self))
    }()

    static func until(_ what: String, _ cond: @escaping () -> Bool, timeout: Double = 120, then: @escaping () -> Void) {
        let end = Date().addingTimeInterval(timeout)
        func poll() {
            if cond() { then(); return }
            if Date() > end { log("scene: gave up waiting for \(what)"); then(); return }
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.4) { poll() }
        }
        poll()
    }

    nonisolated(unsafe) static var recenter: ((NSWindow) -> Void)?

    static func later(_ s: Double, _ f: @escaping () -> Void) { DispatchQueue.main.asyncAfter(deadline: .now() + s, execute: f) }

    /// One window as a 2x PNG, popups left out.
    static func snap(_ win: NSWindow, to path: String) {
        guard let frame = win.contentView?.superview else { return }
        let r = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: Int(frame.bounds.width * 2), pixelsHigh: Int(frame.bounds.height * 2),
                                 bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
        r.size = frame.bounds.size
        frame.cacheDisplay(in: frame.bounds, to: r)
        try? r.representation(using: .png, properties: [:])?.write(to: URL(fileURLWithPath: path))
        log("shot \(path)")
    }

    /// `name` in dark, then in light. `take` saves the PNG for one look.
    static func both(_ name: String, in dir: String, window: @escaping () -> NSWindow?, take: ((String) -> Void)? = nil, then: @escaping () -> Void) {
        func one(_ look: NSAppearance.Name, _ suffix: String, _ next: @escaping () -> Void) {
            window()?.appearance = NSAppearance(named: look)
            if let w = window(), let mini = Optional(w) { Probe.recenter?(mini) }
            later(1.2) {
                let path = "\(dir)/\(name)-\(suffix).png"
                if let take { take(path) } else if let w = window() { snap(w, to: path) }
                next()
            }
        }
        one(.darkAqua, "dark") { one(.aqua, "light", then) }
    }

    static func scenes(_ app: App, _ name: String) {
        guard let dir = value("--shots-to") else { log("scene: --shots-to DIR is needed"); NSApp.terminate(nil); return }
        try? FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
        let done = { log("scene \(name): done"); NSApp.terminate(nil) }
        switch name {
        case "move":
            app.mover.show()
            both("move", in: dir, window: { app.mover.mini.window }, then: done)
        case "setup": setupScene(app, dir, done)
        case "versions":
            app.openWindow()
            later(4) {
                app.windows.library?.go(.thisMac)
                later(2.5) {
                    app.windows.library?.thisMacFocus.id = "Versions"
                    app.windows.library?.thisMacFocus.tick += 1
                    later(1) { shootWindow(app, "versions", dir, done) }
                }
            }
        case "runfail":
            app.openWindow()
            later(4) {
                app.windows.library?.go(.thisMac)
                later(3) {
                    app.windows.library?.thisMacFocus.id = "Last memory run"
                    app.windows.library?.thisMacFocus.tick += 1
                    later(1) {
                        log("notifications: \(Notifier.sent)")
                        shootWindow(app, "runfail", dir, done)
                    }
                }
            }
        case "consent":
            app.openWindow()
            later(5) {
                guard let lib = app.windows.library else { done(); return }
                lib.go(.thisMac)
                let option = lib.facts.models?.options.first { $0.provider == "claude" }
                guard let option else { log("consent: no Claude plan option; models \(lib.facts.models?.options.map(\.id) ?? [])"); done(); return }
                later(1.5) {
                    log("consent: picking \(option.id), now \(String(describing: lib.facts.config?.model?.provider))"); lib.useModel(option)
                    later(3) {
                        shootWindow(app, "consent", dir) {
                            log("consent: sheet open \(lib.popups.isOpen)")
                            // Test and switch (Return): the destination goes back as accepted and the switch is made.
                            _ = lib.popups.keys?(keyEvent("\r", 36))
                            later(6) { log("consent: model note \(String(describing: lib.facts.modelNote))"); shootWindow(app, "consent-after", dir, done) }
                        }
                    }
                }
            }
        default:
            log("scene: unknown \(name)")
            NSApp.terminate(nil)
        }
    }

    static func shootWindow(_ app: App, _ name: String, _ dir: String, _ then: @escaping () -> Void) {
        both(name, in: dir, window: { app.windows.window }, take: { path in app.windows.window.map { _ in capture(app.windows, to: path) } }, then: then)
    }

    // MARK: Setup

    static func setupScene(_ app: App, _ dir: String, _ done: @escaping () -> Void) {
        recenter = { w in app.setup.mini.centerTrafficLights2(w) }
        let drive = CommandLine.arguments.contains("--drive")
        let m = app.setup.model
        app.setup.show(force: CommandLine.arguments.contains("--force"))
        let win = { app.setup.mini.window }
        var retried = false
        func shot(_ name: String, _ next: @escaping () -> Void) { both(name, in: dir, window: win, then: next) }
        func report() { log("setup: phase \(m.phase) agents \(m.agents.map { "\($0.name)=\(String(describing: $0.chats))" }) options \(m.options.map(\.id)) failure \(String(describing: m.testFailure)) trouble \(String(describing: m.trouble))") }
        until("a first step", { m.phase != .starting }) {
            report()
            // Install problems end here: askUV and trouble are shown as they are.
            if m.phase == .askUV {
                shot("setup-uv") {
                    if drive { m.installUV(); until("uv installed", { m.phase != .starting }) { report(); shot("setup-after-uv") { done() } } } else { done() }
                }
                return
            }
            if m.phase == .askUpgrade {
                shot("setup-old-recall") {
                    if drive { m.upgradeOld(); until("the upgrade", { m.phase != .starting && m.phase != .askUpgrade }) { report(); log("setup: recall now \(API.version(m.recall ?? "") ?? "?")"); done() } } else { done() }
                }
                return
            }
            if m.phase == .trouble { shot("setup-trouble") { done() }; return }
            if m.skipped { log("setup: skipped, recall is already connected"); done(); return }
            // Give the agents their counts first when the index is still running.
            until("the index", { !m.indexing }, timeout: 120) {
                shot("setup-1-agents") {
                    guard drive, !m.agents.isEmpty else { done(); return }
                    m.toModel()
                    until("models", { m.optionsLoaded }) {
                        report()
                        shot("setup-2-model") {
                            if let pick = Probe.value("--pick") { m.chosen = pick }
                            m.testChosen()
                            until("the test", { m.phase == .connect || m.testFailure != nil }, timeout: 300) {
                                report()
                                if m.testFailure != nil {
                                    shot("setup-2-failed") {
                                        m.skipModel()
                                        connect()
                                    }
                                } else { connect() }
                            }
                        }
                    }
                }
            }
        }
        func connect() {
            until("setup finished", { m.finished || m.phase == .trouble }, timeout: 300) {
                report()
                // --retry-fix PATH: step 3 failed; remove what blocked it, press Try again twice, and carry on.
                if m.phase == .trouble, !retried, let blocker = Probe.value("--retry-fix") {
                    retried = true
                    shot("setup-3-trouble") {
                        try? FileManager.default.removeItem(atPath: blocker)
                        m.tryAgain()
                        log("setup: after Try again, phase \(m.phase)")
                        m.tryAgain()  // a second click finds the card gone
                        connect()
                    }
                    return
                }
                log("setup: squares \(m.squares) connected \(m.connected) scheduled \(String(describing: m.scheduled)) firstRun \(String(describing: m.firstRun))")
                shot("setup-3-connect") {
                    guard m.finished else { done(); return }
                    m.openMemory()
                    until("the window", { app.windows.library != nil }) {
                        later(6) {
                            log("setup: login item fake \(LoginItem.fake), notify setting \(String(describing: AppState.load().notifyFailures))")
                            shootWindow(app, "setup-4-memory", dir, done)
                        }
                    }
                }
            }
        }
    }
}
