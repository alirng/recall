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


def main() -> None:
    with open(os.path.join(HERE, "results.json")) as f:
        data = json.load(f)
    bars(data["retrieval_user_turns_all5"], os.path.join(HERE, "retrieval.svg"))
    bars(data["qa_accuracy"], os.path.join(HERE, "accuracy.svg"))
    scatter(data["tokens_vs_accuracy"], os.path.join(HERE, "tokens.svg"))


if __name__ == "__main__":
    main()
