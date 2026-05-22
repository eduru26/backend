# Teacher upload templates

Four ingestion endpoints, all teacher-only, all idempotent by SHA-256 of the payload.

## 1. `POST /ingest/students` — `students.csv`

| column | type | rules |
|---|---|---|
| `handle` | string | 1–64 chars, unique within tenant (school) |
| `full_name` | string | 1–200 chars |
| `grade` | int | 1–12 |
| `section` | string | 1–8 chars |
| `parent_email` | email \| empty | optional |

Re-uploading the same handle updates the row (upsert by `(tenant_id, handle)`).

## 2. `POST /ingest/attendance` — `attendance.csv`

| column | type | rules |
|---|---|---|
| `date` | YYYY-MM-DD | |
| `student_handle` | string | must already exist for this tenant |
| `status` | enum | `present` \| `absent` \| `late` |

Upserts by `(student_id, date)`.

## 3. `POST /ingest/question-bank` — `question-bank.json` (JSON, not CSV)

This endpoint takes a JSON body, not a file upload, because one assessment maps to many questions.

```json
{
  "assessment": {
    "type": "homework | unit_test | exam",
    "title": "...",
    "subject": "Mathematics",          // subject is upserted by name
    "assessment_date": "YYYY-MM-DD",
    "max_marks": 20.00
  },
  "questions": [
    { "question_no": 1, "topic": "Linear Equations", "max_marks": 5.00, "difficulty": "easy | medium | hard" }
  ]
}
```

Rules:
- `question_no` must be unique within the assessment (1–10000).
- `topic` is upserted under the assessment's subject.
- Response returns the new `assessment_id` — **save it**, you'll need it for the marks CSV.

## 4. `POST /ingest/marks` — `marks.csv`

| column | type | rules |
|---|---|---|
| `assessment_id` | UUID | must be the same on every row in one upload |
| `student_handle` | string | must exist for this tenant |
| `question_no` | int | must exist in the assessment |
| `marks_obtained` | decimal(8,2) | `0 ≤ marks_obtained ≤ question.max_marks` |

Upserts by `(question_id, student_id)`. `is_correct` is auto-derived (`marks_obtained == question.max_marks`).

## Recommended teacher workflow

1. Upload `students.csv` once per term (or whenever roster changes).
2. Upload `attendance.csv` daily/weekly.
3. Before grading: `POST /ingest/question-bank` with the JSON, capture `assessment_id`.
4. After grading: upload `marks.csv` referencing that `assessment_id`.

## Validation behaviour

- CSV parse errors return HTTP 422 with `{line, errors}` pointing at the offending row.
- Identical payloads (same SHA-256) are short-circuited and return `duplicate: true` — safe to retry.
- Unknown `student_handle`, unknown `question_no`, or `marks_obtained > max_marks` all return 422.
