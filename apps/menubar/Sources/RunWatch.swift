// When a night's memory run fails (boards H1 and H2): the app notices, marks the menu bar icon, notifies
// once per problem, and This Mac's run panel offers the fix that fits. It looks at `/v1/runs/latest` and
// `/v1/config` when the run log has changed since the last look (checked by a stat at launch, on wake and
// every 6 hours or so), and when recall says `runs` changed (the window's event stream). Nothing polls.
//
// What goes wrong is read from the run's `error` and its log lines, worded in src/recall/memory/dream.py
// ("reading failed for N chats (REASON); the next run tries them again") and src/recall/llm/base.py
// (CREDIT_ERRORS).

import AppKit
import SwiftUI

struct RunProblem: Equatable, Codable {
    enum Kind: String, Codable { case spend, keyRefused, signedOut, ollamaDown, noRun, died, other }
    var kind: Kind
    var title: String
    var detail: String
    var notice: String
    var provider: String
    var failed = 0
    var read = 0
    var raiseURL: String?
    var loginCommand: String?
    /// The Claude plan is listed in /v1/models and isn't what runs now.
    var claudePlanAvailable = false
}

/// What the run watch knew when it last asked recall, kept in app.json so a launch or a wake can tell
/// without starting `recall api`: the run log's mtime then, the last run's start, the failure found.
struct RunSnapshot: Codable, Equatable {
    var logMTime: Double?
    var started: String?
    var scheduleEnabled: Bool?
    var since: Double?  // when the nightly run was first seen on with no run yet: "two nights" counts from here
    var problem: RunProblem?
}

final class RunWatch: ObservableObject {
    static let shared = RunWatch()

    @Published var problem: RunProblem?
    @Published var fix: Said?
    @Published var checkedAt: Date?
    var onChange: () -> Void = {}
    var checking = false
    var again = false
    var pendingNoRun = false
    var tick: Timer?
    var awakeSince = Date()
    var uptimeAtAwake = ProcessInfo.processInfo.systemUptime
    var snap = RunSnapshot()
    let work = DispatchQueue(label: "recall.runwatch", qos: .utility)

    static var logPath: String { API.recallHome + "/logs/dream.log" }

    /// The nightly log's mtime: a stat, and the sign that a run wrote something new.
    static func logMTime() -> Double? {
        ((try? FileManager.default.attributesOfItem(atPath: logPath))?[.modificationDate] as? Date)?.timeIntervalSince1970
    }

    // MARK: When to look

    /// At launch and on wake the watch only stats the log; recall is asked (and `recall api` started) when
    /// it changed since the last look. Nothing polls.
    func start() {
        if Probe.fixture == nil {
            snap = AppState.load().runWatch ?? RunSnapshot()
            problem = snap.problem
            if problem != nil { onChange() }
        }
        check(gated: true)
        armTick()
        NSWorkspace.shared.notificationCenter.addObserver(forName: NSWorkspace.didWakeNotification, object: nil, queue: .main) { [weak self] _ in
            self?.markAwake()
            self?.armTick()
            // A few seconds after wake, once the network and the disk are back.
            DispatchQueue.main.asyncAfter(deadline: .now() + 20) { self?.check(gated: true) }
        }
    }

    /// The window's stream said the run log changed (recall api is up already).
    func changed() { check() }

    func markAwake() {
        awakeSince = Date()
        uptimeAtAwake = ProcessInfo.processInfo.systemUptime
    }

    /// `gated`: ask recall only if the run log changed since the last look.
    func check(gated: Bool = false) {
        guard !checking else { again = true; return }
        if Probe.fixture == nil {
            guard Tool.find("recall") != nil || API.commands().count > 0 else { settle(); return }
            if gated, Self.logMTime() == snap.logMTime { settle(); return }
        }
        checking = true
        let mtime = Self.logMTime()
        work.async {
            var run: Runs.Run?, config: Config?, models: Models?
            if let fx = Probe.fixture {
                run = fx.run; config = fx.config; models = fx.models
            } else {
                if API.ensureRunning(idle: App.apiIdle) != nil { self.finishCheck(nil, nil, keep: true); return }
                if case .success(let d) = API.raw("/v1/runs/latest") { run = (try? JSONDecoder().decode(Runs.self, from: d))?.latest }
                if case .success(let d) = API.raw("/v1/config") { config = try? JSONDecoder().decode(Config.self, from: d) }
            }
            // A run still going says nothing yet: what the last one found stays until it ends.
            if let run, run.finished == nil, run.error == nil, run.started != nil { self.finishCheck(nil, config, keep: true); return }
            var found = Self.classify(run: run, config: config, now: Date())
            if found != nil, found?.kind != .signedOut, found?.kind != .ollamaDown {
                if models == nil, Probe.fixture == nil, case .success(let d) = API.raw("/v1/models") { models = try? JSONDecoder().decode(Models.self, from: d) }
                found?.claudePlanAvailable = models?.options.contains { $0.provider == "claude" && $0.now != true } ?? false
            }
            self.finishCheck(found, config, run: run, mtime: mtime)
        }
    }

    func finishCheck(_ found: RunProblem?, _ config: Config?, run: Runs.Run? = nil, mtime: Double? = nil, keep: Bool = false) {
        DispatchQueue.main.async {
            self.checking = false
            self.checkedAt = Date()
            if !keep {
                if Probe.fixture == nil {
                    self.snap.logMTime = mtime
                    self.snap.started = run?.started
                    if let config { self.snap.scheduleEnabled = config.schedule?.enabled }
                    self.noteSince()
                    self.snap.problem = found
                    self.save()
                }
                // "No run for two nights" is raised by the timer only; here it is kept while it still holds.
                var shown = found
                if shown == nil, self.problem?.kind == .noRun, self.noRunDue(Date()) != nil { shown = self.problem }
                if self.problem?.kind != shown?.kind { self.fix = nil }
                self.problem = shown
                self.onChange()
                self.notifyIfNew(shown)
            }
            self.settle()
            if self.again { self.again = false; self.check() }
        }
    }

    func save() { let s = snap; AppState.update { $0.runWatch = s } }

    /// Setup has just turned the nightly run on: two nights are counted from now when there is no run yet.
    func scheduleSet() {
        guard Probe.fixture == nil else { return }
        snap.scheduleEnabled = true
        noteSince()
        save()
    }

    func noteSince() {
        if snap.scheduleEnabled == true, snap.started == nil, snap.since == nil { snap.since = Date().timeIntervalSince1970 }
    }

    // MARK: No run for two nights

    /// About every 6 hours (wide tolerance; it fires 3 hours after a wake or launch at the earliest): a stat of
    /// the log, a look at recall only if it changed, then the two-nights test on what is already held.
    func armTick() {
        tick?.invalidate()
        tick = nil
        guard Probe.fixture == nil else { return }
        let now = Date()
        let ripe = awakeSince.addingTimeInterval(3 * 3600 + 300)
        let t = Timer(fire: now < ripe ? ripe : now.addingTimeInterval(6 * 3600), interval: 0, repeats: false) { [weak self] _ in
            self?.pendingNoRun = true
            self?.armTick()
            self?.check(gated: true)
        }
        t.tolerance = 1800
        RunLoop.main.add(t, forMode: .common)
        tick = t
    }

    func settle() {
        guard pendingNoRun else { return }
        pendingNoRun = false
        guard problem == nil, let n = noRunDue(Date()) else { return }
        problem = n
        onChange()
        notifyIfNew(n)
    }

    /// The Mac has been awake for 3 hours since its last wake (or launch). A sleep this app wasn't told about
    /// shows as wall time running ahead of the uptime clock, which stops while asleep.
    func awakeLongEnough() -> Bool {
        let wall = Date().timeIntervalSince(awakeSince)
        let up = ProcessInfo.processInfo.systemUptime - uptimeAtAwake
        if wall - up > 120 { markAwake(); return false }
        return wall >= 3 * 3600
    }

    /// A memory run holds an exclusive flock on dream.lock for as long as it goes (dream.py `_exclusive`).
    static func runInProgress() -> Bool {
        let fd = open(API.recallHome + "/dream.lock", O_RDONLY)
        guard fd >= 0 else { return false }
        defer { close(fd) }
        if flock(fd, LOCK_SH | LOCK_NB) == 0 { flock(fd, LOCK_UN); return false }
        return errno == EWOULDBLOCK
    }

    /// The nightly run is on, the last run started over 49 hours ago (or setup did, with no run yet), the Mac
    /// has been awake 3 hours, and no run is going. Held data only: no process is started.
    func noRunDue(_ now: Date) -> RunProblem? {
        guard Probe.fixture == nil, snap.scheduleEnabled == true else { return nil }
        guard let ref = Self.date(snap.started) ?? snap.since.map({ Date(timeIntervalSince1970: $0) }),
              now.timeIntervalSince(ref) > 49 * 3600, awakeLongEnough(), !Self.runInProgress() else { return nil }
        return RunProblem(kind: .noRun, title: "No memory run for two nights", detail: "The nightly run hasn’t happened since \(Self.day(ref)).",
                          notice: "No memory run for two nights. Click to fix it.", provider: "")
    }

    // MARK: What went wrong

    static let credit = ["credit balance is too low", "insufficient_quota", "billing_error", "api usage limits"]
    static let refused = ["http 401", "http 403", "invalid x-api-key", "invalid api key", "incorrect api key", "authentication_error"]
    static let signedOut = ["not signed in", "not logged in", "/login", "codex login"]
    static let unreachable = ["cannot reach", "connection refused", "ollama", "11434"]

    /// The last memory run, as a problem to fix or nothing. A few failed chats are not a problem:
    /// they retry by themselves. A problem is a run that learned nothing, at least half of a batch of five or
    /// more failing, or a run that died. (A nightly run that hasn't happened is `noRunDue`'s.)
    static func classify(run: Runs.Run?, config: Config?, now: Date) -> RunProblem? {
        if let run, run.finished != nil || run.error != nil {
            let failed = run.counts?["failed"] ?? 0
            let read = run.counts?["chats"] ?? run.chats_read ?? 0
            let reasons = (run.steps ?? []).compactMap { reason($0.text) }
            let provider = (run.backend ?? "").split(separator: ":").first.map(String.init) ?? ""
            let text = ([run.error ?? ""] + reasons).joined(separator: " ").lowercased()
            var p: RunProblem?
            if let e = run.error, !e.isEmpty {
                p = RunProblem(kind: .died, title: "Last run stopped", detail: clip(firstLine(e), 140),
                               notice: "The last memory run stopped. Click to fix it.", provider: provider, failed: failed, read: read)
            } else {
                let nothing = read == 0 && failed > 0
                let half = failed + read >= 5 && failed * 2 >= failed + read
                if nothing || half {
                    let title = nothing ? "Last run learned nothing" : "Last run failed for \(failed) of \(failed + read) chats"
                    let notice = nothing ? "The last memory run learned nothing. Click to fix it."
                        : "The last memory run failed for \(failed) of \(failed + read) chats. Click to fix it."
                    p = kindOf(text: text, provider: provider, reasons: reasons, title: title, notice: notice)
                    p?.failed = failed
                    p?.read = read
                }
            }
            if let p { return p }
        }
        return nil
    }

    static func kindOf(text: String, provider: String, reasons: [String], title: String, notice: String) -> RunProblem {
        let who = provider == "anthropic" ? "Your Anthropic key" : provider == "openai" ? "Your OpenAI key" : provider == "openrouter" ? "Your OpenRouter key" : "Your API key"
        if credit.contains(where: text.contains) {
            let url = provider == "anthropic" ? "https://console.anthropic.com/settings/limits"
                : provider == "openai" ? "https://platform.openai.com/settings/organization/limits"
                : provider == "openrouter" ? "https://openrouter.ai/settings/credits" : nil
            return RunProblem(kind: .spend, title: title, detail: who + " hit its spend limit.", notice: notice, provider: provider, raiseURL: url)
        }
        if refused.contains(where: text.contains) {
            return RunProblem(kind: .keyRefused, title: title, detail: who.replacingOccurrences(of: "Your ", with: "") + " was refused. Check the key with recall model.", notice: notice, provider: provider)
        }
        if signedOut.contains(where: text.contains) {
            let codex = provider == "chatgpt"
            return RunProblem(kind: .signedOut, title: title, detail: codex ? "Codex isn’t signed in." : "Claude Code isn’t signed in.", notice: notice,
                              provider: provider, loginCommand: codex ? "codex login" : "claude /login")
        }
        if provider == "local" || unreachable.contains(where: text.contains) {
            if unreachable.contains(where: text.contains) {
                return RunProblem(kind: .ollamaDown, title: title, detail: "Ollama isn’t running.", notice: notice, provider: provider)
            }
        }
        let first = reasons.first.map { clip(firstLine($0), 140) } ?? "Some chats couldn’t be read."
        return RunProblem(kind: .other, title: title, detail: first, notice: notice, provider: provider)
    }

    /// "reading failed for 19 chats (REASON); the next run tries them again" → REASON.
    static func reason(_ line: String) -> String? {
        guard line.hasPrefix("reading failed for "), let open = line.firstIndex(of: "("),
              let close = line.range(of: "); the next run tries them again", options: .backwards) else { return nil }
        return String(line[line.index(after: open)..<close.lowerBound])
    }

    static func firstLine(_ s: String) -> String { s.split(separator: "\n").first.map(String.init) ?? s }
    static func clip(_ s: String, _ n: Int) -> String { s.count <= n ? s : String(s.prefix(n - 1)) + "…" }
    static func date(_ s: String?) -> Date? { s.flatMap { ISO8601DateFormatter().date(from: $0) } }
    static func day(_ s: String?) -> String { date(s).map { day($0) } ?? "a while" }
    static func day(_ d: Date) -> String {
        let f = DateFormatter()
        f.dateFormat = "d MMM"
        return f.string(from: d)
    }

    // MARK: Telling the person

    /// One notification per problem. The last one told is kept in app.json; a healthy run clears it.
    func notifyIfNew(_ p: RunProblem?) {
        guard let p else { Self.remember(nil); return }
        guard Self.remembered() != p.kind.rawValue else { return }
        Self.remember(p.kind.rawValue)
        switch AppState.load().notifyFailures {
        case false?: return
        case true?: Notifier.shared.authorize { ok in if ok { Notifier.shared.post(p.notice, id: "run-" + p.kind.rawValue) } }
        case nil:
            // People who skipped setup are asked now, at their first problem.
            Notifier.shared.authorize { ok in
                AppState.update { $0.notifyFailures = ok }
                if ok { Notifier.shared.post(p.notice, id: "run-" + p.kind.rawValue) }
            }
        }
    }

    /// The last problem told about, with the app's other state in app.json.
    static func remembered() -> String? { AppState.load().lastProblem }
    static func remember(_ v: String?) {
        guard remembered() != v else { return }
        AppState.update { $0.lastProblem = v }
    }

    // MARK: The fixes

    func useClaudePlan(_ library: Library) {
        fix = .working("Testing with one short call…")
        API.send("PUT", "/v1/model", ["option": "claude:haiku"], ModelSaved.self, timeout: 300) { [weak self] r in
            switch r {
            case .success: self?.fix = .done("Saved: runs use your Claude plan")
            case .failure(let f): self?.fix = .failed("That didn’t work: \(f.message). Nothing changed.")
            }
            library.fetch("/v1/config", Config.self) { library.facts.config = $0 }
            library.fetch("/v1/models", Models.self) { library.facts.models = $0 }
        }
    }

    func copy(_ command: String) {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(command, forType: .string)
        fix = .done("Copied. Paste it in Terminal, then run again.")
    }

    func openOllama() {
        let app = "/Applications/Ollama.app"
        if FileManager.default.fileExists(atPath: app) { Open.path(app) } else { Open.url("https://ollama.com/download") }
        fix = .done("Opened Ollama. Run again when it’s up.")
    }
}

// MARK: - Run again now

/// The same detached run setup starts: `recall dream` with the nightly run's limits, its output appended
/// to the memory run log. Shows squares while it goes and what it read and changed when it ends.
final class RunNow: ObservableObject {
    static let shared = RunNow()
    enum State: Equatable { case idle, running, finished(read: Int, changed: Int), failed(String) }
    @Published var state: State = .idle
    @Published var squares = 0

    var running: Bool { state == .running }
    /// This app's run or anyone else's (the nightly job, Terminal). The lock is the test, because a run that
    /// crashed never records an end but can't still hold it.
    var busy: Bool { running || RunWatch.runInProgress() }
    var log: String { API.recallHome + "/logs/dream.log" }

    func start(limits: (Int, Int)? = nil) {
        guard !running, let recall = Tool.find("recall") else { return }
        state = .running
        squares = 1
        let go = { (minutes: Int, calls: Int) in
            let command = [recall, "dream", "--max-minutes", String(minutes), "--max-calls", String(calls), "--on-battery", "run"]
            let started = Shell.detached(command, log: self.log) { status in self.ended(status) }
            if !started { self.state = .failed("Couldn’t start the run.") }
        }
        if let limits { go(limits.0, limits.1); return }
        RunWatch.shared.work.async {
            var minutes = 120, calls = 80
            if API.ensureRunning(idle: App.apiIdle) == nil, case .success(let d) = API.raw("/v1/config"), let c = try? JSONDecoder().decode(Config.self, from: d) {
                minutes = Int(c.schedule?.limits?.max_minutes ?? 120)
                calls = c.schedule?.limits?.max_calls ?? 80
            }
            DispatchQueue.main.async { go(minutes, calls) }
        }
    }

    /// The window's event stream says the run wrote its log: one more square.
    func progress(_ run: Runs.Run?) {
        guard running else { return }
        squares = min(11, max(squares, 1 + 2 * (run?.steps?.count ?? 0)))
    }

    func ended(_ status: Int32) {
        RunWatch.shared.work.async {
            var run: Runs.Run?
            if API.ensureRunning(idle: App.apiIdle) == nil, case .success(let d) = API.raw("/v1/runs/latest") { run = (try? JSONDecoder().decode(Runs.self, from: d))?.latest }
            DispatchQueue.main.async {
                let c = run?.counts ?? [:]
                let read = c["chats"] ?? run?.chats_read ?? 0
                let changed = ["created", "added", "refined", "retired"].reduce(0) { $0 + (c[$1] ?? 0) }
                if status != 0 && run?.finished == nil {
                    self.state = .failed("The run stopped. See \(self.log.replacingOccurrences(of: API.home, with: "~")).")
                } else if read == 0, (c["failed"] ?? 0) > 0 {
                    self.state = .failed("It still can’t read your chats.")
                } else {
                    self.squares = 12
                    self.state = .finished(read: read, changed: changed)
                }
                RunWatch.shared.check()
            }
        }
    }
}

extension Library {
    /// This Mac's Run now: starts the run and goes to the panel its progress shows in, which is further up the page.
    func runNow() {
        RunNow.shared.start()
        thisMacFocus.id = "Last memory run"
        thisMacFocus.tick += 1
    }
}

// MARK: - The block in This Mac's run panel

struct RunProblemView: View {
    @ObservedObject var watch: RunWatch
    @ObservedObject var now: RunNow
    let library: Library
    @Environment(\.colorScheme) var scheme

    var body: some View {
        let p = Palette.of(scheme)
        if let q = watch.problem {
            VStack(alignment: .leading, spacing: 8) {
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Rectangle().strokeBorder(p.kind("open"), lineWidth: 1).frame(width: 8, height: 8)
                    Text(q.title).font(PaperMono.font(12.5, 520)).foregroundStyle(p.ink)
                }
                Text(q.detail).foregroundStyle(p.body).fixedSize(horizontal: false, vertical: true)
                if let fix = watch.fix { SaidLine(said: fix) }
                HStack(spacing: 8) {
                    fixes(q, p)
                    if q.kind != .noRun, q.kind != .other, q.kind != .died { again(primary: watch.fix != nil) }
                }
                if q.kind == .noRun || q.kind == .other || q.kind == .died { HStack { again(primary: true, title: q.kind == .noRun ? "Run now" : "Run again now") } }
                progress(p)
            }
            .padding(12)
            .frame(maxWidth: .infinity, alignment: .leading)
            .overlay(Rectangle().strokeBorder(p.kind("open").opacity(0.6), lineWidth: 1))
            .padding(.bottom, 16)
        } else if now.state != .idle {
            VStack(alignment: .leading, spacing: 8) { progress(p) }.padding(.bottom, 12)
        }
    }

    @ViewBuilder
    func fixes(_ q: RunProblem, _ p: Palette) -> some View {
        switch q.kind {
        case .spend:
            if q.claudePlanAvailable { MainButton(title: "Use Claude plan") { watch.useClaudePlan(library) } }
            if let url = q.raiseURL { WindowButton(title: "Raise limit ↗") { Open.url(url) } }
        case .keyRefused:
            if q.claudePlanAvailable { MainButton(title: "Use Claude plan") { watch.useClaudePlan(library) } }
        case .signedOut:
            if let c = q.loginCommand { MainButton(title: "Copy: " + c) { watch.copy(c) } }
        case .ollamaDown:
            MainButton(title: "Open Ollama") { watch.openOllama() }
        default:
            EmptyView()
        }
    }

    func again(primary: Bool, title: String = "Run again now") -> some View {
        let busy = now.busy
        return Group {
            if primary { MainButton(title: busy ? "Running…" : title, enabled: !busy) { now.start() } } else {
                WindowButton(title: busy ? "Running…" : title, soon: busy, why: "A run is going") { now.start() }
            }
        }
    }

    @ViewBuilder
    func progress(_ p: Palette) -> some View {
        switch now.state {
        case .running:
            VStack(alignment: .leading, spacing: 8) { SquareBar(filled: now.squares); Text("Reading your chats…").foregroundStyle(p.muted) }
        case .finished(let read, let changed):
            CheckLine(text: "\(count(read)) chat\(read == 1 ? "" : "s") read · \(count(changed)) note\(changed == 1 ? "" : "s") changed")
        case .failed(let why):
            SaidLine(said: .failed(why))
        case .idle:
            EmptyView()
        }
    }
}
