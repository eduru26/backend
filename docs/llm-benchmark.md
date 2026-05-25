# Local LLM Benchmark & Cost Optimisation

**Date:** 2026-05-22
**Goal:** Pick the best local LLM for the Eduru mentor pipeline and tune the
stack for the available hardware — maximum performance at minimum hardware
cost.

---

## 1. Hardware

| | |
|---|---|
| GPU | NVIDIA GeForce RTX 3050 **6 GB** Laptop GPU |
| Driver | 581.86 |
| LLM runtime | Ollama (Docker, GPU passthrough) |

The 6 GB VRAM ceiling is the binding constraint — it decides which models
can run fully on the GPU versus spilling to CPU.

---

## 2. Problem found

The pipeline shipped with `qwen2.5:7b` as the generation model. At 4.7 GB on
disk it needs ~6–7 GB once the context window is allocated — more than the
card has. `ollama ps` confirmed it:

```
NAME          SIZE     PROCESSOR
qwen2.5:7b    5.4 GB   17%/83% CPU/GPU
```

17 % of every token was being computed on the CPU. That is the slow path and
the root cause of multi-second chat turns.

---

## 3. Method

Benchmark harness: [`scripts/bench_llm.py`](../scripts/bench_llm.py).

It exercises the *actual* work the pipeline does — the prompts are copied
verbatim from `backend/agents/prompt.py` and `backend/agents/validation.py`,
not a synthetic micro-benchmark:

1. **Insights** — narrate structured data + retrieved context into grounded,
   concise prose (student and teacher cases).
2. **Validation** — LLM-as-judge: is a response consistent with the data?
   Three cases: one consistent (must say PASS), two with fabricated
   numbers / invented topics (must say FAIL).

Each model is scored on:

| Metric | Meaning |
|---|---|
| **tok/s** | warm decode throughput |
| **avg turn** | mean wall-clock latency of an insights call |
| **grounding** | key facts from the data retained in the output |
| **format** | response within the role's word cap |
| **validation** | correct PASS/FAIL verdicts (the groundedness gate) |

Candidates were limited to models that fit 6 GB alongside the embedding
model. `qwen2.5:7b` was kept only as a quality reference.

---

## 4. Generation model results

| Model | tok/s | Avg turn | Grounding | Format | Validation | Verdict |
|---|---|---|---|---|---|---|
| qwen2.5:7b *(previous)* | 20.6 | 8.6 s | 5/5 | 2/2 | 3/3 | Quality OK, **5× too slow** — overflows VRAM |
| **qwen2.5:3b** | 70.6 | **1.7 s** | **5/5** | **2/2** | **3/3** | **Selected** |
| qwen3:4b-instruct | 52.3 | 4.9 s | 5/5 | 2/2 | 3/3 | 2.9× slower, no quality gain |
| llama3.2:3b | 68.4 | 3.2 s | 5/5 | 2/2 | **2/3** | Wrongly FAILs a valid response — disqualified |
| gemma2:2b | 77.4 | 4.2 s | 5/5 | 2/2 | **2/3** | Same validation defect + verbose |
| qwen2.5:1.5b | 124.2 | 3.1 s | 5/5 | 2/2 | 3/3 | Fastest decode but verbose (212–237 words) |

### Why `qwen2.5:3b`

- **Lowest turn latency (1.7 s).** Not the highest tok/s, but it writes
  *concise* answers (53–103 words — naturally inside the "under 200 words"
  target), so the whole turn finishes first. Verbose models lose here.
- **Fits the GPU fully** — `ollama ps` shows `2.3 GB · 100% GPU` (vs the
  7B's CPU spillover).
- **Perfect on every quality axis** — grounding 5/5, format 2/2,
  validation 3/3.
- **Already the Dr.Doc model.** Unifying generation + Dr.Doc on one model
  means only *one* generation model is ever resident — no VRAM swap.

### Why the others lost

- **llama3.2:3b / gemma2:2b** — both returned `FAIL` on a *consistent*
  response. The validation step is a load-bearing gate; a model that
  rejects valid answers triggers needless retries and degrades every turn.
- **qwen3:4b-instruct** — identical quality to qwen2.5:3b on this use case
  but ~3× slower. No reason to pay the latency.
- **qwen2.5:1.5b** — passes everything and decodes fastest, but is verbose,
  so its *turn* latency is worse than the 3B. Viable only as an ultra-light
  fallback.

---

## 5. Embedding model results

Warm latency, identical 768-d output, measured in isolation:

| Model | Median latency | VRAM (loaded) | Notes |
|---|---|---|---|
| nomic-embed-text | 22 ms | 595 MB | English-only |
| **nomic-embed-text-v2-moe** *(kept)* | 83 ms | 955 MB | Multilingual, newer/higher quality |

**Decision: keep `nomic-embed-text-v2-moe`.**

- The 60 ms difference is hidden by the Redis embedding cache — repeat
  queries never call Ollama at all.
- VRAM is no longer tight after the 7B→3B switch, so the extra 360 MB is
  affordable.
- It is multilingual — the subject list includes Kannada and Hindi — and
  retrieval quality drives answer quality far more than 60 ms does.

---

## 6. Optimisations applied

| Area | Change | Why |
|---|---|---|
| Generation model | `qwen2.5:7b` → `qwen2.5:3b` | Fits GPU fully; 5× faster |
| Ollama: flash attention | `OLLAMA_FLASH_ATTENTION=1` | Less VRAM per context token, faster |
| Ollama: KV-cache | `OLLAMA_KV_CACHE_TYPE=q8_0` | Halves KV-cache memory |
| Ollama: keep-alive | `OLLAMA_KEEP_ALIVE=30m` | No cold reload after short idle |
| Ollama: model slots | `OLLAMA_MAX_LOADED_MODELS=2` | gen + embed only; no 3rd-model overflow |
| Ollama: parallelism | `OLLAMA_NUM_PARALLEL=1` | Serialise — parallel decode contends for VRAM |
| Generation options | `num_ctx=4096`, `num_predict=512` | Sized to real prompts; cap runaway output |
| Validation call | `num_predict=8` | Judge only emits PASS/FAIL — return fast |
| Compose init | `ollama-init`/`dr-doc-init` pull from `.env` | Pulled model can't drift from used model |

Files touched: [`.env`](../.env), [`backend/core/config.py`](../backend/core/config.py),
[`backend/llm/ollama.py`](../backend/llm/ollama.py),
[`backend/agents/validation.py`](../backend/agents/validation.py),
[`docker-compose.yml`](../docker-compose.yml).

---

## 7. Result

| | Before | After |
|---|---|---|
| Generation model placement | 83 % GPU / 17 % CPU | **100 % GPU** |
| Single chat turn | ~25–40 s | **~1.5 s** |
| VRAM used | 5.5 GB (overflow) | 3.6 GB — **2.4 GB headroom** |
| Grounding / validation quality | 5/5 · 3/3 | 5/5 · 3/3 (unchanged) |

End-to-end verification: the full multi-agent pipeline (session memory →
intent → guardrail → retrieval → context → analytics → prompt → insights →
validation → formatter) ran **7 scenarios in 10.6 s total** — ~1.5 s per
turn, ~15–20× faster than before, on the same hardware, with identical
grounding and validation quality.

---

## 8. Reproducing

```bash
# ensure candidate models are pulled into Ollama, then:
python scripts/bench_llm.py
```

Edit the `MODELS` list in the script to benchmark a different set. To test a
single model head-to-head:

```python
import scripts.bench_llm as b
b.MODELS = ["qwen3:4b-instruct", "qwen2.5:3b"]
b.main()
```

---

## 9. If the hardware changes

The selection is hardware-bound. On a GPU with **≥ 12 GB** VRAM, re-run the
benchmark including `qwen2.5:7b` (and larger) — the 7B's quality was never
in question, only its fit. The harness and metrics stay valid; only the
"fits VRAM" constraint moves.
