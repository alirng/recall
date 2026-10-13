// recall in the menu bar: what needs you across your coding agents, and what they are working on.
// It runs `recall status --json` (the command is set in Info.plist by build.sh) and shows the result.

import AppKit

// MARK: - Data

struct Status: Decodable {
    let level: String
    let sessions: [Session]
    let overlaps: [Overlap]
}

struct Session: Decodable {
    let id: String
    let agent: String
    let title: String
    let repo: String?
    let branch: String?
    let age_seconds: Int
    let live: Bool
}

struct Side: Decodable {
    let session: String
    let checkout: String
    let branch: String?
    let base: String?
}

struct Overlap: Decodable {
    let repo: String
    let file: String
    let sessions: [String]
    let same_checkout: Bool
    let branches: [String]
    let conflict: Bool?
    let level: String
    let sides: [Side]
    let notes: [String: String]
}

let agentNames = ["claude": "Claude Code", "codex": "Codex", "cursor": "Cursor"]

func ago(_ s: Int) -> String {
    s < 60 ? "just now" : s < 3600 ? "\(s / 60)m ago" : s < 86400 ? "\(s / 3600)h ago" : "\(s / 86400)d ago"
}

func clip(_ s: String, _ n: Int) -> String {
    s.count <= n ? s : String(s.prefix(n - 1)) + "…"
}

// MARK: - The mark

// recall's accent: amber, darker on a light menu bar so it holds its contrast.
let amber = NSColor(name: nil) { a in
    a.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua
        ? NSColor(srgbRed: 0xF2 / 255, green: 0xC1 / 255, blue: 0x4E / 255, alpha: 1)
        : NSColor(srgbRed: 0xA8 / 255, green: 0x78 / 255, blue: 0x0A / 255, alpha: 1)
}

// How much needs you, from `recall status`: nothing; a file two agents edited that merges
// clean or that one of them has left (the outer leaves light); a real conflict, or two live
// agents in one working tree (the whole mark lights).
enum Level: String { case none, watch, act }

// The site's logomark: a block over a narrow stalk with filleted shoulders, three lines cut
// down the stalk, the outer two bending out to the block's edges. Drawn on a 32-unit grid.
// The site turns the SVG 180°; in AppKit's y-up space that turn cancels the flip, and the
// mark is symmetric, so the SVG's own coordinates draw it the right way up.
func drawMark(fill: NSColor, leaves: NSColor? = nil, line: CGFloat = 1.5) {
    guard let ctx = NSGraphicsContext.current else { return }
    let k: CGFloat = 1.6569  // 3 × 0.5523: a quarter circle of radius 3 as one cubic
    let block = NSBezierPath()
    block.move(to: NSPoint(x: 2, y: 30))
    block.line(to: NSPoint(x: 2, y: 16))
    block.line(to: NSPoint(x: 9, y: 16))
    block.curve(to: NSPoint(x: 12, y: 13), controlPoint1: NSPoint(x: 9 + k, y: 16), controlPoint2: NSPoint(x: 12, y: 13 + k))
    block.line(to: NSPoint(x: 12, y: 2))
    block.line(to: NSPoint(x: 20, y: 2))
    block.line(to: NSPoint(x: 20, y: 13))
    block.curve(to: NSPoint(x: 23, y: 16), controlPoint1: NSPoint(x: 20, y: 13 + k), controlPoint2: NSPoint(x: 23 - k, y: 16))
    block.line(to: NSPoint(x: 30, y: 16))
    block.line(to: NSPoint(x: 30, y: 30))
    block.close()
    fill.setFill()
    block.fill()

    // The outer leaves: everything outside the two bending lines, split along their centres
    // so the cut lines below become the seams.
    if let leaves {
        let outer = NSBezierPath()
        outer.move(to: NSPoint(x: 13.5, y: 0))
        outer.line(to: NSPoint(x: 13.5, y: 19))
        outer.curve(to: NSPoint(x: 10.5, y: 22), controlPoint1: NSPoint(x: 13.5, y: 19 + k), controlPoint2: NSPoint(x: 10.5 + k, y: 22))
        outer.line(to: NSPoint(x: 0, y: 22))
        outer.line(to: NSPoint(x: 0, y: 0))
        outer.close()
        outer.move(to: NSPoint(x: 18.5, y: 0))
        outer.line(to: NSPoint(x: 18.5, y: 19))
        outer.curve(to: NSPoint(x: 21.5, y: 22), controlPoint1: NSPoint(x: 18.5, y: 19 + k), controlPoint2: NSPoint(x: 21.5 - k, y: 22))
        outer.line(to: NSPoint(x: 32, y: 22))
        outer.line(to: NSPoint(x: 32, y: 0))
        outer.close()
        NSGraphicsContext.saveGraphicsState()
        outer.addClip()
        leaves.setFill()
        block.fill()
        NSGraphicsContext.restoreGraphicsState()
    }

    let lines = NSBezierPath()
    lines.move(to: NSPoint(x: 16, y: 2))
    lines.line(to: NSPoint(x: 16, y: 30))
    lines.move(to: NSPoint(x: 13.5, y: 2))
    lines.line(to: NSPoint(x: 13.5, y: 19))
    lines.curve(to: NSPoint(x: 10.5, y: 22), controlPoint1: NSPoint(x: 13.5, y: 19 + k), controlPoint2: NSPoint(x: 10.5 + k, y: 22))
    lines.line(to: NSPoint(x: 2, y: 22))
    lines.move(to: NSPoint(x: 18.5, y: 2))
    lines.line(to: NSPoint(x: 18.5, y: 19))
    lines.curve(to: NSPoint(x: 21.5, y: 22), controlPoint1: NSPoint(x: 18.5, y: 19 + k), controlPoint2: NSPoint(x: 21.5 - k, y: 22))
    lines.line(to: NSPoint(x: 30, y: 22))
    lines.lineWidth = line
    ctx.compositingOperation = .destinationOut  // cut the lines out, as the site's mask does
    lines.stroke()

    ctx.compositingOperation = .sourceOver
}

func markImage(size: CGFloat, level: Level = .none) -> NSImage {
    let img = NSImage(size: NSSize(width: size, height: size), flipped: false) { _ in
        let t = NSAffineTransform()
        t.scale(by: size / 32)
        t.concat()
        // Drawn when shown, so labelColor and amber resolve against the menu bar's own appearance.
        switch level {
        case .none: drawMark(fill: .black)
        case .watch: drawMark(fill: .labelColor, leaves: amber)
        case .act: drawMark(fill: amber)
        }
        return true
    }
    // At rest it is a template, so macOS tints it like every other menu bar icon.
    img.isTemplate = level == .none
    return img
}

// MARK: - App

final class App: NSObject, NSApplicationDelegate, NSMenuDelegate {
    let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    let menu = NSMenu()
    var status: Status?
    var error: String?
    var updated: Date?
    var running = false

    func applicationDidFinishLaunching(_ note: Notification) {
        menu.delegate = self
        menu.autoenablesItems = false
        item.menu = menu
        rebuild()
        refresh()
        // Opening the menu always refreshes; the timer only keeps the icon honest, so it can be slow.
        Timer.scheduledTimer(withTimeInterval: 300, repeats: true) { [weak self] _ in self?.refresh() }
    }

    func menuWillOpen(_ menu: NSMenu) { refresh() }

    @objc func refresh() {
        guard !running else { return }
        running = true
        DispatchQueue.global(qos: .userInitiated).async {
            let result = Self.fetch()
            DispatchQueue.main.async {
                self.running = false
                switch result {
                case .success(let s):
                    self.status = s
                    self.error = nil
                    self.updated = Date()
                case .failure(let e):
                    self.error = e.message
                }
                self.rebuild()
            }
        }
    }

    struct RunError: Error { let message: String }

    static func fetch() -> Result<Status, RunError> {
        let command = Bundle.main.object(forInfoDictionaryKey: "RecallCommand") as? [String] ?? ["recall", "status", "--json"]
        switch run(command) {
        case .success(let data):
            do { return .success(try JSONDecoder().decode(Status.self, from: data)) } catch {
                return .failure(RunError(message: "Couldn't read recall's output"))
            }
        case .failure(let e): return .failure(e)
        }
    }

    static func run(_ command: [String], in folder: String? = nil) -> Result<Data, RunError> {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: command[0])
        p.arguments = Array(command.dropFirst())
        p.currentDirectoryURL = URL(fileURLWithPath: folder ?? FileManager.default.homeDirectoryForCurrentUser.path)
        let out = Pipe(), err = Pipe()
        p.standardOutput = out
        p.standardError = err
        do { try p.run() } catch { return .failure(RunError(message: "Couldn't start \(command[0])")) }
        let data = out.fileHandleForReading.readDataToEndOfFile()
        let errData = err.fileHandleForReading.readDataToEndOfFile()
        p.waitUntilExit()
        if p.terminationStatus != 0 {
            let msg = String(data: errData, encoding: .utf8)?.split(separator: "\n").last.map(String.init)
            return .failure(RunError(message: msg ?? "\(command[0]) exited \(p.terminationStatus)"))
        }
        return .success(data)
    }

    // MARK: Menu

    func rebuild() {
        let overlaps = status?.overlaps ?? []
        item.button?.image = markImage(size: 18, level: Level(rawValue: status?.level ?? "") ?? .none)
        item.button?.toolTip = overlaps.isEmpty ? "recall" : "recall: \(overlaps.count) files open in more than one chat"
        menu.removeAllItems()
        if let error { menu.addItem(note(error)) }
        guard let status else {
            if error == nil { menu.addItem(note("Reading your agents…")) }
            footer()
            return
        }

        // What needs you: files two chats both have open changes to, conflicts first.
        let byId = Dictionary(uniqueKeysWithValues: status.sessions.map { ($0.id, $0) })
        // Conflicts always show; past three rows, the quieter overlaps fold into one.
        if overlaps.isEmpty { menu.addItem(note("Nothing needs you")) }
        let shown = overlaps.enumerated().filter { $0.offset < 3 || $0.element.level == "act" }.map(\.element)
        for o in shown { menu.addItem(overlapRow(o, byId)) }
        let rest = overlaps.filter { o in !shown.contains { $0.file == o.file && $0.repo == o.repo } }
        if !rest.isEmpty {
            let more = NSMenuItem(title: "", action: nil, keyEquivalent: "")
            more.attributedTitle = line("\(rest.count) more files open in two chats", Set(rest.map(\.repo)).sorted().joined(separator: ", "))
            more.submenu = NSMenu()
            for o in rest { more.submenu!.addItem(overlapRow(o, byId)) }
            menu.addItem(more)
        }

        // What is running: one line per repo with a live chat.
        menu.addItem(.separator())
        menu.addItem(.sectionHeader(title: "Working now"))
        var order: [String] = []
        var repos: [String: [Session]] = [:]
        for s in status.sessions where s.live {
            let key = s.repo ?? "No folder"
            if repos[key] == nil { order.append(key) }
            repos[key, default: []].append(s)
        }
        if order.isEmpty { menu.addItem(note("No agents working")) }
        for repo in order {
            let chats = repos[repo]!
            var branches: [String] = []
            for s in chats where !branches.contains(s.branch ?? "?") { branches.append(s.branch ?? "?") }
            let row = NSMenuItem(title: repo, action: nil, keyEquivalent: "")
            row.attributedTitle = line(repo, "\(chats.count) live · \(branches.joined(separator: ", "))")
            let sub = NSMenu()
            sub.autoenablesItems = false
            for s in chats {
                let r = NSMenuItem(title: s.title, action: nil, keyEquivalent: "")
                r.attributedTitle = line(clip(s.title, 48), [agentNames[s.agent] ?? s.agent, s.branch ?? "", ago(s.age_seconds)].filter { !$0.isEmpty }.joined(separator: " · "))
                sub.addItem(r)
            }
            row.submenu = sub
            menu.addItem(row)
        }
        footer()
    }

    func overlapRow(_ o: Overlap, _ byId: [String: Session]) -> NSMenuItem {
        let name = (o.file as NSString).lastPathComponent
        let chats = o.sessions.count == 2 ? "two" : "\(o.sessions.count)"
        let verdict = o.conflict.map { $0 ? "will conflict" : "merges clean" } ?? "is open in \(chats) chats"
        let row = NSMenuItem(title: name, action: nil, keyEquivalent: "")
        let where_ = o.same_checkout ? "same working copy" : o.branches.joined(separator: " ↔ ")
        row.attributedTitle = line("\(name) \(verdict)", "\(o.repo) · \(where_)")
        row.submenu = overlapMenu(o, byId)
        return row
    }

    func overlapMenu(_ o: Overlap, _ byId: [String: Session]) -> NSMenu {
        let sub = NSMenu()
        sub.autoenablesItems = false
        sub.addItem(note(o.file))
        for id in o.sessions {
            guard let s = byId[id] else { continue }
            let who = [agentNames[s.agent] ?? s.agent, s.branch ?? "", ago(s.age_seconds)].filter { !$0.isEmpty }
            let r = NSMenuItem(title: s.title, action: nil, keyEquivalent: "")
            r.attributedTitle = line(clip(s.title, 48), who.joined(separator: " · "))
            r.isEnabled = false
            sub.addItem(r)
        }
        sub.addItem(.separator())
        for id in o.sessions {
            guard let s = byId[id], let text = o.notes[id], !text.isEmpty else { continue }
            let copy = NSMenuItem(title: "Copy Note for “\(clip(s.title, 32))”", action: #selector(copyText(_:)), keyEquivalent: "")
            copy.target = self
            copy.representedObject = text
            copy.toolTip = "Paste into that chat so its agent knows about the other one"
            sub.addItem(copy)
        }
        let show = NSMenuItem(title: o.same_checkout ? "Show Changes" : "Show Both Changes", action: #selector(showChanges(_:)), keyEquivalent: "")
        show.target = self
        show.representedObject = ChangesRequest(overlap: o, titles: byId.mapValues(\.title))
        sub.addItem(show)
        return sub
    }

    func footer() {
        menu.addItem(.separator())
        if let updated {
            let f = DateFormatter()
            f.timeStyle = .short
            menu.addItem(note("Updated \(f.string(from: updated))"))
        }
        let r = NSMenuItem(title: "Refresh", action: #selector(refresh), keyEquivalent: "r")
        r.target = self
        menu.addItem(r)
        menu.addItem(NSMenuItem(title: "Quit Recall", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q"))
    }

    // MARK: Actions

    @objc func copyText(_ sender: NSMenuItem) {
        guard let text = sender.representedObject as? String else { return }
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
    }

    final class ChangesRequest: NSObject {
        let overlap: Overlap
        let titles: [String: String]
        init(overlap: Overlap, titles: [String: String]) { self.overlap = overlap; self.titles = titles }
    }

    // Each side's change to the file since it forked from the default branch, uncommitted
    // edits included, in one file opened in Cursor (or whatever opens .diff files).
    @objc func showChanges(_ sender: NSMenuItem) {
        guard let req = sender.representedObject as? ChangesRequest else { return }
        let o = req.overlap
        DispatchQueue.global(qos: .userInitiated).async {
            var text = ""
            var done = Set<String>()
            for side in o.sides where !done.contains(side.checkout) {
                done.insert(side.checkout)
                let who = o.sides.filter { $0.checkout == side.checkout }.map { "“\(req.titles[$0.session] ?? $0.session)”" }
                text += "# \(o.file) on \(side.branch ?? "detached") (\(side.checkout))\n# changed by \(who.joined(separator: " and "))\n\n"
                let diff = Self.run(["/usr/bin/git", "-C", side.checkout, "diff", side.base ?? "HEAD", "--", o.file])
                if case .success(let data) = diff, let s = String(data: data, encoding: .utf8), !s.isEmpty {
                    text += s + "\n"
                } else {
                    text += "(no changes)\n\n"
                }
            }
            let dir = FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask)[0].appendingPathComponent("recall")
            try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
            let url = dir.appendingPathComponent("\((o.file as NSString).lastPathComponent)-both-changes.diff")
            try? text.write(to: url, atomically: true, encoding: .utf8)
            DispatchQueue.main.async {
                if let cursor = NSWorkspace.shared.urlForApplication(withBundleIdentifier: "com.todesktop.230313mzl4w4u92") {
                    NSWorkspace.shared.open([url], withApplicationAt: cursor, configuration: NSWorkspace.OpenConfiguration())
                } else {
                    NSWorkspace.shared.open(url)
                }
            }
        }
    }

    // MARK: Text

    func note(_ text: String) -> NSMenuItem {
        let i = NSMenuItem(title: text, action: nil, keyEquivalent: "")
        i.isEnabled = false
        return i
    }

    func line(_ main: String, _ meta: String) -> NSAttributedString {
        let font = NSFont.menuFont(ofSize: 0)
        let out = NSMutableAttributedString(string: main, attributes: [.font: font])
        if !meta.isEmpty {
            out.append(NSAttributedString(string: "   " + meta, attributes: [
                .font: NSFont.systemFont(ofSize: font.pointSize - 2),
                .foregroundColor: NSColor.secondaryLabelColor,
            ]))
        }
        return out
    }
}

// `Recall --render-mark out.png` draws each level on a light and a dark menu bar at 4x, to
// compare them without a screenshot.
if CommandLine.arguments.count == 3, CommandLine.arguments[1] == "--render-mark" {
    let cell: CGFloat = 32, scale: CGFloat = 4
    let sheet = NSImage(size: NSSize(width: cell * 3 * scale, height: cell * 2 * scale), flipped: false) { _ in
        for (row, name) in [NSAppearance.Name.darkAqua, .aqua].enumerated() {
            NSAppearance(named: name)!.performAsCurrentDrawingAppearance {
                let y = CGFloat(row) * cell * scale
                (name == .darkAqua ? NSColor(white: 0.13, alpha: 1) : NSColor(white: 0.93, alpha: 1)).setFill()
                NSRect(x: 0, y: y, width: cell * 3 * scale, height: cell * scale).fill()
                for (col, level) in [Level.none, .watch, .act].enumerated() {
                    // Its own image, so the cut-outs show the bar rather than punching through it.
                    let mark = NSImage(size: NSSize(width: 18 * scale, height: 18 * scale), flipped: false) { _ in
                        let t = NSAffineTransform()
                        t.scale(by: scale * 18 / 32)
                        t.concat()
                        drawMark(fill: level == .act ? amber : .labelColor, leaves: level == .watch ? amber : nil)
                        return true
                    }
                    mark.draw(in: NSRect(x: (CGFloat(col) * cell + 7) * scale, y: y + 7 * scale, width: 18 * scale, height: 18 * scale))
                }
            }
        }
        return true
    }
    let rep = NSBitmapImageRep(data: sheet.tiffRepresentation!)!
    try! rep.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: CommandLine.arguments[2]))
    exit(0)
}

// `Recall --dump-menu` fetches once and prints the menu as text, to check it without a screen.
if CommandLine.arguments.count == 2, CommandLine.arguments[1] == "--dump-menu" {
    let a = App()
    if case .success(let s) = App.fetch() { a.status = s }
    a.rebuild()
    print("menu bar: [mark \(a.status?.level ?? "?")]")
    func dump(_ m: NSMenu, _ depth: Int) {
        for i in m.items {
            let text = i.isSeparatorItem ? "────" : (i.attributedTitle?.string ?? i.title)
            print(String(repeating: "    ", count: depth) + text + (i.isEnabled || i.isSeparatorItem ? "" : "  (disabled)"))
            if let s = i.submenu { dump(s, depth + 1) }
        }
    }
    dump(a.menu, 0)
    exit(0)
}

let app = NSApplication.shared
let delegate = App()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
