# Agentic Mentor System — MVP Architecture

> Multi-agent FastAPI service: teachers upload structured CSV/JSON for attendance, question banks, and marks. A LangGraph pipeline (ingestion → prompt → analysis → insights → validation → mentor) produces grounded, validated reports for students and parents. Performance computation is deterministic Python; LLMs (Ollama) only narrate and validate.

---

## 1. Gap Analysis on the Original Prompt

| # | Gap | Why it matters | MVP fix |
|---|-----|---------------|---------|
| 1 | No auth / role model | Teachers, students, parents need different views; uploads must be authenticated | JWT-based auth, 3 roles: `teacher`, `student`, `parent` |
| 2 | Input format undefined after dropping OCR | "Teacher uploads marks sheet" — but as what? | Define CSV + JSON schemas for attendance, marks, question bank |
| 3 | Topic taxonomy missing | "Topic-level analysis" is unreliable without controlled vocabulary | First-class `topics` table, seeded per subject |
| 4 | Question-level tagging | Topic analysis requires every question mapped to a topic | Question bank table; teacher tags questions on upload |
| 5 | Performance Analyzer as LLM is wrong | Computing accuracy/severity via LLM = hallucination risk | Make analyzer deterministic Python; LLM only narrates |
| 6 | Severity thresholds undefined | "low/medium/high" is vague | Rule: `>=75% low`, `50–74% medium`, `<50% high` (configurable) |
| 7 | Validation retry semantics | Infinite retry risk | Max 2 retries, then surface failure to API |
| 8 | Idempotency on upload | Re-uploading duplicates marks | SHA256 hash of upload + `upload_id` unique constraint |
| 9 | Cold-start handling | New student → LLM may fabricate insights | Pipeline short-circuits if `n_questions < threshold`, returns "insufficient data" |
| 10 | PII to LLM | Sending student names to local LLM is unnecessary | Anonymize: send `student_handle` (e.g., `S-1042`), not name |
| 11 | JSON output enforcement | Ollama models drift from schema | Pydantic validation + retry-on-parse-fail for analyzer JSON narration |
| 12 | Trend/time-series ignored | Mentoring is inherently longitudinal | Add `assessment_date`; analyzer computes delta vs previous test |
| 13 | FAISS/Qdrant unjustified | Vector store with no clear consumer = dead code | MVP: skip vector DB, defer to v2 (RAG over teacher comments) |
| 14 | Frontend scope ambiguous | "Optional scaffold" → scope creep | MVP: minimal Jinja2 SSR dashboards + JSON API; React deferred |
| 15 | No DB migrations | Schema evolution unmanaged | Alembic from day one |
| 16 | Observability | "Add logging" too vague | Structured JSON logs with `run_id` + `agent_name` correlation |
| 17 | Async + Ollama mismatch | Default Ollama client is sync | Use `httpx.AsyncClient` against Ollama HTTP API |
| 18 | Tests not requested | "Production-grade" needs them | Pytest: agent unit tests + pipeline integration test |

---

## 2. Updated MVP Prompt

A multi-agent FastAPI service where teachers upload structured CSV/JSON for attendance, question banks, and marks. A LangGraph pipeline (ingestion → prompt → analysis → insights → validation → mentor) produces grounded, validated reports for students and parents. Performance computation is deterministic Python; LLMs (Ollama) only narrate and validate. PostgreSQL stores all structured data; FAISS deferred to v2. Jinja2 dashboards for student/parent views.

**Inputs (direct, no OCR):**

- `POST /ingest/students` — CSV roster
- `POST /ingest/attendance` — CSV: `date, student_handle, status`
- `POST /ingest/question-bank` — JSON: questions tagged with subject + topic + max_marks
- `POST /ingest/marks` — CSV: `assessment_id, student_handle, question_id, marks_obtained`

**Pipeline trigger:** `POST /analyze` with `student_id` (+ optional date range)

**Hard constraints:**

- No external paid APIs
- No PII sent to LLM (handles only)
- Validation gate is non-bypassable; max 2 retries
- All numerical claims traceable to a SQL row

---

## 3. Architecture

### 3.1 Folder Structure

```
ai-mentor/
├── backend/
│   ├── agents/
│   │   ├── __init__.py
│   │   ├── base.py                  # AgentBase: run(), logging, retry
│   │   ├── ingestion_agent.py       # Validates + persists CSV/JSON
│   │   ├── prompt_agent.py          # Builds constrained prompts from DB rows
│   │   ├── performance_agent.py     # DETERMINISTIC Python analyzer
│   │   ├── insights_agent.py        # LLM: numbers → prose
│   │   ├── validation_agent.py      # LLM: groundedness judge
│   │   └── mentor_agent.py          # LLM: actionable recs (student + parent)
│   ├── pipelines/
│   │   ├── __init__.py
│   │   ├── state.py                 # TypedDict for LangGraph state
│   │   └── mentor_graph.py          # LangGraph DAG with retry edge
│   ├── llm/
│   │   ├── __init__.py
│   │   ├── ollama_client.py         # async httpx client + JSON-mode
│   │   └── prompts/                 # Jinja2 prompt templates
│   ├── models/                      # Pydantic schemas (API + agent I/O)
│   │   ├── __init__.py
│   │   ├── ingestion.py
│   │   ├── analysis.py
│   │   └── mentor.py
│   ├── db/
│   │   ├── __init__.py
│   │   ├── base.py                  # SQLAlchemy async engine
│   │   ├── models.py                # ORM tables
│   │   ├── repositories/            # Per-aggregate data access
│   │   └── migrations/              # Alembic
│   ├── api/
│   │   ├── __init__.py
│   │   ├── deps.py                  # Auth, DB session
│   │   ├── ingest.py                # /ingest/*
│   │   ├── analyze.py               # /analyze, /student/{id}/*
│   │   └── auth.py                  # JWT login
│   ├── core/
│   │   ├── config.py                # Pydantic Settings
│   │   ├── logging.py               # structlog setup
│   │   └── security.py              # password hashing, JWT
│   ├── templates/                   # Jinja2 dashboards
│   ├── tests/
│   │   ├── unit/
│   │   └── integration/
│   └── main.py                      # FastAPI app factory
├── sample_data/
│   ├── students.csv
│   ├── attendance.csv
│   ├── question_bank.json
│   └── marks.csv
├── docs/
│   └── architecture.md
├── .env.example
├── docker-compose.yml               # postgres + ollama
├── pyproject.toml
├── alembic.ini
└── README.md
```

### 3.2 Data Model

```
students
  id (uuid pk) | handle (unique, e.g. S-1042) | full_name | grade | section
  parent_email | created_at

users
  id | email (unique) | password_hash | role (teacher|student|parent)
  student_id (fk, nullable — for student/parent users)

subjects
  id | name (unique)

topics
  id | subject_id (fk) | name | UNIQUE(subject_id, name)

assessments
  id | type (homework|unit_test|exam) | title | subject_id (fk)
  assessment_date | max_marks | created_by (fk users)

questions
  id | assessment_id (fk) | question_no | topic_id (fk)
  max_marks | difficulty (easy|medium|hard)

question_responses
  id | question_id (fk) | student_id (fk) | marks_obtained
  is_correct (computed: marks_obtained == max_marks) | answered_at
  UNIQUE(question_id, student_id)

attendance
  id | student_id (fk) | date | status (present|absent|late)
  UNIQUE(student_id, date)

uploads                              -- idempotency
  id | sha256 (unique) | kind | uploaded_by | uploaded_at | row_count

agent_runs                           -- observability
  id (run_id) | student_id | started_at | finished_at | status
  graph_trace (jsonb)                -- per-agent input/output

mentor_reports                       -- final pipeline output
  id | run_id (fk) | student_id | analysis_json | insights_text
  validation_json | mentor_json | created_at
```

**Aggregation (computed on-read by the Performance Agent, not stored):**

- topic accuracy = `SUM(marks_obtained) / SUM(max_marks)` grouped by `topic_id`
- attendance % = `count(present) / count(*)` over date range
- trend = current accuracy − previous-period accuracy

### 3.3 Pipeline (LangGraph)

```
              ┌─────────────────┐
              │  Ingestion      │  (entry: pulls raw rows from DB by student_id + date range)
              └───────┬─────────┘
                      ▼
              ┌─────────────────┐
              │  Prompt         │  (deterministic; assembles constrained context blocks)
              └───────┬─────────┘
                      ▼
              ┌─────────────────┐
              │  Performance    │  (PURE PYTHON: SQL aggregates → analysis JSON)
              └───────┬─────────┘
                      ▼
              ┌─────────────────┐
              │  Insights       │  (LLM: JSON → prose, temp=0)
              └───────┬─────────┘
                      ▼
              ┌─────────────────┐
              │  Validation ◄───┼── retry loop (max 2)
              └───────┬─────────┘
                      │ valid=true
                      ▼
              ┌─────────────────┐
              │  Mentor         │  (LLM: recommendations for student + parent)
              └───────┬─────────┘
                      ▼
              ┌─────────────────┐
              │  Validation     │  (re-validate Mentor output)
              └───────┬─────────┘
                      ▼
                  Persist `mentor_reports`
```

**LangGraph state (TypedDict):**

```python
class MentorState(TypedDict):
    run_id: str
    student_id: str
    date_range: tuple[date, date]
    raw_data: IngestionPayload          # rows from DB
    prompt_context: PromptContext       # structured blocks
    analysis: AnalysisResult            # deterministic JSON
    insights_text: str
    mentor_output: MentorOutput
    validation: ValidationResult
    retry_count: int
    error: str | None
```

**Retry edge:** Validation → Insights (or Mentor) if `valid=false AND retry_count<2`. After max retries → terminate with `status=validation_failed`, persist trace.

### 3.4 Agent Contracts

| Agent | Input | Output | LLM? |
|---|---|---|---|
| Ingestion | `student_id, date_range` | `IngestionPayload` (rows) | No |
| Prompt | `IngestionPayload` | `PromptContext` (templated strings + numeric facts) | No |
| Performance | `IngestionPayload` | `AnalysisResult` (topic, accuracy, weak_areas, severity, root_causes) | No — **deterministic** |
| Insights | `AnalysisResult, PromptContext` | `insights_text: str` | Yes — Ollama |
| Validation | `analysis, generated_text` | `{valid, reason}` | Yes — Ollama |
| Mentor | `AnalysisResult, insights_text` | `MentorOutput` (student_recs[], parent_actions[]) | Yes — Ollama |

### 3.5 Key Design Decisions

1. **Performance Agent is deterministic Python.** Biggest deviation from the original prompt. Arithmetic + thresholding via LLM is a hallucination factory. The original spec says the analyzer "outputs JSON" — contract preserved, computation moved to code.
2. **`root_causes` in analysis JSON is rule-based** (e.g., `"low_attendance"` if attendance < 75%, `"weak_easy_questions"` if easy-question accuracy < 70%). Not LLM-inferred.
3. **No FAISS/Qdrant in MVP.** Adding it for "completeness" violates the no-half-finished-implementations rule. Stub a `vector_store/` interface only; document v2 use case (RAG over teacher comments).
4. **No OCR module.** Ingestion is CSV/JSON only.
5. **Frontend = Jinja2 SSR** in `/templates`, served by FastAPI. Two pages: student dashboard, parent dashboard. JSON API stays available for a future React rewrite.
6. **Auth = local JWT** (HS256). No OAuth/SSO.
7. **Anonymization at LLM boundary:** prompts use `student_handle` (e.g., `S-1042`); the API layer maps back to `full_name` for human viewers.

### 3.6 Tech Stack Pin

- Python 3.11
- FastAPI + uvicorn
- SQLAlchemy 2.x async + asyncpg
- Alembic
- LangGraph (latest)
- httpx (async Ollama client)
- Pydantic v2
- Jinja2
- pytest + pytest-asyncio
- structlog
- passlib[bcrypt], python-jose

---

## 4. Open Questions Awaiting Confirmation

1. Performance Agent as deterministic Python (not LLM) — OK?
2. Drop FAISS/Qdrant from MVP — OK?
3. Jinja2 SSR dashboards (vs separate React app) — OK?
4. Ingestion = CSV + JSON only with the schemas above — OK?
5. Auth = JWT + 3 roles (teacher/student/parent) — OK, or skip auth for MVP?
6. Default Ollama model: `llama3:8b` — OK, or prefer `qwen2.5:7b`?

---

## 5. Execution Plan

| Step | Phase | Status |
|------|-------|--------|
| 1 | Architecture design | Done — this document |
| 2 | Folder scaffold + DB schema + Alembic migration | Pending confirmation |
| 3 | Agent implementations | Pending |
| 4 | LangGraph pipeline wiring | Pending |
| 5 | API routes + auth | Pending |
| 6 | Sample data + Jinja2 dashboards | Pending |
| 7 | Integration tests + README | Pending |