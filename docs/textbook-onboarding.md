# Textbook onboarding — uploading PDFs for AI question generation

> **Scope note**: This pipeline applies to **Mathematics, Science, Social
> Science**, and any other subject that uses AI question generation.
> **English, Hindi, and Kannada** papers are filled in manually by the
> teacher — the create wizard skips AI for those subjects and lands the
> teacher on a blank template form. No PDF upload required for language
> subjects.

When a teacher sees:

> 6 selected topic(s) had no textbook coverage
> No questions were generated for these topics — strict grounding is on,
> so the AI never invents content.

it means the school's textbook for that subject/grade hasn't been
**uploaded and applied** yet. Strict grounding is intentional — we
never let the LLM make up content the textbook doesn't back. This doc
walks through the one-time process to fix it.

---

## How grounding works (60-second mental model)

```
 PDF                  TextbookIngestion         Qdrant chunks         Question
┌────┐ POST /textbooks ┌────────────┐  docling  ┌──────────────┐ gen ┌──────────┐
│    │ ───────────────►│ queued     │ ────────► │ tenant_id    │ ──► │ grounded │
│PDF │                 │ extracting │  chunks   │ chapter_order│     │ in       │
│    │                 │ chunking   │  embeds   │ topic_name   │     │ chunks   │
│    │                 │ embedding  │           │ chunk_type   │     │          │
└────┘                 │ ready ─────┼─proposal──┤              │     │          │
                       └────────────┘   apply   │ chapter_id   │     │          │
                                       ───────► │ topic_id     │     │          │
                                                └──────────────┘     └──────────┘
```

1. **Upload**: school admin picks subject + grade and uploads a raw PDF
   via `POST /school/curriculum/textbooks`. The bytes land on the shared
   `textbook_pdfs` Docker volume; a `TextbookIngestion` row is created
   with status=`queued`.
2. **Ingest worker (docling)**: the dedicated `ingest-worker` service
   picks up the job and walks through:
   - `extracting` — docling parses the PDF into structured markdown
   - `chunking` — splits markdown by H1/H2 into chapters/topics and
     by 600/100 windowed sub-chunks per topic body
   - `embedding` — embeds every chunk via Ollama and upserts to Qdrant
     tagged with `chapter_order` / `topic_name` / `chunk_type=content`
   - `ready` — emits a `CurriculumProposal` with the detected outline
3. **Apply**: admin reviews the proposal (rename / merge / drop
   chapters as needed) and clicks **Apply**. That creates the Postgres
   `chapters` / `topics` rows AND back-tags every Qdrant chunk with the
   matching `chapter_id` / `topic_id`. **This is the step that makes
   chunks queryable per-topic.**
4. **Generate**: teacher picks chapters + topics → paper generator
   hard-filters Qdrant to those `chapter_id` / `topic_id` values → the
   grounding validator rejects any LLM output that drifts from those
   chunks.

If you skip step 3 (apply), chunks exist in Qdrant but have no
`chapter_id` / `topic_id`, so the strict filter returns zero hits and
the "no textbook coverage" banner appears.

---

## Step 1 — Upload the PDF

In the school-admin UI:

1. Go to **Curriculum**.
2. Click **Upload textbook PDF** (top-right).
3. Pick the **Subject** and **Grade** this textbook belongs to.
4. Choose the `.pdf` file (max 150 MB).
5. Click **Upload**. The dialog closes — a new row appears in the
   "Textbook PDFs" status board.

Or via API for scripted onboarding:

```bash
curl -X POST https://<school-host>/school/curriculum/textbooks \
    -H "Authorization: Bearer <admin-token>" \
    -F "file=@/path/to/Maths Part - 1 2025-26.pdf" \
    -F "subject_id=<subject-uuid>" \
    -F "grade=6"
```

Re-uploading the same bytes (same sha256) for the same tenant is a
no-op — it just returns the existing ingestion row.

---

## Step 2 — Watch the status board

The "Textbook PDFs" card on the Curriculum page polls every 3s while
anything is still processing. Each row shows:

| Status      | What's happening                                  |
|-------------|---------------------------------------------------|
| Queued      | Waiting for the ingest-worker to pick it up       |
| Parsing     | Docling is running layout + OCR on the PDF        |
| Splitting   | Markdown is being split into chapters/topics      |
| Embedding   | Chunks are going to Ollama → Qdrant               |
| Ready       | Proposal created; admin needs to Apply            |
| Failed      | See the inline error; click **Retry** to re-queue |

A clean 200-page English textbook takes ~3–5 minutes; a legacy-font
Indian textbook with heavy OCR can run 10–20 minutes. The first PDF
after a fresh deploy is slower because docling lazy-downloads its
~2-3 GB of layout models — pre-warmed during the `ingest-worker` image
build, but the first real PDF still warms the in-process cache.

You can poll directly:

```bash
curl -H "Authorization: Bearer <admin-token>" \
    https://<school-host>/school/curriculum/textbooks/ingestions
```

---

## Step 3 — Apply the proposal

When status flips to **Ready** a `CurriculumProposal` appears under
**Textbook proposals** on the same page.

1. Click **Review**.
2. Rename / merge / drop chapters as desired — but **keep the `order`
   numbers as they came from docling**. The order is what links a
   Postgres chapter to its Qdrant chunks.
3. Click **Apply to curriculum**.

What happens on apply:

- Postgres `chapters` and `topics` rows are created (or reused if the
  admin's edited name matches an existing chapter).
- `QdrantStore.tag_chunks_with_curriculum` iterates every chunk for
  this textbook's `source` filename and merges in the resolved
  `chapter_id` (matched via `chapter_order`) and `topic_id` (matched
  via `(chapter_order, topic_name)`).
- The proposal flips to status=`applied`.

> If you forget to click Apply, chunks stay in Qdrant but the strict
> chapter filter returns zero hits → "no textbook coverage" for every
> topic. Apply is required, every time.

---

## Step 4 — Verify coverage before letting teachers generate

```bash
curl -H "Authorization: Bearer <admin-token>" \
    "https://<school-host>/school/curriculum/coverage?subject_id=<sid>&grade=6"
```

A healthy state has:

- `untagged_chunks: 0` (everything got linked to a chapter on apply)
- Every chapter has `content_chunks > 0`
- Most topics have `content_chunks > 0` — topic-level tagging is
  best-effort (docling's heading detection can miss sub-topics), but
  the paper generator's retrieval falls back to chapter-level when a
  topic is sparse so that's usually fine.

---

## Step 5 — Teacher regenerates the paper

Back in the teacher UI, **Regenerate** any paper that was showing the
warning. Strict grounding now finds the relevant chunks, the banner
disappears, and every generated question carries a `📘 Ch X` badge
linking to the source excerpt.

---

## Troubleshooting

**Status stuck at "Queued"**

The ingest-worker service is down or unreachable. Check `docker compose
ps ingest-worker` — it should be running. If you just brought the stack
up, the worker takes ~30s after `migrate` finishes before it starts
draining the queue.

**Status stuck at "Parsing" for an hour**

Docling can hang on malformed PDFs (corrupt page streams, encrypted
content). Click **Retry** once; if it stalls again, the PDF needs
manual repair (e.g. open in a PDF tool and re-export). Job timeout is
30 minutes; after that the row flips to `failed`.

**"no chapter headings detected in markdown"**

Docling ran but didn't find any H1/H2 in its markdown output. Usually
the PDF is a flat scan with no extractable structure. Workarounds:
add chapters manually via **Write curriculum**, then re-upload to get
chunks indexed against them; or pre-process the PDF through a tool that
produces tagged PDF before uploading.

**"6 topics had no textbook coverage" after apply**

- Confirm the proposal status reads `applied`, not `ready`.
- Run the coverage check (Step 4). If `untagged_chunks > 0`, the apply
  step's chapter-order matching failed — usually because the admin
  edited `order` numbers during review. Delete the ingestion row
  (which clears the Qdrant chunks) and re-upload.
- If a chapter's `content_chunks = 0`, those pages didn't survive
  docling's text extraction. Check the source PDF — sometimes the
  body font is so obfuscated that even docling's OCR can't read it.

**Embedding-model mismatch**

If the backend's `OLLAMA_EMBED_MODEL` changes after a textbook is
already indexed, Qdrant chunks become unreachable (vector_dim could
even mismatch and corrupt cosine search). Re-upload affected PDFs to
re-embed under the new model. The Qdrant collection guard rejects
mismatched vector_dim loudly on startup.

---

## Quick checklist for a new school

- [ ] Stack up: `docker compose up -d` (this now starts `ingest-worker`
      alongside backend/worker).
- [ ] For each subject + grade textbook, upload the PDF via Curriculum
      → **Upload textbook PDF**.
- [ ] Watch the status board until every row reads **Ready**.
- [ ] Open each **proposal** and click **Apply**.
- [ ] `GET /school/curriculum/coverage` shows `untagged_chunks: 0` and
      every chapter has `content_chunks > 0`.
- [ ] Teacher generates a paper — banner is gone, every question
      carries a chapter badge.

---

## Reference files

- Ingest service: [backend/services/textbook_ingest.py](../backend/services/textbook_ingest.py)
- Ingest worker job + settings: [backend/worker/jobs.py](../backend/worker/jobs.py), [backend/worker/ingest_settings.py](../backend/worker/ingest_settings.py)
- Dedicated worker image: [Dockerfile.ingest-worker](../Dockerfile.ingest-worker)
- Upload + status endpoints: [backend/api/curriculum.py](../backend/api/curriculum.py)
- DB schema: [backend/db/models.py](../backend/db/models.py) (`TextbookIngestion`, `IngestStatus`)
- Qdrant filters + chapter back-tagging: [backend/rag/qdrant_store.py](../backend/rag/qdrant_store.py)
- Grounded paper generation: [backend/services/paper_generator.py](../backend/services/paper_generator.py)
- Grounding validator: [backend/agents/grounding.py](../backend/agents/grounding.py)
