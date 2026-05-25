"""Diagnose why a PDF's ToC isn't being detected.

Prints, for each of /tmp/kan.pdf and /tmp/sci.pdf:
  - page count + outline presence
  - the first 25 pages of text (truncated)
  - what each detection strategy says (heading match? chapter footer match?
    dotted-leader count per page?)
"""
from __future__ import annotations
import re
from pathlib import Path
from pypdf import PdfReader
from backend.rag.toc_extract import (
    _CHAPTER_FOOTER, _SECTION_HEADING, _LEADER, _TOC_HEADINGS,
    _scan_structural, _find_heading_page, _format_outline,
)

PATHS = [Path("/tmp/kan.pdf"), Path("/tmp/sci.pdf")]
N_PAGES_TEXT = 22


def diagnose(path: Path) -> None:
    print(f"\n{'='*72}\n{path.name}  ({path.stat().st_size / 1024 / 1024:.1f} MB)\n{'='*72}")
    reader = PdfReader(str(path))
    print(f"  total pages: {len(reader.pages)}")
    # outline
    try:
        otext = _format_outline(reader.outline)
        print(f"  outline lines: {otext.count(chr(10))+1 if otext else 0}")
    except Exception as exc:
        print(f"  outline failed: {exc}")

    # extract first 25 pages
    pages = []
    for i in range(min(25, len(reader.pages))):
        try:
            pages.append(reader.pages[i].extract_text() or "")
        except Exception:
            pages.append("")

    # ToC heading detection
    hp = _find_heading_page(pages)
    print(f"  ToC-heading first-page hit: {hp}")
    # Show which headings we look for vs which appear
    for h in _TOC_HEADINGS:
        for i, p in enumerate(pages):
            if h.lower() in p.lower()[:600]:
                print(f"    found '{h}' on page {i}")
                break

    # Chapter footer detection (scan full set up to 30 pages)
    all_pages = []
    for i in range(min(30, len(reader.pages))):
        try:
            all_pages.append(reader.pages[i].extract_text() or "")
        except Exception:
            all_pages.append("")
    footer_hits = []
    for i, t in enumerate(all_pages):
        for m in _CHAPTER_FOOTER.finditer(t):
            footer_hits.append((i, m.group(1), m.group(2)))
    print(f"  chapter footer (.indd) matches in first 30 pages: {len(footer_hits)}")
    for fh in footer_hits[:5]:
        print(f"    page {fh[0]}: chapter {fh[1]} = {fh[2]!r}")

    # Section heading matches
    sec_hits = []
    for i, t in enumerate(all_pages):
        for m in _SECTION_HEADING.finditer(t):
            sec_hits.append((i, m.group(1), m.group(2), m.group(3)[:60]))
    print(f"  section heading matches (N.M Title) in first 30 pages: {len(sec_hits)}")
    for sh in sec_hits[:8]:
        print(f"    page {sh[0]}: {sh[1]}.{sh[2]} {sh[3]}")

    # Dotted-leader rows
    leader_pages = []
    for i, t in enumerate(pages):
        lines = sum(1 for line in t.splitlines() if _LEADER.match(line.strip()))
        if lines:
            leader_pages.append((i, lines))
    print(f"  dotted-leader candidates: {leader_pages[:5]}")

    # Structural scan result
    sx = _scan_structural(all_pages)
    if sx:
        print(f"\n  STRUCTURAL OUTPUT ({sx.count(chr(10))+1} lines):")
        print("  " + sx.replace("\n", "\n  "))
    else:
        print("\n  STRUCTURAL: None")

    # Dump front-matter so we can see what's there
    print(f"\n  --- FRONT MATTER (first {N_PAGES_TEXT} pages, snippets) ---")
    for i in range(min(N_PAGES_TEXT, len(pages))):
        snip = " ".join(pages[i].split())[:200]
        print(f"  p{i:>2}: {snip}")


for p in PATHS:
    if p.exists():
        diagnose(p)
    else:
        print(f"missing: {p}")
