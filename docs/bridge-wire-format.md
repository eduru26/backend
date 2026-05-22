# Frappe ↔ FastAPI bridge — wire format

> Connects the Frappe app at [`/ai_mentor/`](../ai_mentor/) to the FastAPI service at [`/backend/`](../backend/).
> Two channels: **inbound** (Frappe → backend) and **outbound** (backend → Frappe). Both are HTTP + JSON, both use static bearer tokens, both run **server-to-server with no end-user context**.

---

## 1. Secrets

| Name (Tenant Profile field) | Used for | Mirrored on backend as |
|---|---|---|
| `ai_backend_secret` | Frappe → backend (inbound from backend's POV) | `LMS_INBOUND_SECRET` |
| `ai_webhook_secret` | Backend → Frappe (outbound from backend's POV) | `LMS_OUTBOUND_SECRET` |

Generate two unrelated random tokens. Rotate them by editing `Tenant Profile` and the backend's `.env`; both ends read the live value on every request.

Comparison is constant-time on both sides (`hmac.compare_digest`).

---

## 2. Inbound: Frappe → backend

```
POST {ai_backend_url}/lms/events/<event_slug>
Authorization: Bearer <ai_backend_secret>
X-Tenant-Code:  <Tenant Profile.tenant_code>
Content-Type:   application/json

{
  "event":   "<event_slug>",
  "site":    "<frappe-site-name>",
  "ts":      "<iso-8601-utc>",
  "payload": { ... event-specific ... }
}
```

The backend validates: bearer matches → tenant code matches the configured one (defence against DNS misroute) → URL slug matches the body's `event` field → the slug is a known event → payload parses against the per-event Pydantic schema.

On success: returns **`202 Accepted`** with `{accepted, event, tenant}`. The handler runs as a FastAPI `BackgroundTask` — the inbound HTTP request returns immediately so it never blocks the user-facing Frappe transaction.

### Event catalogue

| Slug | Source (Frappe doc-event) | Payload fields | Handler |
|---|---|---|---|
| `quiz_submitted` | `LMS Quiz Submission.after_insert` | `submission_id, quiz, course, member, score, score_out_of, percentage, passing_percentage, passed, results[]` | post-quiz feedback insight |
| `assignment_graded` | `LMS Assignment Submission.on_update` (Pass/Fail transition) | `submission_id, assignment, course, member, status, evaluator, comments, answer, type` | grade-assist insight |
| `lesson_completed` | `LMS Course Progress.after_insert` (status=Complete) | `progress_id, course, chapter, lesson, member, completed_at` | mastery refresh |
| `enrolled` | `LMS Enrollment.after_insert` | `enrollment_id, course, member, from_batch` | baseline study-plan insight |
| `post_quiz_request` | SPA call to `ai_mentor.mentor.api.get_post_quiz_analysis` | `submission_id` | re-trigger post-quiz pipeline |
| `lesson_chat_message` | SPA call to `ai_mentor.mentor.api.post_user_message` | `conversation, message, lesson, course, content` | append assistant reply |
| `daily_risk_refresh` | scheduler (`hooks.py:scheduler_events.daily`) | `as_of` | refresh `AI Risk Score` for active enrolments |
| `weekly_summaries` | scheduler (`hooks.py:scheduler_events.weekly_long`) | `as_of` | weekly digest insights per student |

`results[]` rows in `quiz_submitted`:

```json
{ "question": "<LMS Question name>", "is_correct": 0|1, "marks": <num>, "marks_out_of": <num>, "answer": "<text>" }
```

Payload schemas accept extra fields (`extra="allow"` on the Pydantic models) so a future Frappe-side field doesn't break older backends.

### Failure modes

| Status | Cause | Frappe response |
|---|---|---|
| 503 | Backend started without `LMS_INBOUND_SECRET` configured. | Bridge logs the failure and stops; Frappe's `frappe.enqueue` retries. |
| 401 | Wrong / missing bearer. | Same. |
| 400 | URL slug ≠ body `event`, or tenant code mismatch. | Same. |
| 404 | Unknown event slug. | Frappe's `frappe.log_error` records — investigate version mismatch between apps. |
| 422 | Payload doesn't validate against the event's schema. | Same — usually a Frappe-side change; bump backend Pydantic models. |

---

## 3. Outbound: backend → Frappe

After processing an event, the backend writes results back via Frappe's whitelisted endpoints under `ai_mentor.mentor.webhook.*`. All four endpoints take `Authorization: Bearer <ai_webhook_secret>`.

### `ingest_insights`

```
POST {lms_base_url}/api/method/ai_mentor.mentor.webhook.ingest_insights
{
  "insights": [
    {
      "member": "<user>",
      "kind":   "post_quiz_feedback" | "weak_topics" | "risk_score" | "study_plan" | "weekly_summary" | "grade_assist" | "lesson_chat",
      "title":  "<short headline>",
      "summary":"<≤500 chars>",
      "payload": { ... arbitrary structured data ... },
      "context_doctype": "LMS Quiz Submission",
      "context_name":    "<doc name>",
      "course": "<LMS Course>",
      "lesson": "<Course Lesson>",
      "model":  "<model id>",
      "version":"<pipeline version>"
    }
  ]
}
```

Persists one `Eduru Insight` row per element. Frappe responds `{ok: true, inserted: [<names>]}`.

### `ingest_question_tags`

```json
{
  "tags": [
    {
      "question":   "<LMS Question name>",
      "topic":      "<string>",
      "subtopic":   "<string?>",
      "skill":      "<Skills doc?>",
      "difficulty": 0.42,
      "confidence": 0.88,
      "source":     "ai" | "teacher" | "import"
    }
  ]
}
```

**Upserts** — keyed by `(question, topic)`. Frappe responds `{ok: true, upserted: [...]}`.

### `ingest_risk_scores`

```json
{
  "rows": [
    {
      "member": "<user>",
      "course": "<LMS Course?>",
      "score":  0.0–1.0,
      "level":  "low" | "medium" | "high",
      "model":  "<id>",
      "attendance_pct": 0.0–1.0,
      "avg_quiz_pct":   0.0–1.0,
      "lessons_completed_pct": 0.0–1.0,
      "days_since_last_activity": <int>,
      "video_skim_ratio": 0.0–1.0,
      "factors_json": { ... }
    }
  ]
}
```

Each row is appended (history preserved). Newest-first by `scored_at`.

### `append_message`

For lesson-chat replies. Single message per call:

```json
{
  "conversation": "<AI Conversation name>",
  "role":         "assistant" | "system" | "tool",
  "content":      "<text>",
  "model":        "<id>",
  "tokens_in":    <int>,
  "tokens_out":   <int>,
  "latency_ms":   <int>,
  "citations":    [ { "lesson": "...", "snippet": "..." } ]
}
```

The Frappe doctype controller updates the parent `AI Conversation.last_message_at` and `message_count`.

### Failure modes

The backend retries each callback up to `LMS_CALLBACK_MAX_RETRIES` (default 3) with exponential backoff capped at 10s. If the bridge URL or secret is empty, calls become silent no-ops (returns `{ok: false, skipped: true}`) — useful for local dev where the FastAPI service runs without a Frappe site behind it.

---

## 4. Backend env

```env
# .env (per-tenant deployment)
LMS_BASE_URL=https://learn.acme.com
LMS_TENANT_CODE=acme
LMS_INBOUND_SECRET=<random>   # = Tenant Profile.ai_backend_secret
LMS_OUTBOUND_SECRET=<random>  # = Tenant Profile.ai_webhook_secret
LMS_CALLBACK_TIMEOUT_SEC=8.0
LMS_CALLBACK_MAX_RETRIES=3
```

> **Multi-tenant note**: this design is **single-tenant per backend process**. To serve multiple Frappe sites from one backend instance, replace the `Settings`-driven config with a `Tenant Profile` table inside the backend's Postgres and resolve per-request from `X-Tenant-Code`. Out of scope for v1.

---

## 5. Local end-to-end test

With both services running:

```bash
# 1) Provision the Frappe site (see provisioning.md)
bench --site learn.acme.local execute ai_mentor.whitelabel.provisioning.provision \
  --kwargs '{"tenant_code":"acme","plan":"Pro","ai_backend_url":"http://localhost:8000"}'

# 2) Set both secrets to the same value on Tenant Profile (desk UI), copy them to backend/.env
# 3) Start backend
uvicorn backend.main:app --reload

# 4) Take any quiz in the LMS as a logged-in student → submit
# 5) Backend logs: lms.event.received event=quiz_submitted ...
# 6) Within ~1s: a row appears in `Eduru Insight` for that member with kind=post_quiz_feedback.
# 7) The student dashboard /lms (with the side-bundle loaded) calls
#    ai_mentor.mentor.api.get_post_quiz_analysis(submission_id)
#    and renders the title + summary.
```

If step 6 doesn't happen, check in this order:

1. Does the backend get the inbound POST at all? (`docker logs ai-mentor` or uvicorn console)
2. If yes, is auth failing? (401 → secret mismatch)
3. If 202 returned, is the callback failing? (`lms.callback.retry` / `lms.callback.failed` log lines)
4. If callbacks succeed, does Frappe's webhook endpoint accept them? (check `Error Log` in desk for auth failures.)
