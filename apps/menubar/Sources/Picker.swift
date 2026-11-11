// The project picker (board F1): a filter on top, "All projects", the pinned ones, then the rest,
// each with its count on the right and a diamond pin just left of it. Pins live in app.json.

import AppKit
import SwiftUI

final class PickerModel: ObservableObject {
    let entries: [(name: String, count: Int)]
    let total: Int?
    let current: String?
    let pick: (String?) -> Void
    @Published var filter = ""
    @Published var at = 0
    var close: () -> Void = {}

    init(entries: [(name: String, count: Int)], total: Int?, current: String?, pick: @escaping (String?) -> Void) {
        self.entries = entries
        self.total = total
        self.current = current
        self.pick = pick
    }

    /// The rows in order: nil is "All projects".
    func rows(_ pins: [String]) -> (pinned: [(name: String, count: Int)], rest: [(name: String, count: Int)], showAll: Bool) {
        let q = filter.lowercased()
        let hits = entries.filter { q.isEmpty || $0.name.lowercased().contains(q) }
        return (hits.filter { pins.contains($0.name) }, hits.filter { !pins.contains($0.name) }, q.isEmpty)
    }

    func flat(_ pins: [String]) -> [String?] {
        let r = rows(pins)
        return (r.showAll ? [nil] : []) + r.pinned.map(\.name) + r.rest.map(\.name)
    }

    func choose(_ name: String?) {
        close()
        pick(name)
    }

    func key(_ e: NSEvent, pins: [String]) -> Bool {
        let all = flat(pins)
        switch e.keyCode {
        case 125: at = min(at + 1, max(0, all.count - 1)); return true
        case 126: at = max(at - 1, 0); return true
        case 36, 76:
            if all.indices.contains(at) { choose(all[at]) }
            return true
        default: return false  // digits and letters are the filter's
        }
    }
}

struct PickerView: View {
    @ObservedObject var model: PickerModel
    @ObservedObject var library: Library
    @FocusState var focused: Bool
    @Environment(\.colorScheme) var scheme

    var body: some View {
        let p = Palette.of(scheme)
        let r = model.rows(library.pins)
        let flat = model.flat(library.pins)
        let places = Dictionary(flat.enumerated().map { ($0.element, $0.offset) }, uniquingKeysWith: { a, _ in a })
        VStack(spacing: 0) {
            HStack(spacing: 8) {
                Text("⌕").foregroundStyle(p.muted)
                TextField("", text: $model.filter, prompt: Text("Filter projects").foregroundColor(p.faint))
                    .textFieldStyle(.plain).foregroundStyle(p.ink).focused($focused)
                    .onChange(of: model.filter) { _, _ in model.at = 0 }
                Kbd(text: "esc")
            }
            .padding(.horizontal, 8).frame(height: 30)
            .overlay(Rectangle().strokeBorder(p.line, lineWidth: 1))
            .padding(8)
            Rule()
            ScrollViewReader { proxy in
                ScrollView(.vertical, showsIndicators: false) {
                    VStack(alignment: .leading, spacing: 0) {
                        if r.showAll {
                            row(nil, "All projects", model.total, p, index: 0)
                        }
                        if !r.pinned.isEmpty {
                            Label(text: "Pinned").padding(.horizontal, 12).padding(.top, 12).padding(.bottom, 4)
                            ForEach(r.pinned, id: \.name) { e in row(e.name, e.name, e.count, p, index: places[e.name] ?? 0) }
                        }
                        if !r.rest.isEmpty {
                            Label(text: "Projects").padding(.horizontal, 12).padding(.top, 12).padding(.bottom, 4)
                            ForEach(r.rest, id: \.name) { e in row(e.name, e.name, e.count, p, index: places[e.name] ?? 0) }
                        }
                        if flat.isEmpty {
                            Text("No project matches").foregroundStyle(p.muted).padding(12)
                        }
                    }
                }
                .onChange(of: model.at) { _, at in proxy.scrollTo(at) }
            }
            Rule()
            HStack(spacing: 8) {
                DiamondMark(color: p.muted).frame(width: 7, height: 7)
                Text("pins a project to the top").foregroundStyle(p.muted)
                Spacer()
                Text("↑↓ ↵").foregroundStyle(p.muted)
            }
            .font(PaperMono.font(11))
            .padding(.horizontal, 12).frame(height: 28)
        }
        .font(PaperMono.font(12.5))
        .frame(width: 300, height: 340)
        .background(p.pop)
        .overlay(Rectangle().strokeBorder(p.line, lineWidth: 1))
        .onAppear { focused = true }
    }

    func row(_ name: String?, _ title: String, _ n: Int?, _ p: Palette, index: Int) -> some View {
        PickerRow(name: name, title: title, count: n, filter: model.filter, on: model.at == index,
                  current: model.current == name, pinned: name.map { library.pins.contains($0) } ?? false,
                  pin: { if let name { library.togglePin(name) } }, choose: { model.choose(name) })
            .id(index)
            .onHover { if $0 { model.at = index } }
    }
}

struct PickerRow: View {
    let name: String?
    let title: String
    let count: Int?
    let filter: String
    let on: Bool
    let current: Bool
    let pinned: Bool
    let pin: () -> Void
    let choose: () -> Void
    @StateObject var hover = Hover()
    @Environment(\.colorScheme) var scheme

    var body: some View {
        let p = Palette.of(scheme)
        HStack(spacing: 8) {
            Text(underlined(title, p)).foregroundStyle(p.ink).fontWeight(current ? .medium : .regular).lineLimit(1)
            Spacer(minLength: 8)
            if name != nil {
                // Filled when pinned, which always shows; hollow under the pointer otherwise.
                Button(action: pin) {
                    ZStack {
                        if pinned { Diamond().fill(p.ink) }
                        Diamond().stroke(p.ink, lineWidth: 1)
                    }
                    .frame(width: 7, height: 7)
                    .padding(4)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .opacity(pinned || hover.on ? 1 : 0)
                .help(pinned ? "Unpin" : "Pin to the top")
            }
            Text(n(count)).foregroundStyle(p.muted).monospacedDigit().frame(minWidth: 34, alignment: .trailing)
        }
        .padding(.horizontal, 12).frame(height: 30)
        .background(on ? p.sel : .clear)
        .contentShape(Rectangle())
        .onTapGesture(perform: choose)
        .onHover { hover.on = $0 }
    }

    func n(_ c: Int?) -> String { c.map { countFormat.string(from: NSNumber(value: $0)) ?? "\($0)" } ?? "" }

    /// The filter's match underlined in the name.
    func underlined(_ s: String, _ p: Palette) -> AttributedString {
        var out = AttributedString(s)
        guard !filter.isEmpty, let r = s.range(of: filter, options: .caseInsensitive), let ar = Range(r, in: out) else { return out }
        out[ar].underlineStyle = .single
        return out
    }
}

/// A header button that opens the project picker under itself.
struct ProjectButton<L: View>: View {
    let library: Library
    let entries: () -> [(name: String, count: Int)]
    let total: Int?
    let current: String?
    let pick: (String?) -> Void
    @ViewBuilder var label: () -> L
    @StateObject var anchor = Anchor()
    @Environment(\.colorScheme) var scheme

    var body: some View {
        Button {
            let m = PickerModel(entries: entries(), total: total, current: current, pick: pick)
            let popups = library.popups
            m.close = { popups.close() }
            popups.show(PickerView(model: m, library: library).environment(\.colorScheme, scheme), below: anchor.screenRect, width: 300, height: 340,
                        keys: { e in m.key(e, pins: library.pins) })
        } label: {
            label().background(AnchorReader(anchor: anchor)).hitArea(6)
        }
        .buttonStyle(.plain)
    }
}
