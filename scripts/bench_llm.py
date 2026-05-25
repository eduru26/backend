#!/usr/bin/env python3
"""Local-LLM benchmark for the Eduru mentor pipeline.

Scores candidate models on the *actual* work the pipeline does:

  1. Insights  — narrate structured data + retrieved context into grounded,
                 concise prose (the main generation call).
  2. Validation — LLM-as-judge: is a response consistent with the data?
                 (PASS / FAIL — the pipeline's groundedness gate.)

For each model it reports:
  * speed   — warm tokens/sec and average turn latency
  * quality — grounding (key facts present), format (word limit),
              and validation accuracy (correct PASS/FAIL verdicts).

The prompts mirror backend/agents/prompt.py and validation.py verbatim so
the numbers reflect production behaviour, not a synthetic micro-benchmark.

Run on the host (Ollama on :11434):
    python scripts/bench_llm.py
"""
from __future__ import annotations

import io
import json
import sys
import urllib.request

# Windows consoles default to cp1252, which can't encode the box-drawing /
# arrow characters in this report. Force UTF-8 so output never crashes.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
else:  # pragma: no cover
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

OLLAMA = "http://localhost:11434"

# Candidates that fully fit a 6 GB GPU alongside the embedding model.
# qwen2.5:7b is included only as a quality reference — it overflows VRAM.
MODELS = [
    "qwen2.5:7b",     # reference (runs partly on CPU here)
    "qwen2.5:3b",
    "llama3.2:3b",
    "gemma2:2b",
    "qwen2.5:1.5b",
]

# ── prompts copied from the pipeline ────────────────────────────

SYS_STUDENT = (
    "You are an AI academic mentor speaking directly to a student. Help them "
    "understand their performance using ONLY the data and retrieved context "
    "provided. Be encouraging, specific, and actionable. Never invent numbers, "
    "topics, names, or facts not present in the data. Keep your response under "
    "200 words."
)
SYS_TEACHER = (
    "You are an AI teaching assistant helping a teacher analyse class or "
    "student performance using ONLY the data provided. Be analytical, identify "
    "patterns, suggest targeted interventions. Never invent numbers or facts. "
    "Keep your response under 300 words."
)
SYS_VALIDATION = (
    "You are a strict fact-checker. Given a data context and an AI-generated "
    "response, determine if the response contradicts or fabricates data not "
    "present in the context. Reply with exactly one word: PASS if the response "
    "is consistent with the data, FAIL if it contradicts or invents data."
)

# ── insights test cases ─────────────────────────────────────────
# `must_have` = facts that MUST appear (grounding). `word_cap` = limit.

INSIGHTS = [
    {
        "name": "student/weak-areas",
        "system": SYS_STUDENT,
        "user": (
            "DATA CONTEXT:\n"
            "Student: Aisha, Grade 8A\n"
            "Overall score: 61%\n"
            "Subject scores: Mathematics: 61%, Science: 78%, English: 82%\n"
            "Weak topics (below 60%): Algebra, Fractions\n"
            "Attendance rate: 88%\n\n"
            "QUESTION: Where am I weakest and what should I focus on?"
        ),
        "must_have": ["algebra", "fraction"],
        "word_cap": 220,
    },
    {
        "name": "teacher/at-risk",
        "system": SYS_TEACHER,
        "user": (
            "DATA CONTEXT:\n"
            "Class size: 24 students\n"
            "Class subject averages: Mathematics: 58%, Science: 71%\n"
            "At-risk students (3): Bilal (42%), Sara (47%), Omar (49%)\n\n"
            "QUESTION: Which students need attention and why?"
        ),
        "must_have": ["bilal", "sara", "omar"],
        "word_cap": 320,
    },
]

# ── validation test cases ───────────────────────────────────────
# Each: a context + response + the verdict a correct judge must give.

VALIDATION = [
    {
        "name": "consistent→PASS",
        "context": "Student Aisha scored 61% overall. Math: 61%. Attendance: 88%.",
        "response": "Aisha is at 61% overall with 88% attendance — steady, with "
                    "room to grow in math.",
        "expect": "PASS",
    },
    {
        "name": "fabricated-number→FAIL",
        "context": "Student Aisha scored 61% overall. Math: 61%. Attendance: 88%.",
        "response": "Aisha scored an excellent 94% in math and has perfect 100% "
                    "attendance.",
        "expect": "FAIL",
    },
    {
        "name": "invented-topic→FAIL",
        "context": "Weak topics: Algebra, Fractions. Math: 61%.",
        "response": "Aisha's main struggle is with Trigonometry and Calculus.",
        "expect": "FAIL",
    },
]


def _chat(model: str, system: str, user: str, *, num_predict: int = 400) -> dict:
    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": False,
        "options": {"temperature": 0, "num_predict": num_predict},
    }).encode()
    req = urllib.request.Request(
        f"{OLLAMA}/api/chat", data=body,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read())


def _metrics(resp: dict) -> tuple[float, float]:
    total_ms = resp.get("total_duration", 0) / 1e6
    ec = resp.get("eval_count", 0)
    ed = resp.get("eval_duration", 1) or 1
    tps = ec / (ed / 1e9) if ed else 0.0
    return round(total_ms), round(tps, 1)


def bench_model(model: str) -> dict:
    print(f"\n{'='*60}\n{model}\n{'='*60}")
    # warm-up (loads model into VRAM)
    try:
        _chat(model, "hi", "hi", num_predict=4)
    except Exception as exc:  # noqa: BLE001
        print(f"  warm-up failed: {exc}")
        return {"model": model, "error": str(exc)}

    latencies: list[float] = []
    tps_all: list[float] = []
    grounding_hits = 0
    grounding_total = 0
    format_ok = 0

    for case in INSIGHTS:
        resp = _chat(model, case["system"], case["user"])
        text = (resp.get("message", {}) or {}).get("content", "")
        ms, tps = _metrics(resp)
        latencies.append(ms)
        tps_all.append(tps)
        lower = text.lower()
        hits = sum(1 for kw in case["must_have"] if kw in lower)
        grounding_hits += hits
        grounding_total += len(case["must_have"])
        words = len(text.split())
        within = words <= case["word_cap"]
        format_ok += 1 if within else 0
        print(f"  insights[{case['name']}]: {ms} ms, {tps} tok/s, "
              f"grounding {hits}/{len(case['must_have'])}, "
              f"{words} words ({'ok' if within else 'OVER'})")

    val_correct = 0
    for case in VALIDATION:
        check = (
            f"DATA CONTEXT:\n{case['context']}\n\n"
            f"AI RESPONSE:\n{case['response']}\n\n"
            "Does the AI response contradict or fabricate data? Reply PASS or FAIL."
        )
        resp = _chat(model, SYS_VALIDATION, check, num_predict=8)
        verdict_raw = (resp.get("message", {}) or {}).get("content", "").upper()
        verdict = "PASS" if "PASS" in verdict_raw and "FAIL" not in verdict_raw else (
            "FAIL" if "FAIL" in verdict_raw else "?")
        ok = verdict == case["expect"]
        val_correct += 1 if ok else 0
        print(f"  validation[{case['name']}]: said {verdict}, "
              f"expected {case['expect']} ({'ok' if ok else 'WRONG'})")

    return {
        "model": model,
        "avg_latency_ms": round(sum(latencies) / len(latencies)),
        "avg_tok_s": round(sum(tps_all) / len(tps_all), 1),
        "grounding": f"{grounding_hits}/{grounding_total}",
        "grounding_pct": round(grounding_hits / grounding_total * 100),
        "format_ok": f"{format_ok}/{len(INSIGHTS)}",
        "validation": f"{val_correct}/{len(VALIDATION)}",
        "validation_pct": round(val_correct / len(VALIDATION) * 100),
    }


def main() -> None:
    results = []
    for m in MODELS:
        try:
            results.append(bench_model(m))
        except Exception as exc:  # noqa: BLE001
            print(f"  {m} FAILED: {exc}")
            results.append({"model": m, "error": str(exc)})

    print(f"\n\n{'='*78}\nSUMMARY\n{'='*78}")
    print(f"{'model':<16}{'tok/s':>8}{'avg ms':>9}{'ground':>9}{'format':>9}{'valid':>9}")
    print("-" * 78)
    for r in results:
        if "error" in r:
            print(f"{r['model']:<16}  ERROR: {r['error'][:50]}")
            continue
        print(f"{r['model']:<16}{r['avg_tok_s']:>8}{r['avg_latency_ms']:>9}"
              f"{r['grounding']:>9}{r['format_ok']:>9}{r['validation']:>9}")
    print("-" * 78)
    print("grounding = key facts retained · format = within word cap · "
          "valid = correct PASS/FAIL verdicts")


if __name__ == "__main__":
    main()
