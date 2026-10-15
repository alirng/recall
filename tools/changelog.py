"""Changelog fragments: a PR adds changes/<topic>.<section>.md instead of editing CHANGELOG.md.

    python tools/changelog.py --check                               # validate every fragment
    python tools/changelog.py --preview                             # print the section a release would add
    python tools/changelog.py --release 0.5.0 --date 2026-10-20     # write it into CHANGELOG.md, delete the fragments

A fragment's section comes from its name (upgrading, added, changed, fixed); its text is one bullet.
"""

import argparse
import re
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHANGES = ROOT / "changes"
CHANGELOG = ROOT / "CHANGELOG.md"
SECTIONS = ["Upgrading", "Added", "Changed", "Fixed"]  # the order a release lists them in
WIDTH = 100


def fragments() -> list[tuple[str, Path, str]]:
    """(section, path, text) for every fragment, sorted by file name. README.md is not one."""
    out = []
    for p in sorted(CHANGES.glob("*.md")):
        if p.name == "README.md":
            continue
        section = p.name[: -len(".md")].rpartition(".")[2].capitalize()
        out.append((section, p, p.read_text(encoding="utf-8").strip()))
    return out


def problems() -> list[str]:
    bad = []
    for section, p, text in fragments():
        name = p.relative_to(ROOT)
        if section not in SECTIONS:
            bad.append(f"{name}: name it <topic>.<section>.md, section one of {', '.join(s.lower() for s in SECTIONS)}")
        if not text:
            bad.append(f"{name}: empty")
        if "—" in text:
            bad.append(f"{name}: contains an em dash")
    return bad


def bullet(text: str) -> str:
    return textwrap.fill(" ".join(text.split()), WIDTH, initial_indent="- ", subsequent_indent="  ", break_on_hyphens=False)


def section_text(version: str, date: str) -> str:
    frags = fragments()
    parts = [f"## [{version}] - {date}"]
    for name in SECTIONS:
        items = [bullet(t) for s, _, t in frags if s == name]
        if items:
            parts.append(f"### {name}\n\n" + "\n".join(items))
    return "\n\n".join(parts) + "\n"


def release(version: str, date: str) -> None:
    text = CHANGELOG.read_text(encoding="utf-8")
    if not fragments():
        sys.exit("no fragments in changes/")
    if re.search(rf"^## \[{re.escape(version)}\]", text, re.M):
        sys.exit(f"CHANGELOG.md already has {version}")
    head = "## [Unreleased]\n"
    if head not in text:
        sys.exit("CHANGELOG.md has no '## [Unreleased]' heading")
    text = text.replace(head, head + "\n" + section_text(version, date), 1)
    m = re.search(r"^\[Unreleased\]: (\S+)/compare/(v\S+?)\.\.\.HEAD$", text, re.M)
    if m:  # the compare links at the bottom
        base, old = m.groups()
        text = text.replace(
            m.group(0),
            f"[Unreleased]: {base}/compare/v{version}...HEAD\n[{version}]: {base}/compare/{old}...v{version}",
            1,
        )
    CHANGELOG.write_text(text, encoding="utf-8")
    for _, p, _ in fragments():
        p.unlink()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--preview", action="store_true")
    ap.add_argument("--release", metavar="X.Y.Z")
    ap.add_argument("--date", metavar="YYYY-MM-DD")
    a = ap.parse_args()
    if not (a.check or a.preview or a.release):
        ap.error("give --check, --preview or --release")
    bad = problems()
    if bad:
        sys.exit("\n".join(bad))
    if a.check:
        print(f"{len(fragments())} fragment(s) ok")
    if a.release:
        if not re.fullmatch(r"\d+\.\d+\.\d+", a.release) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", a.date or ""):
            ap.error("--release needs X.Y.Z and --date YYYY-MM-DD")
        release(a.release, a.date)
    elif a.preview:
        print(section_text("X.Y.Z", "YYYY-MM-DD"), end="")


if __name__ == "__main__":
    main()
