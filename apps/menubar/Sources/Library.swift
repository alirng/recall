// What the window shows, fetched from `recall api` and kept fresh by its event stream and by a
// refetch when the window gains focus. No timers: nothing here runs unless recall says something
// changed or you do something.

import AppKit
import SwiftUI

// MARK: - The app's own state file

/// The app's one file of its own, `~/.agents/recall/app.json`: window size, sidebar width, the
/// menu bar's look and the last section. Nothing goes to UserDefaults or ~/Library.
struct AppState: Codable {
    var window: [Double]?  // x, y, width, height
    var sidebar: Double?
    var ui: String?  // "menu" for the plain menu instead of the panel
    var section: String?
    var pins: [String]?
    var appearance: String?  // "dark" or "light" when set from the palette; the system's otherwise

    static let url = URL(fileURLWithPath: API.home + "/.agents/recall/app.json")

    static func load() -> AppState {
        (try? JSONDecoder().decode(AppState.self, from: Data(contentsOf: url))) ?? AppState()
    }

    func save() {
        let e = JSONEncoder()
        e.outputFormatting = [.prettyPrinted, .sortedKeys]
        guard let data = try? e.encode(self) else { return }
        try? FileManager.default.createDirectory(at: Self.url.deletingLastPathComponent(), withIntermediateDirectories: true)
        try? data.write(to: Self.url, options: .atomic)
    }
}

// MARK: - Places

enum Place: Hashable {
    case search, memory, chats, projects, agent(String), now, thisMac

    var title: String {
        switch self {
        case .search: return "Search"
        case .memory: return "Memory"
        case .chats: return "Chats"
        case .projects: return "Projects"
        case .agent(let a): return agentName(a)
        case .now: return "Now"
        case .thisMac: return "This Mac"
        }
    }

    var key: String {
        switch self {
        case .agent(let a): return "agent:" + a
        default: return title.lowercased()
        }
    }

    init?(key: String) {
        if key.hasPrefix("agent:") { self = .agent(String(key.dropFirst(6))); return }
        let all: [Place] = [.search, .memory, .chats, .projects, .now, .thisMac]
        guard let s = all.first(where: { $0.key == key }) else { return nil }
        self = s
    }
}

func agentName(_ a: String) -> String {
    agentNames[a] ?? a.prefix(1).uppercased() + a.dropFirst()
}

// MARK: - Shared

final class Library: ObservableObject {
    enum Link: Equatable { case connecting, ready, failed(String) }

    @Published var section: Place = .memory
    @Published var link: Link = .connecting
    @Published var info: Info?
    let popups = Popups()
    let palette = PaletteModel()
    let find = FindModel()
    let thisMacFocus = Cursor()  // a This Mac panel to scroll to, from the palette
    /// Projects pinned to the top of the project pickers, kept in app.json.
    @Published var pins: [String] = AppState.load().pins ?? []
    var toggleAppearance: () -> Void = {}

    let facts = Facts()
    let notes = NotesModel()
    let chats = ChatsModel()
    /// One chats model per agent page, so each keeps its own list and switching agents doesn't
    /// swap a hundred rows in one list.
    var agentChats: [String: ChatsModel] = [:]
    var prewarm: (([String]) -> Void)?
    var projectsStale = false
    var warmed = false

    func chats(for agent: String) -> ChatsModel {
        if let m = agentChats[agent] { return m }
        let m = ChatsModel()
        m.library = self
        m.agent = agent
        agentChats[agent] = m
        return m
    }

    /// The chats model on screen, if a chats page is.
    var shownChats: ChatsModel? {
        switch section {
        case .chats: return chats
        case .agent(let a): return chats(for: a)
        default: return nil
        }
    }
    let search = SearchModel()
    var events: Events?
    var lastLoad = Date.distantPast
    var visible = false
    // While the window is hidden behind others or minimised, changes are only noted, then fetched
    // when it shows again: busy agents can change the index every few seconds.
    var onScreen = true { didSet { if onScreen, !stale.isEmpty { let t = stale; stale = []; changed(t) } } }
    var stale: Set<String> = []
    var inflight: Set<String> = []
    var again: [String: () -> Void] = [:]
    var last: [String: Data] = [:]

    deinit { if Probe.on { FileHandle.standardError.write(Data("library released\n".utf8)) } }

    init() {
        palette.library = self
        find.library = self
        notes.library = self
        chats.library = self
        search.library = self
    }

    // MARK: Opening and closing

    func open() {
        visible = true
        connect()
    }

    /// The window closed: drop the stream, so `recall api` can exit once it's idle.
    func close() {
        visible = false
        events?.stop()
        events = nil
    }

    var connecting = false

    func connect() {
        guard !connecting else { return }  // the window's first focus arrives while opening connects
        connecting = true
        if link != .ready { link = .connecting }
        API.queue.async {
            let failure = API.ensureRunning()
            DispatchQueue.main.async {
                if let failure { self.connecting = false; self.link = .failed(failure.message); return }
                API.get("/v1/info", Info.self) { r in
                    self.connecting = false
                    switch r {
                    case .success(let info):
                        self.info = info
                        if info.api < API.minimumAPI {
                            self.link = .failed("This recall (\(info.recall)) is too old for the app. Upgrade it with uv tool upgrade recall-agents.")
                            return
                        }
                        self.link = .ready
                        self.listen()
                        self.loadAll()
                    case .failure(let f):
                        self.link = .failed(f.status == 404 ? "This recall is too old for the app: it has no /v1/info. Upgrade it with uv tool upgrade recall-agents." : f.message)
                    }
                }
            }
        }
    }

    func listen() {
        guard visible, events == nil else { return }
        let e = Events(onChange: { [weak self] topics in self?.changed(Set(topics)) }, onEnd: { [weak self] in
            // The stream ended (recall stopped or was upgraded). Reconnect on the next focus, not on a timer.
            self?.events = nil
        })
        events = e
        e.start()
    }

    /// The window gained focus: catch up, since edits made by hand to a note aren't streamed.
    func focused() {
        guard visible else { return }
        if link != .ready || events == nil { connect(); return }
        if Date().timeIntervalSince(lastLoad) > 5 { loadAll() }
    }

    func loadAll() {
        lastLoad = Date()
        // The view first. The slow answers (stats about 1 s, status about 0.5 s) hold the server's
        // database thread, so they're asked for only once the first view's data is in.
        let rest = { [weak self] in
            guard let self else { return }
            self.loadCounts()
            self.fetch("/v1/doctor", Doctor.self) { self.facts.doctor = $0 }
            self.fetch("/v1/runs/latest", Runs.self) { self.facts.runs = $0; self.notes.lastRun = $0.latest?.started }
            self.fetch("/v1/config", Config.self) { self.facts.config = $0 }
            self.fetch("/v1/coordination", Coordination.self) { self.facts.coordination = $0 }
            self.fetch("/v1/projects", Projects.self) { self.facts.projects = $0 }
            self.fetch("/v1/storage", Storage.self) { self.facts.storage = $0 }
            self.fetch("/v1/stats", Stats.self) { self.facts.stats = $0; self.loadAgentTotals($0) }
            self.fetch("/v1/status", Status.self) { self.facts.status = $0 }
            self.warm()
        }
        shownChats?.reload()
        notes.reload(then: rest)
    }

    var needsYou: Int { facts.needsYou }

    func loadAgentTotals(_ s: Stats) {
        for a in s.agents.keys {
            fetch("/v1/chats?agent=\(API.q(a))&limit=1", ChatsPage.self) { self.facts.agentTotals[a] = $0.total }
        }
    }

    /// Once per window: load the other pages' first data and build their views out of sight.
    func warm() {
        guard !warmed else { return }
        warmed = true
        let agents = ["claude", "codex", "cursor"]
        // A hidden list doesn't fill in, so each page is built once its first data is in.
        let loaded = DispatchGroup()
        for m in [chats] + agents.map({ chats(for: $0) }) where m.rows.isEmpty {
            loaded.enter()
            m.reload(then: { loaded.leave() })
        }
        loaded.enter()
        fetch("/v1/projects", Projects.self, always: { loaded.leave() }) { self.facts.projects = $0 }
        // Agent pages are built on their first visit (about 0.2 s): each kept page costs about 20 MB.
        let keys = ["chats", "this mac", "projects", "now", "search"]
        if CommandLine.arguments.contains("--no-prewarm") { return }
        loaded.notify(queue: .main) { self.prewarm?(keys.filter { $0 != self.section.host }) }
    }

    let projectsCursor = Cursor(), searchCursor = Cursor(), nowCursor = Cursor()

    /// A key pressed in a list: true if it was used. In Memory and Chats the arrows select, as in
    /// Mail; elsewhere they mark a row and Return opens it.
    func key(_ k: Key) -> Bool {
        switch section {
        case .memory:
            guard k != .open, let id = notes.cursor.step(k == .down, notes.rows.map(\.id)) else { return false }
            notes.select(id)
            if notes.rows.suffix(10).contains(where: { $0.id == id }) { notes.more() }
            return true
        case .chats, .agent:
            guard k != .open, let m = shownChats, let id = m.cursor.step(k == .down, m.rows.map(\.id)) else { return false }
            m.select(id)
            if m.rows.suffix(10).contains(where: { $0.id == id }) { m.more() }
            return true
        case .projects:
            let all = facts.projects?.projects ?? []
            if k == .open {
                guard let id = projectsCursor.id, let pr = all.first(where: { $0.name == id }) else { return false }
                showProject(pr.name, notesFirst: pr.notes > 0)
                return true
            }
            return projectsCursor.step(k == .down, all.map(\.name)) != nil
        case .search:
            let r = search.result
            let ids = (r?.notes ?? []).map { "note:" + $0.note_id } + (r?.chats ?? []).map { "chat:" + $0.chat_id }
            if k == .open {
                guard let id = searchCursor.id else { return false }
                if id.hasPrefix("note:") { openNote(String(id.dropFirst(5))) } else { openChat(String(id.dropFirst(5))) }
                return true
            }
            return searchCursor.step(k == .down, ids) != nil
        case .now:
            let live = (facts.status?.sessions ?? []).filter(\.live).map(\.id)
            if k == .open {
                guard let id = nowCursor.id else { return false }
                openChat(id)
                return true
            }
            return nowCursor.step(k == .down, live) != nil
        case .thisMac:
            return false
        }
    }

    var isAgent: Bool { if case .agent = section { return true } else { return false } }

    func loadCounts() {
        fetch("/v1/notes?limit=1", NotesPage.self) { self.facts.notesCount = $0.total }
        fetch("/v1/chats?limit=1", ChatsPage.self) { self.facts.chatsCount = $0.total }
    }

    func changed(_ topics: Set<String>) {
        if Probe.on { FileHandle.standardError.write(Data("\(Date()) changed \(topics.sorted())\(onScreen ? "" : " (hidden)")\n".utf8)) }
        guard onScreen else { stale.formUnion(topics); return }
        if topics.contains("chats") {
            batched {
                fetch("/v1/stats", Stats.self) { self.facts.stats = $0 }
                fetch("/v1/chats?limit=1", ChatsPage.self) { self.facts.chatsCount = $0.total }
                fetch("/v1/doctor", Doctor.self) { self.facts.doctor = $0 }
                if let s = facts.stats { loadAgentTotals(s) }
            }
            // Projects change with every chat but show on one page: fetched there, or on the way there.
            if section == .projects { fetch("/v1/projects", Projects.self) { self.facts.projects = $0 } } else { projectsStale = true }
            for m in [chats] + Array(agentChats.values) {
                if m === shownChats { m.refresh() } else { m.stale = true }
            }
        }
        if topics.contains("notes") {
            notes.refresh()
            loadCounts()
            fetch("/v1/storage", Storage.self) { self.facts.storage = $0 }
        }
        if topics.contains("runs") { fetch("/v1/runs/latest", Runs.self) { self.facts.runs = $0; self.notes.lastRun = $0.latest?.started } }
        if topics.contains("config") {
            fetch("/v1/config", Config.self) { self.facts.config = $0 }
            fetch("/v1/coordination", Coordination.self) { self.facts.coordination = $0 }
        }
        if topics.contains("status") { fetch("/v1/status", Status.self) { self.facts.status = $0 } }
    }

    /// One request per path at a time: a change that arrives while it's out asks once more after.
    /// While set, answers are held and applied together when the last one is in, so one change
    /// from recall redraws once rather than once per answer.
    var batch: DispatchGroup?
    var held: [ObjectIdentifier: [() -> Void]] = [:]

    func batched(_ asks: () -> Void) {
        let group = DispatchGroup()
        batch = group
        asks()
        batch = nil
        group.notify(queue: .main) { [weak self] in
            let apply = self?.held.removeValue(forKey: ObjectIdentifier(group)) ?? []
            apply.forEach { $0() }
        }
    }

    func fetch<T: Decodable>(_ path: String, _ type: T.Type, always: (() -> Void)? = nil, _ use: @escaping (T) -> Void) {
        if inflight.contains(path) {
            again[path] = { [weak self] in self?.fetch(path, type, use) }
            always?()
            return
        }
        inflight.insert(path)
        let group = batch
        group?.enter()
        API.queue.async {
            let r = API.raw(path)
            DispatchQueue.main.async {
                self.inflight.remove(path)
                // The same answer as last time changes nothing, so it isn't decoded or redrawn.
                if case .success(let data) = r, data != self.last[path], let v = try? JSONDecoder().decode(T.self, from: data) {
                    self.last[path] = data
                    if let group { self.held[ObjectIdentifier(group), default: []].append { use(v) } } else { use(v) }
                }
                if let next = self.again.removeValue(forKey: path) { next() }
                always?()
                group?.leave()
            }
        }
    }

    // MARK: Going places

    func togglePin(_ name: String) {
        if let i = pins.firstIndex(of: name) { pins.remove(at: i) } else { pins.append(name) }
        var s = AppState.load()
        s.pins = pins
        s.save()
    }

    func go(_ s: Place) {
        popups.close()
        section = s
        switch s {
        case .chats: chats.show()
        case .agent(let a): chats(for: a).show()
        case .now: fetch("/v1/status", Status.self) { self.facts.status = $0 }
        case .projects where projectsStale:
            projectsStale = false
            fetch("/v1/projects", Projects.self) { self.facts.projects = $0 }
        default: break
        }
    }

    func openNote(_ id: String) {
        section = .memory
        notes.select(id)
    }

    func openChat(_ id: String) {
        section = .chats
        chats.agent = nil
        chats.project = nil
        chats.reload()
        chats.select(id)
    }

    func showProject(_ name: String, notesFirst: Bool) {
        if notesFirst {
            section = .memory
            notes.project = name
            notes.reload()
        } else {
            section = .chats
            chats.agent = nil
            chats.project = name
            chats.reload()
        }
    }

}

/// What recall reports about itself: counts, runs, settings and status. Its own object, so a
/// change to a count redraws the views that show counts, not the notes list beside them.
final class Facts: ObservableObject {
    @Published var stats: Stats?
    @Published var notesCount: Int?
    @Published var chatsCount: Int?
    @Published var doctor: Doctor?
    @Published var storage: Storage?
    @Published var config: Config?
    @Published var coordination: Coordination?
    @Published var runs: Runs?
    @Published var status: Status?
    @Published var projects: Projects?

    var needsYou: Int { (status?.overlaps.count ?? 0) + (status?.restarts.count ?? 0) }

    /// Each agent's total in the Chats list, which leaves out subagent chats as the list does.
    @Published var agentTotals: [String: Int] = [:]

    /// Agents with chats: Claude Code, Codex and Cursor first, then the rest by count. The counts
    /// are the Chats list's (no subagents), once they're in.
    var agents: [(name: String, chats: Int)] {
        guard let stats else { return [] }
        let first = ["claude", "codex", "cursor"]
        return stats.agents.map { ($0.key, agentTotals[$0.key] ?? $0.value.chats) }.filter { $0.1 > 0 }.sorted { a, b in
            let ia = first.firstIndex(of: a.0) ?? 99, ib = first.firstIndex(of: b.0) ?? 99
            return ia != ib ? ia < ib : a.1 > b.1
        }
    }

    var indexedAt: Date? {
        (doctor?.roots ?? []).compactMap(\.indexed_at).max().map { Date(timeIntervalSince1970: $0) }
    }

    var nextRun: String? {
        guard let s = config?.schedule, s.enabled ?? false, let t = s.time else { return nil }
        return t
    }
}

/// Up, Down and Return in a list, without the system's rounded highlight: which row is marked,
/// and a tick the list watches to scroll it into view. Only the keyboard moves it.
final class Cursor: ObservableObject {
    @Published var id: String?
    @Published var tick = 0

    /// Moves to the neighbouring id; false at either end or with nothing to move through.
    func step(_ down: Bool, _ ids: [String]) -> String? {
        guard !ids.isEmpty else { return nil }
        let at = id.flatMap { ids.firstIndex(of: $0) }
        let next = at.map { down ? min($0 + 1, ids.count - 1) : max($0 - 1, 0) } ?? 0
        id = ids[next]
        tick += 1
        return ids[next]
    }
}

enum Key { case up, down, open }

final class ObservableTick: ObservableObject {
    @Published var n = 0
    func fire() { n += 1 }
}

// MARK: - Notes

final class NotesModel: ObservableObject {
    weak var library: Library?
    @Published var type: String?
    @Published var project: String?
    @Published var sort = "last_seen"
    @Published var lastRun: String?  // when last night's run started, to mark the notes it made
    let cursor = Cursor()
    @Published var rows: [NoteRow] = []
    @Published var total = 0
    @Published var types: [String: Int] = [:]
    @Published var projects: [String] = []
    var projectFacet: [String: Int] = [:]
    @Published var selected: String?
    @Published var note: Note?
    @Published var error: String?
    var loading = false
    var generation = 0
    static let page = 100

    var query: String {
        var q = "sort=\(sort)"
        if let type { q += "&type=\(API.q(type))" }
        if let project { q += "&project=\(API.q(project))" }
        return q
    }

    func reload(then: (() -> Void)? = nil) {
        generation += 1
        let g = generation
        loading = true
        API.get("/v1/notes?\(query)&limit=\(Self.page)&offset=0", NotesPage.self) { r in
            defer { then?() }
            guard g == self.generation else { return }
            self.loading = false
            switch r {
            case .success(let page):
                self.rows = page.notes
                self.total = page.total
                self.error = nil
                Probe.drawn("notes list")
                if let f = page.facets {
                    self.types = f.types ?? [:]
                    self.projects = (f.projects ?? [:]).sorted { $0.value > $1.value }.map(\.key)
                    self.projectFacet = f.projects ?? [:]
                }
                if !page.notes.contains(where: { $0.id == self.selected }), let first = page.notes.first {
                    self.select(first.id)
                }
            case .failure(let f): self.error = f.message
            }
        }
    }

    /// recall committed new notes: fetch what's loaded again in one go, so the list keeps its place.
    func refresh() {
        generation += 1
        let g = generation
        let n = max(Self.page, rows.count)
        API.get("/v1/notes?\(query)&limit=\(n)&offset=0", NotesPage.self) { r in
            guard g == self.generation, case .success(let page) = r else { return }
            self.rows = page.notes
            self.total = page.total
        }
        if let selected { select(selected) }
    }

    /// The next page, asked for as the list nears its end.
    func more() {
        guard !loading, rows.count < total else { return }
        loading = true
        let g = generation
        API.get("/v1/notes?\(query)&limit=\(Self.page)&offset=\(rows.count)", NotesPage.self) { r in
            guard g == self.generation else { return }
            self.loading = false
            if case .success(let page) = r {
                let have = Set(self.rows.map(\.id))
                self.rows += page.notes.filter { !have.contains($0.id) }
                self.total = page.total
            }
        }
    }

    func select(_ id: String) {
        selected = id
        cursor.id = id
        API.get("/v1/notes/\(API.q(id))", Note.self) { r in
            guard self.selected == id else { return }
            if case .success(let n) = r { self.note = n }
        }
    }

    func set(type: String?) {
        self.type = type
        reload()
    }

    func set(sort: String) {
        self.sort = sort
        reload()
    }

    func set(project: String?) {
        self.project = project
        reload()
    }

    func copyMarkdown() {
        guard let n = note else { return }
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(markdown(n), forType: .string)
    }

    /// Each project's note count, for the picker, and every note's count for "All projects".
    var projectCounts: [(name: String, count: Int)] { projectFacet.sorted { $0.value > $1.value }.map { ($0.key, $0.value) } }
    var allTotal: Int? { library?.facts.notesCount }

    /// The note as markdown, built from what the API gave, for the clipboard.
    func markdown(_ n: Note) -> String {
        var out = "# \(n.title)\n\n"
        out += [n.type, n.project, n.status].compactMap { $0 }.joined(separator: " · ") + "\n\n"
        let rules = (n.rules_all ?? []).filter { ($0.status ?? "active") == "active" }
        if rules.isEmpty {
            out += (n.body ?? "") + "\n"
        } else {
            out += rules.map { "- " + $0.text }.joined(separator: "\n") + "\n"
        }
        let sources = (n.source_chats ?? []).filter { $0.found ?? true }
        if !sources.isEmpty {
            out += "\n## Learned from\n\n" + sources.map { "- \($0.title ?? $0.chat_id) (\(agentName($0.agent ?? "")), \(day($0.date)))" }.joined(separator: "\n") + "\n"
        }
        return out
    }
}

// MARK: - Chats

final class ChatsModel: ObservableObject {
    weak var library: Library?
    @Published var agent: String?
    @Published var project: String?
    @Published var rows: [ChatRow] = []
    @Published var total = 0
    @Published var selected: String?
    @Published var error: String?
    let reading = Reading()
    let cursor = Cursor()
    var loading = false
    var generation = 0
    static let page = 100

    var query: String {
        var q = ""
        if let agent { q += "&agent=\(API.q(agent))" }
        if let project { q += "&project=\(API.q(project))" }
        return q
    }

    /// Shown again: what it had stays, and it refetches only if chats changed while away.
    func show() {
        if rows.isEmpty || stale { reload() }
    }

    func set(project: String?) {
        self.project = project
        reload()
    }

    var stale = false
    var cache: [String: (rows: [ChatRow], total: Int)] = [:]  // each filter's first page, shown at once on return

    func reload(then: (() -> Void)? = nil) {
        generation += 1
        let g = generation
        loading = true
        stale = false
        let key = query
        if let hit = cache[key] {
            rows = hit.rows
            total = hit.total
        }
        API.get("/v1/chats?limit=\(Self.page)&offset=0\(key)", ChatsPage.self) { r in
            defer { then?() }
            guard g == self.generation else { return }
            self.loading = false
            switch r {
            case .success(let page):
                self.cache[key] = (page.chats, page.total)
                if page.chats != self.rows { self.rows = page.chats }
                if page.total != self.total { self.total = page.total }
                self.error = nil
            case .failure(let f): self.error = f.message
            }
        }
    }

    func refresh() {
        generation += 1
        let g = generation
        API.get("/v1/chats?limit=\(max(Self.page, rows.count))&offset=0\(query)", ChatsPage.self) { r in
            guard g == self.generation, case .success(let page) = r else { return }
            self.rows = page.chats
            self.total = page.total
        }
    }

    func more() {
        guard !loading, rows.count < total else { return }
        loading = true
        let g = generation
        API.get("/v1/chats?limit=\(Self.page)&offset=\(rows.count)\(query)", ChatsPage.self) { r in
            guard g == self.generation else { return }
            self.loading = false
            if case .success(let page) = r {
                let have = Set(self.rows.map(\.id))
                self.rows += page.chats.filter { !have.contains($0.id) }
                self.total = page.total
            }
        }
    }

    func select(_ id: String) {
        selected = id
        cursor.id = id
        reading.open(id)
    }
}

/// The open chat's messages, apart from the chats list: filtering the list then doesn't redraw
/// (and re-measure) a long chat beside it.
final class Reading: ObservableObject {
    @Published var id: String?
    @Published var transcript: Transcript?
    @Published var messages: [Transcript.Message] = []
    var loading = false
    /// Each message's markdown, parsed once per theme, by seq.
    var parsed: [Int: MD] = [:]
    var palette = Palette.of(.dark)

    func md(_ m: Transcript.Message, _ p: Palette? = nil) -> MD {
        if let p { palette = p }
        let key = m.seq * 2 + (palette.light ? 1 : 0)
        if let md = parsed[key] { return md }
        let md = MD.parse(m.text, palette)
        parsed[key] = md
        return md
    }

    func open(_ id: String) {
        guard id != self.id else { return }
        self.id = id
        transcript = nil
        messages = []
        parsed = [:]
        loading = false
        more()
    }

    func more() {
        guard let id, !loading else { return }
        if let t = transcript, t.next_offset == nil { return }
        loading = true
        let offset = transcript?.next_offset ?? 0
        API.get("/v1/chats/\(API.q(id))/messages?offset=\(offset)&limit=40&max_chars=6000", Transcript.self) { r in
            guard self.id == id else { return }
            self.loading = false
            if case .success(let t) = r {
                self.transcript = t
                self.messages += t.transcript
            }
        }
    }
}

// MARK: - Search

final class SearchModel: ObservableObject {
    weak var library: Library?
    @Published var text = ""
    @Published var result: SearchResult?
    @Published var understood: [(key: String, value: String)] = []
    @Published var running = false
    @Published var error: String?
    var generation = 0

    /// Reads filters out of what you typed ("in recall", "codex", "since 2w") and sends the rest
    /// as the query, so the tags show what was understood.
    func parse(_ raw: String, projects: [String]) -> (q: String, filters: [(String, String)]) {
        var words = raw.split(separator: " ").map(String.init)
        var filters: [(String, String)] = []
        let agents = ["claude code": "claude", "claude": "claude", "codex": "codex", "cursor": "cursor"]
        if let i = words.firstIndex(where: { $0.lowercased() == "since" }), i + 1 < words.count,
           words[i + 1].range(of: #"^\d+[hdwmy]$"#, options: .regularExpression) != nil {
            filters.append(("since", words[i + 1]))
            words.removeSubrange(i...i + 1)
        }
        if let i = words.firstIndex(where: { $0.lowercased() == "in" }), i + 1 < words.count,
           let p = projects.first(where: { $0.lowercased() == words[i + 1].lowercased() }) {
            filters.append(("project", p))
            words.removeSubrange(i...i + 1)
        }
        let joined = words.joined(separator: " ").lowercased()
        for (name, agent) in agents where joined.contains(name) && !filters.contains(where: { $0.0 == "agent" }) {
            filters.append(("agent", agent))
            if let r = joined.range(of: name) {
                words = String(joined.replacingCharacters(in: r, with: "")).split(separator: " ").map(String.init)
            }
        }
        return (words.joined(separator: " "), filters)
    }

    func run() {
        let raw = text.trimmingCharacters(in: .whitespaces)
        guard !raw.isEmpty, let library else { return }
        library.section = .search
        let (q, filters) = parse(raw, projects: library.facts.projects?.projects.map(\.name) ?? [])
        understood = filters.map { (key: $0.0, value: $0.1) }
        var path = "/v1/search?q=\(API.q(q.isEmpty ? raw : q))&limit=20"
        for (k, v) in filters { path += "&\(k)=\(API.q(v))" }
        generation += 1
        let g = generation
        running = true
        API.get(path, SearchResult.self) { r in
            guard g == self.generation else { return }
            self.running = false
            switch r {
            case .success(let s): self.result = s; self.error = nil
            case .failure(let f): self.error = f.message
            }
        }
    }
}

// MARK: - Formatting

let isoParser: ISO8601DateFormatter = {
    let f = ISO8601DateFormatter()
    f.formatOptions = [.withInternetDateTime]
    return f
}()
let isoFraction: ISO8601DateFormatter = {
    let f = ISO8601DateFormatter()
    f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    return f
}()

func date(_ s: String?) -> Date? {
    guard let s else { return nil }
    return isoParser.date(from: s) ?? isoFraction.date(from: s)
}

func formatter(_ pattern: String) -> DateFormatter {
    let f = DateFormatter()
    f.locale = Locale(identifier: "en_GB")
    f.dateFormat = pattern
    return f
}

let dayFormat = formatter("d MMM"), dayYearFormat = formatter("d MMM yyyy"), longFormat = formatter("EEE d MMM HH:mm"), timeFormat = formatter("HH:mm")

/// "16 Oct", or "16 Oct 2025" outside this year.
func day(_ s: String?) -> String {
    guard let d = date(s) else { return "" }
    let thisYear = Calendar.current.isDate(d, equalTo: Date(), toGranularity: .year)
    return (thisYear ? dayFormat : dayYearFormat).string(from: d)
}

/// "4 Nov 10:25"
func shortStamp(_ s: String?) -> String {
    guard let d = date(s) else { return "" }
    return Calendar.current.isDate(d, equalTo: Date(), toGranularity: .year) ? stampFormat.string(from: d) : dayYearFormat.string(from: d)
}
let stampFormat = formatter("d MMM HH:mm")
/// "11:58" today, "3 Nov" before.
func dayOrTime(_ s: String?) -> String {
    guard let d = date(s) else { return "" }
    return Calendar.current.isDateInToday(d) ? timeFormat.string(from: d) : day(s)
}

func longDay(_ s: String?) -> String { date(s).map { longFormat.string(from: $0) } ?? "" }
func clock(_ d: Date?) -> String { d.map { timeFormat.string(from: $0) } ?? "" }

let countFormat: NumberFormatter = {
    let f = NumberFormatter()
    f.numberStyle = .decimal
    f.locale = Locale(identifier: "en_GB")
    return f
}()

func count(_ n: Int?) -> String { n.map { countFormat.string(from: NSNumber(value: $0)) ?? "\($0)" } ?? "" }

func size(_ bytes: Int?) -> String {
    guard let bytes else { return "" }
    let b = Double(bytes)
    if b < 1000 { return "\(bytes) B" }
    if b < 1e6 { return String(format: "%.0f KB", b / 1e3) }
    if b < 1e9 { return String(format: b < 1e7 ? "%.1f MB" : "%.0f MB", b / 1e6) }
    return String(format: "%.1f GB", b / 1e9)
}

func duration(_ seconds: Double) -> String {
    seconds < 90 ? "\(Int(seconds.rounded())) s" : String(format: "%.1f min", seconds / 60)
}
