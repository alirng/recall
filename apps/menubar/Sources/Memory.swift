// Memory (board A): the notes list with type filters and sort, and the note reader with its rules,
// the chats it was learned from, related notes and its history in the notes repo.

import AppKit
import SwiftUI

struct MemoryView: View {
    let library: Library
    @ObservedObject var notes: NotesModel
    @Environment(\.colorScheme) var scheme

    var body: some View {
        let p = Palette.of(scheme)
        HStack(spacing: 0) {
            NotesList(library: library, notes: notes)
                .frame(minWidth: 300, idealWidth: 360, maxWidth: 420)
            Rectangle().fill(p.grid).frame(width: 1)
            Group {
                if let n = notes.note {
                    Reader(library: library, notes: notes, note: n)
                } else if let e = notes.error {
                    Message(title: "Couldn't load notes", text: e)
                } else {
                    Color.clear
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
    }
}

struct NotesList: View {
    let library: Library
    @ObservedObject var notes: NotesModel
    @Environment(\.colorScheme) var scheme
    static let types = [("decision", "Decisions"), ("fact", "Facts"), ("lesson", "Lessons"), ("preference", "Preferences"), ("open", "Open")]
    static let sorts = [("last_seen", "Last seen"), ("updated", "Updated"), ("created", "Created"), ("title", "Title")]

    var body: some View {
        let p = Palette.of(scheme)
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 10) {
                ProjectButton(library: library, entries: { notes.projectCounts }, total: notes.allTotal, current: notes.project,
                              pick: { notes.set(project: $0) }) {
                    Label(text: "Notes · " + (notes.project ?? "all projects") + " ▾")
                }
                .fixedSize()
                Spacer()
                Text(count(notes.total)).font(PaperMono.font(11.5)).foregroundStyle(p.muted)
                MenuButton(popups: library.popups, width: 200, menu: {
                    MenuModel(title: "Sort notes", rows: Self.sorts.map { s in
                        MenuRow(title: s.1, checked: notes.sort == s.0, action: { notes.set(sort: s.0) })
                    })
                }) {
                    Text((Self.sorts.first { $0.0 == notes.sort }?.1 ?? "Sort") + " ▾").font(PaperMono.font(11.5)).foregroundStyle(p.muted)
                }
                .fixedSize()
            }
            .padding(.horizontal, 16).padding(.top, 14).padding(.bottom, 10)
            FlowTags(spacing: 6) {
                TagButton(text: "all", on: notes.type == nil) { notes.set(type: nil) }
                ForEach(Self.types.filter { notes.types[$0.0] != nil }, id: \.0) { t in
                    Button { notes.set(type: notes.type == t.0 ? nil : t.0) } label: { KindTag(type: t.0, on: notes.type == t.0) }
                        .buttonStyle(.plain)
                }
            }
            .padding(.horizontal, 16).padding(.bottom, 12)
            Rule()
            Rows(notes.rows, cursor: notes.cursor, more: notes.more) { n in
                NoteItem(note: n, on: notes.selected == n.id, lastRun: notes.lastRun, showProject: notes.project == nil, editing: notes.editor?.noteId == n.id)
                    .onTapGesture { notes.select(n.id) }
            }
        }
        .background(p.paper)
    }
}

/// Tags that wrap onto more lines when the column is narrow.
struct FlowTags<C: View>: View {
    var spacing: CGFloat = 6
    @ViewBuilder var content: () -> C
    var body: some View { Flow(spacing: spacing) { content() } }
}

struct Flow: Layout {
    var spacing: CGFloat

    func rows(_ width: CGFloat, _ subviews: Subviews) -> [(CGFloat, [(Int, CGSize)])] {
        var out: [(CGFloat, [(Int, CGSize)])] = [(0, [])]
        var x: CGFloat = 0
        for (i, v) in subviews.enumerated() {
            let s = v.sizeThatFits(.unspecified)
            if x > 0, x + s.width > width { out.append((0, [])); x = 0 }
            out[out.count - 1].1.append((i, s))
            out[out.count - 1].0 = max(out[out.count - 1].0, s.height)
            x += s.width + spacing
        }
        return out
    }

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let width = proposal.width ?? .infinity
        let r = rows(width, subviews)
        let h = r.map(\.0).reduce(0, +) + spacing * CGFloat(max(0, r.count - 1))
        return CGSize(width: proposal.width ?? r.map { $0.1.map(\.1.width).reduce(0, +) }.max() ?? 0, height: h)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        var y = bounds.minY
        for (h, row) in rows(bounds.width, subviews) {
            var x = bounds.minX
            for (i, s) in row {
                subviews[i].place(at: CGPoint(x: x, y: y), proposal: ProposedViewSize(s))
                x += s.width + spacing
            }
            y += h + spacing
        }
    }
}

struct NoteItem: View {
    let note: NoteRow
    let on: Bool
    let lastRun: String?
    var showProject = true
    var editing = false
    @Environment(\.colorScheme) var scheme

    var body: some View {
        let p = Palette.of(scheme)
        let fresh = lastRun.map { run in day(note.created) == day(run) } ?? false
        VStack(alignment: .leading, spacing: 5) {
            Text(note.title).font(PaperMono.font(13, 500)).foregroundStyle(p.ink).lineLimit(2)
            HStack(spacing: 8) {
                KindTag(type: note.type)
                Text(meta).lineLimit(1)
                if editing { Text("· editing").foregroundStyle(p.ink).lineLimit(1) }
                if fresh { Text("· new last night").foregroundStyle(p.accentText).lineLimit(1) }
            }
            .font(PaperMono.font(11.5)).foregroundStyle(p.muted)
        }
        .padding(.horizontal, 16).padding(.top, 11).padding(.bottom, 12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(on ? p.sel : .clear)
        .overlay(alignment: .bottom) { Rectangle().fill(p.grid).frame(height: 1) }
        .contentShape(Rectangle())
    }

    var meta: String {
        var parts: [String] = []
        if showProject, let project = note.project { parts.append(project) }
        if let n = note.sources, n > 0 { parts.append(n == 1 ? "1 chat" : "\(n) chats") }
        parts.append(day(note.last_seen ?? note.updated))
        return parts.filter { !$0.isEmpty }.joined(separator: " · ")
    }
}

// MARK: - Reader

struct Reader: View {
    let library: Library
    @ObservedObject var notes: NotesModel
    let note: Note
    @Environment(\.colorScheme) var scheme

    var body: some View {
        let p = Palette.of(scheme)
        ScrollView(.vertical, showsIndicators: false) {
            VStack(alignment: .leading, spacing: 22) {
                header(p)
                if let e = editing {
                    EditView(editor: e, library: library)
                } else {
                    rules(p)
                    HStack(alignment: .top, spacing: 28) {
                        learnedFrom(p).frame(maxWidth: .infinity, alignment: .leading)
                        related(p).frame(maxWidth: .infinity, alignment: .leading)
                    }
                    history(p)
                    actions(p)
                }
            }
            .padding(.horizontal, 32).padding(.top, 26).padding(.bottom, 28)
            .frame(maxWidth: 860, alignment: .leading)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .id(note.note_id)
        .background(p.paper)
    }

    /// The edit open on this note, if there is one.
    var editing: Editor? { notes.editor?.noteId == note.note_id ? notes.editor : nil }

    func header(_ p: Palette) -> some View {
        HStack(alignment: .top, spacing: 16) {
            VStack(alignment: .leading, spacing: 0) {
                HStack(alignment: .center, spacing: 8) {
                    KindTag(type: note.type)
                    Label(text: [note.project, note.status].compactMap { $0 }.joined(separator: " · "))
                }
                Text(note.title).font(PaperMono.font(24, 520)).foregroundStyle(p.ink)
                    .padding(.top, 8).padding(.bottom, 6).fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
                Text(meta).font(PaperMono.font(12)).foregroundStyle(p.muted)
            }
            Spacer(minLength: 8)
            if editing != nil {
                Label(text: "Editing", color: p.ink)
            } else if let c = confirmed {
                HStack(spacing: 6) {
                    Text("✓").foregroundStyle(p.accentText)
                    Text(day(c.date)).foregroundStyle(p.ink)
                }
                .font(PaperMono.font(12))
                .help(confirmedTip(c))
            }
        }
    }

    /// The last commit that confirmed this note, from its history.
    var confirmed: Note.Commit? {
        (note.history ?? []).first { ($0.changes ?? []).contains { $0.kind == "confirmed" } }
    }

    func confirmedTip(_ c: Note.Commit) -> String {
        let when = date(c.date).map { stampFormat.string(from: $0) } ?? day(c.date)
        let lastNight = library.facts.runs?.latest.map { day($0.started) == day(c.date) } ?? false
        return lastNight ? "Confirmed by last night's memory run, \(when)" : "Confirmed by the memory run on \(when)"
    }

    var meta: String {
        var parts: [String] = []
        if note.created != nil { parts.append("Created " + day(note.created)) }
        if note.last_seen != nil { parts.append("last seen " + day(note.last_seen)) }
        if let c = note.confidence { parts.append("confidence " + c) }
        let n = note.source_chats?.count ?? 0
        if n > 0 { parts.append(n == 1 ? "from 1 chat" : "from \(n) chats") }
        return parts.joined(separator: " · ")
    }

    @ViewBuilder
    func rules(_ p: Palette) -> some View {
        let all = note.rules_all ?? []
        let active = all.filter { ($0.status ?? "active") == "active" }
        let gone = all.filter { ($0.status ?? "active") != "active" }
        VStack(alignment: .leading, spacing: 9) {
            if all.isEmpty {
                ForEach(Array(bullets(note.body ?? "").enumerated()), id: \.offset) { _, line in bullet(line, p, p.body) }
            }
            ForEach(active, id: \.id) { r in bullet(r.text, p, p.body) }
            if !gone.isEmpty {
                Label(text: "Retired").padding(.top, 8)
                ForEach(gone, id: \.id) { r in bullet(r.text + (r.reason.map { " (\($0))" } ?? ""), p, p.faint) }
            }
        }
        .frame(maxWidth: 640, alignment: .leading)
    }

    func bullets(_ body: String) -> [String] {
        body.split(separator: "\n").map { String($0).trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }
            .map { $0.hasPrefix("- ") ? String($0.dropFirst(2)) : $0 }
    }

    func bullet(_ text: String, _ p: Palette, _ color: Color) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 10) {
            Text("•").foregroundStyle(p.faint)
            Text(inline(text)).foregroundStyle(color).fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
        }
        .font(PaperMono.font(14, 390, prose: true))
        .lineSpacing(5)
    }

    func inline(_ s: String) -> AttributedString {
        (try? AttributedString(markdown: s, options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace))) ?? AttributedString(s)
    }

    func learnedFrom(_ p: Palette) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            Label(text: "Learned from").padding(.bottom, 4)
            ForEach(note.source_chats ?? [], id: \.chat_id) { s in
                let found = s.found ?? true
                Button { if found { library.openChat(s.chat_id) } } label: {
                    RowLine {
                        HStack(spacing: 10) {
                            DiamondMark(color: found ? p.agent(s.agent ?? "") : p.faint, dashed: !found)
                            Text(found ? (s.title ?? s.chat_id) : "A chat that's gone").foregroundStyle(found ? p.ink : p.faint).lineLimit(1)
                        }
                    } right: {
                        Text(day(s.date)).foregroundStyle(p.muted).lineLimit(1).fixedSize()
                    }
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain).disabled(!found)
            }
            Rule()
        }
        .font(PaperMono.font(12.5))
    }

    func related(_ p: Palette) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            Label(text: "Related").padding(.bottom, 4)
            if (note.related ?? []).isEmpty {
                Text("None yet").foregroundStyle(p.muted).padding(.vertical, 8)
            }
            ForEach((note.related ?? []).prefix(5), id: \.note_id) { r in
                Button { library.openNote(r.note_id) } label: {
                    VStack(alignment: .leading, spacing: 5) {
                        Rule()
                        Text(r.title).foregroundStyle(p.ink).lineLimit(2).padding(.top, 4)
                        HStack(spacing: 8) {
                            if let t = r.type { KindTag(type: t) }
                            Text(r.reason ?? "").font(PaperMono.font(11.5)).foregroundStyle(p.muted).lineLimit(1)
                        }
                        .padding(.bottom, 8)
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
            }
            Rule()
        }
        .font(PaperMono.font(12.5))
    }

    func history(_ p: Palette) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            Label(text: "History").padding(.bottom, 4)
            ForEach(Array((note.history ?? []).prefix(12).enumerated()), id: \.offset) { _, c in
                ForEach(Array(lines(c).enumerated()), id: \.offset) { _, line in
                    VStack(spacing: 0) {
                        Rule()
                        HStack(alignment: .firstTextBaseline, spacing: 10) {
                            Text(day(c.date)).foregroundStyle(p.muted).frame(width: 60, alignment: .leading)
                            Text(line.0).foregroundStyle(p.ink).fixedSize()
                            Text(line.1).foregroundStyle(p.muted).lineLimit(1)
                            Spacer(minLength: 0)
                        }
                        .padding(.vertical, 8)
                    }
                }
            }
            Rule()
        }
        .font(PaperMono.font(12.5))
    }

    /// One line per kind of change in a commit, e.g. "Rule added · the menu bar is a separate Swift app".
    /// An edit made by hand is one line: "Edited by you · 1 reworded, 1 removed, 1 new".
    func lines(_ c: Note.Commit) -> [(String, String)] {
        let names = ["created": "Created", "added": "Rule added", "refined": "Refined", "retired": "Retired", "confirmed": "Confirmed", "relinked": "Relinked"]
        let changes = c.changes ?? []
        if c.subject?.hasPrefix("edited by hand:") == true {
            let words = [("refined", "reworded"), ("retired", "removed"), ("added", "new")].compactMap { k, w -> String? in
                let n = changes.filter { $0.kind == k }.count
                return n > 0 ? "\(n) \(w)" : nil
            }
            return [("Edited by you", [words.joined(separator: ", "), String(c.commit.prefix(7))].filter { !$0.isEmpty }.joined(separator: " · "))]
        }
        if c.subject?.hasPrefix("restored:") == true { return [("Put back", "after Forget · " + c.commit.prefix(7))] }
        if changes.isEmpty { return [("Changed", c.commit.prefix(7) + (c.subject.map { " · " + $0 } ?? ""))] }
        var order: [String] = []
        var texts: [String: [String]] = [:]
        for ch in changes {
            if texts[ch.kind] == nil { order.append(ch.kind) }
            texts[ch.kind, default: []] += ch.text.map { [$0] } ?? []
        }
        return order.map { kind in
            let t = texts[kind] ?? []
            let what = kind == "relinked" ? "related notes updated" : t.count > 1 ? "\(t.count) rules" : t.first ?? ""
            return (names[kind] ?? kind.capitalized, [what, String(c.commit.prefix(7))].filter { !$0.isEmpty }.joined(separator: " · "))
        }
    }

    func actions(_ p: Palette) -> some View {
        let writes = library.canWrite("notes")
        let can = notes.editable(note)
        let why = !writes ? updateToWrite : "Only an active note can be changed here"
        return VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                WindowButton(title: "Edit note", soon: !can, keys: "⌘E", why: why) { notes.edit() }
                WindowButton(title: "Copy as markdown") { notes.copyMarkdown() }
                if let path = note.path {
                    WindowButton(title: "Show file") {
                        let full = (path as NSString).expandingTildeInPath
                        NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: full)])
                    }
                }
                Spacer()
                WindowButton(title: "Forget…", soon: !can, why: why) { library.askForget() }
            }
            if library.info != nil, !writes {
                Text(updateToWrite).font(PaperMono.font(11.5)).foregroundStyle(p.muted)
            }
        }
        .padding(.top, 6)
    }
}
