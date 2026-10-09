"""Draw the benchmark charts in docs/benchmarks/ from results.json, as static SVG.

    python docs/benchmarks/make_charts.py

No dependencies. Each SVG carries its own light and dark colours, so it follows the reader's
theme on GitHub. recall's marks take the full blue; other systems take a lighter step of the
same blue, and every mark is labelled, so nothing depends on colour alone.
"""

import json
import math
import os
from html import escape

HERE = os.path.dirname(os.path.abspath(__file__))
FONT = 'system-ui, -apple-system, "Segoe UI", sans-serif'
STYLE = """
<style>
  .surface { fill: #fcfcfb; }
  .ink { fill: #0b0b0b; }
  .ink2 { fill: #52514e; }
  .muted { fill: #898781; }
  .grid { stroke: #e1e0d9; }
  .axis { stroke: #c3c2b7; }
  .ours { fill: #2a78d6; }
  .other { fill: #86b6ef; }
  .ceiling { stroke: #52514e; }
  .ring { stroke: #fcfcfb; }
  .halo { stroke: #fcfcfb; stroke-width: 4px; paint-order: stroke; stroke-linejoin: round; }
  .s1 { fill: #2a78d6; stroke: #2a78d6; }
  .s2 { fill: #eb6834; stroke: #eb6834; }
  .s3 { fill: #1baf7a; stroke: #1baf7a; }
  .ref { fill: #898781; stroke: #898781; }
  .frame { fill: none; stroke: #c3c2b7; }
  @media (prefers-color-scheme: dark) {
    .surface { fill: #1a1a19; }
    .ink { fill: #ffffff; }
    .ink2 { fill: #c3c2b7; }
    .grid { stroke: #2c2c2a; }
    .axis { stroke: #383835; }
    .ours { fill: #3987e5; }
    .other { fill: #184f95; }
    .ceiling { stroke: #c3c2b7; }
    .ring { stroke: #1a1a19; }
    .halo { stroke: #1a1a19; }
    .s1 { fill: #3987e5; stroke: #3987e5; }
    .s2 { fill: #d95926; stroke: #d95926; }
    .s3 { fill: #199e70; stroke: #199e70; }
    .frame { stroke: #383835; }
  }
  text { font-family: %s; }
</style>""" % FONT


def text_width(s: str, size: float) -> float:
    """A rough width for laying out labels (no font metrics available)."""
    return sum(0.33 if c in " .,:;'|il1()" else 0.62 if c.isupper() else 0.54 for c in s) * size


def bar_path(x0: float, y: float, length: float, h: float, r: float = 4) -> str:
    """A horizontal bar, square at the baseline and rounded at its data end."""
    r = min(r, h / 2, length)
    x1 = x0 + length
    return (
        f"M{x0:.1f},{y:.1f} H{x1 - r:.1f} Q{x1:.1f},{y:.1f} {x1:.1f},{y + r:.1f} "
        f"V{y + h - r:.1f} Q{x1:.1f},{y + h:.1f} {x1 - r:.1f},{y + h:.1f} H{x0:.1f} Z"
    )


def header(width: int, height: int, title: str, subtitle: str) -> list[str]:
    return [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-label="{escape(title)}">',
        STYLE,
        f'<rect class="surface" width="{width}" height="{height}" rx="8"/>',
        f'<text class="ink" x="24" y="34" font-size="17" font-weight="600">{escape(title)}</text>',
        f'<text class="ink2" x="24" y="56" font-size="12.5">{escape(subtitle)}</text>',
    ]


def bars(spec: dict, path: str, width: int = 760) -> None:
    rows = spec["rows"]
    label_w = max(text_width(r["name"], 13) for r in rows) + 24
    note_w = 150
    x0, x_max = 24 + label_w, width - 24 - note_w - 48
    top, band, thick = 84, 30, 18
    height = top + band * len(rows) + 46
    scale = (x_max - x0) / 100
    out = header(width, height, spec["title"], spec["subtitle"])
    for tick in range(0, 101, 20):  # recessive hairline grid
        x = x0 + tick * scale
        out.append(f'<line class="grid" x1="{x:.1f}" y1="{top - 6}" x2="{x:.1f}" y2="{height - 34}" stroke-width="1"/>')
        out.append(f'<text class="muted" x="{x:.1f}" y="{height - 18}" font-size="11" text-anchor="middle">{tick}%</text>')
    if "ceiling" in spec:
        x = x0 + spec["ceiling"]["value"] * scale
        out.append(
            f'<line class="ceiling" x1="{x:.1f}" y1="{top - 10}" x2="{x:.1f}" y2="{height - 34}" '
            f'stroke-width="1.5" stroke-dasharray="4 3"/>'
        )
        out.append(
            f'<text class="ink2 halo" x="{x - 6:.1f}" y="{top - 14}" font-size="11" text-anchor="end">'
            f'{escape(spec["ceiling"]["label"])}: {spec["ceiling"]["value"]:.0f}%</text>'
        )
    for i, r in enumerate(rows):
        y = top + i * band
        cls = "ours" if r.get("ours") else "other"
        weight = ' font-weight="600"' if r.get("ours") else ""
        out.append(
            f'<text class="ink" x="{x0 - 10:.1f}" y="{y + thick / 2 + 4.5:.1f}" font-size="13" '
            f'text-anchor="end"{weight}>{escape(r["name"])}</text>'
        )
        out.append(f'<path class="{cls}" d="{bar_path(x0, y, r["value"] * scale, thick)}"><title>'
                   f'{escape(r["name"])}: {r["value"]:.1f}%</title></path>')
        end = x0 + r["value"] * scale
        out.append(
            f'<text class="ink halo" x="{end + 8:.1f}" y="{y + thick / 2 + 4.5:.1f}" font-size="12.5"{weight}>'
            f'{r["value"]:.1f}%</text>'
        )
        if r.get("note"):
            out.append(
                f'<text class="muted" x="{x_max + 56:.1f}" y="{y + thick / 2 + 4.5:.1f}" font-size="11.5">'
                f'{escape(r["note"])}</text>'
            )
    out.append(f'<line class="axis" x1="{x0:.1f}" y1="{top - 6}" x2="{x0:.1f}" y2="{height - 34}" stroke-width="1"/>')
    out.append("</svg>")
    with open(path, "w") as f:
        f.write("\n".join(out) + "\n")


def scatter(spec: dict, path: str, width: int = 760, height: int = 440) -> None:
    pts = spec["points"]
    left, right, top, bottom = 64, width - 40, 80, height - 54
    lo, hi = math.log10(1000), math.log10(300_000)
    y_lo, y_hi = 55, 100

    def px(tokens: float) -> float:
        return left + (math.log10(tokens) - lo) / (hi - lo) * (right - left)

    def py(v: float) -> float:
        return bottom - (v - y_lo) / (y_hi - y_lo) * (bottom - top)

    out = header(width, height, spec["title"], spec["subtitle"])
    for v in range(60, 101, 10):
        out.append(f'<line class="grid" x1="{left}" y1="{py(v):.1f}" x2="{right}" y2="{py(v):.1f}" stroke-width="1"/>')
        out.append(f'<text class="muted" x="{left - 8}" y="{py(v) + 4:.1f}" font-size="11" text-anchor="end">{v}%</text>')
    for t, label in ((1000, "1k"), (3000, "3k"), (10000, "10k"), (30000, "30k"), (100000, "100k"), (300000, "300k")):
        out.append(f'<line class="grid" x1="{px(t):.1f}" y1="{top}" x2="{px(t):.1f}" y2="{bottom}" stroke-width="1"/>')
        out.append(f'<text class="muted" x="{px(t):.1f}" y="{bottom + 18}" font-size="11" text-anchor="middle">{label}</text>')
    out.append(f'<line class="axis" x1="{left}" y1="{bottom}" x2="{right}" y2="{bottom}" stroke-width="1"/>')
    out.append(
        f'<text class="muted" x="{(left + right) / 2:.0f}" y="{height - 14}" font-size="11.5" text-anchor="middle">'
        "tokens of history read per question</text>"
    )
    for p in sorted(pts, key=lambda p: bool(p.get("ours"))):  # recall drawn last, on top
        x, y = px(p["tokens"]), py(p["value"])
        cls = "ours" if p.get("ours") else "other"
        out.append(
            f'<circle class="{cls} ring" cx="{x:.1f}" cy="{y:.1f}" r="{6 if p.get("ours") else 5}" stroke-width="2">'
            f'<title>{escape(p["name"])}: {p["value"]:.1f}% at {p["tokens"]:,} tokens</title></circle>'
        )
        dx, anchor = p.get("label_dx", 10), p.get("anchor", "start")
        weight = ' font-weight="600"' if p.get("ours") else ""
        out.append(
            f'<text class="ink halo" x="{x + dx:.1f}" y="{y + p.get("label_dy", 4):.1f}" font-size="12" '
            f'text-anchor="{anchor}"{weight}>{escape(p["name"])} {p["value"]:.1f}%</text>'
        )
    out.append("</svg>")
    with open(path, "w") as f:
        f.write("\n".join(out) + "\n")


def cost_curve(spec: dict, path: str, width: int = 760, height: int = 480) -> None:
    """Accuracy against cost per question, one line per answer model, each point a setting:
    the chart frontier labs use for effort levels."""
    left, right, top, bottom = 70, width - 40, 118, height - 62
    lo, hi = math.log10(spec["x_min"]), math.log10(spec["x_max"])
    y_lo, y_hi = spec["y_min"], spec["y_max"]

    def px(cost: float) -> float:
        return left + (math.log10(cost) - lo) / (hi - lo) * (right - left)

    def py(v: float) -> float:
        return bottom - (v - y_lo) / (y_hi - y_lo) * (bottom - top)

    out = header(width, height, spec["title"], spec["subtitle"])
    x = 24
    for i, series in enumerate(spec["series"], 1):  # legend, one row above the plot
        out.append(f'<circle class="s{i} ring" cx="{x + 6}" cy="88" r="6" stroke-width="2"/>')
        out.append(f'<text class="ink" x="{x + 18}" y="92.5" font-size="12.5" font-weight="600">{escape(series["name"])}</text>')
        x += 32 + text_width(series["name"], 12.5)
    for ref in spec.get("references", []):
        out.append(f'<circle class="ref ring" cx="{x + 6}" cy="88" r="5" stroke-width="2"/>')
        out.append(f'<text class="ink2" x="{x + 18}" y="92.5" font-size="12.5">Reference points</text>')
        break
    for v in range(int(y_lo), int(y_hi) + 1, 5):
        out.append(f'<line class="grid" x1="{left}" y1="{py(v):.1f}" x2="{right}" y2="{py(v):.1f}" stroke-width="1"/>')
        out.append(f'<text class="muted" x="{left - 8}" y="{py(v) + 4:.1f}" font-size="11" text-anchor="end">{v}%</text>')
    for t in spec["x_ticks"]:
        label = f"${t:.4f}".rstrip("0").rstrip(".") if t < 0.01 else f"${t:.2f}".rstrip("0").rstrip(".")
        out.append(f'<text class="muted" x="{px(t):.1f}" y="{bottom + 18}" font-size="11" text-anchor="middle">{label}</text>')
    out.append(f'<line class="axis" x1="{left}" y1="{bottom}" x2="{right}" y2="{bottom}" stroke-width="1"/>')
    out.append(
        f'<text class="muted" x="{(left + right) / 2:.0f}" y="{height - 22}" font-size="11.5" text-anchor="middle">'
        "cost per question to answer (USD, list price, log scale)</text>"
    )
    out.append(
        f'<text class="muted" transform="translate(18 {(top + bottom) / 2:.0f}) rotate(-90)" font-size="11.5" '
        'text-anchor="middle">answers judged correct</text>'
    )
    for i, series in enumerate(spec["series"], 1):
        pts = sorted(series["points"], key=lambda p: p["cost"])
        line = " ".join(f"{px(p['cost']):.1f},{py(p['value']):.1f}" for p in pts)
        out.append(f'<polyline class="s{i}" points="{line}" fill="none" stroke-width="2" style="fill:none"/>')
        for p in pts:
            cx, cy = px(p["cost"]), py(p["value"])
            out.append(
                f'<circle class="s{i} ring" cx="{cx:.1f}" cy="{cy:.1f}" r="6" stroke-width="2">'
                f'<title>{escape(series["name"])}, {escape(p["label"])}: {p["value"]:.1f}% at ${p["cost"]:.4f}</title></circle>'
            )
            out.append(
                f'<text class="ink halo" x="{cx + p.get("dx", 0):.1f}" y="{cy + p.get("dy", -12):.1f}" font-size="11.5" '
                f'text-anchor="middle">{escape(p["label"])}</text>'
            )
    for ref in spec.get("references", []):
        cx, cy = px(ref["cost"]), py(ref["value"])
        out.append(
            f'<circle class="ref ring" cx="{cx:.1f}" cy="{cy:.1f}" r="5" stroke-width="2">'
            f'<title>{escape(ref["name"])}: {ref["value"]:.1f}%</title></circle>'
        )
        out.append(
            f'<text class="ink2 halo" x="{cx + ref.get("dx", 10):.1f}" y="{cy + ref.get("dy", 4):.1f}" font-size="11.5" '
            f'text-anchor="{ref.get("anchor", "start")}">{escape(ref["name"])}</text>'
        )
    out.append("</svg>")
    with open(path, "w") as f:
        f.write("\n".join(out) + "\n")


def grouped(spec: dict, path: str, width: int = 760) -> None:
    """Accuracy by question type: one group per type, one bar per setting."""
    cats, series = spec["categories"], spec["series"]
    left, right, top = 70, width - 24, 118
    plot_h = 250
    bottom = top + plot_h
    height = bottom + 70
    group_w = (right - left) / len(cats)
    bar_w = min(18, (group_w - 24) / len(series) - 2)

    def py(v: float) -> float:
        return bottom - v / 100 * plot_h

    out = header(width, height, spec["title"], spec["subtitle"])
    x = 24
    for i, s_ in enumerate(series, 1):
        out.append(f'<rect class="s{i}" x="{x}" y="82" width="12" height="12" rx="3"/>')
        out.append(f'<text class="ink" x="{x + 18}" y="92.5" font-size="12.5">{escape(s_["name"])}</text>')
        x += 36 + text_width(s_["name"], 12.5)
    for v in range(0, 101, 25):
        out.append(f'<line class="grid" x1="{left}" y1="{py(v):.1f}" x2="{right}" y2="{py(v):.1f}" stroke-width="1"/>')
        out.append(f'<text class="muted" x="{left - 8}" y="{py(v) + 4:.1f}" font-size="11" text-anchor="end">{v}%</text>')
    for c, cat in enumerate(cats):
        gx = left + c * group_w + (group_w - (bar_w + 2) * len(series)) / 2
        for i, s_ in enumerate(series, 1):
            v = s_["values"][c]
            bx, h = gx + (i - 1) * (bar_w + 2), v / 100 * plot_h
            r = min(4, bar_w / 2, h)
            out.append(
                f'<path class="s{i}" d="M{bx:.1f},{bottom} V{bottom - h + r:.1f} Q{bx:.1f},{bottom - h:.1f} {bx + r:.1f},{bottom - h:.1f} '
                f'H{bx + bar_w - r:.1f} Q{bx + bar_w:.1f},{bottom - h:.1f} {bx + bar_w:.1f},{bottom - h + r:.1f} V{bottom} Z">'
                f'<title>{escape(cat)}, {escape(s_["name"])}: {v:.1f}%</title></path>'
            )
        cx = left + c * group_w + group_w / 2
        for j, part in enumerate(cat.split("\n")):
            out.append(f'<text class="ink2" x="{cx:.1f}" y="{bottom + 18 + j * 14}" font-size="11.5" text-anchor="middle">{escape(part)}</text>')
    out.append(f'<line class="axis" x1="{left}" y1="{bottom}" x2="{right}" y2="{bottom}" stroke-width="1"/>')
    out.append("</svg>")
    with open(path, "w") as f:
        f.write("\n".join(out) + "\n")


def main() -> None:
    with open(os.path.join(HERE, "results.json")) as f:
        data = json.load(f)
    bars(data["retrieval_user_turns_all5"], os.path.join(HERE, "retrieval.svg"))
    bars(data["qa_accuracy"], os.path.join(HERE, "accuracy.svg"))
    scatter(data["tokens_vs_accuracy"], os.path.join(HERE, "tokens.svg"))
    if "cost_curve" in data:
        cost_curve(data["cost_curve"], os.path.join(HERE, "cost.svg"))
    if "by_type" in data:
        grouped(data["by_type"], os.path.join(HERE, "by-type.svg"))


if __name__ == "__main__":
    main()
