// The ⌘K palette (board F3) and find in chat (board F4).

import AppKit
import SwiftUI

// MARK: - Palette

struct PaletteItem: Identifiable {
    enum Group: Int, CaseIterable { case notes, chats, goto, actions
        var title: String { ["Notes", "Chats", "Go to", "Actions"][rawValue] }
    }
    let id: String
    let group: Group
    let title: String
    var detail: String? = nil
    var kind: String? = nil  // a note's category
    var agent: String? = nil  // a chat's agent
    var keys: String? = nil  // an action's own shortcut
    var number: Int? = nil  // Go to rows: press it while the field is empty
    let run: () -> Void
}

/// What the palette shows. Typing searches notes and chats ("in recall", "codex" and "since 2w"
/// become filters), and filters the views and actions. Opened empty it shows recent notes and chats
/// and every view.
final class PaletteModel: ObservableObject {
    weak var library: Library?
    @Published var query = "" { didSet { if query != oldValue { changed() } } }
    @Published var result: SearchResult?
    @Published var understood: [(key: String, value: String)] = []
    @Published var at = 0
    @Published var narrow: PaletteItem.Group?
    var pending: DispatchWorkItem?
    var generation = 0

    func open() {
        guard let library, let w = library.popups.parent else { return }
        query = ""
        result = nil
        understood = []
        at = 0
        narrow = nil
        library.popups.show(PaletteView(model: self).environment(\.colorScheme, w.effectiveAppearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua ? .dark : .light),
                            frame: w.frame, keys: { [weak self] e in self?.key(e) ?? false })
    }

    func close() { library?.popups.close() }

    /// A search a moment after typing stops, so each keystroke doesn't send one.
    func changed() {
        at = 0
        pending?.cancel()
        let q = query.trimmingCharacters(in: .whitespaces)
        guard !q.isEmpty, let library else { result = nil; understood = []; return }
        let (text, filters) = library.search.parse(q, projects: library.facts.projects?.projects.map(\.name) ?? [])
        understood = filters.map { (key: $0.0, value: $0.1) }
        let work = DispatchWorkItem { [weak self] in
            guard let self else { return }
            var path = "/v1/search?q=\(API.q(text.isEmpty ? q : text))&limit=5"
            for (k, v) in filters { path += "&\(k)=\(API.q(v))" }
            self.generation += 1
            let g = self.generation
            API.get(path, SearchResult.self) { r in
                guard g == self.generation, case .success(let s) = r else { return }
                self.result = s
            }
        }
        pending = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.18, execute: work)
    }

    var items: [PaletteItem] {
        guard let library else { return [] }
        let q = query.trimmingCharacters(in: .whitespaces).lowercased()
        var out: [PaletteItem] = []
        if q.isEmpty {
            for n in library.notes.rows.prefix(3) {
                out.append(PaletteItem(id: "n:" + n.id, group: .notes, title: n.title, kind: n.type) { library.openNote(n.note_id) })
            }
            for c in library.chats.rows.prefix(3) {
                out.append(PaletteItem(id: "c:" + c.id, group: .chats, title: c.title ?? "Untitled chat", detail: day(c.updated), agent: c.agent) { library.openChat(c.chat_id) })
            }
        } else if let r = result {
            for n in r.notes ?? [] {
                out.append(PaletteItem(id: "n:" + n.note_id, group: .notes, title: n.title, kind: n.type) { library.openNote(n.note_id) })
            }
            for c in r.chats ?? [] {
                out.append(PaletteItem(id: "c:" + c.chat_id, group: .chats, title: c.title ?? "Untitled chat", detail: day(c.updated), agent: c.agent) { library.openChat(c.chat_id) })
            }
        }
        var number = 0
        for (title, place, panel) in Self.views(library) where q.isEmpty || title.lowercased().contains(q) {
            number += 1
            out.append(PaletteItem(id: "g:" + title, group: .goto, title: title, number: number) {
                library.go(place)
                if let panel {
                    library.thisMacFocus.id = panel
                    library.thisMacFocus.tick += 1
                }
            })
        }
        if !q.isEmpty {
            out.append(PaletteItem(id: "g:all", group: .goto, title: "All results for “\(query)”") {
                library.search.text = self.query
                library.search.run()
            })
        }
        let dark = library.popups.parent?.effectiveAppearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua
        let actions = [
            PaletteItem(id: "a:copy", group: .actions, title: "Copy note as markdown", keys: "⇧⌘C") { library.notes.copyMarkdown() },
            PaletteItem(id: "a:find", group: .actions, title: "Find in chat", keys: "⌘F") { library.find.show() },
            PaletteItem(id: "a:look", group: .actions, title: "Switch to \(dark ? "light" : "dark") appearance") { library.toggleAppearance() },
        ]
        out += actions.filter { q.isEmpty || $0.title.lowercased().contains(q) }
        if let narrow { out = out.filter { $0.group == narrow } }
        return out
    }

    /// Every view and every This Mac panel.
    static func views(_ library: Library) -> [(String, Place, String?)] {
        var v: [(String, Place, String?)] = [("Memory", .memory, nil), ("Chats", .chats, nil), ("Projects", .projects, nil), ("Now", .now, nil), ("This Mac", .thisMac, nil)]
        for panel in ThisMacView.panels { v.append(("This Mac · " + panel, .thisMac, panel)) }
        for a in library.facts.agents { v.append((agentName(a.name), .agent(a.name), nil)) }
        return v
    }

    func run(_ item: PaletteItem, keep: Bool) {
        if !keep { close() }
        item.run()
    }

    func key(_ e: NSEvent) -> Bool {
        let all = items
        let cmd = e.modifierFlags.contains(.command)
        switch e.keyCode {
        case 125: at = min(at + 1, max(0, all.count - 1)); return true
        case 126: at = max(at - 1, 0); return true
        case 36, 76:
            if all.indices.contains(at) { run(all[at], keep: cmd) }
            return true
        case 48:  // tab: narrow to the next group that has rows, then back to all
            let groups = PaletteItem.Group.allCases.filter { g in narrowed(nil).contains { $0.group == g } }
            if let n = narrow, let i = groups.firstIndex(of: n) { narrow = i + 1 < groups.count ? groups[i + 1] : nil } else { narrow = groups.first }
            at = 0
            return true
        default:
            // Go to numbers work only while nothing is typed; after that a digit is text.
            if query.isEmpty, !cmd, let c = e.charactersIgnoringModifiers, let n = Int(c), let hit = all.first(where: { $0.number == n }) {
                run(hit, keep: false)
                return true
            }
            return false
        }
    }

    func narrowed(_ g: PaletteItem.Group?) -> [PaletteItem] {
        let keep = narrow
        narrow = g
        defer { narrow = keep }
        return items
    }
}

struct PaletteView: View {
    @ObservedObject var model: PaletteModel
    @FocusState var focused: Bool
    @Environment(\.colorScheme) var scheme

    var body: some View {
        let p = Palette.of(scheme)
        let items = model.items
        ZStack(alignment: .top) {
            p.scrim.contentShape(Rectangle()).onTapGesture { model.close() }
            VStack(spacing: 0) {
                HStack(spacing: 10) {
                    Text("⌕").foregroundStyle(p.muted)
                    TextField("", text: $model.query, prompt: Text("Search, or go anywhere").foregroundColor(p.faint))
                        .textFieldStyle(.plain).font(PaperMono.font(14)).foregroundStyle(p.ink).focused($focused)
                    ForEach(model.understood, id: \.key) { f in Tag(text: label(f), agent: f.key == "agent" ? f.value : nil) }
                    if let n = model.narrow { Tag(text: n.title.lowercased(), on: true) }
                    Kbd(text: "esc")
                }
                .padding(.horizontal, 16).frame(height: 52)
                Rule()
                ScrollViewReader { proxy in
                    ScrollView(.vertical, showsIndicators: false) {
                        VStack(alignment: .leading, spacing: 0) {
                            ForEach(PaletteItem.Group.allCases, id: \.self) { g in
                                let rows = items.enumerated().filter { $0.element.group == g }
                                if !rows.isEmpty {
                                    Label(text: g.title).padding(.horizontal, 16).padding(.top, 14).padding(.bottom, 6)
                                    ForEach(rows, id: \.element.id) { i, item in
                                        row(item, on: i == model.at, p).id(i)
                                            .onTapGesture { model.run(item, keep: false) }
                                            .onHover { if $0 { model.at = i } }
                                    }
                                }
                            }
                            if items.isEmpty {
                                Text(model.result == nil ? "Searching…" : "Nothing found").foregroundStyle(p.muted).padding(16)
                            }
                        }
                        .padding(.bottom, 8)
                    }
                    .onChange(of: model.at) { _, at in proxy.scrollTo(at) }
                }
                .frame(maxHeight: 520)
                Rule()
                HStack(spacing: 8) {
                    Kbd(text: "↑↓"); Text("move")
                    Kbd(text: "↵"); Text("open")
                    Kbd(text: "⌘↵"); Text("open and keep palette")
                    Spacer()
                    Kbd(text: "tab"); Text("narrow to a group")
                }
                .font(PaperMono.font(11)).foregroundStyle(p.muted)
                .padding(.horizontal, 16).frame(height: 38)
            }
            .frame(width: 660)
            .fixedSize(horizontal: false, vertical: true)
            .background(p.pop)
            .overlay(Rectangle().strokeBorder(p.line, lineWidth: 1))
            .padding(.top, 92)
        }
        .font(PaperMono.font(12.5))
        .onAppear { focused = true }
    }

    func row(_ item: PaletteItem, on: Bool, _ p: Palette) -> some View {
        HStack(spacing: 12) {
            Group {
                switch item.group {
                case .notes: Rectangle().strokeBorder(p.accent, lineWidth: 1).frame(width: 8, height: 8)
                case .chats: DiamondMark(color: p.agent(item.agent ?? ""))
                case .goto: Text("→").foregroundStyle(p.muted)
                case .actions: Text("⌘").foregroundStyle(p.muted)
                }
            }
            .frame(width: 12)
            Text(item.title).foregroundStyle(p.ink).lineLimit(1)
            if let k = item.kind { KindTag(type: k) }
            if let d = item.detail { Text(d).font(PaperMono.font(11.5)).foregroundStyle(p.muted).lineLimit(1) }
            Spacer(minLength: 8)
            // One trailing column: a row's cap, or ↵ on the highlighted row that has none.
            let n = item.number.flatMap { $0 <= 9 && model.query.isEmpty ? $0 : nil }
            if let n { Kbd(text: "\(n)") }
            if let k = item.keys { Kbd(text: k) }
            if on, n == nil, item.keys == nil { Text("↵").foregroundStyle(p.muted) }
        }
        .padding(.horizontal, 16).frame(height: 32)
        .background(on ? p.sel : .clear)
        .contentShape(Rectangle())
    }

    func label(_ f: (key: String, value: String)) -> String {
        switch f.key {
        case "project": return "in " + f.value
        case "agent": return agentName(f.value).lowercased()
        default: return "since " + f.value
        }
    }
}

// MARK: - Find in chat

/// The find box over a chat: always there, tucked into a ⌕ with "–", back with ⌘F. What you typed
/// stays when you switch chats, and the next chat opens on its first match.
final class FindModel: ObservableObject {
    weak var library: Library?
    @Published var query = "" { didSet { if query != oldValue { current = 0; jump() } } }
    @Published var shown = true
    @Published var current = 0
    @Published var hits: [(seq: Int, nth: Int)] = []
    let focus = ObservableTick()
    let scroll = Jump()
    var focused = false

    func show() {
        if let l = library, !(l.section == .chats || l.isAgent) { l.go(.chats) }
        shown = true
        focus.fire()
    }

    /// Finds every match in the rendered words of the messages loaded so far.
    func recount(_ reading: Reading) {
        let q = query
        guard !q.isEmpty else { if !hits.isEmpty { hits = [] }; return }
        var out: [(seq: Int, nth: Int)] = []
        for m in reading.messages {
            let n = reading.md(m).plain.reduce(0) { $0 + FindModel.count(q, in: $1) }
            for i in 0..<n { out.append((m.seq, i)) }
        }
        hits = out
        if current >= hits.count { current = 0 }
    }

    static func count(_ q: String, in s: String) -> Int {
        var from = s.startIndex, n = 0
        while let r = s.range(of: q, options: .caseInsensitive, range: from..<s.endIndex) { n += 1; from = r.upperBound }
        return n
    }

    func step(_ d: Int) {
        guard !hits.isEmpty else { return }
        current = (current + d + hits.count) % hits.count
        jump()
    }

    func jump() {
        guard hits.indices.contains(current) else { return }
        scroll.id = hits[current].seq
        scroll.tick += 1
    }

    /// A rendered block with matches underlined and the current one inverted. `before` is how
    /// many matches the message's earlier blocks hold.
    func marked(_ text: AttributedString, plain: String, seq: Int, before: Int, _ p: Palette) -> AttributedString {
        guard !query.isEmpty, plain.range(of: query, options: .caseInsensitive) != nil else { return text }
        var out = text
        let cur = hits.indices.contains(current) ? hits[current] : nil
        let chars = out.characters
        var from = plain.startIndex, n = before
        while let r = plain.range(of: query, options: .caseInsensitive, range: from..<plain.endIndex) {
            let lo = chars.index(chars.startIndex, offsetBy: plain.distance(from: plain.startIndex, to: r.lowerBound))
            let hi = chars.index(lo, offsetBy: plain.distance(from: r.lowerBound, to: r.upperBound))
            if let cur, cur.seq == seq, cur.nth == n {
                out[lo..<hi].backgroundColor = p.ink
                out[lo..<hi].foregroundColor = p.paper
            } else {
                out[lo..<hi].underlineStyle = .single
                out[lo..<hi].foregroundColor = p.ink
            }
            n += 1
            from = r.upperBound
        }
        return out
    }
}

/// A row to scroll to, of any id type, and a tick that asks for it.
final class Jump: ObservableObject {
    var id: AnyHashable?
    @Published var tick = 0
}

struct JumpScroll: View {
    @ObservedObject var jump: Jump
    let proxy: ScrollViewProxy
    var body: some View {
        Color.clear.onChange(of: jump.tick) { _, _ in
            // On the next turn: the row may have arrived in this same update, and the list
            // doesn't know it yet.
            // Unwrapped: the list matches ids by their own type, and an AnyHashable never matches.
            guard let id = jump.id else { return }
            DispatchQueue.main.async {
                withAnimation(nil) {
                    if let n = id.base as? Int { proxy.scrollTo(n, anchor: .center) } else if let s = id.base as? String { proxy.scrollTo(s, anchor: .center) } else { proxy.scrollTo(id, anchor: .center) }
                }
            }
        }
    }
}

struct FindBox: View {
    @ObservedObject var find: FindModel
    @ObservedObject var focusTick: ObservableTick
    @FocusState var focused: Bool
    @Environment(\.colorScheme) var scheme

    init(find: FindModel) {
        self.find = find
        self.focusTick = find.focus
    }

    var body: some View {
        let p = Palette.of(scheme)
        Group {
            if find.shown {
                HStack(spacing: 8) {
                    Text("⌕").foregroundStyle(p.muted)
                    TextField("", text: $find.query, prompt: Text("Find in chat").foregroundColor(p.faint))
                        .textFieldStyle(.plain).foregroundStyle(p.ink).focused($focused).frame(width: 140)
                    Text(find.query.isEmpty ? "" : find.hits.isEmpty ? "none" : "\(find.current + 1) of \(find.hits.count)")
                        .font(PaperMono.font(11.5)).foregroundStyle(p.muted).monospacedDigit().fixedSize()
                    box("↑") { find.step(-1) }
                    box("↓") { find.step(1) }
                    box("–") { find.shown = false }.help("Tuck away (⌘F brings it back)")
                }
                .padding(.leading, 10).padding(.trailing, 6).frame(height: 30)
                .background(p.pop)
                .overlay(Rectangle().strokeBorder(p.line, lineWidth: 1))
            } else {
                box("⌕") { find.show() }.help("Find in chat (⌘F)")
            }
        }
        .font(PaperMono.font(12.5))
        .onReceive(focusTick.$n.dropFirst()) { _ in DispatchQueue.main.async { focused = true } }
        .onChange(of: focused) { _, f in find.focused = f }
    }

    func box(_ s: String, _ action: @escaping () -> Void) -> some View {
        let p = Palette.of(scheme)
        return Button(action: action) {
            Text(s).foregroundStyle(p.ink).frame(width: 20, height: 20)
                .overlay(Rectangle().strokeBorder(p.grid, lineWidth: 1))
                .background(p.pop)
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }
}
