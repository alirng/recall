// First run (board J1): "Open it and it sets itself up". A small window with three steps, shown only when
// this Mac needs setting up (recall missing, or no agent connected to it yet):
//   1 Finds your agents   2 You pick who writes notes   3 It installs and connects, then the window opens on Memory.
// Setup does the same work `recall setup` does, in pieces that can't stop to ask in a terminal: it never
// runs `recall setup`. The work: uv and recall if missing, `recall index`, `recall install --only
// mcp,skill,hook`, `recall schedule enable`, and the first memory run, detached.

import AppKit
import SwiftUI

final class SetupModel: ObservableObject {
    enum Phase: Equatable { case starting, askUV, askUpgrade, trouble, agents, model, connect }
    struct Trouble: Equatable { var title: String; var reason: String; var details: String }
    struct Agent: Identifiable, Equatable { let name: String; var chats: Int?; var id: String { name } }

    @Published var phase: Phase = .starting
    @Published var status = ""
    @Published var trouble: Trouble?
    @Published var agents: [Agent] = []
    @Published var indexing = false
    // Frame 2
    @Published var options: [Models.Option] = []
    @Published var optionsLoaded = false
    @Published var optionsNote = ""
    @Published var chosen: String?
    @Published var testing = false
    @Published var testFailure: String?
    @Published var modelLabel: String?
    // Frame 3
    @Published var version: String?
    @Published var chatsRead: Int?
    @Published var squares = 0
    @Published var connected = false
    @Published var scheduled: String?
    @Published var firstRun: Bool?
    @Published var finished = false
    @Published var notifyOn = true
    @Published var loginOn = true

    var recall: String?
    var skipped = false  // setup was skipped because recall is already connected
    var onSkip: () -> Void = {}
    var onDone: () -> Void = {}
    var retry: () -> Void = {}
    let work = DispatchQueue(label: "recall.setup", qos: .userInitiated)
    /// `recall index` has a queue of its own: it can take minutes, and the model step must not wait behind it.
    let indexQueue = DispatchQueue(label: "recall.setup.index", qos: .utility)
    /// "Set up again": show the steps even though an agent is already connected.
    var force = false
    var indexState = 0  // 0 not started, 1 running, 2 done
    var waiting: [() -> Void] = []
    var recheck: Timer?
    var open = true

    // MARK: Step 1 (finding recall and the agents)

    func begin() {
        phase = .starting
        trouble = nil
        status = "Looking for recall…"
        work.async {
            if let r = Tool.find("recall") {
                self.recall = r
                self.found(r)
            } else if let uv = Tool.find("uv") {
                self.installRecall(uv)
            } else {
                DispatchQueue.main.async { self.phase = .askUV }
            }
        }
    }

    /// Official installer, run once the person has said yes.
    func installUV() {
        phase = .starting
        status = "Installing uv…"
        work.async {
            // The script goes to a file first, then runs on its own: no pipe is left open behind the shell.
            let script = NSTemporaryDirectory() + "recall-uv-install-\(UUID().uuidString).sh"
            defer { try? FileManager.default.removeItem(atPath: script) }
            let get = Shell.run(["/usr/bin/curl", "-fsSL", "--max-time", "120", "-o", script, "https://astral.sh/uv/install.sh"], timeout: 150)
            guard get.ok else { self.fail("Couldn't install uv", get, "curl -fsSL https://astral.sh/uv/install.sh", again: { self.begin() }); return }
            let r = Shell.run(["/bin/sh", script], timeout: 300)
            guard r.ok else { self.fail("Couldn't install uv", r, "sh install.sh (from https://astral.sh/uv/install.sh)", again: { self.begin() }); return }
            if let uv = Tool.find("uv") { self.installRecall(uv) } else {
                self.fail("Couldn't install uv", r, "uv installed but wasn't found afterwards", again: { self.begin() })
            }
        }
    }

    /// "Update recall": the same upgrade This Mac uses, then on with setup. Another installer gets the command, not a button.
    func upgradeOld() {
        guard let recall else { return }
        let kind = Tool.installer(of: recall)
        guard kind != .other else { return }
        phase = .starting
        status = "Updating recall…"
        work.async {
            let r = Shell.run(Tool.upgradeCommand(kind), timeout: 600)
            guard r.ok else { self.fail("Couldn't update recall", r, Tool.upgradeCommand(kind).joined(separator: " "), again: { self.upgradeOld() }); return }
            self.found(recall)
        }
    }

    var oldInstaller: Tool.Installer { recall.map { Tool.installer(of: $0) } ?? .other }

    func installRecall(_ uv: String) {
        DispatchQueue.main.async { self.phase = .starting; self.status = "Installing recall…" }
        let r = Shell.run([uv, "tool", "install", "recall-agents"], timeout: 600)
        guard r.ok else { fail("Couldn't install recall", r, "uv tool install recall-agents", again: { self.begin() }); return }
        guard let path = Tool.find("recall") else {
            fail("Couldn't install recall", r, "uv tool install recall-agents (recall wasn't found afterwards)", again: { self.begin() })
            return
        }
        recall = path
        found(path)
    }

    func fail(_ title: String, _ r: Ran, _ command: String, again: @escaping () -> Void) {
        let reason = r.lastLine.isEmpty ? "It stopped without saying why." : r.lastLine
        DispatchQueue.main.async {
            self.trouble = Trouble(title: title, reason: reason, details: "\(command)\nexit \(r.status)\n\(r.tail)")
            self.retry = { [weak self] in
                guard let self, self.phase == .trouble else { return }
                self.phase = .starting
                again()
            }
            self.phase = .trouble
        }
    }

    /// recall is there: skip to Memory if an agent is already connected, else look at the agents and start indexing.
    func found(_ recall: String) {
        let v = API.version(recall)
        // An older recall has no `recall api`: ask before updating it.
        if let v, Updates.newer(API.minimumRecall, than: v) {
            DispatchQueue.main.async { self.version = v; self.phase = .askUpgrade }
            return
        }
        let rows = doctor(recall)
        if !force, rows.contains(where: { $0.connected == true }) {
            DispatchQueue.main.async { self.skipped = true; self.onSkip() }
            return
        }
        DispatchQueue.main.async {
            self.version = v
            self.show(rows)
            self.phase = .agents
            self.startIndex()
            self.arm()
        }
    }

    func doctor(_ recall: String) -> [Doctor.Root] {
        let r = Shell.run([recall, "doctor", "--json"], timeout: 60)
        guard r.ok, let rows = try? JSONDecoder().decode([Doctor.Root].self, from: Data(r.out.utf8)) else { return [] }
        return rows
    }

    func show(_ rows: [Doctor.Root]) {
        var order: [String] = []
        var chats: [String: Int] = [:]
        var counted: Set<String> = []
        for r in rows where r.exists == true {
            if !order.contains(r.agent) { order.append(r.agent) }
            if let c = r.chats { chats[r.agent, default: 0] += c; counted.insert(r.agent) }
        }
        let first = ["claude", "codex", "cursor"]
        order.sort { a, b in
            let ia = first.firstIndex(of: a) ?? 99, ib = first.firstIndex(of: b) ?? 99
            return ia != ib ? ia < ib : a < b
        }
        agents = order.map { Agent(name: $0, chats: counted.contains($0) ? chats[$0] : nil) }
    }

    func refresh() {
        guard let recall else { return }
        work.async {
            let rows = self.doctor(recall)
            DispatchQueue.main.async {
                let before = self.agents.map(\.name)
                self.show(rows)
                // An agent that turned up after indexing ran has chats the index doesn't hold yet.
                if self.agents.map(\.name) != before, self.indexState == 2 { self.indexState = 0; self.startIndex() }
                self.arm()
            }
        }
    }

    /// With no agent yet, look again every 5 seconds, only while this window is open and on step 1.
    func arm() {
        recheck?.invalidate()
        recheck = nil
        guard open, phase == .agents, agents.isEmpty else { return }
        let t = Timer.scheduledTimer(withTimeInterval: 5, repeats: false) { [weak self] _ in self?.refresh() }
        t.tolerance = 1
        recheck = t
    }

    func stop() {
        open = false
        recheck?.invalidate()
        recheck = nil
    }

    /// `recall index` in the background; the counts fill in when it finishes.
    func startIndex() {
        guard indexState == 0, let recall else { return }
        indexState = 1
        indexing = true
        indexQueue.async {
            _ = Shell.run([recall, "index"], timeout: 900)
            let rows = self.doctor(recall)
            DispatchQueue.main.async {
                self.show(rows)
                self.indexing = false
                self.indexState = 2
                let w = self.waiting
                self.waiting = []
                w.forEach { $0() }
            }
        }
    }

    func afterIndex(_ f: @escaping () -> Void) {
        if indexState == 2 { f() } else { waiting.append(f) }
    }

    // MARK: Step 2 (who writes notes)

    func toModel() {
        phase = .model
        testFailure = nil
        guard !optionsLoaded else { return }
        work.async {
            if let failure = API.ensureRunning() {
                DispatchQueue.main.async { self.optionsLoaded = true; self.optionsNote = failure.message }
                return
            }
            DispatchQueue.main.async {
                API.get("/v1/models", Models.self) { r in
                    self.optionsLoaded = true
                    switch r {
                    case .success(let m):
                        self.options = Self.shown(m.options)
                        self.chosen = (self.options.first { $0.now == true } ?? self.options.first)?.id
                    case .failure(let f): self.optionsNote = f.message
                    }
                }
            }
        }
    }

    /// Claude plan, ChatGPT plan and at most one local model; API providers stay `recall model`.
    static func shown(_ all: [Models.Option]) -> [Models.Option] {
        ["claude", "chatgpt", "local"].compactMap { p in all.first { $0.provider == p } }
    }

    static func name(_ o: Models.Option) -> String {
        switch o.provider {
        case "claude": return "Claude plan"
        case "chatgpt": return "ChatGPT plan"
        default: return "Local model"
        }
    }

    static func note(_ o: Models.Option) -> String { o.provider == "local" ? "stays on this Mac" : "signed in" }

    /// Where excerpts go for the chosen option, in the line under the rows.
    func goes(_ o: Models.Option?) -> String {
        switch o?.provider {
        case "claude": return "Excerpts go to Anthropic."
        case "chatgpt": return "Excerpts go to OpenAI."
        case "local": return "Nothing leaves this Mac."
        default: return ""
        }
    }

    /// "Test and continue". This screen is the consent: a 409 needs_consent is answered by sending the
    /// destination the error names back as accepted.
    func testChosen(accepting: String? = nil) {
        guard let id = chosen, !testing else { return }
        testing = true
        testFailure = nil
        var body: [String: Any] = ["option": id]
        if let accepting { body["accept_destination"] = accepting }
        API.send("PUT", "/v1/model", body, ModelSaved.self, timeout: 300) { r in
            switch r {
            case .success(let s):
                self.testing = false
                self.modelLabel = s.label
                self.toConnect()
            case .failure(let f) where f.code == "needs_consent" && accepting == nil && f.destination != nil:
                self.testing = false
                self.testChosen(accepting: f.destination)
            case .failure(let f):
                self.testing = false
                self.testFailure = f.message
            }
        }
    }

    func skipModel() {
        modelLabel = nil
        toConnect()
    }

    // MARK: Step 3 (install and connect)

    func toConnect() {
        phase = .connect
        run(from: 0)
    }

    /// The steps in order. A failed one stops the list with the reason and a Try again that starts there.
    func run(from step: Int) {
        guard let recall else { return }
        finished = false
        work.async {
            if step == 0 {
                DispatchQueue.main.async { self.squares = 0; self.connected = false; self.scheduled = nil; self.firstRun = nil }
            }
            // Read N chats: the numbers are in once the background index is.
            let indexed = DispatchSemaphore(value: 0)
            DispatchQueue.main.async { self.afterIndex { indexed.signal() } }
            indexed.wait()
            let total = self.agents.compactMap(\.chats).reduce(0, +)
            DispatchQueue.main.async { self.chatsRead = total }

            if step <= 0 {
                let r = Shell.run([recall, "install", "--only", "mcp,skill,hook"], timeout: 180) { _ in
                    DispatchQueue.main.async { self.squares = min(11, self.squares + 1) }
                }
                guard r.ok else { self.fail("Couldn't connect your agents", r, "recall install --only mcp,skill,hook", again: { self.run(from: 0) }, phase: .connect); return }
                DispatchQueue.main.async { self.squares = 12; self.connected = true }
            }
            // The nightly run's limits are recall's own: the config says what they are.
            var minutes = 120, calls = 80
            if case .success(let data) = API.raw("/v1/config"), let c = try? JSONDecoder().decode(Config.self, from: data) {
                minutes = Int(c.schedule?.limits?.max_minutes ?? 120)
                calls = c.schedule?.limits?.max_calls ?? 80
            }
            if step <= 1 {
                let r = Shell.run([recall, "schedule", "enable", "--at", "03:30", "--max-minutes", String(minutes), "--max-calls", String(calls)], timeout: 60)
                guard r.ok else { self.fail("Couldn't set the nightly run", r, "recall schedule enable --at 03:30", again: { self.run(from: 1) }, phase: .connect); return }
                DispatchQueue.main.async { self.scheduled = "03:30"; RunWatch.shared.scheduleSet() }
            }
            if self.modelLabel != nil {
                DispatchQueue.main.async {
                    RunNow.shared.start(limits: (minutes, calls))
                    self.firstRun = true
                }
            }
            DispatchQueue.main.async { self.finished = true }
        }
    }

    /// A failed step shows the trouble card; Try again goes back to phase `p` at once, so the card is gone
    /// and a second click finds nothing to press.
    func fail(_ title: String, _ r: Ran, _ command: String, again: @escaping () -> Void, phase p: Phase) {
        let reason = r.lastLine.isEmpty ? "It stopped without saying why." : r.lastLine
        DispatchQueue.main.async {
            self.trouble = Trouble(title: title, reason: reason, details: "\(command)\nexit \(r.status)\n\(r.tail)")
            self.retry = { [weak self] in
                guard let self, self.phase == .trouble else { return }
                self.phase = p
                again()
            }
            self.phase = .trouble
        }
    }

    /// "Try again": only while the trouble card is up.
    func tryAgain() {
        guard phase == .trouble else { return }
        retry()
    }

    func copyDetails() {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(trouble?.details ?? "", forType: .string)
    }

    /// The end of setup (the two rows and Open my memory): applies the two choices, then hands over to the window.
    func openMemory() {
        AppState.update { $0.notifyFailures = self.notifyOn }
        if notifyOn { Notifier.shared.authorize { _ in } }
        if loginOn { LoginItem.set(true) }
        onDone()
    }
}

// MARK: - View

struct SetupView: View {
    @ObservedObject var model: SetupModel
    @Environment(\.colorScheme) var scheme

    var body: some View {
        let p = Palette.of(scheme)
        VStack(spacing: 0) {
            HStack(spacing: 8) {
                Color.clear.frame(width: 72, height: 1)
                Image(nsImage: markImage(size: 14, level: .act))
                Text("recall").font(PaperMono.font(12.5, 640)).foregroundStyle(p.ink)
                Text("/ Set up").font(PaperMono.font(12.5)).foregroundStyle(p.muted)
                Spacer()
            }
            .frame(height: WindowController.titlebar)
            .background(p.card)
            .overlay(alignment: .bottom) { Rule() }
            HStack {
                Spacer(minLength: 0)
                card(p).frame(width: 420, height: 430)
                Spacer(minLength: 0)
            }
            .frame(maxHeight: .infinity)
        }
        .background(p.paper)
        .ignoresSafeArea()
        .font(PaperMono.font(12.5))
        .foregroundStyle(p.body)
    }

    func card(_ p: Palette) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            switch model.phase {
            case .starting: starting(p)
            case .askUV: askUV(p)
            case .askUpgrade: askUpgrade(p)
            case .trouble: trouble(p)
            case .agents: agents(p)
            case .model: modelStep(p)
            case .connect: connect(p)
            }
        }
        .padding(16)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .background(p.card)
        .overlay(Rectangle().strokeBorder(p.line, lineWidth: 1))
    }

    /// What this stage is for, in a heading and a sentence.
    func intro(_ title: String, _ text: String, _ p: Palette) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(title).font(PaperMono.font(14, 520)).foregroundStyle(p.ink)
            Text(text).foregroundStyle(p.body).fixedSize(horizontal: false, vertical: true)
        }
        .padding(.bottom, 8)
    }

    func starting(_ p: Palette) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Label(text: "Set up")
            HStack(spacing: 8) {
                Rectangle().strokeBorder(p.muted, lineWidth: 1).frame(width: 8, height: 8)
                Text(model.status).foregroundStyle(p.ink)
            }
            Text("This takes about a minute the first time.").foregroundStyle(p.muted)
        }
    }

    func askUV(_ p: Palette) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Label(text: "Set up")
            Text("recall needs uv").font(PaperMono.font(14, 520)).foregroundStyle(p.ink)
            Text("uv is the small tool that installs recall. Install it from astral.sh?")
                .foregroundStyle(p.body).fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 0)
            HStack { Spacer(); MainButton(title: "Install uv", yellow: true) { model.installUV() } }
        }
    }

    func askUpgrade(_ p: Palette) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Label(text: "Set up")
            Text("recall is older than the app needs").font(PaperMono.font(14, 520)).foregroundStyle(p.ink)
            Text("You have recall \(model.version ?? ""). The app needs \(API.minimumRecall) or newer. Update it?")
                .foregroundStyle(p.body).fixedSize(horizontal: false, vertical: true)
            if model.oldInstaller == .other {
                Text("uv tool upgrade recall-agents").foregroundStyle(p.ink).textSelection(.enabled)
            }
            Spacer(minLength: 0)
            if model.oldInstaller != .other { HStack { Spacer(); MainButton(title: "Update recall", yellow: true) { model.upgradeOld() } } }
        }
    }

    func trouble(_ p: Palette) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Label(text: "Set up")
            Text(model.trouble?.title ?? "Couldn't install recall").font(PaperMono.font(14, 520)).foregroundStyle(p.ink)
            Text(model.trouble?.reason ?? "").foregroundStyle(p.body).lineLimit(5).fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 0)
            HStack(spacing: 8) {
                Spacer()
                WindowButton(title: "Copy details") { model.copyDetails() }
                MainButton(title: "Try again", yellow: true) { model.tryAgain() }
            }
        }
    }

    func agents(_ p: Palette) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Label(text: "Set up · 1 of 3")
            intro("Your agents", "recall found these on your Mac. It copies their chats into one search index here, so each agent can find what the others did.", p)
            if model.agents.isEmpty {
                Text("No coding agent found yet.").foregroundStyle(p.ink)
                Text("Open Claude Code, Codex or Cursor once. recall finds it when it appears.")
                    .foregroundStyle(p.muted).fixedSize(horizontal: false, vertical: true)
            }
            ForEach(model.agents) { a in
                PickRow(name: agentName(a.name), note: a.chats.map { "\(count($0)) chat\($0 == 1 ? "" : "s")" } ?? (model.indexing ? "counting…" : ""))
            }
            Spacer(minLength: 0)
            HStack { Spacer(); MainButton(title: "Continue", enabled: !model.agents.isEmpty, yellow: true) { model.toModel() } }
        }
    }

    func modelStep(_ p: Palette) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Label(text: "Set up · 2 of 3")
            intro("Who writes your notes", "Each night a model reads your new chats and writes down what's worth keeping: preferences, decisions, lessons. Your agents start each session with those notes.", p)
            if !model.optionsLoaded {
                Text("Looking for a model…").foregroundStyle(p.muted)
            } else if model.options.isEmpty {
                Text("No model works here yet.").foregroundStyle(p.ink)
                Text(model.optionsNote.isEmpty ? "Search works now. Notes wait until a model does." : model.optionsNote)
                    .foregroundStyle(p.muted).fixedSize(horizontal: false, vertical: true)
            }
            ForEach(model.options) { o in
                PickRow(name: SetupModel.name(o), note: SetupModel.note(o), on: model.chosen == o.id) { model.chosen = o.id }
            }
            if !model.options.isEmpty {
                Text(model.goes(model.options.first { $0.id == model.chosen })).foregroundStyle(p.muted)
            }
            if let why = model.testFailure {
                Strip(color: p.kind("open"), text: why + " Nothing changed.")
                Text("Search works. Notes wait.").foregroundStyle(p.muted)
            }
            Spacer(minLength: 0)
            HStack(spacing: 8) {
                Spacer()
                if model.optionsLoaded, model.options.isEmpty || model.testFailure != nil {
                    WindowButton(title: "Skip for now") { model.skipModel() }
                }
                if !model.options.isEmpty {
                    MainButton(title: model.testing ? "Testing…" : model.testFailure == nil ? "Test and continue" : "Try again", enabled: !model.testing && model.chosen != nil, yellow: true) {
                        model.testChosen()
                    }
                }
            }
        }
    }

    func connect(_ p: Palette) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Label(text: "Set up · 3 of 3")
            if model.finished {
                intro("You're set", model.firstRun == true ? "Open a new chat in any agent and recall is there. Your first notes are being written now." : "Open a new chat in any agent and search is there. Notes wait until a model works.", p)
            } else {
                intro("Connecting recall", "recall is reading your chats and adding itself to your agents, so they can search past work and load your notes.", p)
            }
            CheckLine(text: "Installed recall " + (model.version ?? ""))
            if let n = model.chatsRead {
                CheckLine(text: "Read \(count(n)) chat\(n == 1 ? "" : "s")")
            } else {
                Text("Reading your chats…").foregroundStyle(p.muted)
            }
            if model.chatsRead != nil {
                if model.connected {
                    CheckLine(text: "Connected your agents")
                } else {
                    Text("Connecting your agents").foregroundStyle(p.ink)
                    SquareBar(filled: model.squares)
                }
            }
            if let t = model.scheduled { CheckLine(text: "Nightly run at \(t)") }
            if model.firstRun == true { CheckLine(text: "First memory run started") }
            Spacer(minLength: 0)
            if model.finished {
                VStack(alignment: .leading, spacing: 0) {
                    toggle("Tell me if a night's run fails", $model.notifyOn, p)
                    toggle("Open when I log in", $model.loginOn, p)
                }
                HStack { Spacer(); MainButton(title: "Open my memory", yellow: true) { model.openMemory() } }
            } else if model.scheduled == nil {
                Text("Then the nightly run, 03:30").foregroundStyle(p.muted)
            }
        }
    }

    func toggle(_ text: String, _ on: Binding<Bool>, _ p: Palette) -> some View {
        VStack(spacing: 0) {
            Rule()
            PickRow(name: text, on: on.wrappedValue) { on.wrappedValue.toggle() }.padding(.vertical, 4)
        }
    }
}

// MARK: - Window

final class SetupController {
    let mini = MiniWindow()
    let model = SetupModel()
    var done: () -> Void = {}   // the window should open on Memory
    var skip: () -> Void = {}   // recall is already connected
    var left: () -> Void = {}   // the window was closed before setup finished
    var finished = false

    var isOpen: Bool { mini.isOpen }

    func show(force: Bool = false) {
        if isOpen { mini.show(EmptyView(), size: .zero, title: ""); return }
        model.open = true
        model.force = force
        model.skipped = false
        finished = false
        // Setup has run its course (or was not needed): launch no longer asks recall doctor whether it must.
        model.onSkip = { [weak self] in AppState.update { $0.setupDone = true }; self?.finished = true; self?.mini.close(); self?.skip() }
        model.onDone = { [weak self] in AppState.update { $0.setupDone = true }; self?.finished = true; self?.mini.close(); self?.done() }
        mini.show(SetupView(model: model), size: NSSize(width: 520, height: 520), title: "Set up recall")
        mini.onClose = { [weak self] in
            self?.model.stop()
            if self?.finished == false { self?.left() }
            if !Probe.background, !(NSApp.windows.contains { $0.isVisible && $0 !== self?.mini.window && $0.level == .normal && $0.canBecomeMain }) {
                NSApp.setActivationPolicy(.accessory)
            }
        }
        model.begin()
    }
}
