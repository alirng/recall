// This Mac (board D): what recall holds here and how it runs. Stats, sources, last night's memory
// run as a waterfall, storage from the API's inventory, model and schedule, coordination rules and
// access. The model, the coordination rules and removing copies kept before upgrades write (board G3),
// through recall api; on a recall without writes they show read-only. The schedule stays read-only.

import AppKit
import SwiftUI

/// The page's two row heights: data, and a row that holds a control; column heads are shorter.
enum RowHeight {
    static let data: CGFloat = 36
    static let control: CGFloat = 44
    static let head: CGFloat = 26
}

extension Facts {
    /// A dollar cap means something only on an API key. A plan or a local model doesn't bill per call.
    var onAPIKey: Bool { config?.model?.key?.configured == true }
}

struct ThisMacView: View {
    let library: Library
    @EnvironmentObject var facts: Facts
    @Environment(\.colorScheme) var scheme

    /// The panels the palette can go to, by title.
    static let panels = ["Versions", "Storage", "Last memory run", "Sources", "Model and schedule", "Coordination rules", "Access"]

    var body: some View {
        let p = Palette.of(scheme)
        ScrollViewReader { proxy in
            ScrollView(.vertical, showsIndicators: false) {
                VStack(alignment: .leading, spacing: 16) {
                    Label(text: [library.info?.instance?.name ?? "This Mac", library.info.map { "recall-agents " + $0.recall } ?? "", "API on this Mac only"].filter { !$0.isEmpty }.joined(separator: " · "))
                    statsRow(p)
                    VStack(alignment: .leading, spacing: 0) {
                        VersionsPanel(library: library, updates: Updates.shared).id("Versions")
                        Rule()
                        RunPanel(library: library).id("Last memory run")
                        Rule()
                        SourcesPanel(library: library).id("Sources")
                        Rule()
                        ModelPanel(library: library).id("Model and schedule")
                        Rule()
                        CoordinationPanel(library: library).id("Coordination rules")
                        Rule()
                        StoragePanel(library: library).id("Storage")
                        Rule()
                        AccessPanel(library: library).id("Access")
                    }
                    Color.clear.frame(height: 1).id("End")  // the page's foot, for a scroll to the bottom
                }
                // Stats, rows, bars and dividers all stop at this one right edge.
                .frame(maxWidth: 1040, alignment: .leading)
                .padding(.horizontal, 24).padding(.top, 24).padding(.bottom, 24)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .background(CursorScroll(cursor: library.thisMacFocus, proxy: proxy, anchor: .top))
        }
        .background(p.paper)
    }

    func statsRow(_ p: Palette) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(alignment: .top, spacing: 48) {
                stat(count(facts.chatsCount), "chats indexed", p, accent: true)
                stat(count(facts.notesCount), "memory notes", p)
                stat(size(facts.storage?.total_bytes), "on disk", p)
                stat(facts.runs?.latest?.cost_usd.map { String(format: "$%.2f", $0) } ?? "", "last run", p)
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

/// One section of This Mac: its name and a quiet note on the left, the content on the right, a divider between sections.
struct Panel<C: View>: View {
    let title: String
    var note = ""
    /// How tall the first row beside the title is, so the title sits on that row's line.
    var first: CGFloat = RowHeight.data
    @ViewBuilder var content: () -> C
    @Environment(\.colorScheme) var scheme
    var body: some View {
        let p = Palette.of(scheme)
        HStack(alignment: .top, spacing: 32) {
            VStack(alignment: .leading, spacing: 4) {
                Text(title).font(PaperMono.font(14, 520)).foregroundStyle(p.ink).fixedSize(horizontal: false, vertical: true)
                if !note.isEmpty {
                    Text(note).font(PaperMono.font(11)).foregroundStyle(p.muted).fixedSize(horizontal: false, vertical: true)
                }
            }
            .padding(.top, max(0, (first - 18) / 2))
            .frame(width: 180, alignment: .leading)
            VStack(alignment: .leading, spacing: 0) { content() }
                .frame(maxWidth: .infinity, alignment: .leading)
        }
        .font(PaperMono.font(12.5))
        .padding(.vertical, 24)
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

/// Columns that line up across rows, as the board's grid rows do.
struct Cols: View {
    let cells: [AnyView]
    let weights: [CGFloat]
    var top = true
    var height = RowHeight.data
    var body: some View {
        VStack(spacing: 0) {
            if top { Rule() }
            WeightedColumns(weights: weights) {
                ForEach(cells.indices, id: \.self) { i in cells[i].lineLimit(1) }
            }
            .frame(height: height)
        }
        .monospacedDigit()
    }
}

/// Each column's share of the width, the same on every row. A Layout, not a GeometryReader, which would lay
/// every row out a second time on every step of a window resize.
struct WeightedColumns: Layout {
    let weights: [CGFloat]
    var gap: CGFloat = 12

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        CGSize(width: proposal.width ?? 0, height: proposal.height ?? 0)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        let total = weights.reduce(0, +)
        let room = bounds.width - gap * CGFloat(weights.count - 1)
        var x = bounds.minX
        for (i, cell) in subviews.enumerated() where i < weights.count {
            let w = max(0, room * weights[i] / total)
            cell.place(at: CGPoint(x: x, y: bounds.midY), anchor: .leading, proposal: ProposedViewSize(width: w, height: bounds.height))
            x += w + gap
        }
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
        Panel(title: "Sources", note: facts.doctor?.autopilot?.on == true ? "autopilot on" : "", first: RowHeight.head) {
            Cols(cells: ["Agent", "Chats", "Latest chat", "State"].map { AnyView(Label(text: $0)) }, weights: w, top: false, height: RowHeight.head)
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
            Panel(title: "Last memory run", note: "\(clock(start)) · \(duration(run.seconds ?? span))", first: RowHeight.head) {
                RunProblemView(watch: RunWatch.shared, now: RunNow.shared, library: library)
                HStack(spacing: 12) {
                    Label(text: "Step").frame(width: 112, alignment: .leading)
                    HStack {
                        if clock(start) == clock(end) {
                            // Under a minute: one time, not the same one three times (board 3.4, frame 4).
                            Text(clock(start) + " · under a minute").font(PaperMono.font(11)).foregroundStyle(p.muted)
                            Spacer()
                        } else {
                            Label(text: clock(start)); Spacer(); Label(text: clock(start.addingTimeInterval(span / 2))); Spacer(); Label(text: clock(end))
                        }
                    }
                    Label(text: "Result").frame(width: 150, alignment: .trailing)
                }
                .frame(height: RowHeight.head)
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
                        .frame(height: RowHeight.data)
                    }
                    .help(ph.result ?? "")
                }
                Text(footer(run)).font(PaperMono.font(11)).foregroundStyle(p.muted).padding(.top, 8)
                    .fixedSize(horizontal: false, vertical: true)
                if let e = run.error { Text(e).font(PaperMono.font(11)).foregroundStyle(p.accentText).padding(.top, 4) }
            }
        } else {
            Panel(title: "Last memory run") {
                RunProblemView(watch: RunWatch.shared, now: RunNow.shared, library: library)
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
        var calls = "\(run.model_calls ?? 0) call\(run.model_calls == 1 ? "" : "s") to \(run.backend?.split(separator: ":").last.map(String.init) ?? "the model")"
        if (run.batches ?? 0) > 0 { calls += " through the batch API at half price" }
        parts.append(calls)
        if facts.onAPIKey, let usd = facts.config?.schedule?.limits?.max_usd { parts.append(String(format: "budget $%.2f", usd)) }
        return parts.joined(separator: " · ")
    }
}

struct StoragePanel: View {
    let library: Library
    @EnvironmentObject var facts: Facts
    @StateObject var open = Held<Set<String>>([])
    @Environment(\.colorScheme) var scheme
    /// The API's kinds as three things a person decides about: settings are yours, running files get made again.
    static let folded = ["data": "data", "settings": "data", "rebuildable": "rebuildable", "running": "rebuildable", "backup": "backup"]
    static let order = ["data", "rebuildable", "backup"]
    static let names = ["data": "Yours", "rebuildable": "Rebuildable", "backup": "Backups"]
    static let gap: CGFloat = 2

    var body: some View {
        let p = Palette.of(scheme)
        let items = (facts.storage?.items ?? []).filter { $0.exists ?? true }
        let groups = Self.order.map { k in (k, items.filter { (Self.folded[$0.kind] ?? "data") == k }.sorted { $0.bytes > $1.bytes }) }.filter { !$0.1.isEmpty }
        let backups = items.filter { $0.kind == "backup" }
        let backup = backups.reduce(0) { $0 + $1.bytes }
        Panel(title: "Storage", note: size(facts.storage?.total_bytes), first: 12) {
            if items.isEmpty {
                Text("Reading…").foregroundStyle(p.muted)
            } else {
                bar(groups, p).padding(.bottom, 12)
            }
            ForEach(groups, id: \.0) { k, xs in
                row(k, xs, backup, p)
                if open.value.contains(k) { files(xs, p) }
            }
            if !groups.isEmpty { Rule() }
            if facts.confirmRemove, !backups.isEmpty { confirm(backups, backup, p) }
            if let said = facts.removeNote { SaidLine(said: said).padding(.top, 12) }
        }
    }

    /// One line, split by what is actually drawn.
    func bar(_ groups: [(String, [Storage.Item])], _ p: Palette) -> some View {
        GeometryReader { g in
            let widths = Self.widths(groups.map { CGFloat($0.1.reduce(0) { $0 + $1.bytes }) }, in: g.size.width)
            HStack(spacing: Self.gap) {
                ForEach(Array(groups.enumerated()), id: \.element.0) { i, group in
                    swatch(group.0, p).frame(width: widths[i])
                }
            }
        }
        .frame(height: 12)
    }

    /// A sliver gets a floor, taken back from the largest part, so the widths always add up to the bar.
    static func widths(_ bytes: [CGFloat], in width: CGFloat) -> [CGFloat] {
        let room = max(0, width - gap * CGFloat(bytes.count - 1)), sum = max(1, bytes.reduce(0, +))
        var out = bytes.map { max(3, room * $0 / sum) }
        if let big = out.indices.max(by: { out[$0] < out[$1] }) { out[big] -= out.reduce(0, +) - room }
        return out
    }

    @ViewBuilder
    func swatch(_ kind: String, _ p: Palette) -> some View {
        if kind == "backup" {
            Color.clear.overlay(Dotted(color: p.line, dash: [3, 3]))
        } else {
            let lit = kind == "data"
            Rectangle().fill(lit ? p.accentSoft : p.ghost).overlay(Rectangle().strokeBorder(lit ? p.accent : p.line, lineWidth: 1))
        }
    }

    func row(_ k: String, _ xs: [Storage.Item], _ backup: Int, _ p: Palette) -> some View {
        let isOpen = open.value.contains(k)
        let removable = k == "backup" && backup > 0 && !facts.confirmRemove
        return VStack(spacing: 0) {
            Rule()
            ZStack(alignment: .trailing) {
                Button {
                    if isOpen { open.value.remove(k) } else { open.value.insert(k) }
                } label: {
                    HStack(spacing: 12) {
                        swatch(k, p).frame(width: 8, height: 8)
                        Text(Self.names[k] ?? k).foregroundStyle(p.ink).frame(width: 100, alignment: .leading)
                        Text(Self.says(k, xs, isOpen)).foregroundStyle(p.muted).lineLimit(1)
                        Spacer(minLength: 12)
                        if removable { remove(backup).hidden() }
                        Text(size(xs.reduce(0) { $0 + $1.bytes })).foregroundStyle(p.ink).monospacedDigit().frame(width: 72, alignment: .trailing)
                        Text(isOpen ? "▾" : "▸").foregroundStyle(p.muted).frame(width: 14, alignment: .trailing)
                    }
                    .frame(height: RowHeight.control)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .help(isOpen ? "Hide the files" : "Show the files")
                // Beside the row's button, not inside it: a button can't hold another button.
                if removable { remove(backup).padding(.trailing, 72 + 14 + 24) }
            }
        }
    }

    func remove(_ bytes: Int) -> some View {
        WindowButton(title: "Remove · \(size(bytes))", soon: !library.canWrite("storage"), why: library.info == nil ? "" : updateToWrite) {
            facts.removeNote = nil
            facts.confirmRemove = true
        }
    }

    static func says(_ k: String, _ xs: [Storage.Item], _ isOpen: Bool) -> String {
        switch k {
        case "data":
            if isOpen { return "Lost if deleted" }
            let small = xs.count - 2
            return "Lost if deleted: chat index, notes" + (small > 0 ? ", \(small) small file\(small == 1 ? "" : "s")" : "")
        case "backup": return "From before upgrades. Nothing reads them"
        default: return "recall makes these again"
        }
    }

    /// The biggest files by name; past five, the smallest fold into one line.
    func files(_ xs: [Storage.Item], _ p: Palette) -> some View {
        let shown = xs.count > 5 ? Array(xs.prefix(4)) : xs
        let rest = xs.count > 5 ? Array(xs.dropFirst(4)) : []
        return VStack(spacing: 0) {
            ForEach(shown) { file(Self.title($0), $0.path, $0.bytes, ink: true, p) }
            if !rest.isEmpty { file("\(rest.count) small files", "", rest.reduce(0) { $0 + $1.bytes }, ink: false, p) }
        }
    }

    func file(_ name: String, _ path: String, _ bytes: Int, ink: Bool, _ p: Palette) -> some View {
        VStack(spacing: 0) {
            Rule()
            HStack(spacing: 12) {
                Text(name).foregroundStyle(ink ? p.ink : p.muted).lineLimit(1).frame(width: 216, alignment: .leading)
                Text(path).foregroundStyle(p.muted).lineLimit(1).truncationMode(.middle).help(path)
                Spacer(minLength: 0)
                Text(size(bytes)).foregroundStyle(p.muted).monospacedDigit().frame(width: 72, alignment: .trailing)
                Color.clear.frame(width: 14, height: 1)
            }
            .padding(.leading, 24)
            .frame(height: 32)
        }
    }

    static func title(_ i: Storage.Item) -> String {
        i.kind == "backup" ? named(i) : (i.label.components(separatedBy: " (").first ?? i.label)
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
                        .font(PaperMono.font(12.5)).padding(.vertical, 4)
                    }
                }
                Rule()
            }
            Text("Nothing reads these. Your chat index, notes and chat archive stay. This can't be undone.")
                .font(PaperMono.font(11)).foregroundStyle(p.muted).lineSpacing(3).fixedSize(horizontal: false, vertical: true)
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
}

struct ModelPanel: View {
    let library: Library
    @ObservedObject var run = RunNow.shared
    @EnvironmentObject var facts: Facts
    @Environment(\.colorScheme) var scheme
    var body: some View {
        let p = Palette.of(scheme), c = facts.config
        let when = c?.schedule?.enabled == true ? "runs nightly at \(c?.schedule?.time ?? "")" : "no nightly run"
        let now = facts.models?.options.first { $0.now == true }?.label
            ?? [c?.model?.provider, c?.model?.name].compactMap { $0 }.joined(separator: " · ")
        Panel(title: "Model and schedule", note: when, first: RowHeight.control) {
            Cols(cells: [AnyView(Label(text: "Writes notes")), AnyView(modelSelect(now, p))], weights: [0.9, 2], top: false, height: RowHeight.control)
            if let f = facts.models?.fallback ?? c?.model?.fallback { row("If that fails", planWords(f), p) }
            row("Key", c?.model?.key?.configured == true ? "in " + (c?.model?.key?.stored_in ?? "settings").replacingOccurrences(of: "macOS ", with: "") : "none", p)
            if let e = c?.embeddings {
                row("Embeddings", e.on == false ? "off" : [e.provider, e.name, e.provider == "ollama" ? "on this Mac" : nil].compactMap { $0 }.joined(separator: " · "), p)
            }
            if let l = c?.schedule?.limits {
                row("Limits", [l.max_minutes.map { "\(Int($0)) min" }, l.max_calls.map { "\($0) calls" }, facts.onAPIKey ? l.max_usd.map { String(format: "$%.2f a night", $0) } : nil].compactMap { $0 }.joined(separator: " · "), p)
            }
            if let b = c?.power?.on_battery { row("On battery", b == "defer" ? "waits for power" : b, p) }
            // Read-only: changing it rewrites a background job, so it stays `recall schedule` in Terminal.
            Cols(cells: [AnyView(Label(text: "Schedule")), AnyView(
                Text("\(Text(c?.schedule?.enabled == true ? "nightly at \(c?.schedule?.time ?? "") · " : "off · ").foregroundColor(p.muted))\(Text("recall schedule").foregroundColor(p.body))")
                .help("Change it with recall schedule in Terminal: it rewrites a background job"))], weights: [0.9, 2])
            Rule()
            if let said = facts.modelNote { SaidLine(said: said).padding(.top, 12) }
            if library.info != nil, !library.canWrite("model") {
                Text(updateToWrite).font(PaperMono.font(11)).foregroundStyle(p.muted).padding(.top, 12)
            }
            HStack(spacing: 8) {
                let busy = run.busy
                WindowButton(title: busy ? "Running…" : "Run now", soon: busy || now.isEmpty, why: busy ? "A run is going" : "Needs a model first") {
                    library.runNow()
                }
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
        .font(PaperMono.font(12.5))
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
        HStack(alignment: .firstTextBaseline, spacing: 8) {
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
        .font(PaperMono.font(11)).foregroundStyle(p.muted)
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
        Panel(title: "Coordination rules", note: "when two chats touch the same work", first: RowHeight.control) {
            if writes {
                HStack(spacing: 12) {
                    Label(text: "Preset")
                    Spacer()
                    MenuButton(popups: library.popups, width: 360, menu: { presetMenu(c) }) {
                        SelectBox(text: c.map { $0.preset.flatMap { Self.presetNames[$0] } ?? "Custom" } ?? "")
                    }
                    .frame(width: 220)
                }
                .frame(height: RowHeight.control)
            }
            ForEach(c?.rules ?? []) { r in
                VStack(spacing: 0) {
                    Rule()
                    HStack(spacing: 0) {
                        Label(text: "When").frame(width: 64, alignment: .leading)
                        Text(Self.when[r.situation] ?? r.meaning ?? r.situation).foregroundStyle(p.ink).lineLimit(1)
                        Spacer(minLength: 12)
                        Label(text: "Then").padding(.trailing, 12)
                        if writes {
                            MenuButton(popups: library.popups, width: 320, menu: { actionMenu(r) }) {
                                SelectBox(text: r.action, strong: r.action != r.default)
                            }
                            .frame(width: 104)
                            .help(r.meaning ?? "")
                        } else {
                            Text(r.action).foregroundStyle(p.ink)
                                .padding(.horizontal, 8).frame(width: 104, height: 28, alignment: .leading)
                                .overlay(Dotted(color: p.faint, dash: [3, 3]))
                                .help((r.meaning ?? "") + (r.action == r.default ? " · the default" : "") + (library.info == nil ? "" : " · " + updateToWrite))
                        }
                    }
                    .frame(height: RowHeight.control)
                }
            }
            if writes {
                if let said = facts.coordNote {
                    SaidLine(said: said, right: c?.preset == "recommended" ? nil : AnyView(back(p))).padding(.top, 12)
                } else if let c, c.preset != "recommended" {
                    HStack { Spacer(); back(p) }.padding(.top, 12)
                }
            } else if library.info != nil {
                Text(updateToWrite).font(PaperMono.font(11)).foregroundStyle(p.muted).padding(.top, 12)
            }
        }
    }

    func back(_ p: Palette) -> some View {
        Button { library.setCoordination(preset: "recommended") } label: {
            Text("Back to Recommended").font(PaperMono.font(11)).foregroundStyle(p.ink).contentShape(Rectangle())
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
    func useModel(_ o: Models.Option, accepting: String? = nil) {
        facts.modelNote = .working("Testing with one short call…")
        var body: [String: Any] = ["option": o.id]
        if let accepting { body["accept_destination"] = accepting }
        API.send("PUT", "/v1/model", body, ModelSaved.self, timeout: 300) { [weak self] r in
            guard let self else { return }
            switch r {
            case .success(let s): self.facts.modelNote = .done("Saved. Tonight's run uses \(s.label).")
            case .failure(let f) where f.code == "needs_consent" && accepting == nil && f.destination != nil:
                // Going from a model that keeps everything here to one that sends excerpts away asks first (board H2).
                self.facts.modelNote = nil
                self.askConsent(o, destination: f.destination ?? "")
            case .failure(let f): self.facts.modelNote = .failed("That didn't work: \(f.message). Nothing changed.")
            }
            self.fetch("/v1/config", Config.self) { self.facts.config = $0 }
            self.fetch("/v1/models", Models.self) { self.facts.models = $0 }
        }
    }

    /// The sheet that asks before excerpts start leaving this Mac. Test and switch sends the destination back as accepted.
    func askConsent(_ o: Models.Option, destination: String) {
        guard !popups.isOpen, let w = popups.parent else { return }
        let scheme: ColorScheme = w.effectiveAppearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua ? .dark : .light
        let go = { [weak self] in
            self?.popups.close()
            self?.useModel(o, accepting: destination)
        }
        popups.show(ConsentSheet(destination: destination, cancel: { [weak self] in self?.popups.close() }, go: go).environment(\.colorScheme, scheme),
                    frame: w.frame, keys: { e in
            if e.keyCode == 36 || e.keyCode == 76 { go(); return true }
            return false
        })
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
            // "Serve this memory to other machines" comes back with server mode (hidden 11 Nov until it's built).
        }
    }
}

/// Board H2: "Send chat excerpts to Anthropic?" Right now nothing leaves this Mac.
struct ConsentSheet: View {
    let destination: String
    var cancel: () -> Void
    var go: () -> Void
    @Environment(\.colorScheme) var scheme

    var body: some View {
        let p = Palette.of(scheme)
        // "Anthropic, under your Claude plan" → "Anthropic".
        let who = destination.split(separator: ",").first.map { String($0).trimmingCharacters(in: .whitespaces) } ?? destination
        ZStack(alignment: .top) {
            p.scrim.contentShape(Rectangle()).onTapGesture { cancel() }
            VStack(alignment: .leading, spacing: 12) {
                Label(text: "This Mac · model")
                Text("Send chat excerpts to \(who)?").font(PaperMono.font(14, 520)).foregroundStyle(p.ink).lineSpacing(4)
                    .fixedSize(horizontal: false, vertical: true)
                Text("Right now nothing leaves this Mac.").foregroundStyle(p.body)
                if who != destination { Text(destination).font(PaperMono.font(11)).foregroundStyle(p.muted) }
                HStack(spacing: 8) {
                    Spacer(minLength: 8)
                    WindowButton(title: "Cancel", keys: "esc", action: cancel)
                    WindowButton(title: "Test and switch", style: .primary, keys: "↵", action: go)
                }
                .padding(.top, 4)
            }
            .padding(.horizontal, 24).padding(.top, 24).padding(.bottom, 16)
            .frame(width: 470, alignment: .leading)
            .background(p.pop)
            .overlay(Rectangle().strokeBorder(p.line, lineWidth: 1))
            .padding(.top, 150)
        }
        .font(PaperMono.font(12.5))
    }
}
