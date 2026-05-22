# Eduru

Chat-first academic mentoring system for Students, Parents, Teachers, School Admins, and Superadmins.
Built with FastAPI + LangGraph (6-agent pipeline) + Ollama (local LLM) + PostgreSQL,
fronted by three Next.js 15 apps.

---

## Architecture

```
┌──────────────────────────┐  ┌──────────────────────────┐  ┌──────────────────────────┐
│ frontend-learner :3000   │  │ frontend-staff   :3001   │  │ frontend-admin   :3002   │
│ for: student, parent     │  │ for: teacher,            │  │ for: superadmin          │
│ PWA-ready, mobile-first  │  │      school_admin        │  │ schools & users console  │
└────────────┬─────────────┘  └────────────┬─────────────┘  └────────────┬─────────────┘
             │                             │                             │
             └────────────── JWT  /api/* ──┴──────────────┬──────────────┘
                                                          │
                                              ┌──────────▼──────────┐
                                              │  FastAPI backend    │
                                              │  (port 8000)        │
                                              └──────────┬──────────┘
                                                         │
                          ┌──────────────────────────────┼────────────────────────────┐
                          │                              │                            │
                ┌─────────▼────────┐         ┌───────────▼──────────┐      ┌──────────▼──────────┐
                │ PostgreSQL +     │         │ LangGraph 6-agent    │      │ Ollama LLM          │
                │ pgvector         │         │ pipeline             │──────│ (qwen2.5:7b)        │
                └──────────────────┘         └──────────────────────┘      └─────────────────────┘
```

**Mentor pipeline (per chat message):**
SessionMemory → IntentClassifier → Guardrail (Intent Fence) →
Retrieval (vector RAG + Knowledge Graph) → ContextAgent → AnalyticsAgent →
PromptAgent → InsightsAgent → ValidationAgent (judge + PII leak check, retry×2) → FormatterAgent.

The RAG / RBAC layer enforces a `(role, intent)` policy matrix before any
data is fetched, then runs a **hybrid** retrieval (pgvector cosine + BM25
full-text, fused with Reciprocal Rank Fusion) over `rag_document_chunks`
(HNSW + GIN indexes). Metadata filters — tenant, `role_visibility`,
student/classroom scope — are applied *before* either ranking function
runs, so cross-tenant or out-of-scope chunks can never reach the LLM.
The rule-based intent classifier falls back to an LLM only when its
confidence is low. See [docs/architecture.md](docs/architecture.md).

---

## Prerequisites

| Tool | Version | Notes |
|------|---------|-------|
| Docker Desktop | latest | https://docker.com/products/docker-desktop |
| Git | any | https://git-scm.com |

That's it. Python, PostgreSQL, and Ollama all run inside Docker.

---

## Quick start (Docker Compose)

### 1. Copy and configure the environment file

```powershell
copy .env.example .env
```

Edit `.env` and set a strong JWT secret:

```
JWT_SECRET=replace-this-with-a-long-random-string
```

Everything else works with the defaults for local Docker development.

### 2. Build and start all services

```powershell
docker compose up --build
```

This does in order:

| Step | Service | What happens |
|------|---------|-------------|
| 1 | `postgres` | Starts PostgreSQL 16 with pgvector |
| 2 | `ollama` | Starts the local LLM server |
| 3 | `ollama-init` | Pulls `qwen2.5:7b` (~4 GB, first run only) |
| 4 | `backend` | Runs DB migrations → starts FastAPI on :8000 |

> **First run:** `ollama-init` downloads the model (~4 GB). This takes several minutes
> depending on your connection. Subsequent starts are instant — the model is cached
> in the `ollama_data` Docker volume.

### 3. Start the three frontend apps

In three more terminals (Node 20+ required):

```powershell
# Learner — student & parent
cd frontend-learner
copy .env.local.example .env.local
npm install
npm run dev          # http://localhost:3000

# Staff — teacher & school admin
cd frontend-staff
copy .env.local.example .env.local
npm install
npm run dev          # http://localhost:3001

# Admin — superadmin (schools & users console)
cd frontend-admin
copy .env.local.example .env.local
npm install
npm run dev          # http://localhost:3002
```

All apps proxy `/api/*` to the FastAPI backend (default `http://localhost:8000`).
The learner app is PWA-installable; the staff and admin apps use a desktop sidebar
with mobile bottom-nav.

---

## Services and ports

| Service | Container | Port | Description |
|---------|-----------|------|-------------|
| nginx | `ai_mentor_gateway` | 8080 | **API gateway + load balancer** — single entry point in front of the backend replicas |
| backend | `ai-mentor-backend-N` | (internal) | FastAPI — runs as N replicas behind nginx, no direct host port |
| worker | `ai-mentor-worker-N` | (internal) | arq worker — durable background jobs (RAG embedding/ingest, LMS events) |
| migrate | `ai_mentor_migrate` | — | One-shot Alembic `upgrade head`; backend/worker wait on it |
| postgres | `ai_mentor_postgres` | 5432 | PostgreSQL 16 + pgvector |
| redis | `ai_mentor_redis` | 6379 | Cache + rate-limit counters + arq job queue |
| ollama | `ai_mentor_ollama` | 11434 | Ollama LLM server |
| frontend-learner | (host process) | 3000 | Next.js — student/parent app |
| frontend-staff | (host process) | 3001 | Next.js — teacher/school admin app |
| frontend-admin | (host process) | 3002 | Next.js — superadmin schools/users console |

> The backend is no longer published on `:8000` — all API traffic goes through
> the nginx gateway on **`http://localhost:8080`**. Point each frontend's
> `NEXT_PUBLIC_API_BASE_URL` at `http://localhost:8080`.

## Scaling & resilience — 7 system-design patterns

| Pattern | Where |
|---------|-------|
| **API gateway** | nginx (`nginx/nginx.conf`) — routing, gzip, security headers, body-size cap, edge throttle |
| **Load balancing** | nginx per-request DNS resolution round-robins across all `backend` replicas |
| **Rate limiting** | `slowapi` + Redis — `8/min` on `/auth/login`, `120/min` global; shared across replicas |
| **Caching** | Redis — embedding cache (deterministic, 7-day TTL) keeps repeat queries off Ollama |
| **Message queue** | `arq` worker + Redis — RAG embedding/ingest + LMS events run off the request thread, survive crashes |
| **Circuit breakers** | `backend/core/circuit_breaker.py` — wraps Ollama (gen + embed), converter, LMS callbacks; fails fast when a dependency is down |
| **Auto scaling** | `scripts/autoscale.py` — polls `/metrics`, scales `backend` replicas between 2–6 on queue depth / request rate / p95 latency (compose `--scale` or Swarm `service scale`) |

Scale manually any time: `docker compose up -d --scale backend=4`.
Run the autoscaler: `python scripts/autoscale.py` (see its docstring for env vars).

---

## Creating users

The system ships with no seed users. Create accounts with this one-liner:

```powershell
docker compose exec backend python -c "
import asyncio, uuid
from backend.core.security import hash_password
from backend.db.base import get_session_factory, init_engine
from backend.db.models import User, UserRole

async def seed():
    init_engine()
    async with get_session_factory()() as s:
        users = [
            User(id=uuid.uuid4(), email='teacher@school.com',
                 password_hash=hash_password('teacher123'),
                 role=UserRole.teacher, full_name='Mr. Smith'),
            User(id=uuid.uuid4(), email='parent@school.com',
                 password_hash=hash_password('parent123'),
                 role=UserRole.parent, full_name='Mrs. Jones'),
        ]
        s.add_all(users)
        await s.commit()
        print('Done.')

asyncio.run(seed())
"
```

| Role | Email | Password |
|------|-------|----------|
| Teacher | teacher@school.com | teacher123 |
| Parent | parent@school.com | parent123 |

> Student accounts are linked to student records uploaded via CSV.
> After uploading students, create a `UserRole.student` user and set
> `student_id` to the matching student row UUID.

---

## Data upload (Teacher)

Log in as teacher → use the **Upload Data** panel in the left sidebar.
Upload in this order:

### 1. Students CSV

```csv
handle,full_name,grade,section,parent_email
STU001,Aisha Khan,8,A,parent@school.com
STU002,Bilal Ahmed,8,A,parent@school.com
STU003,Sara Malik,8,B,other@school.com
```

### 2. Attendance CSV

```csv
date,student_handle,status
2026-01-10,STU001,present
2026-01-10,STU002,absent
2026-01-11,STU001,late
```

`status` values: `present` / `absent` / `late`

### 3. Question Bank JSON

```json
{
  "assessment": {
    "type": "unit_test",
    "title": "Math Unit 1",
    "subject": "Mathematics",
    "date": "2026-01-15",
    "max_marks": 50
  },
  "questions": [
    { "question_no": 1, "topic": "Algebra",   "max_marks": 20, "difficulty": "medium" },
    { "question_no": 2, "topic": "Fractions", "max_marks": 15, "difficulty": "easy"   },
    { "question_no": 3, "topic": "Geometry",  "max_marks": 15, "difficulty": "hard"   }
  ]
}
```

`type` values: `homework` / `unit_test` / `exam`

### 4. Marks CSV (student responses)

Get the `assessment_id` UUID from the DB after uploading the question bank,
or from `GET /ingest/assessments` (coming soon).

```csv
assessment_id,student_handle,question_no,marks_obtained
<uuid>,STU001,1,16
<uuid>,STU001,2,12
<uuid>,STU001,3,9
<uuid>,STU002,1,8
<uuid>,STU002,2,10
<uuid>,STU002,3,5
```

---

## Example chat questions

**Student** — "Where am I weakest?", "How is my attendance?", "What should I study next?"

**Parent** — "How is my child doing in Math?", "Should I be worried about attendance?"

**Teacher** — "Which students are at risk?", "Which topic is the class struggling with?",
"Summarise Aisha's performance."

---

## Common commands

```powershell
# Start everything (detached)
docker compose up -d

# View logs
docker compose logs -f backend
docker compose logs -f ollama

# Stop everything
docker compose down

# Stop and delete all data (full reset)
docker compose down -v

# Rebuild backend image after code changes
docker compose build backend
docker compose up -d backend

# Run migrations manually
docker compose exec backend python -m alembic upgrade head

# Open a shell in the backend container
docker compose exec backend sh

# Check Ollama model list
docker compose exec ollama ollama list
```

---

## Local development (without Docker)

If you prefer to run the backend locally against Docker-hosted Postgres + Ollama:

```powershell
# Start only infra
docker compose up -d postgres ollama ollama-init

# Python env
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"

# Copy env and leave DATABASE_URL / OLLAMA_BASE_URL as localhost defaults
copy .env.example .env

# Migrate
python -m alembic upgrade head

# Run
uvicorn backend.main:app --reload
```

Open http://localhost:8000.

---

## API reference

Interactive Swagger UI at **http://localhost:8000/docs**.

| Area | Routes | Roles |
|------|--------|-------|
| Auth | `POST /auth/login`, `GET /auth/me` | — |
| Chat & analytics | `POST /chat/conversations`, `POST/GET /chat/conversations/{id}/messages`, `GET /chat/students`, `GET /chat/stats`, `GET /chat/suggestions`, `GET /chat/proactive`, `GET /chat/assignments`, `POST /chat/assignments/{id}/submit` | all |
| RAG corpus | `POST /rag/documents` (UTF-8 text), `POST /rag/documents/pdf` (PDF, ≤5 MB), `POST /rag/reindex` (announcements + assignments) | teacher / school_admin |
| Ingest (CSV / JSON) | `POST /ingest/students`, `/ingest/attendance`, `/ingest/question-bank`, `/ingest/marks` | teacher |
| Excel intake (LLM) | `POST /upload/excel` | teacher |
| Teacher tools | `/teacher/classrooms`, `/teacher/students`, `/teacher/students/onboard`, `/teacher/students/import`, `/teacher/attendance` (`/bulk`), `/teacher/assignments` (CRUD + `/publish`, `/unpublish`, `/{id}/stats`) | teacher |
| School admin | `/school/me`, `/school/teachers` (CRUD + assignments + timetable), `/school/classrooms` (CRUD), `/school/students` (CRUD + onboard + import), `/school/subjects` | school_admin |
| School analytics | `GET /school/analytics/overview` | school_admin |
| Academic year | `/school/academic-years` (CRUD + `/set-current` + `/promote` + `/history`) | school_admin |
| Superadmin | `/admin/tenants` (CRUD), `/admin/tenants/{id}/users` (CRUD), `/admin/tenants/{id}/stats` | superadmin |
| Profiles | `GET /students/{id}`, `GET /parent/children` | role-scoped |
| Announcements | `GET/POST /announcements`, `DELETE /announcements/{id}` | author / school_admin |
| Notifications | `GET /notifications`, `GET /notifications/unread-count`, `POST /notifications/{id}/read`, `POST /notifications/read-all` | self |
| LMS events | `POST /lms/events/{event_slug}` | bearer |
| Health | `GET /health`, `GET /health/db` | — |

Auto-emitted notifications:
- **marks_posted** — when a marks CSV is ingested, the affected children's parents are notified.
- **assignment_published** — when an MCQ assignment goes live, all parents in the classroom are notified.
- **attendance_alert** — after a bulk attendance update, parents of children whose rolling-30-day attendance fell below 75% are alerted (rate-limited to one per child per 7 days).
- **announcement** — fan-out per audience when a school admin / teacher posts.

---

## Project layout

```
ai-mentor/
├── Dockerfile              Backend container definition
├── entrypoint.sh           migrate → uvicorn
├── docker-compose.yml      All 4 services (postgres, ollama, ollama-init, backend)
├── .dockerignore           Files excluded from Docker build context
├── .gitattributes          LF line endings for shell scripts
├── pyproject.toml          Python dependencies
├── alembic.ini             Migration config
├── .env.example            Environment template
│
├── backend/
│   ├── agents/
│   │   ├── state.py        MentorState TypedDict
│   │   ├── context.py      Agent 1 — DB fetch (role-aware)
│   │   ├── analytics.py    Agent 2 — pure Python stats
│   │   ├── prompt.py       Agent 3 — prompt builder
│   │   ├── insights.py     Agent 4 — Ollama call
│   │   ├── validation.py   Agent 5 — LLM-as-judge
│   │   └── formatter.py    Agent 6 — response cleanup
│   ├── pipelines/
│   │   └── mentor.py       LangGraph DAG
│   ├── llm/
│   │   └── ollama.py       Async Ollama client
│   ├── api/
│   │   ├── auth.py         /auth/*
│   │   ├── chat.py         /chat/*
│   │   ├── ingest.py       /ingest/*
│   │   └── deps.py         JWT auth, role guards
│   ├── db/
│   │   ├── models.py       SQLAlchemy ORM (15 tables)
│   │   ├── repositories/   Data access layer
│   │   └── migrations/     Alembic versions (0001–0003)
│   ├── core/
│   │   ├── config.py       Pydantic Settings
│   │   ├── security.py     JWT + bcrypt
│   │   └── logging.py      structlog JSON
│   └── main.py             FastAPI app factory
│
├── frontend-learner/        Next.js 15 app — student & parent (port 3000)
│   ├── app/                  App Router routes: login, dashboard, chat, announcements, notifications
│   ├── components/           shadcn-style primitives (Button, Card, Input, …) + SiteHeader + AuthGate
│   ├── lib/                  api client, zustand auth store
│   └── public/sw.js          PWA offline shell
│
├── frontend-staff/          Next.js 15 app — teacher / school admin (port 3001)
│   ├── app/                  App Router routes:
│   │   ├── dashboard         role-aware (teacher class cards + today's timetable · admin analytics)
│   │   ├── students          list, onboard, CSV import, classroom filter
│   │   ├── teachers          CRUD + classroom assignments
│   │   ├── classrooms        CRUD + class teacher picker
│   │   ├── timetable         bell schedule (Mon–Fri/Sat) + per-teacher & per-class grids
│   │   ├── academic-year     years, set-current, promote dialog with diff
│   │   ├── attendance        roster with P/A/L toggles + bulk save
│   │   ├── assignments       MCQ composer + publish + stats dialog
│   │   ├── announcements     composer with audience/classroom + delete
│   │   ├── notifications     inbox with mark read
│   │   └── chat              role-aware mentor chat
│   └── components/           SiteShell (sidebar + mobile bottom nav), Dialog, Toast, Field
│
├── frontend-admin/          Next.js 15 app — superadmin console (port 3002)
│   ├── app/                  App Router routes:
│   │   ├── dashboard         platform overview (schools, students, users totals)
│   │   ├── schools           tenant CRUD (create/edit/activate/delete)
│   │   └── schools/[id]/users per-school user CRUD
│   └── components/           AdminShell (sidebar + mobile bottom nav), Dialog, Toast, Field
│
└── docs/
    └── architecture-audit.md
```

---

## Troubleshooting

**`ollama-init` exits immediately with "model not found" or times out**
The Ollama server may not be ready yet. Run:
```powershell
docker compose restart ollama-init
```

**Backend exits before starting**
Check migration errors:
```powershell
docker compose logs backend
```

**`pgvector` extension error**
The `pgvector/pgvector:pg16` image includes the extension automatically. If you see this error, make sure you're using that image and not a plain `postgres` image.

**Slow first response from AI**
Ollama loads the model into RAM on the first request — this can take 20–60 seconds. Subsequent requests within the same session are fast.

**CORS errors in browser**
The Next.js apps proxy `/api/*` to the FastAPI backend via `next.config.mjs` rewrites, so you should not see CORS errors in normal use. If you do, check `NEXT_PUBLIC_API_BASE_URL` in each app's `.env.local`.

**GPU acceleration (NVIDIA)**
Uncomment the `deploy.resources` block in `docker-compose.yml` under the `ollama` service.
Requires [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html).
