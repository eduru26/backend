"""Run toc_extract on /tmp/big.pdf and print the result. Lets us iterate
on the heuristics without rebuilding the worker each time.
"""
from backend.rag.toc_extract import extract_toc_from_pdf

text = extract_toc_from_pdf("/tmp/big.pdf")
if text is None:
    print("NONE — no ToC detected")
else:
    print(f"{text.count(chr(10))+1} lines:\n")
    print(text)
