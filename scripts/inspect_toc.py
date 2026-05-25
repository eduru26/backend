"""Dump the first N pages of /tmp/big.pdf so we can see how the ToC is
formatted. Helps tune toc_extract.py heuristics for real Indian textbooks.
"""
from __future__ import annotations
from pathlib import Path
from pypdf import PdfReader

PDF = Path("/tmp/big.pdf")
N_PAGES = 30

reader = PdfReader(str(PDF))
print(f"pages={len(reader.pages)}")
print(f"outline_present={bool(reader.outline)}, outline_count={len(reader.outline or [])}")
print(f"---")
for i in range(min(N_PAGES, len(reader.pages))):
    try:
        t = reader.pages[i].extract_text() or ""
    except Exception as exc:
        t = f"[extract failed: {exc}]"
    snippet = t.strip()
    print(f"\n=== PAGE {i} ({len(snippet)} chars) ===")
    print(snippet[:1500])
    if len(snippet) > 1500:
        print(f"... [+{len(snippet)-1500} more chars]")
