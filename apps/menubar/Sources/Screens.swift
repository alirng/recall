// The small screens and pieces Recall adds around the window: the board's squares and ✓ lines,
// notifications, the login item, and "Move Recall to Applications?". The four small screens
// (move, install failure, end of setup, This Mac's login row and Set up again) are kept here and in
// Setup.swift behind plain functions, so each can change or go without touching the rest.

import AppKit
import ServiceManagement
import SwiftUI
import UserNotifications

// MARK: - Pieces

/// One value a view can watch, for places that would use @State (whose macro plugin the Command Line Tools do not ship).
final class Held<T>: ObservableObject {
    @Published var value: T
    init(_ value: T) { self.value = value }
}

/// The boards' `.btn.main`: an outline in ink.
struct MainButton: View {
    let title: String
    var enabled = true
    var yellow = false  // the one action on a setup screen
    var action: () -> Void
    @StateObject var hover = Hover()
    @Environment(\.colorScheme) var scheme
    var body: some View {
        let p = Palette.of(scheme)
        Button(action: action) {
            let fill = yellow && enabled
            Text(title).font(PaperMono.font(12.5, fill ? 600 : 500)).foregroundStyle(fill ? Color(hex: 0x141416) : enabled ? p.ink : p.faint)
                .padding(.horizontal, 12).frame(minHeight: 28)
                .background(fill ? p.accent.opacity(hover.on ? 0.86 : 1) : enabled && hover.on ? p.sel : Color.clear)
                .overlay(Rectangle().strokeBorder(fill ? p.accent : enabled ? p.ink : p.faint, lineWidth: 1))
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .disabled(!enabled)
        .onHover { hover.on = $0 }
    }
}

/// A row of squares filled left to right: the board's progress.
struct SquareBar: View {
    let filled: Int
    var total = 12
    @Environment(\.colorScheme) var scheme
    var body: some View {
        let p = Palette.of(scheme)
        HStack(spacing: 4) {
            ForEach(0..<total, id: \.self) { i in
                Rectangle().fill(i < filled ? p.accent : Color.clear)
                    .overlay(Rectangle().strokeBorder(i < filled ? p.accent : p.faint, lineWidth: 1))
                    .frame(width: 8, height: 8)
            }
        }
    }
}

/// "✓ Installed recall 0.6.0": an amber tick and the line.
struct CheckLine: View {
    let text: String
    @Environment(\.colorScheme) var scheme
    var body: some View {
        let p = Palette.of(scheme)
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Text("✓").foregroundStyle(p.accentText).fontWeight(.medium)
            Text(text).foregroundStyle(p.ink)
        }
        .font(PaperMono.font(12.5))
    }
}

/// A board row: a square (filled amber when on), a name, a muted note on the right.
struct PickRow: View {
    let name: String
    var note = ""
    var on = true
    var pick: (() -> Void)? = nil
    @Environment(\.colorScheme) var scheme
    var body: some View {
        let p = Palette.of(scheme)
        let row = HStack(spacing: 8) {
            Rectangle().fill(on ? p.accent : Color.clear)
                .overlay(Rectangle().strokeBorder(on ? p.accent : p.faint, lineWidth: 1))
                .frame(width: 8, height: 8)
            Text(name).foregroundStyle(p.ink).lineLimit(1)
            Spacer(minLength: 8)
            if !note.isEmpty { Text(note).foregroundStyle(p.muted).lineLimit(1) }
        }
        .font(PaperMono.font(12.5))
        .padding(.vertical, 4)
        .contentShape(Rectangle())
        if let pick { Button(action: pick) { row }.buttonStyle(.plain) } else { row }
    }
}

// MARK: - Notifications

/// A failed night's one notification. In a test run nothing reaches Notification Center: it is logged.
final class Notifier: NSObject, UNUserNotificationCenterDelegate {
    static let shared = Notifier()
    var onClick: () -> Void = {}
    nonisolated(unsafe) static var sent: [String] = []  // what a test run would have shown

    func setUp() {
        guard !Probe.background else { return }
        UNUserNotificationCenter.current().delegate = self
    }

    /// Asks macOS for permission (it shows its own sheet the first time). Calls `done` with the answer.
    func authorize(_ done: @escaping (Bool) -> Void) {
        if Probe.background { Probe.log("would ask macOS for notification permission"); done(true); return }
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert]) { ok, _ in
            DispatchQueue.main.async { done(ok) }
        }
    }

    func post(_ body: String, id: String) {
        if Probe.background { Self.sent.append(body); Probe.log("would notify: recall · \(body)"); return }
        let c = UNMutableNotificationContent()
        c.title = "Recall"
        c.body = body
        UNUserNotificationCenter.current().add(UNNotificationRequest(identifier: id, content: c, trigger: nil))
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse, withCompletionHandler done: @escaping () -> Void) {
        DispatchQueue.main.async { self.onClick() }
        done()
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification, withCompletionHandler done: @escaping (UNNotificationPresentationOptions) -> Void) {
        done([.banner])
    }
}

// MARK: - Open when I log in

enum LoginItem {
    nonisolated(unsafe) static var fake = false

    static var on: Bool { Probe.background ? fake : SMAppService.mainApp.status == .enabled }

    /// Turns the login item on or off. Returns a reason when macOS refused.
    @discardableResult
    static func set(_ on: Bool) -> String? {
        if Probe.background { fake = on; Probe.log("would \(on ? "register" : "unregister") the login item"); return nil }
        do {
            if on { try SMAppService.mainApp.register() } else { try SMAppService.mainApp.unregister() }
            return nil
        } catch {
            return error.localizedDescription
        }
    }
}

// MARK: - Move to Applications

/// Updates replace the app in place, which needs it in Applications. An app opened from a disk image
/// or from the translocated copy macOS runs a freshly downloaded app from is asked once.
enum Placement {
    static var needsMove: Bool {
        if Probe.value("--scene") == "move" { return true }
        let path = Bundle.main.bundlePath
        if path.hasPrefix("/Applications/") || path.hasPrefix(NSHomeDirectory() + "/Applications/") { return false }
        return path.contains("/AppTranslocation/") || path.hasPrefix("/Volumes/")
    }

    static let destination = "/Applications/Recall.app"
    /// Moves the app into Applications and reopens it from there. A copy that is already there is never
    /// replaced: it is opened instead. Otherwise the copy is made under a temporary name and renamed into
    /// place. The running app quits only once the other has really launched; any failure is handed to
    /// `done` as a reason and leaves this app running.
    static func move(done: @escaping (String?) -> Void) {
        if Probe.background { Probe.log("would copy \(Bundle.main.bundlePath) to \(destination), open it and quit"); done(nil); return }
        let fm = FileManager.default
        let target = URL(fileURLWithPath: destination)
        let existing = fm.fileExists(atPath: destination)
        if !existing {
            let temp = "/Applications/.Recall.app.copying"
            do {
                try? fm.removeItem(atPath: temp)  // only ever left by an earlier, interrupted copy of ours
                try fm.copyItem(atPath: Bundle.main.bundlePath, toPath: temp)
                try fm.moveItem(atPath: temp, toPath: destination)
            } catch {
                try? fm.removeItem(atPath: temp)
                done(error.localizedDescription)
                return
            }
        }
        let config = NSWorkspace.OpenConfiguration()
        // The copy just made starts beside this app; one that was already there is brought forward if it runs.
        config.createsNewApplicationInstance = !existing
        NSWorkspace.shared.openApplication(at: target, configuration: config) { app, error in
            DispatchQueue.main.async {
                if app != nil, error == nil {
                    done(nil)
                    NSApp.terminate(nil)
                } else {
                    done("Couldn’t open the copy in Applications: " + (error?.localizedDescription ?? "it didn’t start") + ". This app is still running.")
                }
            }
        }
    }
}

struct MoveView: View {
    var move: (@escaping (String?) -> Void) -> Void
    var later: () -> Void
    @StateObject private var failure = Held<String?>(nil)
    @Environment(\.colorScheme) var scheme
    var body: some View {
        let p = Palette.of(scheme)
        VStack(alignment: .leading, spacing: 16) {
            Text("Move Recall to Applications?").font(PaperMono.font(14, 520)).foregroundStyle(p.ink)
                .fixedSize(horizontal: false, vertical: true)
            Text("Updates need it there.").font(PaperMono.font(12.5, 390, prose: true)).foregroundStyle(p.muted)
            if let why = failure.value { Text(why).font(PaperMono.font(12.5)).foregroundStyle(p.kind("open")).fixedSize(horizontal: false, vertical: true) }
            Spacer(minLength: 0)
            HStack(spacing: 8) {
                MainButton(title: "Move and reopen", yellow: true) { move { failure.value = $0 } }
                WindowButton(title: "Not now", action: later)
            }
        }
        .padding(24)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .background(p.paper)
        .font(PaperMono.font(12.5))
    }
}

// MARK: - A small window

/// A small titled window for the screens that are not the recall window: the move question and setup.
/// A test run draws it far off screen and never brings it forward.
final class MiniWindow: NSObject, NSWindowDelegate {
    var window: RecallWindow?
    var onClose: () -> Void = {}

    var isOpen: Bool { window?.isVisible ?? false }

    func show<V: View>(_ view: V, size: NSSize, title: String) {
        if let window {
            if Probe.background { window.orderBack(nil); return }
            NSApp.activate(ignoringOtherApps: true)
            window.makeKeyAndOrderFront(nil)
            return
        }
        let screen = NSScreen.main?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1440, height: 900)
        let frame = NSRect(x: screen.midX - size.width / 2, y: screen.midY - size.height / 2 + 30, width: size.width, height: size.height)
        let w = RecallWindow(contentRect: frame, styleMask: [.titled, .closable, .fullSizeContentView], backing: .buffered, defer: false)
        w.titlebarAppearsTransparent = true
        w.titleVisibility = .hidden
        w.title = title
        w.isRestorable = false
        w.isReleasedWhenClosed = false
        w.delegate = self
        w.contentView = NSHostingView(rootView: view.frame(width: size.width, height: size.height))
        window = w
        centerTrafficLights(w)
        if Probe.background {
            w.setFrameOrigin(NSPoint(x: -20000, y: -20000))
            w.orderBack(nil)
        } else {
            NSApp.setActivationPolicy(.regular)
            NSApp.activate(ignoringOtherApps: true)
            w.makeKeyAndOrderFront(nil)
        }
    }

    func close() {
        window?.close()
    }

    /// The traffic lights centred on a 48 pt title row, as the recall window does.
    func centerTrafficLights(_ w: NSWindow) {
        guard let close = w.standardWindowButton(.closeButton), let box = close.superview?.superview else { return }
        var f = box.frame
        f.size.height = WindowController.titlebar
        f.origin.y = w.frame.height - WindowController.titlebar
        box.frame = f
        for kind in [NSWindow.ButtonType.closeButton, .miniaturizeButton, .zoomButton] {
            w.standardWindowButton(kind).map { $0.setFrameOrigin(NSPoint(x: $0.frame.origin.x, y: (WindowController.titlebar - $0.frame.height) / 2)) }
        }
        // AppKit lays the title bar out again as the window first shows and when its look changes.
        for t in [0.05, 0.4, 1.0, 2.0] {
            DispatchQueue.main.asyncAfter(deadline: .now() + t) { [weak self, weak w] in if let w, self?.window === w { self?.centerTrafficLights2(w) } }
        }
    }

    func centerTrafficLights2(_ w: NSWindow) {
        guard let close = w.standardWindowButton(.closeButton), let box = close.superview?.superview else { return }
        var f = box.frame
        f.size.height = WindowController.titlebar
        f.origin.y = w.frame.height - WindowController.titlebar
        box.frame = f
        for kind in [NSWindow.ButtonType.closeButton, .miniaturizeButton, .zoomButton] {
            w.standardWindowButton(kind).map { $0.setFrameOrigin(NSPoint(x: $0.frame.origin.x, y: (WindowController.titlebar - $0.frame.height) / 2)) }
        }
    }

    func windowWillClose(_ note: Notification) {
        window?.contentView = nil
        window?.delegate = nil
        window = nil
        onClose()
    }
}

final class MoveController {
    let mini = MiniWindow()

    func show() {
        mini.show(MoveView(move: { Placement.move(done: $0) }, later: { [weak self] in
            AppState.update { $0.moveDeclined = true }
            self?.mini.close()
        }), size: NSSize(width: 460, height: 210), title: "Recall")
        mini.onClose = { if !Probe.background, !(NSApp.windows.contains { $0.isVisible && $0 !== self.mini.window }) { NSApp.setActivationPolicy(.accessory) } }
    }
}
