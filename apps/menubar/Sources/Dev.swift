// Development flags for measuring and checking the window without a person at the screen.
// `--open` opens the window at launch. `--appearance dark|light` forces one look. `--measure` logs
// the time from opening to the first notes drawn. `--shot DIR` saves the Memory and This Mac views
// in dark and light, then quits. `--scroll-notes` scrolls the whole notes list and logs the time.
// `--close-after SECONDS` closes the window again, to measure what a closed window leaves behind.
// `--panel-open-after SECONDS` and `--panel-close-after SECONDS` open and close the menu bar panel off
// screen, as clicking the icon would (with `--measure` it logs the time until the panel has its status).
// `--no-api` keeps the menu bar on `recall status --json`, as before recall api existed.
// `--open-after SECONDS` opens it once the menu bar has settled, as a real ⌘O would.
// `--event-storm N` replays N "chats changed" events 2 s apart and logs CPU and memory around them;
// with `--storm-force` each one redraws as if the data had changed.

import AppKit
import SwiftUI

enum Probe {
    static let on = CommandLine.arguments.contains("--measure")
    /// Any test flag: the window then stays off screen and the app never comes forward.
    static let background = ["--open", "--open-after", "--measure", "--shot", "--scroll-notes", "--scroll-chat", "--scroll-info",
                             "--switch-test", "--event-storm", "--close-after", "--ui-test", "--panel-open-after", "--panel-close-after"].contains { CommandLine.arguments.contains($0) }
    nonisolated(unsafe) static var opened: Date?

    static func start() { if on { opened = Date() } }

    /// Logged once the data is on screen: the next turn of the run loop, after SwiftUI laid it out.
    static func drawn(_ what: String) {
        guard on, let t = opened else { return }
        opened = nil
        DispatchQueue.main.async {
            FileHandle.standardError.write(Data(String(format: "first view (%@): %.0f ms\n", what, Date().timeIntervalSince(t) * 1000).utf8))
        }
    }

    static func value(_ flag: String) -> String? {
        guard let i = CommandLine.arguments.firstIndex(of: flag), i + 1 < CommandLine.arguments.count else { return nil }
        return CommandLine.arguments[i + 1]
    }

    static func appearance() {
        switch value("--appearance") {
        case "dark": NSApp.appearance = NSAppearance(named: .darkAqua)
        case "light": NSApp.appearance = NSAppearance(named: .aqua)
        default: break
        }
    }

    /// Saves Memory, Chats with find, the palette and This Mac in dark and light, each a PNG of the
    /// whole window with any popup drawn over it, all off screen.
    static func shots(_ w: WindowController, to dir: String) {
        let find = value("--find") ?? "recall"
        let steps: [(String, (Library) -> Void)] = [
            ("memory", { $0.go(.memory) }),
            ("chats-find", { lib in
                lib.go(.chats)
                if let c = lib.chats.rows.max(by: { ($0.messages ?? 0) < ($1.messages ?? 0) }) { lib.chats.select(c.id) }
                lib.find.query = find
            }),
            ("palette", { lib in
                lib.go(.memory)
                lib.palette.open()
            }),
            ("this-mac", { $0.go(.thisMac) }),
            // The themed menus, each opened where its button sits.
            ("menu-sort", { lib in
                lib.go(.memory)
                guard let win = w.window else { return }
                let m = MenuModel(title: "Sort notes", rows: NotesList.sorts.map { s in MenuRow(title: s.1, checked: lib.notes.sort == s.0) })
                let at = NSRect(x: win.frame.minX + 760, y: win.frame.maxY - 100, width: 100, height: 20)
                lib.popups.show(MenuView(model: m, width: 200), below: at, width: 200, keys: { _ in false })
            }),
            ("picker", { lib in
                lib.go(.memory)
                guard let win = w.window else { return }
                let pm = PickerModel(entries: lib.notes.projectCounts, total: lib.notes.allTotal, current: lib.notes.project, pick: { _ in })
                let at = NSRect(x: win.frame.minX + 230, y: win.frame.maxY - 100, width: 160, height: 20)
                lib.popups.show(PickerView(model: pm, library: lib), below: at, width: 300, height: 340, keys: { _ in false })
            }),
            ("switcher", { lib in
                lib.go(.thisMac)
                guard let win = w.window else { return }
                let p = Palette.of(win.appearance?.name == .aqua ? .light : .dark)
                let m = TitleBar(library: lib, search: lib.search).switcher(p)
                let at = NSRect(x: win.frame.maxX - 302, y: win.frame.maxY - 46, width: 288, height: 30)
                lib.popups.show(MenuView(model: m, width: 288), below: at, width: 288, keys: { _ in false })
            }),
        ]
        var plan: [(String, NSAppearance.Name, (Library) -> Void)] = []
        for look in [NSAppearance.Name.darkAqua, .aqua] {
            for (name, act) in steps { plan.append((name + (look == .darkAqua ? "-dark" : "-light"), look, act)) }
        }
        if let only = value("--only")?.split(separator: ",").map(String.init) { plan = plan.filter { only.contains($0.0) } }
        func step(_ i: Int) {
            guard i < plan.count, let lib = w.library else { NSApp.terminate(nil); return }
            let (name, look, act) = plan[i]
            lib.popups.close()
            w.window?.appearance = NSAppearance(named: look)
            act(lib)
            DispatchQueue.main.asyncAfter(deadline: .now() + 3) {
                capture(w, to: dir + "/" + name + ".png")
                step(i + 1)
            }
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 7) { step(0) }
    }

    static func capture(_ w: WindowController, to path: String) {
        guard let win = w.window, let frame = win.contentView?.superview else { return }
        let size = frame.bounds.size
        func rep(_ v: NSView) -> NSBitmapImageRep {
            // At 2x whatever screen it's on (a locked or headless Mac reports 1x).
            let r = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: Int(v.bounds.width * 2), pixelsHigh: Int(v.bounds.height * 2),
                                     bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
            r.size = v.bounds.size
            v.cacheDisplay(in: v.bounds, to: r)
            return r
        }
        let base = rep(frame)
        let image = NSImage(size: size)
        image.addRepresentation(base)
        if let pop = w.library?.popups.panel, let content = pop.contentView {
            let top = rep(content)
            let at = NSPoint(x: pop.frame.minX - win.frame.minX, y: pop.frame.minY - win.frame.minY)
            let W = Int(size.width * 2), H = Int(size.height * 2)
            guard let a = base.cgImage, let b = top.cgImage,
                  let cg = CGContext(data: nil, width: W, height: H, bitsPerComponent: 8, bytesPerRow: 0, space: CGColorSpace(name: CGColorSpace.sRGB)!,
                                     bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else { return }
            cg.draw(a, in: CGRect(x: 0, y: 0, width: W, height: H))
            cg.draw(b, in: CGRect(x: at.x * 2, y: at.y * 2, width: content.bounds.width * 2, height: content.bounds.height * 2))
            if let img = cg.makeImage() {
                try? NSBitmapImageRep(cgImage: img).representation(using: .png, properties: [:])?.write(to: URL(fileURLWithPath: path))
            }
            return
        }
        try? base.representation(using: .png, properties: [:])?.write(to: URL(fileURLWithPath: path))
    }

    static func keyEvent(_ chars: String, _ code: UInt16, _ flags: NSEvent.ModifierFlags = []) -> NSEvent {
        NSEvent.keyEvent(with: .keyDown, location: .zero, modifierFlags: flags, timestamp: 0, windowNumber: 0, context: nil,
                         characters: chars, charactersIgnoringModifiers: chars, isARepeat: false, keyCode: code)!
    }

    /// `--ui-test`: drives the menus, the palette, the picker and find through their key handlers,
    /// counts thin scrollers per section, and prints what happened. Leaves pins as they were.
    static func uiTest(_ w: WindowController) {
        func log(_ s: String) { FileHandle.standardError.write(Data((s + "\n").utf8)) }
        func later(_ s: Double, _ f: @escaping () -> Void) { DispatchQueue.main.asyncAfter(deadline: .now() + s, execute: f) }
        later(8) {
            guard let lib = w.library else { return }
            // A themed menu: 2 picks the second row, no ⌘.
            let sorts = NotesList.sorts
            let m = MenuModel(title: "Sort notes", rows: sorts.map { s in MenuRow(title: s.1, checked: lib.notes.sort == s.0, action: { lib.notes.set(sort: s.0) }) })
            _ = m.key(keyEvent("2", 19))
            log("menu: 2 picked sort \(lib.notes.sort) (want updated)")
            _ = m.key(keyEvent("1", 18))
            log("menu: 1 picked sort \(lib.notes.sort) (want last_seen)")
            // The palette: 3 with nothing typed goes to the third view; once typed, digits are text.
            lib.palette.open()
            let used = lib.palette.key(keyEvent("3", 20))
            log("palette: 3 on empty field used \(used), section \(lib.section.title) (want Projects)")
            lib.palette.open()
            lib.palette.query = "mac"
            log("palette: 3 after typing used \(lib.palette.key(keyEvent("3", 20))) (want false)")
            _ = lib.palette.key(keyEvent("\t", 48))
            log("palette: tab narrowed to \(lib.palette.narrow?.title ?? "none")")
            lib.palette.close()
            // The picker: filter, pin and unpin.
            let names = (lib.facts.projects?.projects ?? []).map { ($0.name, $0.chats) }
            let pm = PickerModel(entries: names, total: nil, current: nil, pick: { _ in })
            pm.filter = "re"
            let r = pm.rows(lib.pins)
            log("picker: 're' matches \(r.pinned.count + r.rest.count) of \(names.count), all-projects row \(r.showAll)")
            if let first = names.first?.0 {
                let had = lib.pins.contains(first)
                lib.togglePin(first)
                let saved = (AppState.load().pins ?? []).contains(first)
                lib.togglePin(first)
                log("picker: pin \(first) saved \(saved != had), restored \((AppState.load().pins ?? []).contains(first) == had)")
            }
            // Find in chat across two chats.
            lib.go(.chats)
            later(1.5) {
                let rows = lib.chats.rows.sorted { ($0.messages ?? 0) > ($1.messages ?? 0) }
                guard rows.count > 1 else { return }
                lib.chats.select(rows[0].id)
                lib.find.query = value("--find") ?? "the"
                later(4) {
                    let a = lib.find.hits.count, tick = lib.find.scroll.tick
                    lib.find.step(1)
                    log("find: \(a) hits in chat 1 (\(lib.chats.reading.messages.count) messages loaded), step moved to \(lib.find.current + 1), scrolled \(lib.find.scroll.tick > tick)")
                    lib.chats.select(rows[1].id)
                    later(4) {
                        log("find: query kept \"\(lib.find.query)\", \(lib.find.hits.count) hits in chat 2, at \(lib.find.current + 1)")
                        // Thin scrollers in every section built so far.
                        if let root = w.window?.contentView {
                            var thin = 0, other = 0
                            func walk(_ v: NSView) {
                                if let s = v as? NSScrollView { if s.verticalScroller is ThinScroller { thin += 1 } else { other += 1 } }
                                v.subviews.forEach(walk)
                            }
                            walk(root)
                            log("scrollers: \(thin) thin, \(other) not")
                        }
                        NSApp.terminate(nil)
                    }
                }
            }
        }
    }

    static func cpu() -> Double {
        var u = rusage()
        getrusage(RUSAGE_SELF, &u)
        return Double(u.ru_utime.tv_sec + u.ru_stime.tv_sec) + Double(u.ru_utime.tv_usec + u.ru_stime.tv_usec) / 1e6
    }

    static func footprint() -> Double {
        var info = task_vm_info_data_t()
        var n = mach_msg_type_number_t(MemoryLayout<task_vm_info_data_t>.size / MemoryLayout<natural_t>.size)
        _ = withUnsafeMutablePointer(to: &info) { $0.withMemoryRebound(to: integer_t.self, capacity: Int(n)) { task_info(mach_task_self_, task_flavor_t(TASK_VM_INFO), $0, &n) } }
        return Double(info.phys_footprint) / 1e6
    }

    static func storm(_ w: WindowController, _ n: Int) {
        func log(_ s: String) { FileHandle.standardError.write(Data((s + "\n").utf8)) }
        DispatchQueue.main.asyncAfter(deadline: .now() + 8) {
            let c0 = cpu(), f0 = footprint()
            log(String(format: "storm start: cpu %.2f s, footprint %.0f MB", c0, f0))
            func fire(_ i: Int) {
                guard i < n else {
                    DispatchQueue.main.asyncAfter(deadline: .now() + 3) {
                        log(String(format: "storm end: %d events, cpu +%.2f s (%.0f ms each), footprint %.0f MB", n, cpu() - c0, (cpu() - c0) / Double(n) * 1000, footprint()))
                        NSApp.terminate(nil)
                    }
                    return
                }
                // `--storm-force` forgets the last answers first, so every event decodes and redraws,
                // as when chats really change.
                if CommandLine.arguments.contains("--storm-force") { w.library?.last.removeAll() }
                w.library?.changed(["chats"])
                DispatchQueue.main.asyncAfter(deadline: .now() + 2) { fire(i + 1) }
            }
            fire(0)
        }
    }

    /// Runs `then` once the run loop has committed the next frame: an observer ordered after Core
    /// Animation's own commit, so the time covers SwiftUI's update, layout and the commit.
    static func afterFrame(_ then: @escaping () -> Void) {
        let o = CFRunLoopObserverCreateWithHandler(nil, CFRunLoopActivity.beforeWaiting.rawValue, false, Int.max) { _, _ in then() }
        // `repeats: false` above: it fires once and is removed.
        CFRunLoopAddObserver(CFRunLoopGetMain(), o, .commonModes)
    }

    /// Clicks through the sidebar twice, logging the time from each click to its first frame.
    static func switchSections(_ w: WindowController) {
        let order: [Place] = [.chats, .projects, .now, .thisMac, .search, .agent("codex"), .memory]
        let plan = order + order
        func log(_ s: String) { FileHandle.standardError.write(Data((s + "\n").utf8)) }
        func step(_ i: Int) {
            guard i < plan.count, let lib = w.library else {
                log(String(format: "footprint after every section: %.0f MB", footprint()))
                NSApp.terminate(nil)
                return
            }
            let place = plan[i], t = Date()
            lib.go(place)
            afterFrame { log(String(format: "%@ %@: %.0f ms", i < order.count ? "first" : "again", place.title, Date().timeIntervalSince(t) * 1000)) }
            DispatchQueue.main.asyncAfter(deadline: .now() + 2) { step(i + 1) }
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 8) { step(0) }
    }

    /// Prints each scroll view under the window, to see what the chat reader scrolls with.
    static func scrollInfo(_ w: WindowController) {
        func walk(_ v: NSView, _ depth: Int) {
            if let s = v as? NSScrollView {
                let doc = s.documentView
                FileHandle.standardError.write(Data(String(format: "%@%@ %.0fx%.0f doc %@ h %.0f line %.1f page %.1f elastic %d\n",
                    String(repeating: "  ", count: depth), String(describing: type(of: s)), s.frame.width, s.frame.height,
                    doc.map { String(describing: type(of: $0)) } ?? "-", doc?.frame.height ?? 0, s.verticalLineScroll, s.verticalPageScroll,
                    s.verticalScrollElasticity.rawValue).utf8))
            }
            for sub in v.subviews { walk(sub, depth + (v is NSScrollView ? 1 : 0)) }
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 5) {
            w.library?.go(.chats)
            DispatchQueue.main.asyncAfter(deadline: .now() + 2) {
                if let first = w.library?.chats.rows.max(by: { ($0.messages ?? 0) < ($1.messages ?? 0) }) { w.library?.chats.select(first.id) }
                DispatchQueue.main.asyncAfter(deadline: .now() + 3) {
                    if let root = w.window?.contentView { walk(root, 0) }
                    // Three mouse-wheel clicks (line units), then one trackpad-style move of 40 px.
                    func tables(_ v: NSView) -> [NSScrollView] {
                        (v is NSScrollView && (v as! NSScrollView).documentView is NSTableView ? [v as! NSScrollView] : []) + v.subviews.flatMap(tables)
                    }
                    if let root = w.window?.contentView, let chat = tables(root).max(by: { $0.documentView!.frame.height < $1.documentView!.frame.height }) {
                        for (label, units, amount) in [("3 wheel clicks", CGScrollEventUnit.line, Int32(-1)), ("40 px trackpad", CGScrollEventUnit.pixel, Int32(-40))] {
                            let before = chat.contentView.bounds.origin.y
                            // What a real wheel event goes through: the window's step fix at the
                            // pointer, then the list's own scrollWheel.
                            let mid = chat.convert(NSPoint(x: chat.bounds.midX, y: chat.bounds.midY), to: nil)
                            for _ in 0..<(units == .line ? 3 : 1) {
                                if let cg = CGEvent(scrollWheelEvent2Source: nil, units: units, wheelCount: 1, wheel1: amount, wheel2: 0, wheel3: 0),
                                   let e = NSEvent(cgEvent: cg) {
                                    if !e.hasPreciseScrollingDeltas { w.wheelStep(at: mid) }
                                    chat.scrollWheel(with: e)
                                }
                            }
                            FileHandle.standardError.write(Data(String(format: "%@: moved %.0f pt\n", label, chat.contentView.bounds.origin.y - before).utf8))
                        }
                    }
                    NSApp.terminate(nil)
                }
            }
        }
    }

    /// Scrolls the notes list a screen at a time until every page is loaded and the end shows.
    static func scrollNotes(_ w: WindowController) {
        func table(_ v: NSView) -> NSScrollView? {
            if let s = v as? NSScrollView, s.documentView is NSTableView { return s }
            for sub in v.subviews { if let s = table(sub) { return s } }
            return nil
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 4) { w.library?.go(.memory) }
        DispatchQueue.main.asyncAfter(deadline: .now() + 5) {
            guard let root = w.window?.contentView, let scroll = table(root), let notes = w.library?.notes else {
                FileHandle.standardError.write(Data("no notes table found\n".utf8))
                return
            }
            let started = Date()
            var steps = 0
            func tick() {
                let clip = scroll.contentView
                let doc = scroll.documentView!.frame.height
                let y = min(clip.bounds.origin.y + clip.bounds.height, doc - clip.bounds.height)
                clip.scroll(to: NSPoint(x: 0, y: max(0, y)))
                scroll.reflectScrolledClipView(clip)
                steps += 1
                let atEnd = clip.bounds.maxY >= doc - 1
                if atEnd && notes.rows.count >= notes.total {
                    let msg = String(format: "scrolled %d notes in %.1f s, %d screens\n", notes.rows.count, Date().timeIntervalSince(started), steps)
                    FileHandle.standardError.write(Data(msg.utf8))
                    return
                }
                if steps % 200 == 0 { FileHandle.standardError.write(Data("… \(notes.rows.count) of \(notes.total), y \(Int(clip.bounds.maxY)) of \(Int(doc))\n".utf8)) }
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.016) { tick() }
            }
            tick()
        }
    }

    /// Opens the longest chat and scrolls it to the end a screen per frame, loading every page,
    /// then logs the time, the slowest frame and how long find takes to count its matches.
    static func scrollChat(_ w: WindowController) {
        func tables(_ v: NSView) -> [NSScrollView] {
            (v is NSScrollView && (v as! NSScrollView).documentView is NSTableView ? [v as! NSScrollView] : []) + v.subviews.flatMap(tables)
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 5) { w.library?.go(.chats) }
        DispatchQueue.main.asyncAfter(deadline: .now() + 7) {
            guard let lib = w.library, let longest = lib.chats.rows.max(by: { ($0.messages ?? 0) < ($1.messages ?? 0) }) else { return }
            lib.chats.select(longest.id)
            DispatchQueue.main.asyncAfter(deadline: .now() + 3) {
                guard let root = w.window?.contentView,
                      let scroll = tables(root).max(by: { $0.documentView!.frame.height < $1.documentView!.frame.height }) else { return }
                let reading = lib.chats.reading
                let started = Date()
                var last = Date(), worst = 0.0, steps = 0
                func tick() {
                    let now = Date()
                    worst = max(worst, now.timeIntervalSince(last)); last = now
                    let clip = scroll.contentView
                    let doc = scroll.documentView!.frame.height
                    clip.scroll(to: NSPoint(x: 0, y: max(0, min(clip.bounds.maxY, doc - clip.bounds.height))))
                    scroll.reflectScrolledClipView(clip)
                    steps += 1
                    if clip.bounds.maxY >= doc - 1, reading.transcript?.next_offset == nil, !reading.loading {
                        let total = Date().timeIntervalSince(started)
                        let f = Date()
                        lib.find.query = value("--find") ?? "the"
                        lib.find.recount(reading)
                        let findMs = Date().timeIntervalSince(f) * 1000
                        FileHandle.standardError.write(Data(String(format: "chat of %d messages: scrolled in %.1f s, %d frames, %.1f ms a frame, slowest %.0f ms; find counted %d matches in %.0f ms\n",
                            reading.messages.count, total, steps, total * 1000 / Double(steps), worst * 1000, lib.find.hits.count, findMs).utf8))
                        // Then wheel speed: back to the top and down 48 pt a frame (two wheel
                        // clicks), timing each frame's scroll, layout and draw.
                        lib.find.query = ""
                        clip.scroll(to: .zero); scroll.reflectScrolledClipView(clip)
                        w.window?.displayIfNeeded()
                        var times: [Double] = []
                        func wheel() {
                            let t0 = Date()
                            clip.scroll(to: NSPoint(x: 0, y: min(clip.bounds.origin.y + 48, scroll.documentView!.frame.height - clip.bounds.height)))
                            scroll.reflectScrolledClipView(clip)
                            w.window?.displayIfNeeded()
                            times.append(Date().timeIntervalSince(t0) * 1000)
                            if times.count < 600 { DispatchQueue.main.asyncAfter(deadline: .now() + 0.016) { wheel() }; return }
                            let sorted = times.sorted()
                            FileHandle.standardError.write(Data(String(format: "wheel speed: %d frames, mean %.1f ms, p95 %.1f ms, max %.0f ms, over 16 ms %d\n",
                                times.count, times.reduce(0, +) / Double(times.count), sorted[Int(Double(sorted.count) * 0.95)], sorted.last!, times.filter { $0 > 16 }.count).utf8))
                            NSApp.terminate(nil)
                        }
                        DispatchQueue.main.asyncAfter(deadline: .now() + 1) { wheel() }
                        return
                    }
                    DispatchQueue.main.asyncAfter(deadline: .now() + 0.016) { tick() }
                }
                tick()
            }
        }
    }
}
