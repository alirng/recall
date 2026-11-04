// This Mac (board D): what recall holds here and how it runs. Stats, sources, last night's memory
// run as a waterfall, storage from the API's inventory, model and schedule, coordination rules and
// access. Everything here reads; the writes the board shows arrive in step 4 and are dashed.

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
                        AnyView(Text(item.label).foregroundStyle(p.ink).truncationMode(.tail)),
                        AnyView(Text(item.path).foregroundStyle(p.muted).truncationMode(.middle).help(item.path)),
                        AnyView(Text(size(item.bytes)).frame(maxWidth: .infinity, alignment: .trailing)),
                    ], weights: [1.3, 2.2, 0.5])
                }
            }
            if let backup = facts.storage?.totals?["backup"], backup > 0 {
                HStack(spacing: 10) {
                    Text("\(Int(Double(backup) / Double(total) * 100))% of the space is copies kept before upgrades.")
                        .font(PaperMono.font(11.5)).foregroundStyle(p.muted)
                    Spacer()
                    WindowButton(title: "Remove old copies · \(size(backup))", soon: true)
                }
                .padding(.top, 12)
            }
        }
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
        Panel(title: "Model and schedule", note: when) {
            row("Writes notes", [c?.model?.provider, c?.model?.name, c?.model?.batch.map { $0 == "off" ? "" : "batch" }].compactMap { $0 }.filter { !$0.isEmpty }.joined(separator: " · "), p, strong: true)
            if let f = c?.model?.fallback { row("After credits", f, p) }
            row("Key", c?.model?.key?.configured == true ? "in " + (c?.model?.key?.stored_in ?? "settings").replacingOccurrences(of: "macOS ", with: "") : "none", p)
            if let e = c?.embeddings {
                row("Embeddings", e.on == false ? "off" : [e.provider, e.name, e.provider == "ollama" ? "on this Mac" : nil].compactMap { $0 }.joined(separator: " · "), p)
            }
            if let l = c?.schedule?.limits {
                row("Limits", [l.max_minutes.map { "\(Int($0)) min" }, l.max_calls.map { "\($0) calls" }, l.max_usd.map { String(format: "$%.2f", $0) }].compactMap { $0 }.joined(separator: " · ") + " a night", p)
            }
            if let b = c?.power?.on_battery { row("On battery", b == "defer" ? "waits for power" : b, p) }
            HStack(spacing: 8) {
                WindowButton(title: "Change model…", soon: true)
                WindowButton(title: "Run now", soon: true)
            }
            .padding(.top, 12)
        }
    }

    func row(_ k: String, _ v: String, _ p: Palette, strong: Bool = false) -> some View {
        Cols(cells: [AnyView(Label(text: k)), AnyView(Text(v).foregroundStyle(strong ? p.ink : p.body))], weights: [0.9, 2])
    }
}

struct CoordinationPanel: View {
    let library: Library
    @EnvironmentObject var facts: Facts
    @Environment(\.colorScheme) var scheme
    static let when = ["same_copy": "two chats share one checkout", "conflict": "their edits conflict", "apart": "same file, different lines", "overlap": "changed lines overlap"]

    var body: some View {
        let p = Palette.of(scheme)
        Panel(title: "Coordination rules", note: "when two chats touch the same work") {
            ForEach(facts.coordination?.rules ?? []) { r in
                VStack(spacing: 0) {
                    Rule()
                    HStack(spacing: 10) {
                        Label(text: "When").frame(width: 44, alignment: .leading)
                        Text(Self.when[r.situation] ?? r.meaning ?? r.situation).foregroundStyle(p.ink).lineLimit(1)
                        Spacer(minLength: 6)
                        Label(text: "then")
                        Text(r.action).foregroundStyle(p.ink)
                            .padding(.horizontal, 8).frame(width: 96, height: 24, alignment: .leading)
                            .overlay(Dotted(color: p.faint, dash: [3, 3]))
                            .help((r.meaning ?? "") + (r.action == r.default ? " · the default" : "") + " · changing it comes in a later version")
                    }
                    .padding(.vertical, 7)
                }
            }
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
