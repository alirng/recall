// The panel the menu bar icon opens: what needs you and what is running, drawn in the site's
// grammar (rererecall.com): ink on paper, one-pixel lines, square corners, no shadows, Paper
// Mono, amber only for recall, each agent in its own colour, hollow diamonds for chats.

import AppKit
import SwiftUI

// MARK: - Look

struct Palette {
    let paper, card, ink, body, muted, faint, ghost, line, grid, lane, accent, accentText, claude, codex: Color

    static func of(_ scheme: ColorScheme) -> Palette {
        if scheme == .light {
            let ink = Color(hex: 0x141416)
            return Palette(
                paper: Color(hex: 0xFAFAFA), card: .white, ink: ink, body: ink.opacity(0.78), muted: ink.opacity(0.62),
                faint: ink.opacity(0.32), ghost: ink.opacity(0.14), line: ink.opacity(0.3), grid: ink.opacity(0.12),
                lane: ink.opacity(0.5), accent: Color(hex: 0xF2C14E), accentText: Color(hex: 0xA8780A),
                claude: Color(hex: 0xD26A3C), codex: Color(hex: 0x2F6FB3)
            )
        }
        let ink = Color(hex: 0xE8E6DF)
        return Palette(
            paper: Color(hex: 0x101012), card: Color(hex: 0x151518), ink: ink, body: ink.opacity(0.76), muted: ink.opacity(0.56),
            faint: ink.opacity(0.3), ghost: ink.opacity(0.16), line: ink.opacity(0.28), grid: ink.opacity(0.12),
            lane: ink.opacity(0.5), accent: Color(hex: 0xF2C14E), accentText: Color(hex: 0xF2C14E),
            claude: Color(hex: 0xE07A4C), codex: Color(hex: 0x6FAEE6)
        )
    }

    func agent(_ name: String) -> Color {
        switch name {
        case "claude": return claude
        case "codex": return codex
        case "cursor": return ink
        default: return muted
        }
    }
}

extension Color {
    init(hex: UInt32) {
        self.init(red: Double(hex >> 16 & 0xFF) / 255, green: Double(hex >> 8 & 0xFF) / 255, blue: Double(hex & 0xFF) / 255)
    }
}

/// Paper Mono, the site's typeface (SIL OFL, bundled in Resources), at an exact weight on its
/// variable axis. Prose turns on ss02 and ss03 as the site does.
enum PaperMono {
    static func register() {
        guard let url = Bundle.main.url(forResource: "PaperMono", withExtension: "ttf") else { return }
        CTFontManagerRegisterFontsForURL(url as CFURL, .process, nil)
    }

    static func font(_ size: CGFloat, _ weight: CGFloat = 390, prose: Bool = false) -> Font {
        var attrs: [NSFontDescriptor.AttributeName: Any] = [
            .family: "Paper Mono",
            .variation: [NSNumber(value: 0x7767_6874): weight],  // 'wght'
        ]
        if prose {
            attrs[.featureSettings] = [4, 6].map {
                [NSFontDescriptor.FeatureKey.typeIdentifier: 35, NSFontDescriptor.FeatureKey.selectorIdentifier: $0]
            }
        }
        let font = NSFont(descriptor: NSFontDescriptor(fontAttributes: attrs), size: size)
        return Font(font ?? .monospacedSystemFont(ofSize: size, weight: .regular))
    }
}

// MARK: - State and actions

final class Store: ObservableObject {
    @Published var status: Status?
    @Published var error: String?
    @Published var updated: Date?
    @Published var sending: Set<String> = []  // overlaps whose Resolve is on its way
}

struct Actions {
    var refresh: () -> Void = {}
    var resolve: (Overlap) -> Void = { _ in }
    var showChanges: (Overlap, [String: String]) -> Void = { _, _ in }
    var quit: () -> Void = {}
}

// MARK: - Pieces

struct Label: View {
    let text: String
    var color: Color? = nil
    @Environment(\.colorScheme) var scheme
    var body: some View {
        Text(text.uppercased()).font(PaperMono.font(10.5, 500)).tracking(0.84)
            .foregroundStyle(color ?? Palette.of(scheme).muted)
    }
}

struct Diamond: Shape {
    func path(in r: CGRect) -> Path {
        Path { p in
            p.move(to: CGPoint(x: r.midX, y: r.minY))
            p.addLine(to: CGPoint(x: r.maxX, y: r.midY))
            p.addLine(to: CGPoint(x: r.midX, y: r.maxY))
            p.addLine(to: CGPoint(x: r.minX, y: r.midY))
            p.closeSubpath()
        }
    }
}

/// A chat: a hollow diamond in its agent's colour, filled while it is live.
struct ChatMark: View {
    let agent: String
    let live: Bool
    @Environment(\.colorScheme) var scheme
    var body: some View {
        let p = Palette.of(scheme), c = p.agent(agent)
        ZStack {
            Diamond().fill(live ? c : p.paper)
            Diamond().stroke(c, lineWidth: 1)
        }.frame(width: 8, height: 8)
    }
}

/// Hover state without @State, whose macro plugin the Command Line Tools do not ship.
final class Hover: ObservableObject {
    @Published var on = false
}

struct BoxButton: View {
    let title: String
    var action: () -> Void
    @ObservedObject var hover = Hover()
    @Environment(\.colorScheme) var scheme
    var body: some View {
        let p = Palette.of(scheme)
        Button(action: action) {
            Text(title.uppercased()).font(PaperMono.font(10.5, 500)).tracking(0.6)
                .foregroundStyle(hover.on ? p.ink : p.muted)
                .padding(.horizontal, 8).padding(.vertical, 5)
                .overlay(Rectangle().stroke(hover.on ? p.line : p.faint, lineWidth: 1))
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .onHover { hover.on = $0 }
    }
}

// MARK: - Panel

struct PanelView: View {
    @ObservedObject var store: Store
    var actions: Actions
    @Environment(\.colorScheme) var scheme

    var body: some View {
        let p = Palette.of(scheme)
        VStack(alignment: .leading, spacing: 0) {
            header(p)
            rule(p)
            if let error = store.error {
                Text(error).font(PaperMono.font(12)).foregroundStyle(p.muted).padding(16)
                rule(p)
            }
            if let status = store.status {
                needsYou(status, p)
                rule(p)
                WorkingNow(sessions: status.sessions.filter(\.live))
                    .padding(.horizontal, 16).padding(.vertical, 14)
            } else if store.error == nil {
                Text("Reading your agents…").font(PaperMono.font(12)).foregroundStyle(p.muted).padding(16)
            }
            rule(p)
            HStack {
                Spacer()
                BoxButton(title: "Quit", action: actions.quit)
            }.padding(.horizontal, 16).padding(.vertical, 10)
        }
        .frame(width: 360)
        .background(p.paper)
        .overlay(Rectangle().stroke(p.line, lineWidth: 1))
    }

    func rule(_ p: Palette) -> some View { Rectangle().fill(p.grid).frame(height: 1) }

    func header(_ p: Palette) -> some View {
        HStack(spacing: 8) {
            Image(nsImage: markImage(size: 15)).renderingMode(.template).foregroundStyle(p.ink)
            Text("recall").font(PaperMono.font(15, 600)).foregroundStyle(p.ink)
            Spacer()
            if let updated = store.updated {
                Label(text: updated.formatted(date: .omitted, time: .shortened))
            }
            BoxButton(title: "↻", action: actions.refresh).help("Refresh")
        }
        .padding(.horizontal, 16).padding(.vertical, 12)
    }

    @ViewBuilder
    func needsYou(_ status: Status, _ p: Palette) -> some View {
        let byId = Dictionary(uniqueKeysWithValues: status.sessions.map { ($0.id, $0) })
        VStack(alignment: .leading, spacing: 10) {
            let count = status.overlaps.count + status.restarts.count
            HStack {
                Label(text: "Needs you")
                Spacer()
                if count > 0 { Label(text: "\(count)") }
            }
            if count == 0 {
                Text("Nothing needs you. No two chats have open changes to the same file.")
                    .font(PaperMono.font(12.5, 390, prose: true)).foregroundStyle(p.muted)
                    .fixedSize(horizontal: false, vertical: true)
            }
            ForEach(status.restarts, id: \.agent) { r in
                VStack(alignment: .leading, spacing: 2) {
                    Text("Restart \(r.agent)").font(PaperMono.font(12.5, 500)).foregroundStyle(p.ink)
                    Text(r.detail).font(PaperMono.font(11.5)).foregroundStyle(p.muted)
                }
            }
            ForEach(status.overlaps.prefix(4), id: \.id) { o in
                OverlapCard(overlap: o, byId: byId, actions: actions, sending: store.sending.contains(o.id))
            }
            if status.overlaps.count > 4 {
                Text("and \(status.overlaps.count - 4) more").font(PaperMono.font(11.5)).foregroundStyle(p.muted)
            }
        }
        .padding(.horizontal, 16).padding(.vertical, 14)
    }
}

extension Overlap {
    var verdict: String {
        if let c = conflict { return c ? "will conflict" : "merges clean" }
        switch situation {
        case "apart": return "other lines"
        case "same_copy": return "same lines"
        default: return "open in both"
        }
    }
}

struct OverlapCard: View {
    let overlap: Overlap
    let byId: [String: Session]
    let actions: Actions
    var sending = false
    @Environment(\.colorScheme) var scheme

    var body: some View {
        let p = Palette.of(scheme), act = overlap.level == "act"
        let name = (overlap.file as NSString).lastPathComponent
        let where_ = overlap.same_checkout ? "same working copy" : overlap.branches.map { short($0, 14) }.joined(separator: " ↔ ")
        VStack(alignment: .leading, spacing: 9) {
            HStack(alignment: .center, spacing: 8) {
                if act {
                    Rectangle().fill(p.accent).frame(width: 7, height: 7)
                } else {
                    Diamond().stroke(p.lane, lineWidth: 1).frame(width: 8, height: 8)
                }
                Text(name).font(PaperMono.font(13, 520)).foregroundStyle(p.ink).lineLimit(1)
                Spacer(minLength: 8)
                Label(text: overlap.verdict, color: act ? p.accentText : p.muted)
                    .padding(.horizontal, 6).padding(.vertical, 3)
                    .overlay(Rectangle().stroke(act ? p.accent.opacity(0.7) : p.faint, lineWidth: 1))
            }
            Text("\(overlap.repo) · \(where_)").font(PaperMono.font(11.5)).foregroundStyle(p.muted).lineLimit(1)
            VStack(alignment: .leading, spacing: 6) {
                ForEach(overlap.sessions, id: \.self) { id in
                    if let s = byId[id] {
                        HStack(spacing: 8) {
                            ChatMark(agent: s.agent, live: s.live)
                            Text(s.title).font(PaperMono.font(12.5, 390, prose: true)).foregroundStyle(p.body).lineLimit(1)
                            Spacer(minLength: 6)
                            Text(ago(s.age_seconds)).font(PaperMono.font(11)).foregroundStyle(p.muted)
                        }
                    }
                }
            }
            if let letters = overlap.messages, !letters.isEmpty {
                Conversation(letters: Array(letters.suffix(3)), byId: byId)
            }
            HStack(spacing: 6) {
                if let at = overlap.sent_at {
                    Label(text: "Sent · " + ago(Int(Date().timeIntervalSince1970 - at)), color: p.ink)
                        .padding(.horizontal, 8).padding(.vertical, 5)
                        .overlay(Rectangle().stroke(p.faint, style: StrokeStyle(lineWidth: 1, dash: [3, 3])))
                        .help("recall messaged every chat here; it clears when the overlap does")
                } else if sending {
                    Label(text: "Sending…").padding(.horizontal, 8).padding(.vertical, 5)
                } else {
                    BoxButton(title: "Resolve") { actions.resolve(overlap) }
                        .help("Message every chat here to agree who goes first, at their next turn")
                }
                BoxButton(title: overlap.same_checkout ? "Show changes" : "Show both changes") {
                    actions.showChanges(overlap, byId.mapValues(\.title))
                }
            }
        }
        .padding(12)
        .background(p.card)
        .overlay(Rectangle().stroke(act ? p.accent.opacity(0.55) : p.line, lineWidth: 1))
    }
}

/// What two chats said to each other, behind a dotted rule as the site marks references.
struct Conversation: View {
    let letters: [Letter]
    let byId: [String: Session]
    @Environment(\.colorScheme) var scheme

    var body: some View {
        let p = Palette.of(scheme), now = Date().timeIntervalSince1970
        HStack(alignment: .top, spacing: 10) {
            Rectangle().fill(.clear).frame(width: 1)
                .overlay(Path { $0.move(to: .zero); $0.addLine(to: CGPoint(x: 0, y: 2000)) }
                    .stroke(p.faint, style: StrokeStyle(lineWidth: 1, dash: [1, 2.5])))
                .clipped()
            VStack(alignment: .leading, spacing: 8) {
                ForEach(letters, id: \.at) { m in
                    VStack(alignment: .leading, spacing: 3) {
                        HStack(spacing: 5) {
                            Text(m.from == "recall" ? "recall" : agentNames[byId[m.from]?.agent ?? ""] ?? "chat")
                                .foregroundStyle(m.from == "recall" ? p.accentText : p.agent(byId[m.from]?.agent ?? ""))
                            Text("→").foregroundStyle(p.faint)
                            Text(agentNames[byId[m.to]?.agent ?? ""] ?? "chat").foregroundStyle(p.agent(byId[m.to]?.agent ?? ""))
                            Text("· " + ago(Int(now - m.at))).foregroundStyle(p.muted)
                        }.font(PaperMono.font(10.5, 500))
                        Text(m.text).font(PaperMono.font(12, 390, prose: true)).foregroundStyle(p.body)
                            .lineLimit(3).fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
        }
    }
}

/// Live chats per repo as a git log: one lane per branch, the default branch on the left and
/// the others forking off it, each chat a node on its branch in its agent's colour.
struct WorkingNow: View {
    let sessions: [Session]
    @Environment(\.colorScheme) var scheme
    static let row: CGFloat = 36, lane: CGFloat = 14

    var body: some View {
        let p = Palette.of(scheme)
        let repos = Dictionary(grouping: sessions, by: { $0.repo ?? "no folder" })
        let order = sessions.map { $0.repo ?? "no folder" }.reduce(into: [String]()) { if !$0.contains($1) { $0.append($1) } }
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Label(text: "Working now")
                Spacer()
                Label(text: "\(sessions.count) live")
            }
            if sessions.isEmpty {
                Text("No agent is working right now.").font(PaperMono.font(12.5, 390, prose: true)).foregroundStyle(p.muted)
            }
            ForEach(order, id: \.self) { repo in
                RepoGraph(repo: repo, chats: repos[repo] ?? [])
            }
        }
    }
}

struct RepoGraph: View {
    let repo: String
    let chats: [Session]
    @Environment(\.colorScheme) var scheme

    var body: some View {
        let p = Palette.of(scheme)
        let branches = lanes()
        let rows = chats.sorted { (branches.firstIndex(of: $0.branch ?? "?") ?? 0, $0.age_seconds) < (branches.firstIndex(of: $1.branch ?? "?") ?? 0, $1.age_seconds) }
        let width = WorkingNow.lane * CGFloat(max(branches.count, 1)) + 4
        VStack(alignment: .leading, spacing: 4) {
            Text(repo).font(PaperMono.font(12.5, 520)).foregroundStyle(p.ink)
            ZStack(alignment: .topLeading) {
                Canvas { ctx, _ in draw(ctx, p, branches, rows) }
                    .frame(width: width, height: WorkingNow.row * CGFloat(rows.count))
                VStack(alignment: .leading, spacing: 0) {
                    ForEach(rows, id: \.id) { s in
                        VStack(alignment: .leading, spacing: 2) {
                            Text(s.title).font(PaperMono.font(12.5, 390, prose: true)).foregroundStyle(p.body).lineLimit(1)
                            Text([agentNames[s.agent] ?? s.agent, short(s.branch, 22), ago(s.age_seconds)].filter { !$0.isEmpty }.joined(separator: " · "))
                                .font(PaperMono.font(11)).foregroundStyle(p.muted).lineLimit(1)
                        }
                        .frame(height: WorkingNow.row, alignment: .leading)
                    }
                }
                .padding(.leading, width + 8)
            }
        }
    }

    /// The default branch first, then the others in order of their newest chat.
    func lanes() -> [String] {
        var out: [String] = []
        for b in ["main", "master"] where chats.contains(where: { $0.branch == b }) { out.append(b) }
        for s in chats.sorted(by: { $0.age_seconds < $1.age_seconds }) where !out.contains(s.branch ?? "?") {
            out.append(s.branch ?? "?")
        }
        return out
    }

    func draw(_ ctx: GraphicsContext, _ p: Palette, _ branches: [String], _ rows: [Session]) {
        let h = WorkingNow.row, x = { (i: Int) in 4 + CGFloat(i) * WorkingNow.lane }
        let trunk = branches.first.map { ["main", "master"].contains($0) } ?? false
        for (i, b) in branches.enumerated() {
            let ys = rows.enumerated().filter { ($0.element.branch ?? "?") == b }.map { CGFloat($0.offset) * h + h / 2 }
            guard let first = ys.first, let last = ys.last else { continue }
            var line = Path()
            if i == 0 && trunk {
                line.move(to: CGPoint(x: x(0), y: 0))
                line.addLine(to: CGPoint(x: x(0), y: CGFloat(rows.count) * h))
            } else {
                // A branch forks off the default branch's lane in an S-curve above its first chat.
                let start = max(first - h * 0.8, 0), end = first - 7
                if trunk {
                    let mid = (start + end) / 2
                    line.move(to: CGPoint(x: x(0), y: start))
                    line.addCurve(to: CGPoint(x: x(i), y: end), control1: CGPoint(x: x(0), y: mid), control2: CGPoint(x: x(i), y: mid))
                } else {
                    line.move(to: CGPoint(x: x(i), y: start))
                }
                line.addLine(to: CGPoint(x: x(i), y: last))
            }
            ctx.stroke(line, with: .color(p.lane), lineWidth: 1)
        }
        for (r, s) in rows.enumerated() {
            let i = branches.firstIndex(of: s.branch ?? "?") ?? 0
            let rect = CGRect(x: x(i) - 4, y: CGFloat(r) * h + h / 2 - 4, width: 8, height: 8)
            let d = Diamond().path(in: rect), c = p.agent(s.agent)
            ctx.fill(d, with: .color(s.live ? c : p.paper))
            ctx.stroke(d, with: .color(c), lineWidth: 1)
        }
    }
}

// MARK: - Window

/// A borderless panel under the menu bar icon. It closes on Escape or a click elsewhere.
final class PanelController {
    let store: Store
    let actions: Actions
    var window: NSPanel?
    var monitors: [Any] = []
    static let maxHeight: CGFloat = 620

    init(store: Store, actions: Actions) {
        self.store = store
        self.actions = actions
    }

    var isOpen: Bool { window?.isVisible ?? false }

    func toggle(below button: NSStatusBarButton) {
        isOpen ? close() : open(below: button)
    }

    func open(below button: NSStatusBarButton) {
        let host = NSHostingView(rootView: AnyView(ScrollView(.vertical, showsIndicators: false) {
            PanelView(store: store, actions: actions)
        }.scrollBounceBehavior(.basedOnSize)))
        let panel = NSPanel(contentRect: .zero, styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        panel.isFloatingPanel = true
        panel.level = .popUpMenu
        panel.backgroundColor = .clear
        panel.hasShadow = false
        panel.contentView = host
        window = panel
        place(below: button)
        panel.orderFrontRegardless()
        panel.makeKey()
        monitors.append(NSEvent.addGlobalMonitorForEvents(matching: [.leftMouseDown, .rightMouseDown]) { [weak self] _ in
            self?.close()
        } as Any)
        monitors.append(NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] e in
            if e.keyCode == 53 { self?.close(); return nil }
            return e
        } as Any)
    }

    /// Size the panel to its content (up to maxHeight, then it scrolls) with its top under the icon.
    func place(below button: NSStatusBarButton? = nil) {
        guard let panel = window else { return }
        let natural = NSHostingView(rootView: PanelView(store: store, actions: actions)).fittingSize
        let size = NSSize(width: natural.width, height: min(natural.height, Self.maxHeight))
        var top = panel.frame.maxY, left = panel.frame.minX
        if let button, let w = button.window {
            let r = w.convertToScreen(button.convert(button.bounds, to: nil))
            top = r.minY - 4
            left = r.minX
            if let screen = w.screen ?? NSScreen.main {
                left = min(left, screen.visibleFrame.maxX - size.width - 8)
            }
        }
        panel.setFrame(NSRect(x: left, y: top - size.height, width: size.width, height: size.height), display: true)
    }

    func close() {
        monitors.forEach { NSEvent.removeMonitor($0) }
        monitors = []
        window?.orderOut(nil)
        window = nil
    }
}

// MARK: - Preview

/// `Recall --render-panel out.png [demo]` draws the panel in dark and light side by side, with
/// live data or a fixed demo, to judge it without opening the menu bar.
@MainActor
func renderPanel(to path: String, status: Status?) {
    PaperMono.register()
    let store = Store()
    store.status = status
    store.updated = Date()
    let pair = HStack(alignment: .top, spacing: 24) {
        PanelView(store: store, actions: Actions()).environment(\.colorScheme, .dark)
        PanelView(store: store, actions: Actions()).environment(\.colorScheme, .light)
    }
    .padding(24)
    .background(Color(hex: 0x6E6E73))
    let renderer = ImageRenderer(content: pair)
    renderer.scale = 2
    guard let image = renderer.nsImage, let tiff = image.tiffRepresentation,
          let png = NSBitmapImageRep(data: tiff)?.representation(using: .png, properties: [:]) else { return }
    try? png.write(to: URL(fileURLWithPath: path))
}

let demoStatus = """
{"level":"act","sessions":[
 {"id":"claude:a","agent":"claude","title":"Fix the login redirect","repo":"harbor","branch":"main","age_seconds":40,"live":true},
 {"id":"codex:b","agent":"codex","title":"Restyle the sign-in page","repo":"harbor","branch":"new-login","age_seconds":180,"live":true},
 {"id":"cursor:c","agent":"cursor","title":"Rate-limit the session API","repo":"harbor","branch":"main","age_seconds":420,"live":true},
 {"id":"claude:d","agent":"claude","title":"Draft the changelog","repo":"harbor-site","branch":"main","age_seconds":90,"live":true},
 {"id":"codex:e","agent":"codex","title":"Move docs to the new theme","repo":"harbor-site","branch":"theme-2026","age_seconds":600,"live":true}],
"overlaps":[
 {"repo":"harbor","file":"src/auth/session.py","sessions":["claude:a","codex:b"],"same_checkout":false,"branches":["main","new-login"],
  "conflict":true,"situation":"conflict","level":"act","sides":[],"notes":{"claude:a":"…","codex:b":"…"},"sent_at":\(Date().timeIntervalSince1970 - 120),
  "messages":[{"at":\(Date().timeIntervalSince1970 - 150),"from":"codex:b","to":"claude:a","text":"I'm rewriting the redirect check in session.py lines 40–72. Are you in the same function?"},
              {"at":\(Date().timeIntervalSince1970 - 60),"from":"claude:a","to":"codex:b","text":"Yes, lines 52–60. Go first; I'll rebase onto your branch once it lands."}]},
 {"repo":"harbor","file":"src/api/limits.py","sessions":["claude:a","cursor:c"],"same_checkout":true,"branches":["main"],
  "conflict":null,"situation":"apart","level":"watch","sides":[],"notes":{"claude:a":"…","cursor:c":"…"},"messages":[]}]}
"""
