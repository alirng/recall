// The pieces the 4 Nov design round added (canvas row F): category colours and tags, key caps,
// the thin overlay scroller, and the themed popups that replace native menus.

import AppKit
import SwiftUI

// MARK: - Tokens

extension Palette {
    /// Menus and the palette: a step lighter than a card in dark, white in light.
    var pop: Color { light ? .white : Color(hex: 0x1A1A1D) }
    var scrim: Color { light ? Color(hex: 0xE9E8E3).opacity(0.7) : Color(hex: 0x060608).opacity(0.66) }

    /// rererecall.com's own hue set, oklch(.72 .17 H), darkened in light until each clears 4.5:1 on
    /// paper. Decisions, the most common, stay grey.
    func kind(_ type: String) -> Color {
        switch type {
        case "all": return ink  // the filter that shows every kind
        case "fact": return Color(hex: light ? 0x00836C : 0x00C5A9)
        case "lesson": return Color(hex: light ? 0x5F7400 : 0x98B305)
        case "preference": return Color(hex: light ? 0x7750B1 : 0xB688FE)
        case "open": return Color(hex: light ? 0xA93B65 : 0xF771A0)
        default: return ink.opacity(0.5)
        }
    }
}

/// A note's category: its lowercase name boxed in its colour, 17 pt tall.
struct KindTag: View {
    let type: String
    var on = false
    @Environment(\.colorScheme) var scheme
    var body: some View {
        let c = Palette.of(scheme).kind(type)
        Text(type.lowercased())
            .font(PaperMono.font(11, 500)).tracking(0.6)
            .foregroundStyle(c)
            .padding(.horizontal, 4).frame(height: 17)
            .background(on ? c.opacity(0.16) : .clear)
            .overlay(Rectangle().strokeBorder(c, lineWidth: 1))
            .fixedSize()
    }
}

/// A key cap: "⌘K", "1", "esc".
struct Kbd: View {
    let text: String
    @Environment(\.colorScheme) var scheme
    var body: some View {
        let p = Palette.of(scheme)
        Text(text).font(PaperMono.font(11)).foregroundStyle(p.muted)
            .padding(.horizontal, 4).frame(minWidth: 18, minHeight: 17)
            .overlay(Rectangle().strokeBorder(p.grid, lineWidth: 1))
            .fixedSize()
    }
}

/// An outline drawn inside its frame on whole points, so a 1 pt dash or dot lands on the pixel
/// grid at 1x and 2x instead of smearing across two pixels.
struct Dotted: View {
    var color: Color
    var dash: [CGFloat] = [1, 1]
    var body: some View {
        Rectangle().strokeBorder(color, style: StrokeStyle(lineWidth: 1, dash: dash))
    }
}

/// A diamond drawn inside its frame by `by` points, so its 1 pt line isn't cut at the frame's edge.
struct InsetDiamond: Shape {
    var by: CGFloat
    func path(in r: CGRect) -> Path { Diamond().path(in: r.insetBy(dx: by, dy: by)) }
}

// MARK: - The thin scroller

/// A 3 pt thumb in faint ink with no track, widening to 6 pt under the pointer. It's an overlay
/// scroller whatever the system setting, so it shows while scrolling and fades after.
final class ThinScroller: NSScroller {
    static let faint = NSColor(name: nil) { a in
        a.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua
            ? NSColor(srgbRed: 232 / 255, green: 230 / 255, blue: 223 / 255, alpha: 0.3)
            : NSColor(srgbRed: 20 / 255, green: 20 / 255, blue: 22 / 255, alpha: 0.32)
    }
    var wide = false

    override class var isCompatibleWithOverlayScrollers: Bool { true }
    override class func scrollerWidth(for controlSize: NSControl.ControlSize, scrollerStyle: NSScroller.Style) -> CGFloat { 10 }

    override func drawKnobSlot(in slotRect: NSRect, highlight flag: Bool) {}

    override func drawKnob() {
        let knob = rect(for: .knob)
        let w: CGFloat = wide ? 6 : 3
        Self.faint.setFill()
        NSRect(x: (bounds.maxX - w - 2).rounded(), y: knob.minY + 2, width: w, height: max(0, knob.height - 4)).fill()
    }

    override func updateTrackingAreas() {
        super.updateTrackingAreas()
        trackingAreas.forEach(removeTrackingArea)
        addTrackingArea(NSTrackingArea(rect: bounds, options: [.mouseEnteredAndExited, .activeInActiveApp, .inVisibleRect], owner: self))
    }

    override func mouseEntered(with event: NSEvent) { wide = true; needsDisplay = true }
    override func mouseExited(with event: NSEvent) { wide = false; needsDisplay = true }

    /// Puts the thin scroller on every scroll view under `root`. SwiftUI makes its scroll views as it
    /// goes, so this runs when a section shows, when a popup opens and before each wheel event.
    static func install(in root: NSView) {
        if let s = root as? NSScrollView { install(on: s) }
        for sub in root.subviews { install(in: sub) }
    }

    static func install(on s: NSScrollView) {
        var changed = false
        if s.scrollerStyle != .overlay { s.scrollerStyle = .overlay; changed = true }
        if !(s.verticalScroller is ThinScroller) {
            s.verticalScroller = ThinScroller()
            s.verticalScroller?.scrollerStyle = .overlay
            changed = true
        }
        if s.hasHorizontalScroller, !(s.horizontalScroller is ThinScroller) { s.hasHorizontalScroller = false; changed = true }
        // SwiftUI scroll views are told to show no indicator of their own, so their content takes the
        // full width; the thin one goes back on top, as an overlay that reserves nothing.
        if !s.hasVerticalScroller { s.hasVerticalScroller = true; changed = true }
        // Give the content back the width a legacy scroller had reserved.
        if changed {
            s.tile()
            // SwiftUI sized the content for the old scroller; ask it to lay out again.
            var v: NSView? = s
            while let x = v { x.needsLayout = true; v = x.superview }
        }
    }
}

extension View {
    /// A bigger area to click than the drawn control, with the layout unchanged: small marks and labels are hard to hit
    /// (the reel's "touch target": the tappable area, not just the icon).
    func hitArea(_ inset: CGFloat = 6) -> some View { padding(inset).contentShape(Rectangle()).padding(-inset) }
}

/// Whether a list or page is moving, however it's being moved: any clip view's bounds changing in the last moment.
/// Work that would drop a frame waits until it isn't.
final class Scrolling {
    static let shared = Scrolling()
    private var movedAt: CFTimeInterval = 0

    var now: Bool { CACurrentMediaTime() - movedAt < 0.15 }

    private init() {
        // No queue: told at once, before the list builds the rows this movement brings in.
        NotificationCenter.default.addObserver(forName: NSView.boundsDidChangeNotification, object: nil, queue: nil) { [weak self] note in
            guard note.object is NSClipView else { return }
            self?.movedAt = CACurrentMediaTime()
        }
    }
}

/// Puts the thin scroller on the scroll views near it each time its view updates: SwiftUI makes a
/// list's scroll view when the list first fills, and sets its scroller back on some updates.
struct ScrollerHook: NSViewRepresentable {
    final class Hook: NSView {
        override func viewDidMoveToWindow() {
            super.viewDidMoveToWindow()
            schedule()
        }
        var scheduled = false
        func schedule() {
            guard !scheduled else { return }
            scheduled = true
            DispatchQueue.main.async { [weak self] in
                guard let self else { return }
                self.scheduled = false
                if let host = self.enclosingHost { ThinScroller.install(in: host) }
            }
        }
        /// The nearest hosting view up the tree: one section, not the whole window.
        var enclosingHost: NSView? {
            var v = superview
            while let s = v, !(String(describing: type(of: s)).hasPrefix("NSHostingView")) { v = s.superview }
            return v ?? window?.contentView
        }
    }
    func makeNSView(context: Context) -> Hook { Hook() }
    func updateNSView(_ v: Hook, context: Context) { v.schedule() }
}

// MARK: - Popups

/// A borderless panel attached to the window: menus, the project picker and the ⌘K palette. It
/// can take the keyboard (for a filter field) without making the app or window lose its place.
final class Popup: NSPanel {
    override var canBecomeKey: Bool { true }
}

final class Popups {
    weak var parent: NSWindow?
    var panel: Popup?
    var monitors: [Any] = []
    /// Keys go here first while a popup is open: true means it was used.
    var keys: ((NSEvent) -> Bool)?
    var onClose: (() -> Void)?

    var isOpen: Bool { panel != nil }
    var closedAt: CFTimeInterval = 0

    /// Shows `view` at `frame` (screen coordinates). `cover` puts it over the whole window instead.
    func show<V: View>(_ view: V, frame: NSRect, keys: @escaping (NSEvent) -> Bool, onClose: (() -> Void)? = nil) {
        close()
        guard let parent else { return }
        let p = Popup(contentRect: frame, styleMask: [.borderless], backing: .buffered, defer: false)
        p.isReleasedWhenClosed = false
        p.isRestorable = false
        p.hasShadow = false
        p.backgroundColor = .clear
        p.isOpaque = false
        p.appearance = parent.appearance
        let host = NSHostingView(rootView: view)
        host.frame = NSRect(origin: .zero, size: frame.size)
        p.contentView = host
        parent.addChildWindow(p, ordered: .above)
        if Probe.background { p.orderFront(nil) } else { p.makeKeyAndOrderFront(nil) }
        panel = p
        self.keys = keys
        self.onClose = onClose
        DispatchQueue.main.async { ThinScroller.install(in: host) }
        monitors.append(NSEvent.addLocalMonitorForEvents(matching: [.leftMouseDown, .rightMouseDown]) { [weak self] e in
            if let self, let panel = self.panel, e.window !== panel { self.close() }
            return e
        } as Any)
        monitors.append(NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] e in
            guard let self, self.panel != nil else { return e }
            if e.keyCode == 53 { self.close(); return nil }  // esc
            return self.keys?(e) == true ? nil : e
        } as Any)
    }

    /// Below an anchor (screen rect), `width` wide, as tall as its content up to `maxHeight`.
    func show<V: View>(_ view: V, below anchor: NSRect, width: CGFloat, height: CGFloat? = nil, keys: @escaping (NSEvent) -> Bool, onClose: (() -> Void)? = nil) {
        var h = height ?? min(480, NSHostingView(rootView: view).fittingSize.height)
        var x = anchor.minX, y = anchor.minY - 4 - h
        // Below the button when it fits on screen, as macOS menus do; above it when it doesn't and there's more room
        // there, so a control near the bottom of the window doesn't open a menu off the screen.
        // The screen the button is on. A window drawn off screen (a test run) has none, and its menus stay off screen too.
        if let screen = NSScreen.screens.first(where: { $0.frame.intersects(anchor) })?.visibleFrame {
            x = max(screen.minX + 8, min(x, screen.maxX - width - 8))
            let below = anchor.minY - 4 - (screen.minY + 8)
            let above = (screen.maxY - 8) - (anchor.maxY + 4)
            if h <= below {
                y = anchor.minY - 4 - h
            } else if h <= above || above > below {
                h = min(h, above)
                y = anchor.maxY + 4
            } else {
                h = min(h, below)
                y = anchor.minY - 4 - h
            }
        }
        show(view, frame: NSRect(x: x, y: y, width: width, height: h), keys: keys, onClose: onClose)
    }

    func close() {
        monitors.forEach { NSEvent.removeMonitor($0) }
        monitors = []
        guard let p = panel else { return }
        panel = nil
        keys = nil
        p.parent?.removeChildWindow(p)
        p.orderOut(nil)
        closedAt = CACurrentMediaTime()
        parent?.makeKey()
        let done = onClose
        onClose = nil
        done?()
    }
}

/// Where a button sits on screen, for a popup to open under it.
final class Anchor: ObservableObject {
    weak var view: NSView?
    var screenRect: NSRect {
        guard let v = view, let w = v.window else { return .zero }
        return w.convertToScreen(v.convert(v.bounds, to: nil))
    }
}

struct AnchorReader: NSViewRepresentable {
    let anchor: Anchor
    func makeNSView(context: Context) -> NSView {
        let v = NSView()
        anchor.view = v
        return v
    }
    func updateNSView(_ v: NSView, context: Context) { anchor.view = v }
}

// MARK: - Menus

struct MenuRow: Identifiable {
    let id = UUID()
    var title: String
    var detail: String? = nil
    var checked = false
    var enabled = true
    var mark: AnyView? = nil
    var action: () -> Void = {}
}

/// A themed menu: a label, rows with ✓ on the current one and number key caps on the right.
/// While it's open, 1 to 9 pick a row, ↑↓ move, ↵ picks and esc closes. No ⌘.
final class MenuModel: ObservableObject {
    let title: String?
    let rows: [MenuRow]
    @Published var at: Int
    var close: () -> Void = {}
    var ticks = true  // the ✓ column; a menu that marks the current row its own way leaves it out
    var footer: (String, String, String)?  // a quiet last line: text, a command in body colour, text

    init(title: String?, rows: [MenuRow]) {
        self.title = title
        self.rows = rows
        at = rows.firstIndex(where: \.checked) ?? 0
    }

    func pick(_ i: Int) {
        guard rows.indices.contains(i), rows[i].enabled else { return }
        close()
        rows[i].action()
    }

    func key(_ e: NSEvent) -> Bool {
        switch e.keyCode {
        case 125: at = min(at + 1, rows.count - 1); return true
        case 126: at = max(at - 1, 0); return true
        case 36, 76: pick(at); return true
        default:
            if let c = e.charactersIgnoringModifiers, let n = Int(c), n >= 1, n <= min(9, rows.count) { pick(n - 1); return true }
            return false
        }
    }
}

struct MenuView: View {
    @ObservedObject var model: MenuModel
    var width: CGFloat
    @Environment(\.colorScheme) var scheme

    var body: some View {
        let p = Palette.of(scheme)
        VStack(alignment: .leading, spacing: 0) {
            if let t = model.title { Label(text: t).padding(.horizontal, 12).padding(.top, 8).padding(.bottom, 4) }
            ForEach(Array(model.rows.enumerated()), id: \.element.id) { i, r in
                Button { model.pick(i) } label: {
                    HStack(alignment: r.detail == nil ? .center : .top, spacing: 8) {
                        if model.ticks { Text(r.checked ? "✓" : "").foregroundStyle(p.ink).frame(width: 10) }
                        if let m = r.mark { m.padding(.top, r.detail == nil ? 0 : 4) }
                        VStack(alignment: .leading, spacing: 1) {
                            Text(r.title).foregroundStyle(r.enabled ? p.ink : p.muted).fontWeight(i == model.at ? .medium : .regular)
                            if let d = r.detail { Text(d).font(PaperMono.font(11)).foregroundStyle(r.enabled ? p.muted : p.faint) }
                        }
                        Spacer(minLength: 8)
                        if i < 9, r.enabled { Kbd(text: "\(i + 1)") }
                    }
                    .padding(.horizontal, 12).padding(.vertical, 8)
                    .background(i == model.at && r.enabled ? p.sel : .clear)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .disabled(!r.enabled)
                .onHover { if $0, r.enabled { model.at = i } }
            }
            if let f = model.footer {
                Text("\(Text(f.0).foregroundColor(p.muted))\(Text(f.1).foregroundColor(p.body))\(Text(f.2).foregroundColor(p.muted))")
                    .font(PaperMono.font(11))
                    .padding(.horizontal, 12).padding(.vertical, 8)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .overlay(alignment: .top) { Rule() }
            }
        }
        .font(PaperMono.font(12.5))
        .padding(.bottom, 4)
        .frame(width: width, alignment: .leading)
        .background(p.pop)
        .overlay(Rectangle().strokeBorder(p.line, lineWidth: 1))
        .environment(\.colorScheme, scheme)
    }
}

/// A button that opens a themed menu under itself.
struct MenuButton<L: View>: View {
    let popups: Popups
    var width: CGFloat = 220
    let menu: () -> MenuModel
    @ViewBuilder var label: () -> L
    @StateObject var anchor = Anchor()
    @Environment(\.colorScheme) var scheme

    var body: some View {
        Button {
            let m = menu()
            m.close = { popups.close() }
            popups.show(MenuView(model: m, width: width).environment(\.colorScheme, scheme), below: anchor.screenRect, width: width, keys: m.key)
        } label: {
            label().background(AnchorReader(anchor: anchor)).hitArea(6)
        }
        .buttonStyle(.plain)
    }
}
