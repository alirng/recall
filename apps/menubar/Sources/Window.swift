// The recall window: one titlebar (search, the This Mac switcher), one sidebar, and the section
// it shows. The traffic lights and the title bar stay native; the row beside them is ours.

import AppKit
import SwiftUI

// MARK: - Controller

final class WindowController: NSObject, NSWindowDelegate {
    var window: NSWindow?
    var library: Library?
    var state = AppState.load()
    let chrome = Chrome()
    var look: NSKeyValueObservation?
    var wheel: Any?
    var keys: Any?

    var isOpen: Bool { window?.isVisible ?? false }

    func show() {
        if let window {
            if Probe.background { window.orderBack(nil); return }
            NSApp.activate(ignoringOtherApps: true)
            window.makeKeyAndOrderFront(nil)
            return
        }
        let library = Library()
        if let key = state.section, let s = Place(key: key) { library.section = s }
        chrome.sidebar = CGFloat(state.sidebar ?? 220)
        self.library = library
        let frame: NSRect
        if let f = state.window, f.count == 4 {
            frame = NSRect(x: f[0], y: f[1], width: f[2], height: f[3])
        } else {
            let screen = NSScreen.main?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1440, height: 900)
            let size = NSSize(width: min(1280, screen.width - 80), height: min(820, screen.height - 60))
            frame = NSRect(x: screen.midX - size.width / 2, y: screen.midY - size.height / 2, width: size.width, height: size.height)
        }
        let w = RecallWindow(contentRect: frame, styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView], backing: .buffered, defer: false)
        w.titlebarAppearsTransparent = true
        w.titleVisibility = .hidden
        w.title = "recall"
        w.minSize = NSSize(width: 900, height: 560)
        // Nothing of the window goes to ~/Library: no frame autosave, no state restoration.
        w.isRestorable = false
        w.isReleasedWhenClosed = false
        w.delegate = self
        library.popups.parent = w
        if let a = state.appearance { w.appearance = NSAppearance(named: a == "light" ? .aqua : .darkAqua) }
        library.toggleAppearance = { [weak self, weak w] in
            guard let self, let w else { return }
            let dark = w.effectiveAppearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua
            w.appearance = NSAppearance(named: dark ? .aqua : .darkAqua)
            self.state.appearance = dark ? "light" : "dark"
        }
        w.contentView = NSHostingView(rootView: RootView(library: library, notes: library.notes, chrome: chrome, onSidebar: { [weak self] width in
            self?.state.sidebar = Double(width)
        }))
        w.setFrame(frame, display: false)
        window = w
        if Probe.background {
            // A test run: drawn off screen and never brought forward, so it doesn't take your screen.
            w.setFrameOrigin(NSPoint(x: -20000, y: -20000))
            w.orderBack(nil)
        } else {
            NSApp.setActivationPolicy(.regular)
            NSApp.activate(ignoringOtherApps: true)
            w.makeKeyAndOrderFront(nil)
        }
        placeTrafficLights()
        DispatchQueue.main.async { [weak self] in
            self?.placeTrafficLights()  // after AppKit's first layout
            // No blinking caret in the search field until you ask for it (⌘K): a key window with a
            // focused field redraws twice a second.
            self?.window?.makeFirstResponder(nil)
        }
        // A change of look lays the titlebar out again, which puts the traffic lights back.
        look = w.observe(\.effectiveAppearance) { [weak self] _, _ in DispatchQueue.main.async { self?.placeTrafficLights() } }
        library.open()
        keys = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] e in
            self?.list(e) == true ? nil : e
        }
        wheel = NSEvent.addLocalMonitorForEvents(matching: .scrollWheel) { [weak self] e in
            self?.wheelStep(e)
            return e
        }
    }

    /// SwiftUI's List sets its scroll view's line scroll to 1 pt, and puts it back on its own
    /// updates, so a mouse wheel click moved one point. Before each wheel click this sets the scroll
    /// view under the pointer to what an AppKit table uses (24 pt), as Finder's and Notes' lists do.
    /// Trackpads send pixels and aren't touched.
    /// Up, Down and Return go to the list on screen, unless you're typing in a field. In the chat
    /// find field, Return and Shift-Return step through the matches.
    func list(_ e: NSEvent) -> Bool {
        guard e.window === window, let library, !library.popups.isOpen else { return false }
        if library.find.focused, e.keyCode == 36 || e.keyCode == 76 {
            library.find.step(e.modifierFlags.contains(.shift) ? -1 : 1)
            return true
        }
        if window?.firstResponder is NSText { return false }
        if !e.modifierFlags.intersection([.command, .option, .control, .shift]).isEmpty { return false }
        switch e.keyCode {
        case 125: return library.key(.down)
        case 126: return library.key(.up)
        case 36, 76: return library.key(.open)
        default: return false
        }
    }

    func wheelStep(_ e: NSEvent) {
        guard e.window === window, let root = window?.contentView else { return }
        // The thin scroller goes on whatever is about to scroll, mouse wheel or trackpad.
        var v = root.hitTest(root.convert(e.locationInWindow, from: nil))
        while let view = v, !(view is NSScrollView) { v = view.superview }
        if let s = v as? NSScrollView { ThinScroller.install(on: s) }
        if !e.hasPreciseScrollingDeltas { wheelStep(at: e.locationInWindow) }
    }

    func wheelStep(at point: NSPoint) {
        guard let root = window?.contentView else { return }
        var v = root.hitTest(root.convert(point, from: nil))
        while let view = v, !(view is NSScrollView) { v = view.superview }
        if let s = v as? NSScrollView, s.verticalLineScroll < 10 { s.verticalLineScroll = 24 }
    }

    func windowDidBecomeKey(_ note: Notification) {
        placeTrafficLights()
        library?.focused()
    }
    func windowDidResize(_ note: Notification) { placeTrafficLights() }
    func windowDidChangeOcclusionState(_ note: Notification) {
        library?.onScreen = Probe.background || (window?.occlusionState.contains(.visible) ?? false)
    }
    func windowDidExitFullScreen(_ note: Notification) { placeTrafficLights() }

    static let titlebar: CGFloat = 48

    /// The native traffic lights, centred on our taller titlebar row: their container is made as
    /// tall as the row. AppKit lays it out again on resize, so this runs then too.
    func placeTrafficLights() {
        guard let w = window, let close = w.standardWindowButton(.closeButton), let box = close.superview?.superview else { return }
        var f = box.frame
        f.size.height = Self.titlebar
        f.origin.y = w.frame.height - Self.titlebar
        box.frame = f
        for kind in [NSWindow.ButtonType.closeButton, .miniaturizeButton, .zoomButton] {
            guard let b = w.standardWindowButton(kind) else { continue }
            b.setFrameOrigin(NSPoint(x: b.frame.origin.x + (kind == .closeButton ? 0 : 0), y: (Self.titlebar - b.frame.height) / 2))
        }
    }

    func windowWillClose(_ note: Notification) {
        saveState()
        library?.close()
        // Let the views and their data go, so a closed window costs what the menu bar costs.
        look = nil
        if let wheel { NSEvent.removeMonitor(wheel) }
        if let keys { NSEvent.removeMonitor(keys) }
        wheel = nil
        keys = nil
        window?.contentView = nil
        window?.delegate = nil
        window = nil
        library = nil
        NSApp.setActivationPolicy(.accessory)
    }

    func saveState() {
        guard !Probe.background else { return }  // a test run's off-screen frame isn't yours
        if let f = window?.frame { state.window = [f.minX, f.minY, f.width, f.height] }
        if let s = library?.section { state.section = s.key }
        if let l = library { state.pins = l.pins }
        state.sidebar = Double(chrome.sidebar)
        state.save()
    }

    // Menu actions
    @objc func focusSearch() { library?.palette.open() }
    @objc func findInChat() { library?.find.show() }
    @objc func copyNote() { library?.notes.copyMarkdown() }
    @objc func goMemory() { library?.go(.memory) }
    @objc func goChats() { library?.go(.chats) }
    @objc func goThisMac() { library?.go(.thisMac) }
    @objc func goNow() { library?.go(.now) }
}

/// The window. A test run puts it off screen, which AppKit would otherwise pull back on.
final class RecallWindow: NSWindow {
    override func constrainFrameRect(_ frame: NSRect, to screen: NSScreen?) -> NSRect {
        Probe.background ? frame : super.constrainFrameRect(frame, to: screen)
    }
}

/// The sidebar width while it's dragged; saved with the window state.
final class Chrome: ObservableObject {
    @Published var sidebar: CGFloat = 220
    var dragStart: CGFloat?
}

// MARK: - Root

struct RootView: View {
    @ObservedObject var library: Library
    @ObservedObject var notes: NotesModel
    @ObservedObject var chrome: Chrome
    var onSidebar: (CGFloat) -> Void
    @Environment(\.colorScheme) var scheme

    var body: some View {
        let p = Palette.of(scheme)
        VStack(spacing: 0) {
            TitleBar(library: library, search: library.search)
            HStack(spacing: 0) {
                Sidebar(library: library, notes: notes)
                    .frame(width: chrome.sidebar)
                    .overlay(alignment: .trailing) { resizer(p) }
                content(p)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .background(p.paper)
        .ignoresSafeArea()
        .environmentObject(library.facts)
        .font(PaperMono.font(12.5))
        .foregroundStyle(p.body)
    }

    func resizer(_ p: Palette) -> some View {
        Rectangle().fill(p.grid).frame(width: 1)
            .overlay(Color.clear.frame(width: 7).contentShape(Rectangle())
                .onHover { $0 ? NSCursor.resizeLeftRight.push() : NSCursor.pop() }
                .gesture(DragGesture(minimumDistance: 1)
                    .onChanged { g in
                        let start = chrome.dragStart ?? chrome.sidebar
                        chrome.dragStart = start
                        chrome.sidebar = min(360, max(180, start + g.translation.width))
                    }
                    .onEnded { _ in
                        chrome.dragStart = nil
                        onSidebar(chrome.sidebar)
                    }))
    }

    @ViewBuilder
    func content(_ p: Palette) -> some View {
        switch library.link {
        case .connecting where library.info == nil:
            Message(title: "Connecting to recall…", text: "Starting recall api if it isn't running.")
        case .failed(let why):
            Message(title: "Can't reach recall", text: why, action: ("Try again", { library.connect() }))
        default:
            Sections(library: library, shown: library.section.host)
        }
    }
}

extension Place {
    /// Which kept view shows this place. Each agent has its own Chats view.
    var host: String { key }
}

/// Each section in its own hosting view, made on its first visit and then only hidden and shown,
/// so going back to one shows it on the next frame with the data it had. A hidden hosting view
/// isn't laid out or drawn.
struct Sections: NSViewRepresentable {
    let library: Library
    let shown: String

    final class Stack: NSView {
        weak var library: Library?
        var views: [String: NSView] = [:]

        var current = ""

        func make(_ key: String) -> NSView? {
            guard let library else { return nil }
            if let v = views[key] { return v }
            let v = NSHostingView(rootView: SectionRoot(library: library, key: key).environmentObject(library.facts))
            v.frame = bounds
            v.autoresizingMask = [.width, .height]
            views[key] = v
            return v
        }

        func show(_ key: String) {
            current = key
            if let v = make(key), v.superview !== self { addSubview(v) }
            for (k, v) in views where v.isHidden != (k != key) { v.isHidden = k != key }
            if let v = views[key] { DispatchQueue.main.async { ThinScroller.install(in: v) } }
        }

        /// Builds sections not yet visited behind the one on screen, one per turn of the run loop,
        /// so a first visit is as quick as a return. Each is laid out and drawn once, then hidden.
        func prewarm(_ keys: [String]) {
            guard let key = keys.first else { return }
            if views[key] == nil, let v = make(key), let top = views[current] {
                addSubview(v, positioned: .below, relativeTo: top)
                v.layoutSubtreeIfNeeded()
                v.displayIfNeeded()
                ThinScroller.install(in: v)
                v.isHidden = true
            }
            DispatchQueue.main.async { [weak self] in self?.prewarm(Array(keys.dropFirst())) }
        }
    }

    func makeNSView(context: Context) -> Stack {
        let s = Stack()
        s.library = library
        library.prewarm = { [weak s] keys in s?.prewarm(keys) }
        return s
    }

    func updateNSView(_ s: Stack, context: Context) { s.show(shown) }
}

struct SectionRoot: View {
    let library: Library
    let key: String
    @Environment(\.colorScheme) var scheme

    var body: some View {
        let p = Palette.of(scheme)
        Group {
            switch key {
            case "memory": MemoryView(library: library, notes: library.notes)
            case "this mac": ThisMacView(library: library)
            case "chats": ChatsView(library: library, chats: library.chats)
            case let k where k.hasPrefix("agent:"): ChatsView(library: library, chats: library.chats(for: String(k.dropFirst(6))))
            case "search": SearchView(library: library, search: library.search)
            case "projects": ProjectsView(library: library)
            default: NowView(library: library)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(p.paper)
        .background(ScrollerHook())
        .font(PaperMono.font(12.5))
        .foregroundStyle(p.body)
    }
}

struct Message: View {
    let title: String
    let text: String
    var action: (String, () -> Void)? = nil
    @Environment(\.colorScheme) var scheme
    var body: some View {
        let p = Palette.of(scheme)
        VStack(alignment: .leading, spacing: 10) {
            Text(title).font(PaperMono.font(16, 520)).foregroundStyle(p.ink)
            Text(text).font(PaperMono.font(13, 390, prose: true)).foregroundStyle(p.muted)
                .fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
            if let action { BoxButton(title: action.0, action: action.1).padding(.top, 4) }
        }
        .frame(maxWidth: 520, alignment: .leading)
        .padding(32)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
    }
}

// MARK: - Titlebar

struct TitleBar: View {
    @ObservedObject var library: Library
    @ObservedObject var search: SearchModel
    @StateObject var anchor = Anchor()
    @Environment(\.colorScheme) var scheme

    var body: some View {
        let p = Palette.of(scheme)
        HStack(spacing: 16) {
            Color.clear.frame(width: 56, height: 1)  // the traffic lights
            HStack(spacing: 8) {
                Image(nsImage: markImage(size: 14, level: .act))
                Text("recall").font(PaperMono.font(13, 640)).foregroundStyle(p.ink)
                Text("/ " + library.section.title).font(PaperMono.font(13)).foregroundStyle(p.muted)
            }
            .fixedSize()
            Spacer(minLength: 12)
            // Not a text field: it opens the palette, as ⌘K does.
            Button { library.palette.open() } label: {
                HStack(spacing: 8) {
                    Text("⌕").foregroundStyle(p.muted)
                    Text("Search, or go anywhere").foregroundStyle(p.muted).lineLimit(1)
                    Spacer(minLength: 8)
                    Kbd(text: "⌘K")
                }
                .font(PaperMono.font(12.5))
                .padding(.leading, 10).padding(.trailing, 6).frame(height: 28)
                .background(p.paper)
                .overlay(Rectangle().strokeBorder(p.line, lineWidth: 1))
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .frame(maxWidth: 620)
            Spacer(minLength: 12)
            MenuButton(popups: library.popups, width: 288, menu: { switcher(p) }) {
                HStack(spacing: 8) {
                    Text(library.info?.instance?.name ?? "This Mac").font(PaperMono.font(12, 500)).foregroundStyle(p.ink)
                    Text("▾").font(PaperMono.font(12)).foregroundStyle(p.muted)
                }
                .padding(.horizontal, 10).frame(height: 28)
                .overlay(Rectangle().strokeBorder(p.line, lineWidth: 1))
            }
        }
        .padding(.horizontal, 14)
        .frame(height: WindowController.titlebar)
        .background(p.card)
        .overlay(alignment: .bottom) { Rectangle().fill(p.grid).frame(height: 1) }
    }

    /// The This Mac switcher: this Mac, and a server that comes later (a plain dotted square).
    func switcher(_ p: Palette) -> MenuModel {
        let f = library.facts
        return MenuModel(title: "Instances", rows: [
            MenuRow(title: library.info?.instance?.name ?? "This Mac",
                    detail: ["local", "\(f.agents.count) agents", size(f.storage?.total_bytes)].filter { !$0.isEmpty }.joined(separator: " · "),
                    checked: true, action: { [weak library] in library?.go(.thisMac) }),
            MenuRow(title: "Add a server…", detail: "share one memory across Macs · not yet", enabled: false,
                    mark: AnyView(Dotted(color: p.lane).frame(width: 9, height: 9))),
        ])
    }
}

// MARK: - Sidebar

struct Sidebar: View {
    @ObservedObject var library: Library
    @EnvironmentObject var facts: Facts
    @ObservedObject var notes: NotesModel
    @Environment(\.colorScheme) var scheme

    var body: some View {
        let p = Palette.of(scheme)
        VStack(alignment: .leading, spacing: 18) {
            group("Library") {
                item(.memory, "Memory", count(facts.notesCount))
                item(.chats, "Chats", count(facts.chatsCount))
                item(.projects, "Projects", count(facts.projects?.projects.count))
            }
            group("Agents") {
                ForEach(facts.agents, id: \.name) { a in
                    item(.agent(a.name), agentName(a.name), count(a.chats), mark: AnyView(DiamondMark(color: p.agent(a.name))))
                }
            }
            group("Instance") {
                item(.now, "Now", facts.needsYou > 0 ? "\(facts.needsYou) needs you" : (facts.status == nil ? "" : "quiet"), strong: facts.needsYou > 0)
                item(.thisMac, library.info?.instance?.name ?? "This Mac", size(facts.storage?.total_bytes))
            }
            Spacer(minLength: 0)
            VStack(alignment: .leading, spacing: 2) {
                if let at = facts.indexedAt { Text("Indexed \(clock(at))") }
                if let next = facts.nextRun { Text("Next memory run \(next)") }
            }
            .font(PaperMono.font(11.5)).foregroundStyle(p.muted)
            .padding(10)
            .frame(maxWidth: .infinity, alignment: .leading)
            .overlay(alignment: .top) { Rectangle().fill(p.grid).frame(height: 1) }
        }
        .padding(.horizontal, 8).padding(.top, 14)
        .frame(maxHeight: .infinity, alignment: .top)
        .background(p.paper)
    }

    func group<C: View>(_ title: String, @ViewBuilder _ c: () -> C) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            Label(text: title).padding(.horizontal, 10).padding(.bottom, 6)
            c()
        }
    }

    /// The selected row is fill only: ink text at 500 and the count in ink, no marker.
    func item(_ s: Place, _ title: String, _ n: String?, mark: AnyView? = nil, strong: Bool = false) -> some View {
        let p = Palette.of(scheme), on = library.section == s
        return Button { library.go(s) } label: {
            HStack(spacing: 10) {
                if let mark { mark }
                Text(title).foregroundStyle(on ? p.ink : p.muted).fontWeight(on ? .medium : .regular).lineLimit(1)
                Spacer(minLength: 4)
                if let n, !n.isEmpty { Text(n).foregroundStyle(strong || on ? p.ink : p.muted).monospacedDigit() }
            }
            .font(PaperMono.font(12.5))
            .padding(.horizontal, 10).frame(height: 30)
            .background(on ? p.sel : .clear)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }
}

// MARK: - Shared pieces

struct DiamondMark: View {
    let color: Color
    var dashed = false
    var body: some View {
        // Drawn half a point in from a 9 pt frame on whole points, so the line sits on the pixel grid.
        InsetDiamond(by: 0.5).stroke(color, style: StrokeStyle(lineWidth: 1, dash: dashed ? [1, 1] : [])).frame(width: 9, height: 9)
    }
}

struct Tag: View {
    let text: String
    var agent: String? = nil
    var on = false
    @Environment(\.colorScheme) var scheme
    var body: some View {
        let p = Palette.of(scheme)
        HStack(spacing: 6) {
            if let agent { DiamondMark(color: p.agent(agent)).frame(width: 8, height: 8) }
            Text(text).lineLimit(1)
        }
        .font(PaperMono.font(11, 500)).tracking(0.4)
        .foregroundStyle(on || agent != nil ? p.ink : p.muted)
        .padding(.horizontal, 6).padding(.vertical, 3)
        .overlay(Rectangle().strokeBorder(on ? p.ink : p.faint, lineWidth: 1))
        .fixedSize()
    }
}

/// A filter tag you can press.
struct TagButton: View {
    let text: String
    let on: Bool
    var agent: String? = nil
    let action: () -> Void
    var body: some View {
        Button(action: action) { Tag(text: text, agent: agent, on: on).contentShape(Rectangle()) }.buttonStyle(.plain)
    }
}

/// A board button. `soon` draws it dashed and does nothing: it's a write that arrives in step 4.
struct WindowButton: View {
    let title: String
    var soon = false
    var action: () -> Void = {}
    @StateObject var hover = Hover()
    @Environment(\.colorScheme) var scheme
    var body: some View {
        let p = Palette.of(scheme)
        Button(action: action) {
            Text(title).font(PaperMono.font(12, 500))
                .foregroundStyle(soon ? p.faint : p.ink)
                .padding(.horizontal, 12).frame(minHeight: 30)
                .overlay(Rectangle().strokeBorder(soon ? p.faint : (hover.on ? p.ink : p.line), style: StrokeStyle(lineWidth: 1, dash: soon ? [3, 3] : [])))
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .disabled(soon)
        .onHover { hover.on = $0 }
        .help(soon ? "Comes with editing, in a later version" : "")
    }
}

struct Rule: View {
    @Environment(\.colorScheme) var scheme
    var body: some View { Rectangle().fill(Palette.of(scheme).grid).frame(height: 1) }
}

/// A board's row: label left, value right, a grid line above.
struct RowLine<L: View, R: View>: View {
    let left: L
    let right: R
    init(@ViewBuilder left: () -> L, @ViewBuilder right: () -> R) {
        self.left = left()
        self.right = right()
    }
    var body: some View {
        VStack(spacing: 0) {
            Rule()
            HStack(spacing: 10) { left; Spacer(minLength: 8); right }.padding(.vertical, 8)
        }
    }
}

/// A list that only draws what's on screen (an NSTableView underneath), with the board's rows:
/// no system highlight or separators, and the next page asked for near the end.
struct Rows<Item: Identifiable, Row: View>: View {
    let items: [Item]
    var more: () -> Void = {}
    var cursor: Cursor?
    var jump: Jump?
    let row: (Item) -> Row
    @Environment(\.colorScheme) var scheme

    init(_ items: [Item], cursor: Cursor? = nil, jump: Jump? = nil, more: @escaping () -> Void = {}, @ViewBuilder row: @escaping (Item) -> Row) {
        self.items = items
        self.cursor = cursor
        self.jump = jump
        self.more = more
        self.row = row
    }

    var body: some View {
        let tail = Set(items.suffix(20).map(\.id))
        ScrollViewReader { proxy in
            List(items) { item in
                row(item)
                    .padding(.horizontal, -8)
                    .listRowInsets(EdgeInsets())
                    .listRowSeparator(.hidden)
                    .listRowBackground(Color.clear)
                    .onAppear { if tail.contains(item.id) { more() } }
            }
            .listStyle(.plain)
            .scrollContentBackground(.hidden)
            .environment(\.defaultMinListRowHeight, 1)
            .background {
                if let cursor { CursorScroll(cursor: cursor, proxy: proxy) }
                if let jump { JumpScroll(jump: jump, proxy: proxy) }
                ScrollerHook()
            }
        }
    }
}

/// The board's selected-row tint on the row the keyboard marked.
struct Marked<C: View>: View {
    @ObservedObject var cursor: Cursor
    let id: String
    @ViewBuilder var content: () -> C
    @Environment(\.colorScheme) var scheme
    var body: some View { content().background(cursor.id == id ? Palette.of(scheme).sel : .clear) }
}

/// Scrolls a list to its cursor when the keyboard moves it.
struct CursorScroll: View {
    @ObservedObject var cursor: Cursor
    let proxy: ScrollViewProxy
    var anchor: UnitPoint? = nil
    var body: some View {
        Color.clear.onChange(of: cursor.tick) { _, _ in
            if let id = cursor.id { proxy.scrollTo(id, anchor: anchor) }
        }
    }
}

