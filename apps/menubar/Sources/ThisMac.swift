// This Mac (board D): what recall holds here and how it runs. Stats, sources, last night's memory
// run as a waterfall, storage from the API's inventory, model and schedule, coordination rules and
// access. The model, the coordination rules and removing copies kept before upgrades write (board G3),
// through recall api; on a recall without writes they show read-only. The schedule stays read-only.

import AppKit
import SwiftUI

struct ThisMacView: View {
    let library: Library
    @EnvironmentObject var facts: Facts
    @Environment(\.colorScheme) var scheme

    /// The panels the palette can go to, by title.
    static let panels = ["Storage", "Last night’s memory run", "Sources", "Model and schedule", "Coordination rules", "Access"]

    var body: some View {
        let p = Palette.of(scheme)
        ScrollViewReader { proxy in
            ScrollView(.vertical, showsIndicators: false) {
                VStack(alignment: .leading, spacing: 16) {
                    Label(text: [library.info?.instance?.name ?? "This Mac", library.info.map { "recall-agents " + $0.recall } ?? "", "API on this Mac only"].filter { !$0.isEmpty }.joined(separator: " · "))
                    statsRow(p)
                    LazyVGrid(columns: [GridItem(.adaptive(minimum: 440), spacing: 16, alignment: .top)], alignment: .leading, spacing: 16) {
                        SourcesPanel(library: library).id("Sources")
                        RunPanel(library: library).id("Last night’s memory run")
                        ModelPanel(library: library).id("Model and schedule")
                        CoordinationPanel(library: library).id("Coordination rules")
                        AccessPanel(library: library).id("Access")
                    }
                    StoragePanel(library: library).id("Storage")
                    Color.clear.frame(height: 1).id("End")  // the page's foot, for a scroll to the bottom
                }
                .padding(.horizontal, 24).padding(.top, 22).padding(.bottom, 26)
            }
            .background(CursorScroll(cursor: library.thisMacFocus, proxy: proxy, anchor: .top))
        }
        .background(p.paper)
    }

    func statsRow(_ p: Palette) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(alignment: .top, spacing: 40) {
                stat(count(facts.chatsCount), "chats indexed", p, accent: true)
                stat(count(facts.notesCount), "memory notes", p)
                stat(size(facts.storage?.total_bytes), "on disk", p)
                stat(facts.runs?.latest?.cost_usd.map { String(format: "$%.2f", $0) } ?? "", "last night’s run", p)
            }
            .padding(.bottom, 16)
            Rule()
        }
    }

    func stat(_ value: String, _ label: String, _ p: Palette, accent: Bool = false) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(value.isEmpty ? "–" : value).font(PaperMono.font(30, 560)).foregroundStyle(accent ? p.accentText : p.ink).monospacedDigit()
            Label(text: label)
        }
    }
}

/// A board panel: card, 1px line, title and a quiet note on the right.
struct Panel<C: View>: View {
    let title: String
    var note = ""
    @ViewBuilder var content: () -> C
    @Environment(\.colorScheme) var scheme
    var body: some View {
        let p = Palette.of(scheme)
        VStack(alignment: .leading, spacing: 0) {
            HStack(alignment: .firstTextBaseline, spacing: 10) {
                Text(title).font(PaperMono.font(14, 520)).foregroundStyle(p.ink)
                Spacer()
                Text(note).font(PaperMono.font(11.5)).foregroundStyle(p.muted).lineLimit(1)
            }
            .padding(.bottom, 12)
            content()
        }
        .font(PaperMono.font(12.5))
        .padding(.horizontal, 16).padding(.vertical, 14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(p.card)
        .overlay(Rectangle().strokeBorder(p.line, lineWidth: 1))
    }
}

/// Columns that line up across rows, as the board's grid rows do.
struct Cols: View {
    let cells: [AnyView]
    let weights: [CGFloat]
    var top = true
    var body: some View {
        VStack(spacing: 0) {
            if top { Rule() }
            GeometryReader { g in
                let total = weights.reduce(0, +), gap: CGFloat = 10
                let w = g.size.width - gap * CGFloat(weights.count - 1)
                HStack(spacing: gap) {
                    ForEach(cells.indices, id: \.self) { i in
                        cells[i].frame(width: max(0, w * weights[i] / total), alignment: .leading).lineLimit(1)
                    }
                }
            }
            .frame(height: 18)
            .padding(.vertical, 8)
        }
        .monospacedDigit()
    }
}

struct SourcesPanel: View {
    let library: Library
    @EnvironmentObject var facts: Facts
    @Environment(\.colorScheme) var scheme
    var body: some View {
        let p = Palette.of(scheme)
        // An agent can have more than one folder; show its best state.
        let roots = Dictionary((facts.doctor?.roots ?? []).map { ($0.agent, $0) }, uniquingKeysWith: { a, b in a.state == "connected" ? a : b })
        let w: [CGFloat] = [1.2, 0.6, 1.1, 1.7]
        Panel(title: "Sources", note: facts.doctor?.autopilot?.on == true ? "autopilot on" : "") {
            Cols(cells: ["Agent", "Chats", "Latest chat", "State"].map { AnyView(Label(text: $0)) }, weights: w, top: false)
            ForEach(facts.agents, id: \.name) { a in
                let s = facts.stats?.agents[a.name]
                let state = Self.words(roots[a.name]?.state, a.name)
                Cols(cells: [
                    AnyView(HStack(spacing: 8) { DiamondMark(color: p.agent(a.name)); Text(agentName(a.name)).foregroundStyle(p.ink) }),
                    AnyView(Text(count(a.chats))),
                    AnyView(Text(shortStamp(s?.latest))),
                    AnyView(Text(state).foregroundStyle(state == "connected" ? p.body : p.ink).help(Self.why(roots[a.name]?.state, a.name))),
                ], weights: w)
            }
        }
    }
}

extension SourcesPanel {
    static func why(_ state: String?, _ agent: String) -> String {
        switch state {
        case "trust": return "\(agentName(agent)) hasn't been told to run recall's hooks: trust them once with /hooks in \(agentName(agent))"
        case "approval": return "\(agentName(agent)) hasn't been told to load recall's server: approve it once in \(agentName(agent))"
        default: return words(state, agent)
        }
    }

    /// Doctor's state in plain words, with where to fix it.
    static func words(_ state: String?, _ agent: String) -> String {
        switch state {
        case "connected": return "connected"
        case "trust": return "needs trust (/hooks)"
        case "approval": return "needs approval"
        case "missing": return "not connected (recall install)"
        case "skipped": return "skipped"
        case "waiting": return "not found yet"
        case nil: return "indexed only"  // an agent recall reads but doesn't connect to
        default: return state ?? ""
        }
    }
}

struct RunPanel: View {
    let library: Library
    @EnvironmentObject var facts: Facts
    @Environment(\.colorScheme) var scheme

    var body: some View {
        let p = Palette.of(scheme)
        if let run = facts.runs?.latest, let start = date(run.started), let end = date(run.finished) ?? date(run.phases?.last?.finished) {
            let span = max(1, end.timeIntervalSince(start))
            Panel(title: "Last night’s memory run", note: "\(clock(start)) · \(duration(run.seconds ?? span))") {
                HStack(spacing: 12) {
                    Label(text: "Step").frame(width: 112, alignment: .leading)
                    HStack {
                        Label(text: clock(start)); Spacer(); Label(text: clock(start.addingTimeInterval(span / 2))); Spacer(); Label(text: clock(end))
                    }
                    Label(text: "Result").frame(width: 150, alignment: .trailing)
                }
                .padding(.bottom, 6)
                ForEach(Array((run.phases ?? []).enumerated()), id: \.offset) { _, ph in
                    let a = date(ph.started).map { $0.timeIntervalSince(start) / span } ?? 0
                    let b = date(ph.finished).map { $0.timeIntervalSince(start) / span } ?? a
                    VStack(spacing: 0) {
                        Rule()
                        HStack(spacing: 12) {
                            Text(name(ph.name)).foregroundStyle(p.ink).frame(width: 112, alignment: .leading).lineLimit(1)
                            GeometryReader { g in
                                let x = g.size.width * CGFloat(max(0, min(1, a)))
                                let w = max(3, g.size.width * CGFloat(max(0, min(1, b) - max(0, a))))
                                let lit = ph.name == "consolidate"
                                Rectangle().fill(lit ? p.accentSoft : p.ghost)
                                    .overlay(Rectangle().strokeBorder(lit ? p.accent : p.line, lineWidth: 1))
                                    .frame(width: w, height: 12).offset(x: x)
                            }
                            .frame(height: 12)
                            Text(result(ph, run)).foregroundStyle(p.body).frame(width: 150, alignment: .trailing).lineLimit(1)
                        }
                        .padding(.vertical, 6)
                    }
                    .help(ph.result ?? "")
                }
                Text(footer(run)).font(PaperMono.font(11.5)).foregroundStyle(p.muted).padding(.top, 8)
                    .fixedSize(horizontal: false, vertical: true)
                if let e = run.error { Text(e).font(PaperMono.font(11.5)).foregroundStyle(p.accentText).padding(.top, 4) }
            }
        } else if let run = facts.runs?.latest, let start = date(run.started) {  // no steps to draw: it has only just begun, or stopped without saying so
            Panel(title: "Last night’s memory run", note: clock(start)) {
                Text(run.status == "running" ? "Running now." : "No steps were logged for this run.").foregroundStyle(p.muted)
                if let e = run.error { Text(e).font(PaperMono.font(11.5)).foregroundStyle(p.accentText).padding(.top, 4) }
            }
        } else {
            Panel(title: "Last night’s memory run") {
                Text(facts.runs == nil ? "Reading…" : "No memory run yet.").foregroundStyle(p.muted)
            }
        }
    }

    func name(_ n: String) -> String {
        ["commit links": "Commit links", "read": "Read chats", "consolidate": "Consolidate", "commit notes": "Commit notes"][n] ?? n.capitalized
    }

    /// The board's short result: "245 · 15 s", "27 chats · 3.7 min", the notes commit.
    func result(_ ph: Runs.Phase, _ run: Runs.Run) -> String {
        let r = ph.result ?? ""
        let secs = (date(ph.finished)?.timeIntervalSince(date(ph.started) ?? Date())) ?? 0
        // What consolidating made, from the run's own counts: new notes and rules added.
        if ph.name == "consolidate", let c = run.counts {
            return "\(c["created"] ?? 0) new · \(c["added"] ?? 0) rules"
        }
        if ph.name == "commit notes", let m = r.range(of: #"commit [0-9a-f]{7}"#, options: .regularExpression) {
            return String(r[m].dropFirst(7))
        }
        if ph.name == "read", let m = r.range(of: #"\d+ chats"#, options: .regularExpression) {
            return "\(r[m].split(separator: " ")[0]) · \(duration(secs))"
        }
        if let m = r.range(of: #"\d+"#, options: .regularExpression) {
            let n = String(r[m])
            return "\(n) · \(duration(secs))"
        }
        return duration(secs)
    }

    func footer(_ run: Runs.Run) -> String {
        let c = run.counts ?? [:]
        var parts: [String] = []
        let made = ["created", "added", "refined", "confirmed", "retired"].compactMap { k in c[k].map { "\($0) \(k)" } }
        if !made.isEmpty { parts.append(made.joined(separator: ", ")) }
        var calls = "\(run.model_calls ?? 0) calls to \(run.backend?.split(separator: ":").last.map(String.init) ?? "the model")"
        if (run.batches ?? 0) > 0 { calls += " through the batch API at half price" }
        parts.append(calls)
        if let usd = facts.config?.schedule?.limits?.max_usd { parts.append(String(format: "budget $%.2f", usd)) }
        return parts.joined(separator: " · ")
    }
}

struct StoragePanel: View {
    let library: Library
    @EnvironmentObject var facts: Facts
    @Environment(\.colorScheme) var scheme
    static let order = ["data", "rebuildable", "backup", "settings", "running"]
    static let names = ["data": "Yours", "rebuildable": "Rebuildable", "backup": "Backups", "settings": "Settings", "running": "Running"]

    var body: some View {
        let p = Palette.of(scheme)
        let items = (facts.storage?.items ?? []).filter { $0.exists ?? true }
        let total = max(1, facts.storage?.total_bytes ?? items.reduce(0) { $0 + $1.bytes })
        let groups = Self.order.map { k in (k, items.filter { $0.kind == k }.sorted { $0.bytes > $1.bytes }) }.filter { !$0.1.isEmpty }
        Panel(title: "Storage", note: "everything recall keeps · \(size(facts.storage?.total_bytes))") {
            if items.isEmpty { Text("Reading…").foregroundStyle(p.muted) }
            // One bar, split by kind: what you'd lose, what recall makes again, copies kept before upgrades.
            GeometryReader { g in
                HStack(spacing: 4) {
                    ForEach(groups, id: \.0) { k, xs in
                        let bytes = xs.reduce(0) { $0 + $1.bytes }
                        let w = max(4, (g.size.width - 4 * CGFloat(groups.count - 1)) * CGFloat(bytes) / CGFloat(total))
                        box(k, bytes, p).frame(width: w)
                    }
                }
            }
            .frame(height: 64)
            .padding(.bottom, 14)
            ForEach(groups, id: \.0) { k, xs in
                HStack(alignment: .firstTextBaseline) {
                    Label(text: Self.names[k] ?? k, color: p.ink)
                    Text(facts.storage?.kinds?[k] ?? "").font(PaperMono.font(11.5)).foregroundStyle(p.muted)
                    Spacer()
                    Text(size(xs.reduce(0) { $0 + $1.bytes })).foregroundStyle(p.ink).monospacedDigit()
                }
                .padding(.top, 10).padding(.bottom, 4)
                ForEach(xs) { item in
                    Cols(cells: [
                        AnyView(Text(item.kind == "backup" ? Self.named(item) : item.label).foregroundStyle(p.ink).truncationMode(.tail)),
                        AnyView(Text(item.path).foregroundStyle(p.muted).truncationMode(.middle).help(item.path)),
                        AnyView(Text(size(item.bytes)).frame(maxWidth: .infinity, alignment: .trailing)),
                    ], weights: [1.3, 2.2, 0.5])
                }
            }
            let backups = items.filter { $0.kind == "backup" }
            let backup = backups.reduce(0) { $0 + $1.bytes }
            if backup > 0, !facts.confirmRemove {
                HStack(spacing: 10) {
                    Text("\(Int(Double(backup) / Double(total) * 100))% of the space is copies kept before upgrades.")
                        .font(PaperMono.font(11.5)).foregroundStyle(p.muted)
                    Spacer()
                    WindowButton(title: "Remove old copies · \(size(backup))", soon: !library.canWrite("storage"),
                                 why: library.info == nil ? "" : updateToWrite) {
                        facts.removeNote = nil
                        facts.confirmRemove = true
                    }
                }
                .padding(.top, 12)
            }
            if facts.confirmRemove, !backups.isEmpty { confirm(backups, backup, p) }
            if let said = facts.removeNote { SaidLine(said: said).padding(.top, 10) }
        }
    }

    /// What Remove takes, item by item with sizes, and what stays (G3).
    func confirm(_ backups: [Storage.Item], _ bytes: Int, _ p: Palette) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Remove what was kept before upgrades?").font(PaperMono.font(12.5, 500)).foregroundStyle(p.ink)
            VStack(spacing: 0) {
                ForEach(backups) { b in
                    VStack(spacing: 0) {
                        Rule()
                        HStack {
                            Text(Self.named(b)).foregroundStyle(p.body).lineLimit(1).truncationMode(.middle)
                            Spacer(minLength: 10)
                            Text(size(b.bytes)).foregroundStyle(p.muted).monospacedDigit()
                        }
                        .font(PaperMono.font(12)).padding(.vertical, 5)
                    }
                }
                Rule()
            }
            Text("Nothing reads these. Your chat index, notes and chat archive stay. This can't be undone.")
                .font(PaperMono.font(11.5)).foregroundStyle(p.muted).lineSpacing(3).fixedSize(horizontal: false, vertical: true)
            HStack(spacing: 8) {
                Spacer()
                WindowButton(title: "Cancel", small: true) { facts.confirmRemove = false }
                WindowButton(title: facts.removeNote?.working == true ? "Removing…" : "Remove · \(size(bytes))", style: .risk, small: true) {
                    library.removeBackups(backups)
                }
            }
        }
        .padding(12)
        .background(p.paper)
        .overlay(Rectangle().strokeBorder(p.ink, lineWidth: 1))
        .padding(.top, 12)
    }

    /// "Old index copy · 0.4.0 · 16 Oct": what it is, the version and the day it was kept.
    static func named(_ i: Storage.Item) -> String {
        [i.label, i.version ?? "", day(i.date)].filter { !$0.isEmpty }.joined(separator: " · ")
    }

    func box(_ kind: String, _ bytes: Int, _ p: Palette) -> some View {
        let dashed = kind == "backup", lit = kind == "data"
        return VStack(alignment: .leading) {
            Text(Self.names[kind] ?? kind).font(PaperMono.font(12, 500)).foregroundStyle(p.ink).lineLimit(1)
            Spacer(minLength: 0)
            Text(size(bytes)).font(PaperMono.font(11)).foregroundStyle(p.muted).lineLimit(1)
        }
        .padding(.horizontal, 9).padding(.vertical, 8)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .leading)
        .background(dashed ? Color.clear : (lit ? p.accentSoft : p.ghost))
        .overlay(Rectangle().strokeBorder(lit ? p.accent : p.line, style: StrokeStyle(lineWidth: 1, dash: dashed ? [3, 3] : [])))
        .clipped()
    }
}

struct ModelPanel: View {
    let library: Library
    @EnvironmentObject var facts: Facts
    @Environment(\.colorScheme) var scheme
    var body: some View {
        let p = Palette.of(scheme), c = facts.config
        let when = c?.schedule?.enabled == true ? "runs nightly at \(c?.schedule?.time ?? "")" : "no nightly run"
        let now = facts.models?.options.first { $0.now == true }?.label
            ?? [c?.model?.provider, c?.model?.name].compactMap { $0 }.joined(separator: " · ")
        Panel(title: "Model and schedule", note: when) {
            Cols(cells: [AnyView(Label(text: "Writes notes")), AnyView(modelSelect(now, p))], weights: [0.9, 2], top: false)
            if let f = facts.models?.fallback ?? c?.model?.fallback { row("If that fails", planWords(f), p) }
            row("Key", c?.model?.key?.configured == true ? "in " + (c?.model?.key?.stored_in ?? "settings").replacingOccurrences(of: "macOS ", with: "") : "none", p)
            if let e = c?.embeddings {
                row("Embeddings", e.on == false ? "off" : [e.provider, e.name, e.provider == "ollama" ? "on this Mac" : nil].compactMap { $0 }.joined(separator: " · "), p)
            }
            if let l = c?.schedule?.limits {
                row("Limits", [l.max_minutes.map { "\(Int($0)) min" }, l.max_calls.map { "\($0) calls" }, l.max_usd.map { String(format: "$%.2f", $0) }].compactMap { $0 }.joined(separator: " · ") + " a night", p)
            }
            if let b = c?.power?.on_battery { row("On battery", b == "defer" ? "waits for power" : b, p) }
            // Read-only: changing it rewrites a background job, so it stays `recall schedule` in Terminal.
            Cols(cells: [AnyView(Label(text: "Schedule")), AnyView(
                Text("\(Text(c?.schedule?.enabled == true ? "nightly at \(c?.schedule?.time ?? "") · " : "off · ").foregroundColor(p.muted))\(Text("recall schedule").foregroundColor(p.body))")
                .help("Change it with recall schedule in Terminal: it rewrites a background job"))], weights: [0.9, 2])
            if let said = facts.modelNote { SaidLine(said: said).padding(.top, 10) }
            if library.info != nil, !library.canWrite("model") {
                Text(updateToWrite).font(PaperMono.font(11.5)).foregroundStyle(p.muted).padding(.top, 10)
            }
            HStack(spacing: 8) {
                WindowButton(title: "Run now", soon: true)
            }
            .padding(.top, 12)
        }
    }

    /// The model as a menu of what already works here (G3), or plain text on a recall without writes.
    @ViewBuilder
    func modelSelect(_ now: String, _ p: Palette) -> some View {
        if library.canWrite("model") {
            MenuButton(popups: library.popups, width: 400, menu: { Self.menu(library, p) }) {
                SelectBox(text: now.isEmpty ? "Choose a model" : now, strong: true)
            }
            .disabled(facts.modelNote?.working == true)
        } else {
            Text(now).foregroundStyle(p.ink).help(library.info == nil ? "" : updateToWrite)
        }
    }

    static func menu(_ library: Library, _ p: Palette) -> MenuModel {
        let options = library.facts.models?.options ?? []
        let m = MenuModel(title: nil, rows: options.map { o in
            MenuRow(title: o.label, detail: o.detail, mark: AnyView(Rectangle().fill(o.now == true ? p.ink : .clear)
                .overlay(Rectangle().strokeBorder(p.ink, lineWidth: 1)).frame(width: 8, height: 8)),
                    action: { [weak library] in library?.useModel(o) })
        })
        m.ticks = false
        m.footer = ("A new API key or provider: ", "recall model", " in Terminal")
        if let i = options.firstIndex(where: { $0.now == true }) { m.at = i }
        return m
    }

    func row(_ k: String, _ v: String, _ p: Palette, strong: Bool = false) -> some View {
        Cols(cells: [AnyView(Label(text: k)), AnyView(Text(v).foregroundStyle(strong ? p.ink : p.body))], weights: [0.9, 2])
    }
}

/// "claude:haiku" as the menu says it: "Claude plan · claude:haiku".
func planWords(_ spec: String) -> String {
    if spec.hasPrefix("claude") { return "Claude plan · " + spec }
    if spec.hasPrefix("chatgpt") { return "ChatGPT plan" + (spec.contains(":") ? " · " + spec.split(separator: ":", maxSplits: 1)[1] : "") }
    return spec
}

/// A select as the boards draw it: the value, a ▾, a 1 px box (ink when it differs from the default).
struct SelectBox: View {
    let text: String
    var strong = false
    @Environment(\.colorScheme) var scheme
    var body: some View {
        let p = Palette.of(scheme)
        HStack(spacing: 8) {
            Text(text).foregroundStyle(p.ink).lineLimit(1)
            Spacer(minLength: 4)
            Text("▾").foregroundStyle(p.muted)
        }
        .font(PaperMono.font(12))
        .padding(.horizontal, 8).frame(minHeight: 28)
        .background(p.paper)
        .overlay(Rectangle().strokeBorder(strong ? p.ink : p.line, lineWidth: 1))
        .contentShape(Rectangle())
    }
}

/// What a write said: working on it, done (an amber ✓), or didn't work.
enum Said: Equatable {
    case working(String), done(String), failed(String)
    var working: Bool { if case .working = self { return true } else { return false } }
}

struct SaidLine: View {
    let said: Said
    var right: AnyView? = nil
    @Environment(\.colorScheme) var scheme
    var body: some View {
        let p = Palette.of(scheme)
        HStack(alignment: .firstTextBaseline, spacing: 6) {
            switch said {
            case .working(let t):
                Rectangle().strokeBorder(p.muted, lineWidth: 1).frame(width: 8, height: 8)
                Text(t)
            case .done(let t):
                Text("✓").foregroundStyle(p.accentText).fontWeight(.medium)
                Text(t)
            case .failed(let t):
                Rectangle().strokeBorder(p.kind("open"), lineWidth: 1).frame(width: 8, height: 8)
                Text(t).fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 6)
            if let right { right }
        }
        .font(PaperMono.font(11.5)).foregroundStyle(p.muted)
    }
}

struct CoordinationPanel: View {
    let library: Library
    @EnvironmentObject var facts: Facts
    @Environment(\.colorScheme) var scheme
    static let when = ["same_copy": "two chats share one checkout", "conflict": "their edits conflict", "apart": "same file, different lines", "overlap": "changed lines overlap"]
    static let actions = [("worktree", "stop once and move to its own worktree"), ("ask", "stop once and check with you"),
                          ("tell", "let the edit through and mention it"), ("off", "do nothing")]
    static let presetNames = ["recommended": "Recommended", "hands-off": "Hands-off", "tell": "Tell", "off": "Off"]

    var body: some View {
        let p = Palette.of(scheme)
        let writes = library.canWrite("coordination")
        let c = facts.coordination
        Panel(title: "Coordination rules", note: "when two chats touch the same work") {
            if writes {
                HStack(spacing: 10) {
                    Label(text: "Preset").frame(width: 44, alignment: .leading)
                    MenuButton(popups: library.popups, width: 360, menu: { presetMenu(c) }) {
                        SelectBox(text: c.map { $0.preset.flatMap { Self.presetNames[$0] } ?? "Custom" } ?? "")
                    }
                    .frame(maxWidth: 220)
                    Spacer()
                }
                .padding(.bottom, 7)
            }
            ForEach(c?.rules ?? []) { r in
                VStack(spacing: 0) {
                    Rule()
                    HStack(spacing: 10) {
                        Label(text: "When").frame(width: 44, alignment: .leading)
                        Text(Self.when[r.situation] ?? r.meaning ?? r.situation).foregroundStyle(p.ink).lineLimit(1)
                        Spacer(minLength: 6)
                        Label(text: "then")
                        if writes {
                            MenuButton(popups: library.popups, width: 320, menu: { actionMenu(r) }) {
                                SelectBox(text: r.action, strong: r.action != r.default)
                            }
                            .frame(width: 104)
                            .help(r.meaning ?? "")
                        } else {
                            Text(r.action).foregroundStyle(p.ink)
                                .padding(.horizontal, 8).frame(width: 96, height: 24, alignment: .leading)
                                .overlay(Dotted(color: p.faint, dash: [3, 3]))
                                .help((r.meaning ?? "") + (r.action == r.default ? " · the default" : "") + (library.info == nil ? "" : " · " + updateToWrite))
                        }
                    }
                    .padding(.vertical, 7)
                }
            }
            Rule()
            if writes {
                if let said = facts.coordNote {
                    SaidLine(said: said, right: c?.preset == "recommended" ? nil : AnyView(back(p))).padding(.top, 10)
                } else if let c, c.preset != "recommended" {
                    HStack { Spacer(); back(p) }.padding(.top, 10)
                }
            } else if library.info != nil {
                Text(updateToWrite).font(PaperMono.font(11.5)).foregroundStyle(p.muted).padding(.top, 10)
            }
        }
    }

    func back(_ p: Palette) -> some View {
        Button { library.setCoordination(preset: "recommended") } label: {
            Text("Back to Recommended").font(PaperMono.font(11.5)).foregroundStyle(p.ink).contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }

    func presetMenu(_ c: Coordination?) -> MenuModel {
        let names = ["recommended", "hands-off", "tell", "off"]
        return MenuModel(title: "Preset", rows: names.map { n in
            MenuRow(title: Self.presetNames[n] ?? n, detail: c?.presets?[n]?.meaning, checked: c?.preset == n,
                    action: { [weak library] in library?.setCoordination(preset: n) })
        })
    }

    func actionMenu(_ r: Coordination.Rule) -> MenuModel {
        MenuModel(title: Self.when[r.situation] ?? r.situation, rows: Self.actions.map { a, what in
            MenuRow(title: a, detail: what + (a == r.default ? " · recommended" : ""), checked: r.action == a,
                    action: { [weak library] in library?.setCoordination(r.situation, a) })
        })
    }
}

extension Library {
    /// Picks a model from the menu: recall makes one short test call and saves only if it answers.
    func useModel(_ o: Models.Option) {
        facts.modelNote = .working("Testing with one short call…")
        API.send("PUT", "/v1/model", ["option": o.id], ModelSaved.self, timeout: 300) { [weak self] r in
            guard let self else { return }
            switch r {
            case .success(let s): self.facts.modelNote = .done("Saved. Tonight's run uses \(s.label).")
            case .failure(let f): self.facts.modelNote = .failed("That didn't work: \(f.message). Nothing changed.")
            }
            self.fetch("/v1/config", Config.self) { self.facts.config = $0 }
            self.fetch("/v1/models", Models.self) { self.facts.models = $0 }
        }
    }

    /// One rule's action, or a preset. Each saves at once; agents read it on their next edit.
    func setCoordination(_ situation: String? = nil, _ action: String? = nil, preset: String? = nil) {
        let body: [String: Any] = preset.map { ["preset": $0] } ?? ["rules": [situation ?? "": action ?? ""]]
        API.send("PUT", "/v1/coordination", body, Coordination.self) { [weak self] r in
            guard let self else { return }
            switch r {
            case .success(let c):
                self.facts.coordination = c
                if let situation, let was = c.was?[situation], was != action {
                    self.facts.coordNote = .done("Saved · agents use it from their next edit · was \(was)")
                } else {
                    self.facts.coordNote = .done("Saved · agents use it from their next edit")
                }
            case .failure(let f): self.facts.coordNote = .failed("That didn't save: \(f.message)")
            }
        }
    }

    /// Removes every copy kept before an upgrade, as the confirm lists them.
    func removeBackups(_ items: [Storage.Item]) {
        facts.removeNote = .working("Removing…")
        API.send("POST", "/v1/storage/remove", ["paths": items.map(\.path)], Removed.self) { [weak self] r in
            guard let self else { return }
            self.facts.confirmRemove = false
            switch r {
            case .success(let d): self.facts.removeNote = .done("Removed \(size(d.bytes)). Your chat index, notes and chat archive stay.")
            case .failure(let f): self.facts.removeNote = .failed("Nothing was removed: \(f.message)")
            }
            self.fetch("/v1/storage", Storage.self) { self.facts.storage = $0 }
        }
    }
}

struct AccessPanel: View {
    let library: Library
    @Environment(\.colorScheme) var scheme
    var body: some View {
        let p = Palette.of(scheme)
        Panel(title: "Access", note: "recall api") {
            Cols(cells: [AnyView(Label(text: "Listening")), AnyView(Text(API.socketPath.replacingOccurrences(of: API.home, with: "~")).foregroundStyle(p.ink).truncationMode(.middle))], weights: [0.9, 2], top: false)
            Cols(cells: [AnyView(Label(text: "Clients")), AnyView(Text("this app · agents' recall servers"))], weights: [0.9, 2])
            Rule()
            HStack(spacing: 10) {
                Dotted(color: p.faint).frame(width: 13, height: 13)
                Text("Serve this memory to other machines").foregroundStyle(p.muted)
            }
            .padding(.vertical, 10)
            .help("Server mode comes later")
            Text("Turning this on lets Macs you approve read and add to this memory. Today recall keeps everything on this Mac; this would change that.")
                .font(PaperMono.font(12, 390, prose: true)).foregroundStyle(p.body).lineSpacing(3)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.horizontal, 12).padding(.vertical, 10)
                .overlay(Dotted(color: p.lane, dash: [3, 3]))
        }
    }
}
