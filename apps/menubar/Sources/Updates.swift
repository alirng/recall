// Updates (board J2): two versions, one place to update. Recall updates through Sparkle with a
// user driver of our own, so nothing Sparkle draws ever appears: its state shows in This Mac's Versions
// panel and as a small mark on the menu bar icon. recall, the engine, is checked against PyPI once a day
// and upgraded with the tool that installed it. Nothing is installed without a click on Update.

import AppKit
import Sparkle
import SwiftUI

final class Updates: ObservableObject {
    static let shared = Updates()

    enum Desktop: Equatable {
        case current
        case found(String)
        case downloading(Double)
        case installing
        case failed(String)
    }
    enum Upgrade: Equatable { case idle, running, done(String), failed(String) }

    @Published var desktop: Desktop = .current
    @Published var recallInstalled: String?
    @Published var recallLatest: String? = AppState.load().recallLatest
    @Published var upgrade: Upgrade = .idle
    @Published var updatedLines: [String] = []  // "Recall 0.1.1", shown once after an update

    /// The version of this app, and whether this build can check for app updates at all (it needs the public key).
    let version = (Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String) ?? "0.1.0"
    var appUpdatesOn: Bool { updater != nil || fakeSparkle }
    var fakeSparkle = false
    var onChange: () -> Void = {}
    var onRecallUpgraded: () -> Void = {}

    var updater: SPUUpdater?
    var driver: UpdateDriver?
    var foundReply: ((SPUUserUpdateChoice) -> Void)?
    var expected: UInt64 = 0
    var received: UInt64 = 0
    var daily: Timer?
    let work = DispatchQueue(label: "recall.updates", qos: .utility)

    // MARK: Starting

    func start() {
        let was = AppState.load().lastVersion
        if let was, was != version, Probe.value("--scene") == nil { updatedLines = ["Recall \(version)"] }
        if was != version { AppState.update { $0.lastVersion = self.version } }
        if let v = Probe.value("--fake-update") { desktop = .found(v); fakeSparkle = true }
        if let v = Probe.value("--fake-latest") { recallLatest = v }
        if let v = Probe.value("--fake-updated") { updatedLines = v.split(separator: ",").map(String.init) }
        readRecallVersion()
        guard !Probe.background else { return }
        // Sparkle needs the public key this build was made with; without it app updates stay off.
        let key = Bundle.main.object(forInfoDictionaryKey: "SUPublicEDKey") as? String
        if let key, !key.isEmpty {
            MainActor.assumeIsolated {
                let d = UpdateDriver(self)
                let u = SPUUpdater(hostBundle: .main, applicationBundle: .main, userDriver: d, delegate: nil)
                do { try u.start(); updater = u; driver = d } catch { desktop = .failed(error.localizedDescription) }
            }
        }
        checkRecall()
    }

    func readRecallVersion() {
        work.async {
            // Asking costs a Python start at every launch, so the answer is kept until the installed file changes.
            var file: String?
            var v: String?
            if let path = Tool.find("recall") {
                let real = URL(fileURLWithPath: path).resolvingSymlinksInPath().path
                let changed = (try? FileManager.default.attributesOfItem(atPath: real))?[.modificationDate] as? Date
                file = changed.map { "\(real) \($0.timeIntervalSince1970)" }
                let state = AppState.load()
                v = file != nil && state.recallFile == file ? state.recallVersion : nil
                if v == nil {
                    v = API.version(path)
                    if v == nil { file = nil }
                } else {
                    file = nil  // nothing new to keep
                }
            }
            DispatchQueue.main.async {
                if let file, let v { AppState.update { $0.recallFile = file; $0.recallVersion = v } }
                self.recallInstalled = v
                self.onChange()
            }
        }
    }

    // MARK: What's new

    var desktopLatest: String? { if case .found(let v) = desktop { return v } else { return nil } }

    var recallBehind: Bool {
        guard let a = recallInstalled, let b = recallLatest else { return false }
        return Self.newer(b, than: a)
    }

    var anyUpdate: Bool { desktopLatest != nil || recallBehind }

    /// "0.6.1" is newer than "0.6.0": numbers compared part by part.
    static func newer(_ a: String, than b: String) -> Bool {
        let x = a.split(separator: ".").map { Int($0.prefix { $0.isNumber }) ?? 0 }
        let y = b.split(separator: ".").map { Int($0.prefix { $0.isNumber }) ?? 0 }
        for i in 0..<max(x.count, y.count) {
            let p = i < x.count ? x[i] : 0, q = i < y.count ? y[i] : 0
            if p != q { return p > q }
        }
        return false
    }

    // MARK: recall, from PyPI

    /// The latest recall, asked of PyPI at most once a day; a one-shot timer with wide tolerance brings the next look.
    func checkRecall() {
        daily?.invalidate()
        daily = nil
        let last = AppState.load().lastPyPICheck.map { Date(timeIntervalSince1970: $0) } ?? .distantPast
        let due = last.addingTimeInterval(24 * 3600)
        if Date() < due {
            let t = Timer(fire: due, interval: 0, repeats: false) { [weak self] _ in self?.checkRecall() }
            t.tolerance = 3600
            RunLoop.main.add(t, forMode: .common)
            daily = t
            return
        }
        // Ephemeral: nothing is cached or kept in ~/Library.
        var request = URLRequest(url: URL(string: "https://pypi.org/pypi/recall-agents/json")!, timeoutInterval: 30)
        request.setValue("Recall/\(version)", forHTTPHeaderField: "User-Agent")
        URLSession(configuration: .ephemeral).dataTask(with: request) { data, _, _ in
            struct PyPI: Decodable { struct Info: Decodable { let version: String }; let info: Info }
            let latest = data.flatMap { try? JSONDecoder().decode(PyPI.self, from: $0) }?.info.version
            DispatchQueue.main.async {
                if let latest {
                    self.recallLatest = latest
                    AppState.update { $0.lastPyPICheck = Date().timeIntervalSince1970; $0.recallLatest = latest }
                }
                self.onChange()
                let t = Timer(fire: Date().addingTimeInterval(24 * 3600), interval: 0, repeats: false) { [weak self] _ in self?.checkRecall() }
                t.tolerance = 3600
                RunLoop.main.add(t, forMode: .common)
                self.daily = t
            }
        }.resume()
    }

    var installer: Tool.Installer { Tool.find("recall").map { Tool.installer(of: $0) } ?? .other }

    /// What a person types when there is no button.
    var upgradeText: String { "uv tool upgrade recall-agents" }

    /// Upgrade with the tool that installed recall, then make the app's recall api run the new code.
    func upgradeRecall() {
        guard upgrade != .running, installer != .other else { return }
        upgrade = .running
        let command = Tool.upgradeCommand(installer)
        work.async {
            let r = Shell.run(command, timeout: 600)
            guard r.ok else {
                DispatchQueue.main.async { self.upgrade = .failed(r.lastLine.isEmpty ? "The upgrade stopped." : r.lastLine) }
                return
            }
            let v = Tool.find("recall").flatMap { API.version($0) }
            Self.restartAPI()
            DispatchQueue.main.async {
                self.recallInstalled = v
                self.upgrade = .done(v ?? "")
                if let v { self.updatedLines.append("recall \(v)") }
                self.onChange()
                self.onRecallUpgraded()
            }
        }
    }

    /// The `recall api` the app started keeps running the old code until it exits: ask it to (SIGTERM, from the
    /// pid it reports), then start a new one. Blocking.
    static func restartAPI() {
        if let fd = API.connect(timeout: 2) {
            close(fd)
            if case .success(let d) = API.raw("/v1/info"), let pid = (try? JSONDecoder().decode(Info.self, from: d))?.pid, pid > 1 {
                kill(pid_t(pid), SIGTERM)
                for _ in 0..<30 {  // up to 3 s for the socket to go
                    usleep(100_000)
                    guard let f = API.connect(timeout: 1) else { break }
                    close(f)
                }
            }
        }
        _ = API.ensureRunning()
    }

    // MARK: Recall, through Sparkle

    func installDesktop() {
        guard case .found = desktop else { return }
        if let reply = foundReply {
            foundReply = nil
            desktop = .downloading(0)
            reply(.install)
        } else if fakeSparkle {
            Probe.log("would install the app update")
        } else {
            updater?.checkForUpdates()
        }
    }

    func progress() {
        if expected > 0 { desktop = .downloading(min(1, Double(received) / Double(expected))) }
    }
}

/// Sparkle's user driver, ours: it draws nothing. Every call lands in `Updates`, which This Mac shows.
@MainActor
final class UpdateDriver: NSObject, SPUUserDriver {
    unowned let updates: Updates
    init(_ updates: Updates) { self.updates = updates }

    func show(_ request: SPUUpdatePermissionRequest, reply: @escaping (SUUpdatePermissionResponse) -> Void) {
        // Info.plist already says automatic checks are on; this answer matches it and shares nothing.
        reply(SUUpdatePermissionResponse(automaticUpdateChecks: true, sendSystemProfile: false))
    }

    func showUserInitiatedUpdateCheck(cancellation: @escaping () -> Void) {}

    func showUpdateFound(with appcastItem: SUAppcastItem, state: SPUUserUpdateState, reply: @escaping (SPUUserUpdateChoice) -> Void) {
        updates.foundReply = reply
        updates.desktop = .found(appcastItem.displayVersionString)
        updates.onChange()
    }

    func showUpdateReleaseNotes(with downloadData: SPUDownloadData) {}
    func showUpdateReleaseNotesFailedToDownloadWithError(_ error: Error) {}

    func showUpdateNotFoundWithError(_ error: Error, acknowledgement: @escaping () -> Void) {
        if case .found = updates.desktop {} else { updates.desktop = .current }
        acknowledgement()
    }

    func showUpdaterError(_ error: Error, acknowledgement: @escaping () -> Void) {
        updates.desktop = .failed(error.localizedDescription)
        updates.onChange()
        acknowledgement()
    }

    func showDownloadInitiated(cancellation: @escaping () -> Void) {
        updates.expected = 0
        updates.received = 0
        updates.desktop = .downloading(0)
    }

    func showDownloadDidReceiveExpectedContentLength(_ expectedContentLength: UInt64) {
        updates.expected = expectedContentLength
    }

    func showDownloadDidReceiveData(ofLength length: UInt64) {
        updates.received += length
        updates.progress()
    }

    func showDownloadDidStartExtractingUpdate() { updates.desktop = .installing }
    func showExtractionReceivedProgress(_ progress: Double) {}

    /// The person pressed Update, which was the ask; the install and relaunch go ahead.
    func showReady(toInstallAndRelaunch reply: @escaping (SPUUserUpdateChoice) -> Void) {
        updates.desktop = .installing
        reply(.install)
    }

    func showInstallingUpdate(withApplicationTerminated applicationTerminated: Bool, retryTerminatingApplication: @escaping () -> Void) {
        updates.desktop = .installing
    }

    func showUpdateInstalledAndRelaunched(_ relaunched: Bool, acknowledgement: @escaping () -> Void) { acknowledgement() }

    func dismissUpdateInstallation() {
        if case .found = updates.desktop { return }  // still waiting for the person
        if case .failed = updates.desktop { return }
        if case .installing = updates.desktop { return }
        updates.desktop = .current
    }
}

// MARK: - The Versions panel on This Mac

struct VersionsPanel: View {
    let library: Library
    @ObservedObject var updates: Updates
    @EnvironmentObject var facts: Facts
    @StateObject var show = Held<Bool>(false)
    @StateObject var login = Held<Bool>(LoginItem.on)
    @Environment(\.colorScheme) var scheme

    var body: some View {
        let p = Palette.of(scheme)
        let restarts = facts.status?.restarts ?? []
        Panel(title: "Versions", note: "this Mac") {
            desktopRow(p)
            recallRow(p)
            ForEach(updates.updatedLines, id: \.self) { CheckLine(text: $0).padding(.vertical, 4) }
            if updates.anyUpdate || !updates.updatedLines.isEmpty {
                Button { Open.url("https://rererecall.com/changelog/") } label: {
                    Text("What’s new ↗").foregroundStyle(p.muted).contentShape(Rectangle())
                }
                .buttonStyle(.plain).padding(.top, 8)
            }
            if !restarts.isEmpty { restartRows(restarts, p) }
            Rule()
            HStack(spacing: 12) {
                PickRow(name: "Open when I log in", on: login.value) {
                    let why = LoginItem.set(!login.value)
                    login.value = LoginItem.on
                    if why != nil { login.value = LoginItem.on }
                }
                Spacer(minLength: 8)
                WindowButton(title: "Set up again", small: true) { SetupHook.again() }
            }
            .frame(height: RowHeight.control)
        }
    }

    func row<R: View>(_ name: String, _ versions: String, top: Bool = true, @ViewBuilder right: () -> R) -> some View {
        let p = Palette.of(scheme)
        return VStack(spacing: 0) {
            if top { Rule() }
            HStack(spacing: 8) {
                Text(name).foregroundStyle(p.ink)
                Spacer(minLength: 8)
                Text(versions).foregroundStyle(p.muted).monospacedDigit()
                right()
            }
            .frame(height: RowHeight.data)
        }
    }

    @ViewBuilder
    func desktopRow(_ p: Palette) -> some View {
        switch updates.desktop {
        case .found(let v):
            row("Recall", "\(updates.version) → \(v)", top: false) { MainButton(title: "Update") { updates.installDesktop() } }
        case .downloading(let f):
            row("Recall", "Downloading \(Int(f * 100))%", top: false) { EmptyView() }
        case .installing:
            row("Recall", "Installing, then it reopens", top: false) { EmptyView() }
        case .failed(let why):
            row("Recall", updates.version, top: false) { EmptyView() }
            Text("Couldn’t check for an update: \(why)").font(PaperMono.font(11)).foregroundStyle(p.muted).fixedSize(horizontal: false, vertical: true)
        case .current:
            row("Recall", updates.version, top: false) {
                if !updates.appUpdatesOn { Text("updates off in this build").font(PaperMono.font(11)).foregroundStyle(p.faint) }
            }
        }
    }

    @ViewBuilder
    func recallRow(_ p: Palette) -> some View {
        let installed = updates.recallInstalled ?? library.info?.recall ?? ""
        if updates.recallBehind, let latest = updates.recallLatest {
            row("recall-agents", "\(installed) → \(latest)") {
                switch updates.upgrade {
                case .running: Text("Updating…").foregroundStyle(p.muted)
                default:
                    if updates.installer != .other { MainButton(title: "Update") { updates.upgradeRecall() } }
                }
            }
            if updates.installer == .other {
                Text(updates.upgradeText).font(PaperMono.font(11)).foregroundStyle(p.body).textSelection(.enabled)
                    .padding(.vertical, 4)
            }
            if case .failed(let why) = updates.upgrade { SaidLine(said: .failed("Couldn’t update: \(why)")).padding(.top, 4) }
        } else {
            row("recall-agents", installed) { EmptyView() }
        }
    }

    /// "N agents still on the old recall": the button shows which agents and what to do. Killing their recall servers
    /// would cut a live chat, so the rows say "Restart Claude Code" and the agents' own windows do the restarting.
    @ViewBuilder
    func restartRows(_ restarts: [(agent: String, detail: String)], _ p: Palette) -> some View {
        row("\(restarts.count) agent\(restarts.count == 1 ? "" : "s") still on the old recall", "") {
            WindowButton(title: show.value ? "Hide" : "Show", small: true) { show.value.toggle() }
        }
        if show.value {
            ForEach(restarts, id: \.agent) { r in
                VStack(alignment: .leading, spacing: 4) {
                    Text("Restart \(agentName(r.agent))").foregroundStyle(p.ink)
                    Text(r.detail).font(PaperMono.font(11)).foregroundStyle(p.muted)
                }
                .padding(.vertical, 4)
            }
        }
    }
}

/// "Set up again" on This Mac reopens the setup window; the app sets it, so the panel needn't know the app.
enum SetupHook {
    nonisolated(unsafe) static var again: () -> Void = {}
}
