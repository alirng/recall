// The plain lists: Chats (with a chat's messages), Search results, Projects, and Now.

import AppKit
import SwiftUI

// MARK: - Chats

struct ChatsView: View {
    let library: Library
    @EnvironmentObject var facts: Facts
    @ObservedObject var chats: ChatsModel
    @Environment(\.colorScheme) var scheme

    var body: some View {
        let p = Palette.of(scheme)
        HStack(spacing: 0) {
            VStack(alignment: .leading, spacing: 0) {
                HStack(spacing: 10) {
                    ProjectButton(library: library, entries: { (facts.projects?.projects ?? []).map { ($0.name, $0.chats) } },
                                  total: chats.agent.map { facts.agentTotals[$0] } ?? facts.chatsCount, current: chats.project,
                                  pick: { chats.set(project: $0) }) {
                        Label(text: (chats.agent.map(agentName) ?? "Chats") + " · " + (chats.project ?? "all projects") + " ▾")
                    }
                    .fixedSize()
                    Spacer()
                    Text(count(chats.total)).font(PaperMono.font(11.5)).foregroundStyle(p.muted)
                }
                .padding(.horizontal, 16).padding(.top, 14).padding(.bottom, 12)
                Rule()
                if let e = chats.error { Text(e).foregroundStyle(p.muted).padding(16) }
                Rows(chats.rows, cursor: chats.cursor, more: chats.more) { c in
                    ChatItem(chat: c, on: chats.selected == c.id).onTapGesture { chats.select(c.id) }
                }
            }
            .frame(minWidth: 300, idealWidth: 380, maxWidth: 440)
            Rectangle().fill(p.grid).frame(width: 1)
            ChatReader(reading: chats.reading, find: library.find).frame(maxWidth: .infinity, maxHeight: .infinity)
        }
        .background(p.paper)
    }
}

struct ChatItem: View {
    let chat: ChatRow
    let on: Bool
    @Environment(\.colorScheme) var scheme
    var body: some View {
        let p = Palette.of(scheme)
        VStack(alignment: .leading, spacing: 5) {
            Text(chat.title ?? "Untitled chat").font(PaperMono.font(13, 500)).foregroundStyle(p.ink).lineLimit(2)
            HStack(spacing: 8) {
                DiamondMark(color: p.agent(chat.agent))
                Text([chat.project ?? "", chat.messages.map { "\($0) messages" } ?? "", dayOrTime(chat.updated)].filter { !$0.isEmpty }.joined(separator: " · "))
                    .font(PaperMono.font(11.5)).foregroundStyle(p.muted).lineLimit(1)
            }
        }
        .padding(.horizontal, 16).padding(.top, 11).padding(.bottom, 12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(on ? p.sel : .clear)
        .overlay(alignment: .bottom) { Rectangle().fill(p.grid).frame(height: 1) }
        .contentShape(Rectangle())
    }
}

struct ChatReader: View {
    @ObservedObject var reading: Reading
    @ObservedObject var find: FindModel
    @Environment(\.colorScheme) var scheme

    var body: some View {
        let p = Palette.of(scheme)
        ZStack(alignment: .topTrailing) {
            if let t = reading.transcript {
                VStack(alignment: .leading, spacing: 0) {
                    VStack(alignment: .leading, spacing: 6) {
                        Label(text: [agentName(t.agent), t.project ?? "", t.branch ?? ""].filter { !$0.isEmpty }.joined(separator: " · "))
                        Text(t.title ?? "Untitled chat").font(PaperMono.font(20, 520)).foregroundStyle(p.ink).textSelection(.enabled)
                        Text("\(count(t.messages)) messages" + (t.folder.map { " · " + $0.replacingOccurrences(of: API.home, with: "~") } ?? ""))
                            .font(PaperMono.font(12)).foregroundStyle(p.muted).lineLimit(1).truncationMode(.middle)
                    }
                    .padding(.horizontal, 32).padding(.top, 24).padding(.bottom, 14)
                    .padding(.trailing, find.shown ? 300 : 0)
                    Rule()
                    Rows(reading.messages, jump: find.scroll, more: reading.more) { m in
                        VStack(alignment: .leading, spacing: 6) {
                            HStack(spacing: 8) {
                                if m.role == "user" {
                                    Rectangle().fill(p.ink).frame(width: 6, height: 6)
                                    Label(text: "You", color: p.ink)
                                } else {
                                    DiamondMark(color: p.agent(t.agent)).frame(width: 7, height: 7)
                                    Label(text: agentName(t.agent), color: p.agent(t.agent))
                                }
                                Spacer()
                                Text(dayOrTime(m.time)).font(PaperMono.font(11)).foregroundStyle(p.muted)
                            }
                            MDView(md: reading.md(m, p), seq: m.seq, find: find, p: p)
                        }
                        .padding(.horizontal, 32).padding(.vertical, 12)
                        .frame(maxWidth: 820, alignment: .leading)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .overlay(alignment: .bottom) { Rectangle().fill(p.grid).frame(height: 1) }
                    }
                }
            } else if reading.id != nil {
                Message(title: "Reading the chat…", text: "")
            } else {
                Message(title: "Pick a chat", text: "Its messages show here.")
            }
            FindBox(find: find).padding(.top, 18).padding(.trailing, 20)
        }
        // A chat's matches: counted as its pages load (all of them while a query is set), and the
        // first one shown when it opens.
        .onChange(of: reading.messages.count) { _, _ in
            find.recount(reading)
            if !find.query.isEmpty { reading.more() }
        }
        .onChange(of: reading.id) { _, _ in find.current = 0 }
        .onChange(of: find.query) { _, _ in
            find.recount(reading)
            if !find.query.isEmpty { reading.more() }
            find.jump()
        }
        .onChange(of: find.hits.count) { old, new in if old == 0, new > 0 { find.jump() } }
    }
}

// MARK: - Search

struct SearchView: View {
    let library: Library
    @ObservedObject var search: SearchModel
    @Environment(\.colorScheme) var scheme

    var body: some View {
        let p = Palette.of(scheme)
        if let e = search.error {
            Message(title: "Search failed", text: e)
        } else if let r = search.result {
            ScrollViewReader { proxy in ScrollView(.vertical, showsIndicators: false) {
                VStack(alignment: .leading, spacing: 0) {
                    Label(text: "Notes · \(r.notes?.count ?? 0)").padding(.bottom, 6)
                    ForEach(r.notes ?? []) { n in
                        Button { library.openNote(n.note_id) } label: {
                            Marked(cursor: library.searchCursor, id: "note:" + n.note_id) {
                                hit(mark: AnyView(Rectangle().fill(p.accent).frame(width: 8, height: 8)), title: n.title,
                                    meta: [n.type, n.project, day(n.last_seen)].compactMap { $0 }.filter { !$0.isEmpty }.joined(separator: " · "), text: n.gist, p)
                            }
                        }
                        .buttonStyle(.plain)
                        .id("note:" + n.note_id)
                    }
                    Label(text: "Chats · \(r.chats?.count ?? 0)").padding(.top, 24).padding(.bottom, 6)
                    ForEach(r.chats ?? []) { c in
                        Button { library.openChat(c.chat_id) } label: {
                            Marked(cursor: library.searchCursor, id: "chat:" + c.chat_id) {
                                hit(mark: AnyView(DiamondMark(color: p.agent(c.agent))), title: c.title ?? "Untitled chat",
                                    meta: [agentName(c.agent), c.project ?? "", day(c.updated)].filter { !$0.isEmpty }.joined(separator: " · "), text: c.snippet, p)
                            }
                        }
                        .buttonStyle(.plain)
                        .id("chat:" + c.chat_id)
                    }
                }
                .padding(.horizontal, 24).padding(.vertical, 22)
                .frame(maxWidth: 900, alignment: .leading)
                .background(CursorScroll(cursor: library.searchCursor, proxy: proxy))
            } }
        } else {
            Message(title: search.running ? "Searching…" : "Search chats and notes", text: "Press ⌘K and type. Words like “in recall”, “codex” or “since 2w” become filters.")
        }
    }

    /// The API marks matched words «like this»; show them in ink instead.
    func marked(_ s: String, _ p: Palette) -> AttributedString {
        var out = AttributedString()
        for (i, part) in s.components(separatedBy: "«").enumerated() {
            let halves = part.components(separatedBy: "»")
            if i > 0, halves.count > 1 {
                var hit = AttributedString(halves[0])
                hit.foregroundColor = p.ink
                out += hit
                out += AttributedString(halves.dropFirst().joined(separator: "»"))
            } else {
                out += AttributedString(part)
            }
        }
        return out
    }

    func hit(mark: AnyView, title: String, meta: String, text: String?, _ p: Palette) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Rule()
            HStack(spacing: 10) {
                mark
                Text(title).font(PaperMono.font(13, 500)).foregroundStyle(p.ink).lineLimit(1)
                Spacer(minLength: 8)
                Text(meta).font(PaperMono.font(11.5)).foregroundStyle(p.muted).lineLimit(1).fixedSize()
            }
            .padding(.top, 8)
            if let text, !text.isEmpty {
                Text(marked(text.replacingOccurrences(of: "\n", with: " "), p)).font(PaperMono.font(12, 390, prose: true)).foregroundStyle(p.muted)
                    .lineLimit(2).padding(.leading, 18).padding(.bottom, 8)
            }
        }
        .contentShape(Rectangle())
    }
}

// MARK: - Projects

struct ProjectsView: View {
    let library: Library
    @EnvironmentObject var facts: Facts
    @Environment(\.colorScheme) var scheme

    var body: some View {
        let p = Palette.of(scheme)
        let w: [CGFloat] = [2, 0.7, 0.7, 1, 1.1]
        VStack(alignment: .leading, spacing: 0) {
            Cols(cells: ["Project", "Chats", "Notes", "Agents", "Latest"].map { AnyView(Label(text: $0)) }, weights: w, top: false)
                .padding(.horizontal, 24).padding(.top, 14)
            Rule()
            Rows(facts.projects?.projects ?? [], cursor: library.projectsCursor) { pr in
                Marked(cursor: library.projectsCursor, id: pr.name) { Cols(cells: [
                    AnyView(Text(pr.name).foregroundStyle(p.ink)),
                    AnyView(Button { library.showProject(pr.name, notesFirst: false) } label: { Text(count(pr.chats)).underline(color: p.faint) }.buttonStyle(.plain)),
                    AnyView(Button { library.showProject(pr.name, notesFirst: true) } label: {
                        Text(pr.notes > 0 ? count(pr.notes) : "–").underline(pr.notes > 0, color: p.faint)
                    }.buttonStyle(.plain).disabled(pr.notes == 0)),
                    AnyView(HStack(spacing: 5) { ForEach(pr.agents ?? [], id: \.self) { DiamondMark(color: p.agent($0)) } }),
                    AnyView(Text(day(pr.latest)).foregroundStyle(p.muted)),
                ], weights: w, top: false)
                .padding(.horizontal, 24) }
                .overlay(alignment: .bottom) { Rectangle().fill(p.grid).frame(height: 1) }
                .contentShape(Rectangle())
                .onTapGesture { library.showProject(pr.name, notesFirst: pr.notes > 0) }
            }
        }
        .font(PaperMono.font(12.5))
        .background(p.paper)
    }
}

// MARK: - Now

struct NowView: View {
    let library: Library
    @EnvironmentObject var facts: Facts
    @Environment(\.colorScheme) var scheme

    var body: some View {
        let p = Palette.of(scheme)
        ScrollView(.vertical, showsIndicators: false) {
            VStack(alignment: .leading, spacing: 0) {
                if let s = facts.status {
                    let byId = Dictionary(s.sessions.map { ($0.id, $0) }, uniquingKeysWith: { a, _ in a })
                    Label(text: "Needs you · \(facts.needsYou)").padding(.bottom, 6)
                    if facts.needsYou == 0 {
                        Text("Nothing needs you. No two chats are at the same file right now.").foregroundStyle(p.muted).padding(.vertical, 8)
                    }
                    ForEach(s.overlaps, id: \.id) { o in
                        RowLine {
                            HStack(spacing: 10) {
                                if o.level == "act" { Rectangle().fill(p.accent).frame(width: 8, height: 8) } else { DiamondMark(color: p.lane) }
                                Text((o.file as NSString).lastPathComponent).foregroundStyle(p.ink)
                                Text(o.verdict).foregroundStyle(o.level == "act" ? p.accentText : p.muted)
                            }
                        } right: {
                            Text(([o.repo] + o.sessions.compactMap { byId[$0].map { clip($0.title, 30) } }).joined(separator: " · ")).foregroundStyle(p.muted).lineLimit(1)
                        }
                    }
                    ForEach(s.restarts, id: \.agent) { r in
                        RowLine { Text("Restart \(r.agent)").foregroundStyle(p.ink) } right: { Text(r.detail).foregroundStyle(p.muted).lineLimit(1) }
                    }
                    Rule()
                    let live = s.sessions.filter(\.live)
                    Label(text: "Working now · \(live.count) live").padding(.top, 24).padding(.bottom, 6)
                    if live.isEmpty { Text("No agent is working right now.").foregroundStyle(p.muted).padding(.vertical, 8) }
                    ForEach(live, id: \.id) { c in
                        Button { library.openChat(c.id) } label: {
                            Marked(cursor: library.nowCursor, id: c.id) { RowLine {
                                HStack(spacing: 10) {
                                    ChatMark(agent: c.agent, live: true)
                                    Text(c.title).foregroundStyle(p.ink).lineLimit(1)
                                }
                            } right: {
                                Text([c.repo ?? "", short(c.branch, 24), ago(c.age_seconds)].filter { !$0.isEmpty }.joined(separator: " · ")).foregroundStyle(p.muted).lineLimit(1).fixedSize()
                            } }
                            .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                    }
                    Rule()
                } else {
                    Text("Reading what your agents are doing…").foregroundStyle(p.muted)
                }
            }
            .font(PaperMono.font(12.5))
            .padding(.horizontal, 24).padding(.vertical, 22)
            .frame(maxWidth: 1000, alignment: .leading)
        }
        .background(p.paper)
    }
}
