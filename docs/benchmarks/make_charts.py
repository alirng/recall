"""Draw the benchmark charts in docs/benchmarks/ from results.json, as static SVG.

    uv run --with fonttools --with brotli python docs/benchmarks/make_charts.py

Every chart is written twice, NAME-light.svg and NAME-dark.svg, and the README and docs/benchmarks.md show the one
that matches the reader's GitHub theme with <picture>. The look is rererecall.com's: one warm ink
on paper, every lesser line an alpha of that ink, one-pixel lines, square corners. recall alone is
amber (an outlined bar with a translucent fill, or a filled square); other systems are outlined ink
bars or hollow diamonds; an earlier recall release is a dashed outline; reference lines are dotted.
Every mark carries its value, so nothing depends on colour.

Text is Paper Mono (SIL OFL 1.1, fonts/OFL.txt), subset to the glyphs each chart uses and embedded,
with ui-monospace as the fallback. Without fonttools the charts are still drawn, with the fallback.
"""

import base64
import io
import json
import math
import os
import re
from html import escape, unescape

HERE = os.path.dirname(os.path.abspath(__file__))
FONT = os.path.join(HERE, "fonts", "PaperMono-wght.woff2")
W, PAD = 760, 24

# rererecall.com's two themes: paper, ink, the ink's alphas for every lesser line, and the accent
THEMES = {
    "light": {
        "paper": "#F3F1EB",
        "ink": (20, 20, 22),
        "accent": (168, 120, 10),
        "body": 0.78,
        "muted": 0.58,
        "faint": 0.32,
        "ghost": 0.14,
        "line": 0.3,
        "grid": 0.12,
        "lane": 0.5,
        "soft": 0.2,
    },
    "dark": {
        "paper": "#101012",
        "ink": (232, 230, 223),
        "accent": (242, 193, 78),
        "body": 0.76,
        "muted": 0.56,
        "faint": 0.3,
        "ghost": 0.16,
        "line": 0.28,
        "grid": 0.12,
        "lane": 0.5,
        "soft": 0.18,
    },
}


def rgba(c: tuple, a: float = 1) -> str:
    return f"rgb({c[0]},{c[1]},{c[2]})" if a == 1 else f"rgba({c[0]},{c[1]},{c[2]},{a})"


def style(t: dict, font: str) -> str:
    ink, acc = t["ink"], t["accent"]
    face = (
        f'@font-face{{font-family:"Paper Mono";src:url(data:font/woff2;base64,{font}) format("woff2");'
        "font-weight:400 600}\n"
        if font
        else ""
    )
    return (
        "<style>\n"
        + face
        # the default size sits on the root, so a text's own font-size attribute still wins
        + 'svg{font-family:"Paper Mono",ui-monospace,"SF Mono",Menlo,Consolas,monospace;font-size:13px}\n'
        f"text{{fill:{rgba(ink)}}}\n"
        f".b{{font-weight:560}} .body{{fill:{rgba(ink, t['body'])}}} .m{{fill:{rgba(ink, t['muted'])}}}\n"
        f".lbl{{font-size:11px;font-weight:500;letter-spacing:.08em;fill:{rgba(ink, t['muted'])}}}\n"
        f".panel{{fill:{t['paper']};stroke:{rgba(ink, t['line'])}}} .sep,.gl{{stroke:{rgba(ink, t['grid'])}}}\n"
        f".ax{{stroke:{rgba(ink, t['faint'])}}}\n"
        f".bar{{fill:{rgba(ink, t['ghost'])};stroke:{rgba(ink, t['faint'])}}}\n"
        f".abar{{fill:{rgba(acc, t['soft'])};stroke:{rgba(acc)}}} .obar{{fill:none;stroke:{rgba(acc)}}}\n"
        f".bbar{{fill:none;stroke:{rgba(ink, t['faint'])};stroke-dasharray:2 2}}\n"
        f".dots{{fill:none;stroke:{rgba(ink, t['lane'])};stroke-dasharray:1 2.5}}\n"
        f".al{{fill:none;stroke:{rgba(acc)}}} .rx{{fill:{rgba(acc)}}}\n"
        f".dia{{fill:{t['paper']};stroke:{rgba(ink, t['lane'])}}} .adia{{fill:{t['paper']};stroke:{rgba(acc)}}}\n"
        "</style>"
    )


# ---------------------------------------------------------------------------------------------
# Paper Mono is monospaced: every glyph is 0.606 em wide, so text can be laid out exactly.


def tw(s: str, size: float) -> float:
    return len(s) * 0.606 * size


def wrap(s: str, size: float, width: float) -> list[str]:
    lines, cur = [], ""
    for word in s.split():
        nxt = f"{cur} {word}".strip()
        if cur and tw(nxt, size) > width:
            lines.append(cur)
            cur = word
        else:
            cur = nxt
    return [*lines, cur] if cur else lines


_BASE = None


def font_for(svg: str) -> str:
    """Paper Mono at weights 400 to 600, cut down to the characters this chart draws."""
    global _BASE
    try:
        from fontTools import subset
        from fontTools.ttLib import TTFont
        from fontTools.varLib import instancer
    except ImportError:
        return ""
    if _BASE is None:  # the 400-600 slice of the variable font, saved and reloaded so it subsets cleanly
        f = instancer.instantiateVariableFont(TTFont(FONT, lazy=False), {"wght": (400, 600)})
        buf = io.BytesIO()
        f.flavor = None
        f.save(buf)
        _BASE = buf.getvalue()
    chars = {unescape(m) for m in re.findall(r"<text[^>]*>(.*?)</text>", svg)}
    text = "".join(chars)
    opts = subset.Options()
    opts.layout_features = []
    opts.flavor, opts.hinting, opts.notdef_outline, opts.name_IDs = "woff2", False, False, []
    opts.drop_tables += ["meta", "STAT"]
    f = TTFont(io.BytesIO(_BASE))
    sub = subset.Subsetter(opts)
    sub.populate(text=text)
    sub.subset(f)
    out = io.BytesIO()
    f.save(out)
    return base64.b64encode(out.getvalue()).decode()


def write(name: str, height: float, title: str, body: list[str]) -> None:
    """Wrap a chart's marks in the panel and write it in both themes."""
    height = math.ceil(height)
    inner = "\n".join(body)
    font = font_for(inner)
    for theme, t in THEMES.items():
        svg = (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{height}" viewBox="0 0 {W} {height}" '
            f'role="img" aria-label="{escape(title)}">\n<title>{escape(title)}</title>\n{style(t, font)}\n'
            f'<rect class="panel" x=".5" y=".5" width="{W - 1}" height="{height - 1}"/>\n{inner}\n</svg>\n'
        )
        with open(os.path.join(HERE, f"{name}-{theme}.svg"), "w") as f:
            f.write(svg)


def text(x: float, y: float, s: str, cls: str = "", size: float | None = None, anchor: str = "start") -> str:
    a = f' class="{cls}"' if cls else ""
    a += f' font-size="{size:g}"' if size else ""
    a += f' text-anchor="{anchor}"' if anchor != "start" else ""
    return f'<text x="{x:.1f}" y="{y:.1f}"{a}>{escape(s)}</text>'


def head(title: str, subtitle: str) -> tuple[list[str], float]:
    """The panel head, as on the site: title, a muted subtitle, a hairline under both."""
    out, y = [], 36
    for line in wrap(title, 16, W - 2 * PAD):
        out.append(text(PAD, y, line, "b", 16))
        y += 22
    for line in wrap(subtitle, 12.5, W - 2 * PAD):
        out.append(text(PAD, y, line, "m", 12.5))
        y += 18
    y += 2
    out.append(f'<line class="sep" x1="{PAD}" y1="{y + 0.5:.1f}" x2="{W - PAD}" y2="{y + 0.5:.1f}"/>')
    return out, y + 1


def dia(x: float, y: float, cls: str = "dia", r: float = 5.5) -> str:
    return (
        f'<path class="{cls}" d="M{x:.1f} {y - r:.1f}L{x + r:.1f} {y:.1f}L{x:.1f} {y + r:.1f}L{x - r:.1f} {y:.1f}Z"/>'
    )


def sq(x: float, y: float, cls: str = "rx", n: float = 9) -> str:
    return f'<rect class="{cls}" x="{x - n / 2:.1f}" y="{y - n / 2:.1f}" width="{n}" height="{n}"/>'


def hbar(x0: float, y: float, length: float, h: float, cls: str) -> str:
    """An outlined box from the baseline, on half pixels so its one-pixel edges stay sharp."""
    return (
        f'<rect class="{cls}" x="{x0 + 0.5:.1f}" y="{y + 0.5:.1f}" width="{max(length - 1, 1):.1f}" '
        f'height="{h - 1:g}"/>'
    )


# ---------------------------------------------------------------------------------------------


def bars(spec: dict, name: str) -> None:
    """Horizontal bars, one per row. A row's "group" starts a labelled block when it changes (to
    compare only rows measured the same way); "unit", "max" and "ticks" set the scale for figures
    that are not percentages; a row is recall's ("ours"), an earlier release ("cls": "past") or
    another system's. "ceiling" draws a dotted reference line."""
    rows = spec["rows"]
    unit, top_value = spec.get("unit", "%"), spec.get("max", 100)
    ticks = list(spec.get("ticks", range(0, 101, 20)))
    NAME, VAL, NOTE, ROW, BAR = 13, 14, 11.5, 30, 16
    shown = [r.get("label") or f"{r['value']:.1f}{unit}" for r in rows]
    label_w = max(tw(r["name"], NAME) for r in rows) + 20
    val_w = max(tw(s, VAL) for s in shown) + 12
    note_w = max((tw(r.get("note") or "", NOTE) for r in rows), default=0)
    note_w = note_w + 28 if note_w else 0
    x0, x_max = PAD + label_w, W - PAD - note_w - val_w
    scale = (x_max - x0) / top_value
    out, y = head(spec["title"], spec["subtitle"])
    y += 18
    ceiling = spec.get("ceiling")
    if ceiling:
        ceil_label_y = y + 12
        y += 26
    blocks, group = [], object()
    for r, s in zip(rows, shown, strict=True):
        if r.get("group") != group:
            if blocks:
                y += 14
            if r.get("group"):
                out.append(text(PAD, y + 11, r["group"].upper(), "lbl"))
                y += 24
            blocks.append([y, y])
            group = r.get("group")
        cy = y + ROW / 2
        kind = "ours" if r.get("ours") else r.get("cls") or "other"
        cls = {"ours": "abar", "past": "bbar"}.get(kind, "bar")
        out.append(text(PAD, cy + 4.5, r["name"], {"ours": "b", "past": "m"}.get(kind, ""), NAME))
        out.append(hbar(x0, cy - BAR / 2, r["value"] * scale, BAR, cls))
        end = x0 + r["value"] * scale
        out.append(text(end + 9, cy + 5, s, "b" if kind == "ours" else "body", VAL))
        if r.get("note"):
            out.append(text(W - PAD, cy + 4, r["note"], "m", NOTE, "end"))
        y += ROW
        blocks[-1][1] = y
    grid = []
    for top, bottom in blocks:  # a hairline grid behind each block, and its baseline
        for tick in ticks[1:]:
            x = round(x0 + tick * scale) + 0.5
            grid.append(f'<line class="gl" x1="{x}" y1="{top + 2}" x2="{x}" y2="{bottom - 2}"/>')
        grid.append(f'<line class="ax" x1="{x0 + 0.5}" y1="{top + 2}" x2="{x0 + 0.5}" y2="{bottom - 2}"/>')
    y += 4
    for tick in ticks:
        x = x0 + tick * scale
        label = f"{tick}{unit}" if tick or unit == "%" else "0"
        out.append(text(x, y + 12, label, "m", 11, "middle"))
    if ceiling:
        x = round(x0 + ceiling["value"] * scale) + 0.5
        grid.append(f'<line class="dots" x1="{x}" y1="{ceil_label_y - 4}" x2="{x}" y2="{blocks[-1][1]}"/>')
        label = f"{ceiling['label']}: {ceiling['value']:.1f}{unit}"
        fits = x + 8 + tw(label, 11.5) <= W - PAD
        out.append(text(x + 8 if fits else x - 8, ceil_label_y + 4, label, "body", 11.5, "start" if fits else "end"))
    write(name, y + 12 + PAD, spec["title"], grid + out)


def grouped_rows(spec: dict) -> dict:
    """A before-and-after table (categories by series) as blocks of bars: one block per category."""
    rows = []
    for c, cat in enumerate(spec["categories"]):
        for s_ in spec["series"]:
            cls = s_.get("cls")
            rows.append(
                {
                    "group": cat.replace("\n", " "),
                    "name": s_["name"],
                    "value": s_["values"][c],
                    "ours": cls == "ours",
                    **({"cls": cls} if cls and cls != "ours" else {}),
                }
            )
    return {"title": spec["title"], "subtitle": spec["subtitle"], "rows": rows}


class Labels:
    """Point labels, placed one at a time: the spec's own offset if it has one, else right of the
    mark, then left, above and below; never over a mark or an earlier label, never out of the plot."""

    def __init__(self, plot: tuple, marks: list[tuple]):
        self.plot, self.taken = plot, [(x - 7, y - 7, x + 7, y + 7) for x, y in marks]

    def place(self, x: float, y: float, s: str, size: float, prefer: tuple | None = None) -> tuple:
        w, h = tw(s, size), size
        tries = ([prefer] if prefer else []) + [
            (x + 12, y + h * 0.35, "start"),
            (x - 12, y + h * 0.35, "end"),
            (x, y - 12, "middle"),
            (x, y + h + 8, "middle"),
            (x + 8, y - 10, "start"),
            (x + 8, y + h + 6, "start"),
            (x - 8, y - 10, "end"),
            (x - 8, y + h + 6, "end"),
        ]
        px0, py0, px1, py1 = self.plot
        best, best_cost = None, None
        for lx, ly, anchor in tries:
            left = lx if anchor == "start" else lx - w if anchor == "end" else lx - w / 2
            box = (left - 4, ly - h - 3, left + w + 4, ly + h * 0.4 + 3)
            if box[0] < px0 or box[2] > px1 or box[1] < py0 or box[3] > py1:
                continue
            cost = sum(
                max(0, min(box[2], t[2]) - max(box[0], t[0])) * max(0, min(box[3], t[3]) - max(box[1], t[1]))
                for t in self.taken
            )
            if best_cost is None or cost < best_cost:
                best, best_cost = ((lx, ly, anchor), box), cost
            if cost == 0:
                break
        if best is None:
            return tries[0]
        self.taken.append(best[1])
        return best[0]


def scatter(spec: dict, name: str, height: int = 460) -> None:
    pts = spec["points"]
    out, top = head(spec["title"], spec["subtitle"])
    left, right, top, bottom = 72, W - 40, top + 30, height - 58
    lo, hi = math.log10(1000), math.log10(300_000)
    y_lo, y_hi = 55, 100

    def px(tokens: float) -> float:
        return left + (math.log10(tokens) - lo) / (hi - lo) * (right - left)

    def py(v: float) -> float:
        return bottom - (v - y_lo) / (y_hi - y_lo) * (bottom - top)

    for v in range(60, 101, 10):
        out.append(f'<line class="gl" x1="{left}" y1="{round(py(v)) + 0.5}" x2="{right}" y2="{round(py(v)) + 0.5}"/>')
        out.append(text(left - 10, py(v) + 4, f"{v}%", "m", 11, "end"))
    for t, label in ((1000, "1k"), (3000, "3k"), (10000, "10k"), (30000, "30k"), (100000, "100k"), (300000, "300k")):
        out.append(f'<line class="gl" x1="{round(px(t)) + 0.5}" y1="{top}" x2="{round(px(t)) + 0.5}" y2="{bottom}"/>')
        out.append(text(px(t), bottom + 20, label, "m", 11, "middle"))
    out.append(f'<line class="ax" x1="{left}" y1="{bottom + 0.5}" x2="{right}" y2="{bottom + 0.5}"/>')
    out.append(
        text((left + right) / 2, height - 22, "TOKENS OF HISTORY READ PER QUESTION, LOG SCALE", "lbl", anchor="middle")
    )
    pts = sorted(pts, key=lambda p: not p.get("ours"))  # recall's labels placed first
    labels = Labels((left, top, right, bottom), [(px(p["tokens"]), py(p["value"])) for p in pts])
    for p in reversed(pts):  # recall's marks drawn last, on top
        x, y = px(p["tokens"]), py(p["value"])
        out.append(sq(x, y) if p.get("ours") else dia(x, y))
    for p in pts:
        x, y = px(p["tokens"]), py(p["value"])
        s = f"{p['name']} {p['value']:.1f}%"
        prefer = (x + p["label_dx"], y + p.get("label_dy", 4.5), p.get("anchor", "start")) if "label_dx" in p else None
        lx, ly, anchor = labels.place(x, y, s, 12.5, prefer)
        out.append(text(lx, ly, s, "b" if p.get("ours") else "body", 12.5, anchor))
    write(name, height, spec["title"], out)


def cost_curve(spec: dict, name: str, height: int = 500) -> None:
    """Accuracy against cost per question, one line per answer model, each point a setting."""
    out, top = head(spec["title"], spec["subtitle"])
    key_y = top + 26
    left, right, top, bottom = 78, W - 40, top + 62, height - 66
    lo, hi = math.log10(spec["x_min"]), math.log10(spec["x_max"])
    y_lo, y_hi = spec["y_min"], spec["y_max"]

    def px(cost: float) -> float:
        return left + (math.log10(cost) - lo) / (hi - lo) * (right - left)

    def py(v: float) -> float:
        return bottom - (v - y_lo) / (y_hi - y_lo) * (bottom - top)

    marks = [lambda x, y: sq(x, y), lambda x, y: dia(x, y, "adia")]
    x = PAD
    for i, series in enumerate(spec["series"]):  # the key, one row above the plot
        out.append(marks[min(i, 1)](x + 5, key_y - 4))
        out.append(text(x + 16, key_y, series["name"], "b", 12.5))
        x += 44 + tw(series["name"], 12.5)
    if spec.get("references"):
        out.append(dia(x + 5, key_y - 4))
        out.append(text(x + 16, key_y, "reference points", "body", 12.5))
    for v in range(int(y_lo), int(y_hi) + 1, 5):
        out.append(f'<line class="gl" x1="{left}" y1="{round(py(v)) + 0.5}" x2="{right}" y2="{round(py(v)) + 0.5}"/>')
        out.append(text(left - 10, py(v) + 4, f"{v}%", "m", 11, "end"))
    for t in spec["x_ticks"]:
        label = f"${t:.4f}".rstrip("0").rstrip(".") if t < 0.01 else f"${t:.2f}".rstrip("0").rstrip(".")
        out.append(text(px(t), bottom + 20, label, "m", 11, "middle"))
    out.append(f'<line class="ax" x1="{left}" y1="{bottom + 0.5}" x2="{right}" y2="{bottom + 0.5}"/>')
    x_caption = "COST PER QUESTION TO ANSWER, USD LIST PRICE, LOG SCALE"
    out.append(text((left + right) / 2, height - 26, x_caption, "lbl", anchor="middle"))
    out.append(text(PAD, top - 16, "ANSWERS JUDGED CORRECT", "lbl"))
    refs = spec.get("references", [])
    every = [p for s_ in spec["series"] for p in s_["points"]] + refs
    labels = Labels((left, top - 30, right, bottom), [(px(p["cost"]), py(p["value"])) for p in every])
    for ref in refs:
        out.append(dia(px(ref["cost"]), py(ref["value"])))
    for i, series in enumerate(spec["series"]):
        pts = sorted(series["points"], key=lambda p: p["cost"])
        if i == 0 and len(pts) > 1:
            line = " ".join(f"{px(p['cost']):.1f},{py(p['value']):.1f}" for p in pts)
            out.append(f'<polyline class="al" points="{line}"/>')
        for p in pts:
            cx, cy = px(p["cost"]), py(p["value"])
            out.append(marks[min(i, 1)](cx, cy))
            s = f"{p['label']} {p['value']:.1f}"
            lx, ly, anchor = labels.place(cx, cy, s, 12, (cx + p.get("dx", 0), cy + p.get("dy", -14), "middle"))
            out.append(text(lx, ly, s, "b" if i == 0 else "", 12, anchor))
    for ref in refs:
        cx, cy = px(ref["cost"]), py(ref["value"])
        prefer = (cx + ref.get("dx", 12), cy + ref.get("dy", 4.5), ref.get("anchor", "start"))
        lx, ly, anchor = labels.place(cx, cy, ref["name"], 12, prefer)
        out.append(text(lx, ly, ref["name"], "body", 12, anchor))
    write(name, height, spec["title"], out)


def columns(spec: dict, name: str) -> None:
    """Grouped columns, one group per category; the series are told apart by outline and a key."""
    cats, series = spec["categories"], spec["series"]
    out, top = head(spec["title"], spec["subtitle"])
    key_y = top + 26
    left, right = 72, W - PAD
    top = top + 60
    plot_h = 240
    bottom = top + plot_h
    group_w = (right - left) / len(cats)
    bar_w = min(spec.get("bar_width", 16), (group_w - 20) / len(series) - 3)
    kinds = [s_.get("cls") or ("obar", "abar", "bbar")[min(i, 2)] for i, s_ in enumerate(series)]
    kinds = [{"ours": "abar", "past": "bbar", "other": "bar"}.get(k, k) for k in kinds]
    x = PAD
    for k, s_ in zip(kinds, series, strict=True):
        out.append(hbar(x, key_y - 10, 12, 12, k))
        out.append(text(x + 18, key_y, s_["name"], "body", 12.5))
        x += 44 + tw(s_["name"], 12.5)

    def py(v: float) -> float:
        return bottom - v / 100 * plot_h

    for v in range(0, 101, 25):
        y = round(py(v)) + 0.5
        out.append(f'<line class="{"ax" if v == 0 else "gl"}" x1="{left}" y1="{y}" x2="{right}" y2="{y}"/>')
        out.append(text(left - 10, py(v) + 4, f"{v}%", "m", 11, "end"))
    for c, cat in enumerate(cats):
        gx = left + c * group_w + (group_w - (bar_w + 3) * len(series)) / 2
        for i, (k, s_) in enumerate(zip(kinds, series, strict=True)):
            v = s_["values"][c]
            bx, h = gx + i * (bar_w + 3), v / 100 * plot_h
            out.append(
                f'<rect class="{k}" x="{bx + 0.5:.1f}" y="{bottom - h + 0.5:.1f}" '
                f'width="{bar_w - 1:.1f}" height="{max(h - 1, 1):.1f}"/>'
            )
            if spec.get("labels"):
                out.append(
                    text(bx + bar_w / 2, bottom - h - 7, f"{v:.0f}", "b" if k == "abar" else "body", 11.5, "middle")
                )
        cx = left + c * group_w + group_w / 2
        for j, part in enumerate(wrap(cat.replace("\n", " "), 11, group_w - 8)):
            out.append(text(cx, bottom + 20 + j * 14, part, "body", 11, "middle"))
    write(name, bottom + 62 + PAD, spec["title"], out)


def main() -> None:
    with open(os.path.join(HERE, "results.json")) as f:
        data = json.load(f)
    # coding-agent benchmarks
    bars(data["swecb_vs_products"], "swecb")
    bars(data["tokens_per_question"], "agent-tokens")
    bars(grouped_rows(data["notes_quality"]), "notes")
    # chat-memory benchmarks
    bars(data["retrieval_user_turns_all5"], "retrieval")
    bars(data["qa_accuracy"], "accuracy")
    scatter(data["tokens_vs_accuracy"], "tokens")
    if "cost_curve" in data:
        cost_curve(data["cost_curve"], "cost")
    if "by_type" in data:
        columns(data["by_type"], "by-type")


if __name__ == "__main__":
    main()
