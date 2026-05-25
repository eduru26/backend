# Hardware Capacity Planning

**Date:** 2026-05-22
**Question:** Is the current laptop adequate, and what throughput / concurrent
user capacity should we expect if the whole backend stack runs on a Mac Mini
M4 (24 GB)?

> **Status: estimates.** Numbers for the laptop are *measured*
> (see [llm-benchmark.md](llm-benchmark.md)). Numbers for the Mac Mini M4 are
> *projected* from its published memory bandwidth and the measured laptop
> baseline — not yet benchmarked on real hardware. Re-run
> [`scripts/bench_llm.py`](../scripts/bench_llm.py) on the M4 to confirm.

---

## 1. Current laptop — verdict

**Adequate for development, demos, and a small pilot. Not a production server.**

After the model optimisation the laptop runs `qwen2.5:3b` fully on the GPU at
~1.5–2 s per turn — fine for **1–3 concurrent users**. The limits are physical:

- 6 GB VRAM caps the model tier at 3B-class.
- Laptop thermals throttle under sustained load.
- A laptop is not a 24/7 appliance.

Use it to build and demo. Do not put a school on it.

---

## 2. The key insight about the Mac Mini M4

LLM decode speed is **memory-bandwidth bound**. The relevant numbers:

| Machine | Memory bandwidth | LLM-relevant memory |
|---|---|---|
| RTX 3050 6 GB Laptop GPU | ~190 GB/s | 6 GB VRAM |
| **Mac Mini M4 (base, 10-core GPU)** | **~120 GB/s** | **24 GB unified** |
| Mac Mini M4 Pro (for reference) | ~273 GB/s | up to 64 GB unified |

The base M4 has **less bandwidth than the current laptop GPU**. So for the
*same* `qwen2.5:3b` model, the M4 will be **slightly slower per token**, not
faster.

The Mac Mini is **not a raw-speed upgrade**. It is a **capacity, concurrency
and reliability upgrade**:

- 24 GB unified memory — no VRAM ceiling; can run 7B/14B models if desired.
- Memory headroom for many parallel request slots.
- A real desktop — cool, silent, 24/7-grade.

That capacity is exactly what "serve a school" requires.

---

## 3. Projected performance — Mac Mini M4 base, 24 GB

Single user, full pipeline (insights + validation per turn):

| Model | Per-turn latency | Notes |
|---|---|---|
| `qwen2.5:3b` *(current pick)* | ~3–4 s | Slightly slower than the laptop's ~1.5 s — bandwidth |
| `qwen2.5:7b` | ~6–9 s | Now viable (fits 24 GB) — higher answer quality if wanted |
| `qwen2.5:1.5b` | ~2–3 s | Max throughput, lower polish |

Everything non-LLM — Postgres + pgvector, Redis, FastAPI, retrieval,
embeddings (Redis-cached) — is trivial for a 10-core M4 and handles hundreds
of requests per second. **The LLM is the only capacity bottleneck.**

---

## 4. Concurrent user capacity

**Metric.** "Concurrent users" ≠ registered users. A 500-student school has
perhaps 15–40 *actively chatting* at peak. Capacity is measured as **active
users each sending a message every ~45–60 s** (realistic — they read, think,
type between turns).

Projection — whole stack on one Mac Mini M4 24 GB, `OLLAMA_NUM_PARALLEL=3–4`
(batched decode):

| Model | In-flight requests | Active concurrent users | Plan for |
|---|---|---|---|
| `qwen2.5:3b` | 3–5 | **~15–25** | **~15** (conservative) |
| `qwen2.5:7b` | 2–3 | ~8–12 | ~8 |

Behaviour under load:

- Idle per-turn latency ~3–4 s; at saturation it stretches to ~6–10 s as
  parallel requests share the single GPU.
- Past the limit, requests **queue** (Ollama serialises) rather than fail —
  graceful degradation; users just wait longer.

**Bottom line: one base M4 Mac Mini comfortably serves a single-school pilot
— roughly 15 concurrent chatters on `qwen2.5:3b`.**

---

## 5. Whole stack on one M4 — memory budget (24 GB)

| Component | RAM |
|---|---|
| macOS | ~4–6 GB |
| Ollama — `qwen2.5:3b` + embedding model + parallel KV caches | ~5–7 GB |
| Postgres + pgvector (one school) | ~1–2 GB |
| Redis | ~0.5 GB |
| FastAPI backend (2 replicas) + arq worker + nginx | ~2 GB |
| **Total** | **~13–17 GB — fits with headroom** |

- Comfortable with `qwen2.5:3b`.
- Tight if running `qwen2.5:7b` **and** hosting the three Next.js frontends on
  the same box — in that case host the frontends elsewhere (a CDN / Vercel /
  small separate host; they are static-ish and belong at the edge anyway).
- 512 GB SSD is ample: models ~8 GB, Docker images a few GB, Postgres data
  grows slowly for a single school.

---

## 6. Recommendations

**When moving to the M4:**

1. **Re-tune Ollama for the larger memory.** The laptop profile uses
   `OLLAMA_NUM_PARALLEL=1` (6 GB is tight). On 24 GB set it to `3–4`, and
   raise `OLLAMA_MAX_LOADED_MODELS` so a small validation model
   (`qwen2.5:1.5b`) can stay resident *alongside* the insights model — the
   per-turn validation call then never triggers a model swap.
2. **Benchmark before committing.** Run `scripts/bench_llm.py` on the actual
   M4 — it yields real numbers in minutes and replaces the estimates here.

**Scaling beyond one school:**

- **>25 concurrent users:** the base M4 is not enough. Step up to an
  **M4 Pro** (~273 GB/s ≈ 2× throughput ≈ 2× users), or split tiers.
- **Multi-tenant SaaS:** separate the LLM tier from the app tier. Keep a Mac
  Mini (or any modest box) for the cheap app/DB tier; run the LLM on a
  dedicated GPU host that can be scaled independently. The architecture
  already supports this — `OLLAMA_BASE_URL` just points elsewhere.

**Rule of thumb:** one Mac Mini M4 = one school. It is an excellent
single-tenant appliance; it is not a multi-tenant platform.

---

## 7. Summary

| | Laptop (RTX 3050 6 GB) | Mac Mini M4 base 24 GB |
|---|---|---|
| Role | Dev / demo / tiny pilot | Single-school production appliance |
| Model tier | 3B max | up to 14B (3B recommended) |
| Per-turn latency (`qwen2.5:3b`) | ~1.5–2 s *(measured)* | ~3–4 s *(projected)* |
| Concurrent active users | 1–3 | ~15–25 |
| Runs 24/7 reliably | No | Yes |
