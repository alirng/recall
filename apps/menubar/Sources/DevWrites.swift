// Development flags for step 4's writes, run off screen like the others in Dev.swift. They change
// notes, settings and files through recall api, so they refuse to run unless RECALL_HOME points away
// from ~/.agents/recall (a scratch copy, with RECALL_MEMORY beside it).
//
// `--write-shots DIR --note ID --forget-note ID` edits one note (preview, a memory run holding the
// notes, the save that follows it, a file changed meanwhile), forgets another and undoes it, then on
// This Mac opens the model menu, changes a coordination rule and removes the copies kept before
// upgrades. It saves a PNG of each state to DIR, logs what recall answered and how long it took, and
// quits. `--model-test OPTION` picks that model as the menu would and logs the answer.

import AppKit
import SwiftUI

extension Probe {
    static var scratch: Bool { API.recallHome != API.home + "/.agents/recall" }

    /// The server serves a scratch copy: its data folder and its notes are both away from the real
    /// ones. RECALL_HOME alone isn't enough: without RECALL_MEMORY, the notes would be the real ones.
    static func scratchServer(_ lib: Library) -> Bool {
        guard let i = lib.info, let home = i.home, let memory = i.memory else { return false }
        return home != "~/.agents/recall" && memory != "~/.agents/memory"
    }

    static func log(_ s: String) { FileHandle.standardError.write(Data((s + "\n").utf8)) }

    static func writeShots(_ w: WindowController, to dir: String) {
        guard scratch else { log("refusing: the write probes run only against a scratch RECALL_HOME"); NSApp.terminate(nil); return }
        let look = value("--appearance") == "light" ? "-light" : "-dark"
        func shot(_ name: String) { capture(w, to: dir + "/" + name + look + ".png"); log("shot \(name)") }
        typealias Step = (String, Double, (Library) -> Void)  // name, seconds to wait before the shot, what to do
        var steps: [Step] = []
        let editId = value("--note"), forgetId = value("--forget-note")

        steps.append(("edit", 4, { lib in
            if let editId { lib.openNote(editId) }
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) {
                lib.notes.edit()
                guard let e = lib.notes.editor else { log("edit: no editor (writes \(lib.info?.writes ?? []))"); return }
                // Reword the first rule, drop the second, add one: kept, reworded, removed and new.
                var lines = e.text.split(separator: "\n", omittingEmptySubsequences: false).map(String.init)
                if lines.count > 2 { lines.remove(at: 1) }
                if let first = lines.first { lines[0] = first.replacingOccurrences(of: ".", with: ", always.", options: [], range: first.range(of: ".", options: .backwards)) }
                lines.append("- Tag a release only after the smoke test passes.")
                e.text = lines.joined(separator: "\n")
                // Then type a rule key by key into the real text view, as a person would.
                DispatchQueue.main.asyncAfter(deadline: .now() + 1) {
                    guard let t = editorTextView(w) else { log("edit: no text view"); return }
                    w.window?.makeFirstResponder(t)
                    t.setSelectedRange(NSRange(location: (t.string as NSString).length, length: 0))
                    for ch in "\n- Run claude -p --no-session-persistence with \"quotes\" -- don't change it." {
                        t.insertText(String(ch), replacementRange: NSRange(location: NSNotFound, length: 0))
                    }
                    t.checkTextInDocument(nil)  // what a pause in typing would trigger
                    DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) {
                        log("edit: typed line reads: " + (e.text.split(separator: "\n").last.map(String.init) ?? ""))
                    }
                }
            }
        }))
        // A memory run holds the notes: a process takes notes.lock for 8 s, then commits nothing but
        // an empty commit, which is the "notes changed" event the waiting save goes again on.
        steps.append(("edit-busy", 4, { lib in
            let holder = Process()
            holder.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
            holder.arguments = ["-c", "import fcntl,subprocess,time;f=open('\(API.recallHome)/notes.lock','w');fcntl.flock(f,fcntl.LOCK_EX);time.sleep(8);f.close();"
                + "subprocess.run(['git','-C','\(lib.info?.memory ?? "/nonexistent")','commit','-q','--allow-empty','-m','test: a memory run let go'])"]
            try? holder.run()
            let asked = Date()
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { lib.notes.editor?.save() }
            func watch() {
                if lib.notes.editor == nil { log(String(format: "edit: saved %.1f s after Save, once the run let go", Date().timeIntervalSince(asked))); return }
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.5, execute: watch)
            }
            DispatchQueue.main.asyncAfter(deadline: .now() + 3, execute: watch)
        }))
        steps.append(("edit-saved", 12, { lib in
            log("edit: editor open \(lib.notes.editor != nil), state \(String(describing: lib.notes.editor?.state))")
        }))
        // The file changes under an open edit.
        steps.append(("edit-conflict", 5, { lib in
            lib.notes.edit()
            guard let e = lib.notes.editor, let path = lib.notes.note?.path else { return }
            e.text += "\n- One more rule."
            let full = (path as NSString).expandingTildeInPath
            if let h = FileHandle(forWritingAtPath: full) { h.seekToEndOfFile(); h.write(Data("\n".utf8)); try? h.close() }
            DispatchQueue.main.asyncAfter(deadline: .now() + 1) { e.save() }
        }))
        steps.append(("forget-sheet", 5, { lib in
            lib.notes.cancelEdit()
            guard let forgetId else { log("forget: no --forget-note"); return }
            lib.openNote(forgetId)
            DispatchQueue.main.asyncAfter(deadline: .now() + 3) {
                guard lib.notes.note?.note_id == forgetId else { log("forget: \(forgetId) didn't open"); return }
                lib.askForget()
            }
        }))
        steps.append(("forget-toast", 4, { lib in
            let asked = Date()
            lib.forgetting?.go()
            func watch() {
                if lib.toast != nil { log(String(format: "forget: toast %.0f ms after Forget note", Date().timeIntervalSince(asked) * 1000)); return }
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.05, execute: watch)
            }
            watch()
        }))
        steps.append(("forget-undone", 4, { lib in
            let asked = Date(), id = lib.toast?.noteId
            lib.undoForget()
            func watch() {
                if lib.toast == nil { log(String(format: "undo: note back %.0f ms after Undo, reader on it \(lib.notes.selected == id)", Date().timeIntervalSince(asked) * 1000)); return }
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.05, execute: watch)
            }
            watch()
        }))
        steps.append(("model-menu", 4, { lib in
            lib.go(.thisMac)
            guard let win = w.window, let s = NSScreen.main else { return }
            let p = Palette.of(win.effectiveAppearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua ? .dark : .light)
            let m = ModelPanel.menu(lib, p)
            _ = s
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) {
                let at = NSRect(x: win.frame.minX + 420, y: win.frame.maxY - 330, width: 300, height: 20)
                lib.popups.show(MenuView(model: m, width: 400), below: at, width: 400, keys: { _ in false })
            }
        }))
        steps.append(("coordination", 4, { lib in
            lib.popups.close()
            lib.setCoordination("overlap", "tell")
        }))
        steps.append(("remove-confirm", 4, { lib in
            lib.go(.thisMac)
            lib.facts.removeNote = nil
            lib.facts.confirmRemove = true
            lib.thisMacFocus.id = "End"
            lib.thisMacFocus.tick += 1
        }))
        steps.append(("removed", 4, { lib in
            let backups = (lib.facts.storage?.items ?? []).filter { $0.kind == "backup" && ($0.exists ?? true) }
            let asked = Date()
            lib.removeBackups(backups)
            func watch() {
                if case .done(let t)? = lib.facts.removeNote { log(String(format: "remove: %@ in %.0f ms", t, Date().timeIntervalSince(asked) * 1000)); return }
                if case .failed(let t)? = lib.facts.removeNote { log("remove failed: \(t)"); return }
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.05, execute: watch)
            }
            watch()
        }))
        if let only = value("--only")?.split(separator: ",").map(String.init) { steps = steps.filter { only.contains($0.0) } }
        func step(_ i: Int) {
            if i == 0, let lib = w.library, !scratchServer(lib) {
                log("refusing: recall api serves \(lib.info?.home ?? "?") and \(lib.info?.memory ?? "?"), not a scratch copy")
                NSApp.terminate(nil)
                return
            }
            guard i < steps.count, let lib = w.library else {
                w.library?.setCoordination(preset: "recommended")
                DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { NSApp.terminate(nil) }
                return
            }
            let (name, wait, act) = steps[i]
            act(lib)
            DispatchQueue.main.asyncAfter(deadline: .now() + wait) {
                shot(name)
                step(i + 1)
            }
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 7) { step(0) }
    }

    static func editorTextView(_ w: WindowController) -> NSTextView? {
        func find(_ v: NSView) -> NSTextView? {
            if let t = v as? NSTextView, t.isEditable { return t }
            for s in v.subviews { if let t = find(s) { return t } }
            return nil
        }
        return w.window?.contentView.flatMap(find)
    }

    /// `--model-test OPTION`: the model menu's pick, logged: the test call's answer and its time.
    static func modelTest(_ w: WindowController, _ option: String, to dir: String?) {
        guard scratch else { log("refusing: the write probes run only against a scratch RECALL_HOME"); NSApp.terminate(nil); return }
        DispatchQueue.main.asyncAfter(deadline: .now() + 7) {
            guard let lib = w.library else { return }
            guard scratchServer(lib) else {
                log("refusing: recall api serves \(lib.info?.home ?? "?") and \(lib.info?.memory ?? "?"), not a scratch copy")
                NSApp.terminate(nil)
                return
            }
            lib.go(.thisMac)
            let options = lib.facts.models?.options ?? []
            log("models: " + options.map { "\($0.id)\($0.now == true ? " (now)" : "")" }.joined(separator: ", "))
            guard let o = options.first(where: { $0.id == option }) else { log("no option \(option)"); NSApp.terminate(nil); return }
            let asked = Date()
            lib.useModel(o)
            func watch() {
                switch lib.facts.modelNote {
                case .done(let t)?, .failed(let t)?:
                    log(String(format: "model: %@ (%.1f s)", t, Date().timeIntervalSince(asked)))
                    DispatchQueue.main.asyncAfter(deadline: .now() + 2) {
                        if let dir { capture(w, to: dir + "/model-saved-" + (value("--appearance") ?? "dark") + ".png") }
                        NSApp.terminate(nil)
                    }
                default: DispatchQueue.main.asyncAfter(deadline: .now() + 0.25, execute: watch)
                }
            }
            watch()
        }
    }
}
