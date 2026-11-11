// Chat messages as light markdown, in the site's grammar: headings are bold at body size, lists
// hang, fenced code sits in a 1 px box with square corners, quotes get a 1 px rule on the left.
// Inline marks come from Foundation's markdown parser. Each message is parsed once per theme and
// kept by its seq, so scrolling a long chat only lays text out.

import AppKit
import SwiftUI

struct MD {
    enum Kind { case text, heading, item(String, Int), code, quote, table }
    struct Block {
        let kind: Kind
        let text: AttributedString
        var look: NSAttributedString = NSAttributedString()
        var height = TextHeight()
    }
    let blocks: [Block]
    /// The rendered words, block by block, which find searches.
    let plain: [String]

    static let marks = CharacterSet(charactersIn: "`*_#>|[~")

    static func parse(_ raw: String, _ p: Palette) -> MD {
        // Most messages carry no markdown: one plain text, the same row as before.
        if raw.rangeOfCharacter(from: marks) == nil,
           !raw.components(separatedBy: "\n").contains(where: { item($0.trimmingCharacters(in: .whitespaces)) != nil }) {
            return MD(blocks: [styled(Block(kind: .text, text: AttributedString(raw)), p)], plain: [raw])
        }
        var blocks: [Block] = []
        var para: [String] = [], quote: [String] = [], table: [String] = [], code: [String]? = nil
        func inline(_ s: String, bold: Bool = false) -> AttributedString { MD.inline(s, p, bold: bold) }
        func flush() {
            if !para.isEmpty { blocks.append(Block(kind: .text, text: inline(para.joined(separator: "\n")))); para = [] }
            if !quote.isEmpty { blocks.append(Block(kind: .quote, text: inline(quote.joined(separator: "\n")))); quote = [] }
            if !table.isEmpty { blocks.append(Block(kind: .table, text: AttributedString(table.joined(separator: "\n")))); table = [] }
        }
        for line in raw.components(separatedBy: "\n") {
            let t = line.trimmingCharacters(in: .whitespaces)
            if code != nil {
                if t.hasPrefix("```") || t.hasPrefix("~~~") {
                    blocks.append(Block(kind: .code, text: AttributedString(code!.joined(separator: "\n"))))
                    code = nil
                } else {
                    code!.append(line)
                }
                continue
            }
            if t.hasPrefix("```") || t.hasPrefix("~~~") { flush(); code = []; continue }
            if t.isEmpty { flush(); continue }
            if let h = heading(t) { flush(); blocks.append(Block(kind: .heading, text: inline(h, bold: true))); continue }
            if t.hasPrefix(">") {
                if !para.isEmpty || !table.isEmpty { let q = quote; quote = []; flush(); quote = q }
                quote.append(String(t.dropFirst()).trimmingCharacters(in: .whitespaces))
                continue
            }
            if t.hasPrefix("|") {
                if !para.isEmpty || !quote.isEmpty { let tb = table; table = []; flush(); table = tb }
                table.append(t)
                continue
            }
            if let (marker, rest) = item(t) {
                flush()
                let depth = min(4, (line.prefix { $0 == " " || $0 == "\t" }.count) / 2)
                blocks.append(Block(kind: .item(marker, depth), text: inline(rest)))
                continue
            }
            if !quote.isEmpty || !table.isEmpty { flush() }
            para.append(line)
        }
        if let c = code { blocks.append(Block(kind: .code, text: AttributedString(c.joined(separator: "\n")))) }
        flush()
        let merged = coalesce(blocks)
        return MD(blocks: merged.map { styled($0, p) }, plain: merged.map { String($0.text.characters) })
    }

    /// The block as AppKit draws it, in the block kind's type and the marks the parser found.
    static func styled(_ b: Block, _ p: Palette) -> Block {
        let base: NSFont, color: Color, leading: CGFloat
        switch b.kind {
        case .code, .table: (base, color, leading) = (PaperMono.nsFont(12.5), p.ink, 2)
        case .quote: (base, color, leading) = (PaperMono.nsFont(12.5, 390, prose: true), p.muted, 3)
        default: (base, color, leading) = (PaperMono.nsFont(12.5, 390, prose: true), p.body, 3)
        }
        let paragraph = NSMutableParagraphStyle()
        paragraph.lineSpacing = leading
        let out = NSMutableAttributedString(string: String(b.text.characters), attributes: [.font: base, .foregroundColor: NSColor(color), .paragraphStyle: paragraph])
        for run in b.text.runs {
            let r = NSRange(run.range, in: b.text)
            var marks: [NSAttributedString.Key: Any] = [:]
            if let f = run.font, let ns = PaperMono.nsFont(for: f) { marks[.font] = ns }
            if let c = run.foregroundColor { marks[.foregroundColor] = NSColor(c) }
            if let c = run.backgroundColor { marks[.backgroundColor] = NSColor(c) }
            if run.underlineStyle != nil { marks[.underlineStyle] = NSUnderlineStyle.single.rawValue }
            if run.strikethroughStyle != nil { marks[.strikethroughStyle] = NSUnderlineStyle.single.rawValue }
            if let l = run.link { marks[.link] = l }
            if !marks.isEmpty { out.addAttributes(marks, range: r) }
        }
        var styled = b
        styled.look = out
        return styled
    }

    /// Runs of paragraphs, headings and tables become one text, with a short blank line between
    /// them: a long chat lays out far fewer views that way.
    static func coalesce(_ blocks: [Block]) -> [Block] {
        var out: [Block] = []
        var gap = AttributedString("\n\n")
        gap.font = PaperMono.font(5)
        for b in blocks {
            var b = b
            if case .table = b.kind { b = Block(kind: .text, text: b.text.settingAttributes(tableStyle)) }
            switch (out.last?.kind, b.kind) {
            case (.text?, .text), (.text?, .heading), (.heading?, .text), (.heading?, .heading):
                let prev = out.removeLast()
                out.append(Block(kind: .text, text: prev.text + gap + b.text))
            default:
                out.append(b)
            }
        }
        return out
    }

    static let tableStyle: AttributeContainer = {
        var c = AttributeContainer()
        c.font = PaperMono.font(12.5)
        return c
    }()

    static func heading(_ t: String) -> String? {
        for n in 1...3 {
            let mark = String(repeating: "#", count: n) + " "
            if t.hasPrefix(mark) { return String(t.dropFirst(mark.count)).trimmingCharacters(in: .whitespaces) }
        }
        return nil
    }

    /// `- x`, `* x`, `+ x` or `12. x`.
    static func item(_ t: String) -> (String, String)? {
        if let c = t.first, "-*+".contains(c), t.dropFirst().first == " " {
            if c != "-", t.count > 2, t.allSatisfy({ $0 == c || $0 == " " }) { return nil }  // a * * * rule
            return ("–", String(t.dropFirst(2)))
        }
        let digits = t.prefix { $0.isNumber }
        if !digits.isEmpty, digits.count <= 3 {
            let after = t.dropFirst(digits.count)
            if after.hasPrefix(". ") || after.hasPrefix(") ") { return (digits + ".", String(after.dropFirst(2))) }
        }
        return nil
    }

    /// Bold, italic, inline code and links, in Paper Mono. Plain text when the parser gives up.
    static func inline(_ s: String, _ p: Palette, bold: Bool) -> AttributedString {
        var out = (try? AttributedString(markdown: s, options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace))) ?? AttributedString(s)
        let strong = PaperMono.font(12.5, 560, prose: true)
        let mono = PaperMono.font(12.5)
        if bold { out.font = strong; out.foregroundColor = p.ink }
        for run in out.runs {
            let r = run.range
            if let link = run.link {
                out[r].foregroundColor = p.ink
                out[r].underlineStyle = .single
                out[r].link = link
            }
            guard let intent = run.inlinePresentationIntent else { continue }
            out[r].inlinePresentationIntent = nil
            if intent.contains(.code) {
                out[r].font = mono
                out[r].backgroundColor = p.ghost
                out[r].foregroundColor = p.ink
            } else if intent.contains(.stronglyEmphasized) {
                out[r].font = strong
                out[r].foregroundColor = p.ink
            } else if intent.contains(.emphasized) {
                // Paper Mono has no italic, so emphasis is ink, a step up from the body text.
                out[r].foregroundColor = p.ink
            }
            if intent.contains(.strikethrough) { out[r].strikethroughStyle = .single }
        }
        return out
    }
}

/// How tall a block is at the width it was last measured, kept so a list can ask as often as it likes.
final class TextHeight {
    private var width: CGFloat = -1
    private var height: CGFloat = 0

    func at(_ w: CGFloat, of text: NSAttributedString) -> CGFloat {
        if w != width {
            let storage = NSTextStorage(attributedString: text)
            let layout = NSLayoutManager()
            let container = NSTextContainer(size: NSSize(width: w, height: .greatestFiniteMagnitude))
            container.lineFragmentPadding = 0
            layout.addTextContainer(container)
            storage.addLayoutManager(layout)
            layout.ensureLayout(for: container)
            height = ceil(layout.usedRect(for: container).height)
            width = w
        }
        return height
    }
}

/// Message text in an AppKit text view: selectable like any text on the Mac, and unlike SwiftUI's selectable text it
/// lays out and draws only what's showing, and measures once per width. A long message costs a row almost nothing.
struct MessageText: NSViewRepresentable {
    let text: NSAttributedString
    let height: TextHeight
    let p: Palette
    nonisolated(unsafe) static var lastWidth: CGFloat = 0

    final class Coordinator { weak var shown: NSAttributedString? }
    func makeCoordinator() -> Coordinator { Coordinator() }

    /// AppKit fills a highlight down through the line spacing under it; SwiftUI stops at the line's own height. Inline
    /// code and find's current match keep SwiftUI's box.
    final class Layout: NSLayoutManager {
        override func fillBackgroundRectArray(_ rects: UnsafePointer<NSRect>, count: Int, forCharacterRange range: NSRange, color: NSColor) {
            let font = textStorage?.attribute(.font, at: range.location, effectiveRange: nil) as? NSFont
            let line = font.map { defaultLineHeight(for: $0) }
            let tight = (0..<count).map { i -> NSRect in
                var r = rects[i]
                if let line { r.size.height = min(r.height, line) }
                return r
            }
            super.fillBackgroundRectArray(tight, count: count, forCharacterRange: range, color: color)
        }
    }

    func makeNSView(context: Context) -> NSTextView {
        let storage = NSTextStorage()
        let layout = Layout()
        layout.allowsNonContiguousLayout = true
        storage.addLayoutManager(layout)
        let container = NSTextContainer(size: NSSize(width: CGFloat(0), height: CGFloat.greatestFiniteMagnitude))
        container.lineFragmentPadding = 0
        container.widthTracksTextView = true
        layout.addTextContainer(container)
        let view = NSTextView(frame: NSRect.zero, textContainer: container)
        view.isEditable = false
        view.isSelectable = true
        view.drawsBackground = false
        view.textContainerInset = .zero
        view.isVerticallyResizable = false
        view.isHorizontallyResizable = false
        view.isRichText = true
        view.usesFontPanel = false
        let link: [NSAttributedString.Key: Any] = [.foregroundColor: NSColor(p.ink), .underlineStyle: NSUnderlineStyle.single.rawValue, .cursor: NSCursor.pointingHand]
        view.linkTextAttributes = link
        return view
    }

    func updateNSView(_ view: NSTextView, context: Context) {
        guard context.coordinator.shown !== text else { return }
        context.coordinator.shown = text
        view.textStorage?.setAttributedString(text)
    }

    func sizeThatFits(_ proposal: ProposedViewSize, nsView: NSTextView, context: Context) -> CGSize? {
        let w = proposal.width ?? max(Self.lastWidth, 320)
        if proposal.width != nil { Self.lastWidth = w }
        return CGSize(width: w, height: height.at(w, of: text))
    }
}

/// One message, its blocks stacked, with find's marks laid over the rendered words.
struct MDView: View {
    /// About 70 characters of 12.5 pt Paper Mono: the width prose reads best at (board 3.4, frame 2).
    static let prose: CGFloat = 510

    let md: MD
    let seq: Int
    @ObservedObject var find: FindModel
    let p: Palette

    func shown(_ i: Int) -> some View {
        MessageText(text: find.marked(md.blocks[i].look, seq: seq, before: before(i), p), height: md.blocks[i].height, p: p)
    }

    var body: some View {
        let body = PaperMono.font(12.5, 390, prose: true)
        if md.blocks.count == 1, case .text = md.blocks[0].kind {
            shown(0).frame(maxWidth: Self.prose, alignment: .leading)
        } else {
            blocks(body)
        }
    }

    func blocks(_ body: Font) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            ForEach(md.blocks.indices, id: \.self) { i in
                let b = md.blocks[i]
                switch b.kind {
                case .text, .heading:
                    shown(i).frame(maxWidth: Self.prose, alignment: .leading)
                case .item(let marker, let depth):
                    HStack(alignment: .top, spacing: 0) {
                        Text(marker).font(body).foregroundStyle(p.muted).frame(width: marker.count > 2 ? 32 : 22, alignment: .leading)
                        shown(i)
                    }
                    .padding(.leading, CGFloat(depth) * 22)
                    .frame(maxWidth: Self.prose, alignment: .leading)
                case .code, .table:
                    shown(i)
                        .padding(.horizontal, 12).padding(.vertical, 8)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .overlay { if case .code = b.kind { Rectangle().strokeBorder(p.line, lineWidth: 1) } }
                case .quote:
                    shown(i)
                        .padding(.leading, 12)
                        .overlay(alignment: .leading) { Rectangle().fill(p.line).frame(width: 1) }
                        .frame(maxWidth: Self.prose, alignment: .leading)
                }
            }
        }
    }

    /// How many matches come before block i, so the current one is marked in the right block.
    func before(_ i: Int) -> Int {
        guard !find.query.isEmpty, i > 0 else { return 0 }
        return md.plain[..<i].reduce(0) { $0 + FindModel.count(find.query, in: $1) }
    }
}
