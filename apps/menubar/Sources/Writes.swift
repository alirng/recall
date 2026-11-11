// Step 4: the window writes. Editing a note (board G1) and forgetting one with Undo (G2) here; the
// model, coordination rules and removing copies kept before upgrades (G3) are in ThisMac.swift.
// Every change is one request to recall api, which runs recall's own code for it: the app never
// touches a note or a setting itself. No timers run: the preview waits for typing to pause (a one-shot
// delay), the Undo toast goes after 10 s (one-shot), and a change refused because a memory run holds
// the notes goes again when recall says the notes or the run changed.

import AppKit
import SwiftUI

/// Shown where a control would change something this recall can't: it predates writes.
let updateToWrite = "Update recall to change this here"

// MARK: - Editing a note (G1)

/// A note being edited: its text, what saving it would record, and why Save waits. It stays while you
/// look at other notes, so a draft isn't lost; the list marks the note "editing".
final class Editor: ObservableObject {
    enum State: Equatable { case editing, saving, busy, conflict, failed(String) }
    let noteId: String
    let original: String
    var stamp: String?
    @Published var text: String { didSet { if text != oldValue { typed() } } }
    @Published var reading: NoteReading?
    @Published var state: State = .editing
    weak var notes: NotesModel?
    var pending: DispatchWorkItem?
    var generation = 0
    var retry = false  // Save was pressed while a memory run held the notes

    init(_ n: Note, notes: NotesModel) {
        noteId = n.note_id
        original = n.body ?? ""
        stamp = n.stamp
        text = n.body ?? ""
        self.notes = notes
    }

    /// Nothing left that reads as a rule: blank, or only marks ("-"), as recall's preview says.
    var empty: Bool { text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || reading?.empty == true }
    var dirty: Bool { text.trimmingCharacters(in: .whitespacesAndNewlines) != original.trimmingCharacters(in: .whitespacesAndNewlines) }
    var canSave: Bool {
        guard dirty, !empty else { return false }
        if case .failed = state { return true }
        return state == .editing
    }

    /// What saving would record, asked for a moment after typing pauses.
    func typed() {
        pending?.cancel()
        let work = DispatchWorkItem { [weak self] in self?.preview() }
        pending = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.25, execute: work)
    }

    func preview() {
        generation += 1
        let g = generation
        API.send("POST", "/v1/notes/\(API.q(noteId))/preview", ["text": text], NoteReading.self) { [weak self] r in
            guard let self, g == self.generation, case .success(let reading) = r else { return }
            self.reading = reading
        }
    }

    func save() {
        guard dirty, !empty, state != .saving, state != .conflict else { return }
        state = .saving
        let started = Date()
        API.send("PUT", "/v1/notes/\(API.q(noteId))", ["text": text, "stamp": stamp ?? ""], Edited.self) { [weak self] r in
            guard let self else { return }
            if Probe.on { FileHandle.standardError.write(Data(String(format: "save: %.0f ms\n", Date().timeIntervalSince(started) * 1000).utf8)) }
            switch r {
            case .success:
                self.retry = false
                self.notes?.saved(self)
            case .failure(let f) where f.code == "busy":
                self.state = .busy
                self.retry = true
            case .failure(let f) where f.code == "conflict":
                self.state = .conflict
                self.retry = false
            case .failure(let f):
                self.state = .failed(f.message)
                self.retry = false
            }
        }
    }

    /// recall said the notes or the memory run changed: a save that waited on the run goes again.
    func runMoved() {
        if state == .busy, retry { save() }
    }

    func copyEdit() {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
    }
}

extension NotesModel {
    /// A note the window may change: an active note, on a recall that takes writes.
    func editable(_ n: Note) -> Bool {
        (library?.canWrite("notes") ?? false) && (n.status ?? "active") == "active" && !(n.archived ?? false)
    }

    func edit() {
        guard let n = note, editable(n) else { return }
        if editor?.noteId == n.note_id { return }
        if let e = editor, e.dirty { return }  // one draft at a time: the other note's edit is still open
        let e = Editor(n, notes: self)
        editor = e
        e.preview()
    }

    func cancelEdit() {
        editor?.pending?.cancel()
        editor = nil
    }

    func saved(_ e: Editor) {
        if editor === e { editor = nil }
        refresh()
    }

    /// The note changed under the edit: show the new version. "Copy my edit" first keeps the draft.
    func reloadEditor(_ e: Editor) {
        API.get("/v1/notes/\(API.q(e.noteId))", Note.self) { r in
            guard case .success(let n) = r else { self.editor = nil; return }
            if self.selected == n.note_id { self.note = n }
            let fresh = Editor(n, notes: self)
            self.editor = fresh
            fresh.preview()
        }
    }

    /// A note was forgotten: the list moves to the next one.
    func forgot(_ id: String) {
        if editor?.noteId == id { editor = nil }
        guard let i = rows.firstIndex(where: { $0.id == id }) else { refresh(); return }
        let next = i + 1 < rows.count ? rows[i + 1] : (i > 0 ? rows[i - 1] : nil)
        rows.remove(at: i)
        total = max(0, total - 1)
        if let next { select(next.id) } else { note = nil; selected = nil }
    }
}

/// The reader as one text box (G1, option a): the note's rules one per line, as in its file, and
/// below it what saving records, rule by rule.
struct EditView: View {
    @ObservedObject var editor: Editor
    let library: Library
    @FocusState var focused: Bool
    @EnvironmentObject var facts: Facts
    @Environment(\.colorScheme) var scheme

    var body: some View {
        let p = Palette.of(scheme)
        VStack(alignment: .leading, spacing: 16) {
            VStack(alignment: .leading, spacing: 8) {
                Label(text: "Rules · one per line, as in the note's file")
                TextEditor(text: $editor.text)
                    .font(PaperMono.font(14, 390, prose: true))
                    .lineSpacing(9)
                    .foregroundStyle(p.ink)
                    .scrollContentBackground(.hidden)
                    .focused($focused)
                    .background(PlainText())
                    .padding(.horizontal, 12).padding(.vertical, 8)
                    .frame(height: 206)
                    .background(p.card)
                    .overlay(Rectangle().strokeBorder(p.ink, lineWidth: 1))
                    .disabled(editor.state == .saving)
            }
            records(p)
            strip(p)
            HStack(alignment: .center, spacing: 8) {
                WindowButton(title: editor.state == .saving ? "Saving…" : "Save", soon: !editor.canSave, style: .primary, keys: "⌘S",
                             why: editor.empty ? "To drop the whole note, use Forget" : editor.state == .busy ? "Waiting for the memory run" : "Nothing changed yet") { editor.save() }
                WindowButton(title: "Cancel", keys: "esc") { library.notes.cancelEdit() }
                Spacer(minLength: 12)
                Text(editor.empty ? "An empty note has no rules left. To drop the whole note, use Forget." : "Saved to the note's file and its history. Agents see it from their next session.")
                    .font(PaperMono.font(11)).foregroundStyle(p.muted).lineSpacing(3)
                    .frame(maxWidth: 300, alignment: .leading).fixedSize(horizontal: false, vertical: true)
            }
        }
        .frame(maxWidth: 760, alignment: .leading)
        .onAppear { if !Probe.background { DispatchQueue.main.async { focused = true } } }
    }

    @ViewBuilder
    func records(_ p: Palette) -> some View {
        let rows = editor.reading?.changes ?? []
        if !rows.isEmpty {
            VStack(alignment: .leading, spacing: 0) {
                Label(text: "On save, recall records").padding(.bottom, 4)
                ForEach(rows) { c in
                    VStack(spacing: 0) {
                        Rule()
                        HStack(alignment: .firstTextBaseline, spacing: 12) {
                            RecordTag(kind: c.kind).frame(width: 92, alignment: .leading)
                            line(c, p).fixedSize(horizontal: false, vertical: true).frame(maxWidth: .infinity, alignment: .leading)
                        }
                        .padding(.vertical, 8)
                    }
                }
                Rule()
            }
            .font(PaperMono.font(12.5))
        }
    }

    /// One recorded change in words: struck through when it goes, and what follows from it.
    func line(_ c: NoteReading.Change, _ p: Palette) -> Text {
        switch c.kind {
        case "removed":
            return Text("\(Text(c.text).strikethrough().foregroundColor(p.muted))\(Text(" Not learned again from these chats.").foregroundColor(p.body))")
        case "merged":
            return Text("\(Text(c.text).strikethrough().foregroundColor(p.muted))\(Text(" Folded into the rule that holds its words.").foregroundColor(p.body))")
        case "new", "reworded":
            // A new rule in your words is yours; one holding a command or a link isn't vouched for (said.py).
            let mark = c.said_by == "developer" ? " Marked as said by you." : c.kind == "new" && c.said_by == "unverified" ? " Not marked as said by you: it holds a command or a link." : ""
            return Text("\(Text(c.text).foregroundColor(p.ink))\(Text(mark).foregroundColor(p.muted))")
        case "restored":
            return Text("\(Text(c.text).foregroundColor(p.ink))\(Text(" Back from the retired rules.").foregroundColor(p.muted))")
        default:
            return Text(c.text).foregroundColor(p.body)
        }
    }

    @ViewBuilder
    func strip(_ p: Palette) -> some View {
        switch editor.state {
        case .busy:
            let since = facts.runs?.latest.flatMap { $0.finished == nil ? date($0.started) : nil }
            Strip(color: p.accentText, text: "A memory run is writing notes." + (since.map { " It started \(clock($0))." } ?? "") + " Your edit stays here, and Save works when the run ends.")
        case .conflict:
            VStack(alignment: .leading, spacing: 8) {
                Strip(color: p.kind("open"), text: "This note's file changed since you opened it, from another editor or a memory run.")
                HStack(spacing: 8) {
                    WindowButton(title: "Reload note") { library.notes.reloadEditor(editor) }
                    WindowButton(title: "Copy my edit") { editor.copyEdit() }
                }
            }
        case .failed(let why):
            Strip(color: p.kind("open"), text: "That didn't save: \(why)")
        default:
            EmptyView()
        }
    }
}

/// Rules hold commands, flags and paths, so the editor types exactly what you type: no smart quotes
/// or dashes (`--flag` would become an em dash), no text replacement or autocorrect, whatever the
/// system settings say. SwiftUI's TextEditor has no say in these, so this finds its text view.
struct PlainText: NSViewRepresentable {
    final class Hook: NSView {
        override func viewDidMoveToWindow() {
            super.viewDidMoveToWindow()
            DispatchQueue.main.async { [weak self] in self?.apply() }
        }

        func apply() {
            var root: NSView? = superview
            while let v = root, !(String(describing: type(of: v)).hasPrefix("NSHostingView")) { root = v.superview }
            func walk(_ v: NSView) {
                if let t = v as? NSTextView, t.isEditable {
                    if Probe.on {
                        Probe.log("editor text view before: quotes \(t.isAutomaticQuoteSubstitutionEnabled), dashes \(t.isAutomaticDashSubstitutionEnabled), replacement \(t.isAutomaticTextReplacementEnabled), autocorrect \(t.isAutomaticSpellingCorrectionEnabled)")
                    }
                    if CommandLine.arguments.contains("--keep-substitutions") { return }  // to show what the fix prevents
                    t.isAutomaticQuoteSubstitutionEnabled = false
                    t.isAutomaticDashSubstitutionEnabled = false
                    t.isAutomaticTextReplacementEnabled = false
                    t.isAutomaticSpellingCorrectionEnabled = false
                    t.isAutomaticLinkDetectionEnabled = false
                    t.smartInsertDeleteEnabled = false
                    if Probe.on {
                        Probe.log("editor text view: quotes \(t.isAutomaticQuoteSubstitutionEnabled), dashes \(t.isAutomaticDashSubstitutionEnabled), replacement \(t.isAutomaticTextReplacementEnabled), autocorrect \(t.isAutomaticSpellingCorrectionEnabled)")
                    }
                }
                v.subviews.forEach(walk)
            }
            if let root { walk(root) }
        }
    }
    func makeNSView(context: Context) -> Hook { Hook() }
    func updateNSView(_ v: Hook, context: Context) {}
}

/// The boards' uppercase record tag: kept in grey, reworded and new in ink, removed dashed.
struct RecordTag: View {
    let kind: String
    @Environment(\.colorScheme) var scheme
    var body: some View {
        let p = Palette.of(scheme)
        let ink = ["reworded", "new", "restored"].contains(kind)
        let dashed = ["removed", "merged"].contains(kind)
        Text((kind == "restored" ? "back" : kind).uppercased())
            .font(PaperMono.font(11, 500)).tracking(0.6)
            .foregroundStyle(ink ? p.ink : p.muted)
            .padding(.horizontal, 4).frame(height: 17)
            .overlay(Rectangle().strokeBorder(ink ? p.ink : p.line, style: StrokeStyle(lineWidth: 1, dash: dashed ? [3, 2] : [])))
            .fixedSize()
    }
}

/// A state line in a dashed box, with a square in the state's colour.
struct Strip: View {
    let color: Color
    let text: String
    @Environment(\.colorScheme) var scheme
    var body: some View {
        let p = Palette.of(scheme)
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Rectangle().strokeBorder(color, lineWidth: 1).frame(width: 8, height: 8)
            Text(text).foregroundStyle(p.ink).fixedSize(horizontal: false, vertical: true)
        }
        .font(PaperMono.font(12.5)).lineSpacing(3)
        .padding(.horizontal, 12).padding(.vertical, 8)
        .frame(maxWidth: .infinity, alignment: .leading)
        .overlay(Dotted(color: p.line, dash: [3, 3]))
    }
}

// MARK: - Forgetting a note (G2)

/// The Forget sheet's state. A memory run holding the notes makes it wait, then go ahead.
final class Forgetting: ObservableObject {
    let note: Note
    weak var library: Library?
    @Published var waiting = false
    @Published var sending = false
    @Published var error: String?
    /// An edit of this note is open with changes not saved: forgetting drops it too (see forgot()).
    let hasDraft: Bool

    init(_ note: Note, library: Library) {
        self.note = note
        self.library = library
        hasDraft = library.notes.editor.map { $0.noteId == note.note_id && $0.dirty } ?? false
    }

    func go() {
        guard !sending, error == nil else { return }
        sending = true
        let id = note.note_id
        API.send("POST", "/v1/notes/\(API.q(id))/forget", ["stamp": note.stamp ?? ""], Forgotten.self) { [weak self] r in
            guard let self, let library = self.library else { return }
            self.sending = false
            switch r {
            case .success(let f):
                self.waiting = false
                library.forgetting = nil
                library.popups.close()
                library.notes.forgot(id)
                library.show(Toast(title: f.title, noteId: id, commit: f.commit))
            case .failure(let f) where f.code == "busy":
                self.waiting = true
            case .failure(let f) where f.code == "conflict":
                // The reader shows the note as it is now, so a second try forgets what you see.
                self.error = "This note changed since you opened it. Close this, look at it again, then forget it if you still want to."
                library.notes.select(id)
            case .failure(let f):
                self.error = f.message
            }
        }
    }

    func runMoved() { if waiting { go() } }
}

struct ForgetSheet: View {
    @ObservedObject var model: Forgetting
    @Environment(\.colorScheme) var scheme

    var body: some View {
        let p = Palette.of(scheme)
        let chats = model.note.source_chats?.count ?? 0
        ZStack(alignment: .top) {
            p.scrim.contentShape(Rectangle()).onTapGesture { model.library?.popups.close() }
            VStack(alignment: .leading, spacing: 12) {
                Text("Forget “\(model.note.title)”?").font(PaperMono.font(14, 520)).foregroundStyle(p.ink).lineSpacing(4)
                    .fixedSize(horizontal: false, vertical: true)
                VStack(alignment: .leading, spacing: 0) {
                    fact("The note's file is deleted, and agents stop seeing it from their next session.", p)
                    fact(chats == 1 ? "recall won't learn it again from the chat it came from. That chat stays searchable."
                         : "recall won't learn it again from the chats it came from. Those chats stay searchable.", p)
                    fact("recall keeps only its title, scope and project, to know what not to relearn. The old text stays in the notes folder's git history.", p)
                    if model.hasDraft { fact("Your unsaved edit is dropped too.", p) }
                    Rule()
                }
                if let e = model.error {
                    Text(e).font(PaperMono.font(12.5)).foregroundStyle(p.kind("open")).fixedSize(horizontal: false, vertical: true)
                } else if model.waiting {
                    Text("A memory run is writing notes. Forget waits for it, then goes ahead.").font(PaperMono.font(12.5)).foregroundStyle(p.ink)
                        .fixedSize(horizontal: false, vertical: true)
                }
                HStack(spacing: 8) {
                    Text("Undo for 10 seconds after.").font(PaperMono.font(11)).foregroundStyle(p.muted)
                    Spacer(minLength: 8)
                    WindowButton(title: "Cancel", keys: "esc") { model.library?.popups.close() }
                    WindowButton(title: model.waiting ? "waiting…" : model.sending ? "Forgetting…" : "Forget note", soon: model.error != nil,
                                 style: .risk, why: "Close this and look at the note again") { model.go() }
                }
            }
            .padding(.horizontal, 24).padding(.top, 24).padding(.bottom, 16)
            .frame(width: 470, alignment: .leading)
            .background(p.pop)
            .overlay(Rectangle().strokeBorder(p.line, lineWidth: 1))
            .padding(.top, 150)
        }
        .font(PaperMono.font(12.5))
    }

    func fact(_ text: String, _ p: Palette) -> some View {
        VStack(spacing: 0) {
            Rule()
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Rectangle().strokeBorder(p.ink, lineWidth: 1).frame(width: 8, height: 8)
                Text(text).foregroundStyle(p.body).lineSpacing(3).fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 0)
            }
            .padding(.vertical, 8)
        }
    }
}

extension Library {
    func canWrite(_ what: String) -> Bool { info?.writes?.contains(what) ?? false }

    /// Opens the Forget sheet for the note in the reader (Forget… or ⌫ in the list).
    func askForget() {
        guard section == .memory, !popups.isOpen, let id = notes.selected else { return }
        // The sheet is for the note highlighted in the list. If the reader still shows another one (the
        // highlight just moved), fetch the highlighted note first rather than forgetting what is on screen.
        if let n = notes.note, n.note_id == id { present(forgetting: n); return }
        API.get("/v1/notes/\(API.q(id))", Note.self) { [weak self] r in
            guard let self, self.section == .memory, self.notes.selected == id, case .success(let n) = r else { return }
            self.notes.note = n
            self.present(forgetting: n)
        }
    }

    private func present(forgetting n: Note) {
        guard notes.editable(n), !popups.isOpen, let w = popups.parent else { return }
        let f = Forgetting(n, library: self)
        forgetting = f
        let scheme: ColorScheme = w.effectiveAppearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua ? .dark : .light
        popups.show(ForgetSheet(model: f).environment(\.colorScheme, scheme), frame: w.frame, keys: { e in
            if e.keyCode == 36 || e.keyCode == 76 { f.go(); return true }
            return false
        }, onClose: { [weak self] in self?.forgetting = nil })
    }

    func show(_ t: Toast) {
        toast = t
        DispatchQueue.main.asyncAfter(deadline: .now() + 10) { [weak self, weak t] in
            guard let self, let t, self.toast === t, !t.undoing else { return }
            self.toast = nil
        }
    }

    /// Undo (⌘Z, or the toast's button): put the forgotten note back.
    func undoForget() {
        guard let t = toast, let commit = t.commit, !t.undoing else { return }
        t.undoing = true
        API.send("POST", "/v1/notes/\(API.q(t.noteId))/restore", ["commit": commit], Forgotten.self) { [weak self, weak t] r in
            guard let self, let t else { return }
            t.undoing = false
            switch r {
            case .success:
                self.toast = nil
                self.notes.refresh()
                self.openNote(t.noteId)
            case .failure(let f) where f.code == "busy":
                t.waiting = true
            case .failure(let f):
                t.error = f.message
            }
        }
    }

    /// recall said the notes or the memory run changed: writes that waited on the run go again.
    func runMoved() {
        notes.editor?.runMoved()
        forgetting?.runMoved()
        if let t = toast, t.waiting { t.waiting = false; undoForget() }
    }
}

/// "Forgot …  Undo ⌘Z" for 10 seconds after a note is forgotten.
final class Toast: ObservableObject {
    let title: String
    let noteId: String
    let commit: String?
    @Published var undoing = false
    @Published var waiting = false
    @Published var error: String?
    init(title: String, noteId: String, commit: String?) {
        self.title = title
        self.noteId = noteId
        self.commit = commit
    }
}

struct ToastView: View {
    @ObservedObject var toast: Toast
    let library: Library
    @Environment(\.colorScheme) var scheme
    var body: some View {
        let p = Palette.of(scheme)
        HStack(spacing: 12) {
            Text(toast.error.map { "Couldn't undo: \($0)" } ?? (toast.waiting ? "Undo waits for the memory run…" : "Forgot “\(toast.title)”"))
                .foregroundStyle(toast.error == nil ? p.ink : p.kind("open")).lineLimit(1)
            if toast.commit != nil, toast.error == nil {
                Button { library.undoForget() } label: {
                    HStack(spacing: 8) {
                        Text(toast.undoing ? "Undoing…" : "Undo").fontWeight(.medium).foregroundStyle(p.ink)
                        Kbd(text: "⌘Z")
                    }
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
            }
        }
        .font(PaperMono.font(12.5))
        .padding(.horizontal, 12).padding(.vertical, 8)
        .frame(maxWidth: 520)
        .background(p.pop)
        .overlay(Rectangle().strokeBorder(p.line, lineWidth: 1))
        .fixedSize()
    }
}
