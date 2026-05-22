# Eduru — Architecture Audit

**Date:** 2026-05-06
**Auditor:** Claude Code (Principal Engineer / SaaS Architect)

---

## PHASE 1: Current State Analysis

### 1.1 What Is Actually Implemented

| Component | Status | Notes |
|-----------|--------|-------|
| FastAPI backend | Skeleton exists | Boots, auth works, endpoints registered |
| JWT auth (3 roles) | Implemented | student, parent, teacher |
| PostgreSQL schema | Implemented | 13 tables via Alembic |
| CSV/JSON ingestion endpoints | Implemented | `/ingest/students`, `/ingest/marks`, etc. |
| LMS event receiver | Implemented | Accepts Frappe webhooks, dispatches to stub handlers |
| LMS callback client | Implemented | httpx async, retry logic |
| Frappe app (ai_mentor) | Implemented | Hooks, bridge, webhook receiver, 5 doctypes |
| Multi-tenant whitelabel | Implemented | Tenant Profile, Plans, Feature flags, 5 doctypes |
| 6-agent pipeline | **EMPTY SKELETON** | `agents/`, `pipelines/`, `llm/` — all empty `__init__.py` |
| LangGraph DAG | **EMPTY SKELETON** | Imported, not implemented |
| Ollama LLM client | **EMPTY SKELETON** | Not implemented |
| Chat endpoint | **DOES NOT EXIST** | Zero `/chat` route anywhere |
| Frontend / UI | **DOES NOT EXIST** | "Deferred to Phase 8" |
| Role-based views | **DOES NOT EXIST** | No student/parent/teacher UX |
| Tests | **EMPTY** | `/tests/` directory is empty |

---

### 1.2 Current Architecture (How Data Flows Today)

```
Frappe LMS
  │
  │  doc-event hooks fire on quiz/assignment/lesson/enrollment
  │
  ▼
ai_mentor Frappe app (bridge.py)
  │
  │  HTTP POST /lms/events/{slug}
  │  Bearer token + X-Tenant-Code
  │
  ▼
FastAPI /lms/events/{slug}   ← validates bearer, validates schema
  │
  │  BackgroundTask → handlers.py (stub)
  │
  ▼
handlers.py  ← STUBS: computes accuracy, builds bullet list
  │
  │  HTTP POST back to Frappe (LMSCallbackClient)
  │
  ▼
Frappe webhook receiver (mentor/webhook.py)
  │
  ▼
Eduru Insight / AI Risk Score / AI Question Tag doctypes
  (stored back in Frappe)

SEPARATELY (no connection to above):
CSV → POST /ingest/* → PostgreSQL (13-table schema)
```

**There is no chat interface. There is no analysis pipeline. The two halves (event flow + CSV ingestion) do not connect.**

---

## PHASE 2: Gap Analysis

### Gap Table

| Area | Current | Target | Gap | Fix |
|------|---------|--------|-----|-----|
| **Chat interface** | Does not exist | Primary entry point | **CRITICAL** | Build `/chat` endpoint first |
| **Student view** | No UI | Profile + stats + Q&A | **CRITICAL** | Role-aware context builder + response |
| **Parent view** | No UI | Multi-child cards + Q&A | **CRITICAL** | Parent → children lookup → context |
| **Teacher view** | No UI | Class-level + at-risk queries | **CRITICAL** | Teacher → class roster → aggregated context |
| **Data source of truth** | Duplicated in PostgreSQL (13 tables) | Frappe LMS only | **CRITICAL** | Read from Frappe API, don't mirror |
| **AI engine** | Empty skeleton (LangGraph + 6 agents) | Single LLM call with context | Overengineered skeleton | Collapse to one function |
| **LMS integration** | Event-push (Frappe → FastAPI) | On-demand read (FastAPI → Frappe) | Wrong direction | Pull data when user asks |
| **CSV ingestion** | Implemented | Not in spec | Scope creep | Remove or defer |
| **Frontend** | Deferred to Phase 8 | MVP requirement | **CRITICAL** | Build basic chat UI now |
| **Multi-tenant** | Fully designed (5 doctypes, 3 plans) | Not needed for MVP | Premature | Defer entirely |
| **pgvector/embeddings** | Schema column exists, unused | Not in MVP | Premature | Remove column |
| **Tests** | Zero | Expected for production | Gap | Write tests alongside code |

---

### 2.1 Product Design Gaps

- **No chat-first interface.** The target product is a chat UI. The current codebase has no chat concept anywhere — no `/chat` route, no message history, no conversational context management.
- **No role-based rendering.** There is role-based access control (JWT claims) but zero role-specific behavior after authentication.
- **The UI is "Phase 8."** This is a chat-first product. The UI is not a feature — it is the product. Deferring it to Phase 8 means Phase 1–7 built the wrong thing.

---

### 2.2 Data Integration Gaps

**LMS is NOT the source of truth in the current design.** The architecture runs backwards:

- Student data is uploaded via CSV into PostgreSQL (the AI backend's own DB).
- Frappe pushes events on quiz/assignment activity.
- The AI backend has its own 13-table schema that mirrors LMS structure.

This violates the core constraint. You now have two databases that must stay in sync. The CSV ingestion system, the PostgreSQL schema, and the LMS event listener are all compensating for not reading directly from Frappe.

---

### 2.3 AI Usage Gaps

- **The pipeline does not exist.** `backend/agents/`, `backend/pipelines/`, `backend/llm/` are all empty `__init__.py` files.
- **The stub handlers in `lms/handlers.py` do hardcoded bullet-point formatting** — that is not AI.
- **LangGraph is imported but there is zero graph code.** It's a dependency with no implementation.
- **6 separate agents for what is essentially:** fetch data → compute % → call LLM → return text.

---

### 2.4 UX Gaps

- No chat UI exists at all.
- No student dashboard. No parent dashboard. No teacher dashboard.
- No conversation history model in the FastAPI backend (Frappe has `AI Conversation` doctype, but the backend doesn't use it).

---

### 2.5 Scalability / SaaS Gaps

- **Multi-tenant is over-designed prematurely.** Five doctypes, three plan tiers, feature flags, CSS theming — none of this is relevant until you have one working tenant.
- **Site-per-tenant Frappe** is a valid future architecture but adds enormous operational complexity (separate DB, separate Redis, separate workers per tenant) for an MVP.

---

## PHASE 3: Simplification Check

**Verdict: The system is significantly over-engineered and misaligned with the chat-first goal.**

### Overengineering Warnings

**1. LangGraph + 6-agent DAG — REMOVE for MVP**
The pipeline design is: `ingestion → prompt → performance → insights → validation → mentor`. This is 6 LLM-adjacent steps for answering "where am I weak?" The answer to that question is: fetch the student's scores from LMS, compute subject averages in Python (5 lines), pass to Ollama, return the response. One function, one LLM call.

**2. PostgreSQL 13-table mirror of LMS — REMOVE**
You built a second LMS database. Every time a student's grade changes in Frappe, it must also be updated in your PostgreSQL. This is a synchronization problem you created by not reading from Frappe directly. The right approach: when a user asks a question, fetch their data from Frappe's REST API at that moment.

**3. CSV ingestion endpoints — REMOVE**
Frappe LMS already stores all this data. If LMS is the source of truth, there is no reason to re-upload it via CSV into a separate database. This endpoint exists only because the pipeline doesn't read from Frappe.

**4. Multi-tenant whitelabel system — DEFER**
5 doctypes, 3 plan tiers, feature flags, CSS theming. This is post-Series-A work. For MVP, one tenant configuration (a few env vars) is enough.

**5. pgvector/embeddings column — DEFER**
A 768-dim embedding column exists on `topics`. There is no embedding generation code, no vector search code, and no use case defined for it yet.

**6. SHA256 idempotency for CSV uploads — REMOVE WITH CSV**
A nice engineering detail for a feature that shouldn't exist in MVP.

**7. Frappe event-push architecture — RETHINK**
The current design requires Frappe to push events to the AI backend. For a chat system, the user asks a question → you fetch data then. No need for a real-time event pipeline at MVP.

---

## PHASE 4: Corrected Architecture

### Minimal Required Components

```
Browser (chat UI)
    │
    │  POST /chat  { role, user_id, message }
    │  GET  /chat/history
    ▼
FastAPI (backend)
    │
    ├── Auth middleware (JWT)
    │
    ├── Context Builder
    │     Student → fetch own data from Frappe
    │     Parent  → fetch children list, then selected child data
    │     Teacher → fetch class roster + aggregated data
    │
    ├── LMS Data Layer (Frappe REST API client)
    │     frappe.get_doc(), frappe.get_list()
    │     No PostgreSQL mirror — read live from Frappe
    │
    ├── Analytics Layer (pure Python, no LLM)
    │     compute_subject_averages()
    │     compute_attendance_rate()
    │     identify_weak_topics()
    │     flag_at_risk_students()
    │
    ├── AI Engine (single Ollama call)
    │     build_prompt(context, analytics, question)
    │     call_ollama(prompt) → text response
    │
    └── Conversation store (PostgreSQL — minimal)
          users        (id, email, role, frappe_user_id)
          conversations(id, user_id, created_at)
          messages     (id, conversation_id, role, content, ts)
```

### What to Keep vs Discard

| Keep | Discard / Defer |
|------|-----------------|
| FastAPI app structure | LangGraph (defer) |
| JWT auth + 3 roles | 6-agent pipeline (collapse to functions) |
| Pydantic schemas (adapt) | PostgreSQL 13-table LMS mirror |
| structlog logging | CSV/JSON ingestion endpoints |
| httpx async client (for Frappe) | pgvector/embeddings |
| core/config.py | Multi-tenant whitelabel system |
| Frappe event receiver (simplify) | SHA256 upload deduplication |
| Alembic (for 3 new tables) | `agent_runs`, `mentor_reports` tables |

### Corrected Data Flow

```
User types: "Where am I weak in math?"
    ↓
POST /chat  { message: "...", conversation_id: "..." }
    ↓
Auth: decode JWT → user_id, role
    ↓
Context Builder:
  if student: frappe_client.get_student_scores(user.frappe_id)
  if parent:  frappe_client.get_children(user.frappe_id)
              frappe_client.get_student_scores(selected_child_id)
  if teacher: frappe_client.get_class_roster(teacher.class_id)
              frappe_client.get_class_scores(class_id)
    ↓
Analytics (Python):
  subject_averages = { math: 61%, science: 78%, english: 82% }
  weak_topics      = [algebra, fractions]
    ↓
Prompt:
  "Student Aisha has scored 61% in math.
   Weakest topics: algebra (45%), fractions (52%).
   User question: Where am I weak in math?
   Provide a concise, actionable answer."
    ↓
Ollama (qwen2.5:7b) → response text
    ↓
Save message to DB (conversation history)
    ↓
Return response to chat UI
```

---

## PHASE 5: Updated Implementation Plan

### Phase 1 — MVP: Student Chat (2–3 weeks)

1. **Frappe REST client** — `backend/lms/frappe_client.py`
   - `get_student_profile(frappe_user_id)`
   - `get_student_quiz_scores(student_id)`
   - `get_student_attendance(student_id)`
2. **Analytics layer** — `backend/analytics/student.py`
   - `compute_subject_averages(scores) → dict`
   - `identify_weak_topics(scores) → list`
   - `compute_attendance_rate(attendance) → float`
3. **Ollama client** — `backend/llm/ollama.py`
   - `complete(system_prompt, user_prompt) → str`
4. **Chat endpoint** — `backend/api/chat.py`
   - `POST /chat` — receive message, build context, call Ollama, save, return
   - `GET /chat/history` — return conversation history
5. **Minimal DB schema** — 3 tables: `users`, `conversations`, `messages`
6. **Basic chat UI** — `frontend/index.html` — simple HTML/JS, no framework needed for MVP

### Phase 2 — Parent Flow (1 week)

7. Frappe client: `get_parent_children(parent_frappe_id)`
8. Context builder: parent selects child → load child context
9. UI: child selector cards before chat

### Phase 3 — Teacher Flow (1–2 weeks)

10. Frappe client: `get_teacher_class(teacher_id)`, `get_class_roster(class_id)`, `get_class_performance(class_id)`
11. Analytics: `flag_at_risk_students(class_scores)`, `find_class_weak_areas(class_scores)`
12. Context builder: teacher role → class-level context
13. UI: class roster view + chat

### Phase 4 — AI Improvements (ongoing)

14. Structured output from Ollama (JSON mode)
15. Validation layer (check response grounds claims in data)
16. Prompt refinement
17. Conversation memory (include last N messages in prompt)
18. LangGraph (only if pipeline complexity actually warrants it)

### Defer Indefinitely Until Needed

- Multi-tenant whitelabel
- pgvector / semantic search
- Real-time event pipeline (Frappe → FastAPI push)
- CSV ingestion
- 6-agent LangGraph DAG
