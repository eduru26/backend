"""
Dr.Doc — a dedicated Ollama model for CSV schema identification.

Receives: sheet name + CSV headers + first 5 data rows
Returns: a structured JSON schema mapping describing how CSV columns
         relate to our DB fields (students / attendance / marks).
"""
from __future__ import annotations

import json
import re
from typing import Any

import httpx

from backend.core.config import get_settings

_SYSTEM = """\
You are Dr.Doc, a school-data schema analyst.
Given a CSV sheet name, its column headers, and 5 sample rows, you must:
1. Decide what type of school data the sheet contains.
2. Map every relevant CSV column to the matching database field.

Return ONLY a single JSON object — no markdown, no explanation.

──────────────────────────────────────────────────
SCHEMA REFERENCE
──────────────────────────────────────────────────
Students table fields : handle, full_name, grade, section, parent_email
Attendance table fields: student_handle, date (YYYY-MM-DD), status (present|absent|late)
Marks fields           : student_handle, marks_obtained

──────────────────────────────────────────────────
OUTPUT FORMAT (pick the matching template)
──────────────────────────────────────────────────

Student roster (long — one row per student):
{
  "data_type": "students",
  "format": "long",
  "column_map": { "CSV_Col": "db_field", ... }
}

Attendance — long (one row per student per date):
{
  "data_type": "attendance",
  "format": "long",
  "column_map": { "CSV_Col": "student_handle|date|status", ... }
}

Attendance — wide (student rows, one column per date, cell = P/A/L):
{
  "data_type": "attendance",
  "format": "wide",
  "id_col": "column_that_holds_student_id",
  "date_cols": ["2026-01-01", "2026-01-02", ...]
}

Marks — wide by subject (student rows, one column per subject, cell = total score):
{
  "data_type": "marks",
  "format": "wide_subjects",
  "id_col": "column_that_holds_student_id",
  "subject_cols": [
    {"csv_col": "Math", "subject": "Mathematics", "max_marks": 100}
  ],
  "assessment": {"title": "...", "type": "unit_test", "date": "YYYY-MM-DD"}
}

Marks — wide by question (student rows, one column per question, cell = score):
{
  "data_type": "marks",
  "format": "wide_questions",
  "id_col": "column_that_holds_student_id",
  "question_cols": [
    {"csv_col": "Q1", "question_no": 1, "topic": "General", "max_marks": 10}
  ],
  "assessment": {"title": "...", "subject": "...", "type": "unit_test", "date": "YYYY-MM-DD"}
}

Marks — long (one row per student per question):
{
  "data_type": "marks",
  "format": "long",
  "column_map": { "CSV_Col": "student_handle|marks_obtained|question_no" },
  "assessment": {"title": "...", "subject": "...", "type": "unit_test", "date": "YYYY-MM-DD",
                 "question_no": 1, "max_marks": 100, "topic": "General"}
}

──────────────────────────────────────────────────
RULES
──────────────────────────────────────────────────
• status: map P/Yes/1/Present → "present", A/No/0/Absent → "absent", L/Late → "late"
• date format output: always YYYY-MM-DD; infer year from context if only day/month shown
• grade: integer
• If cell contains a number and the column name looks like a date → wide attendance
• If cell contains a number and column name looks like a subject/score → wide_subjects
• Return ONLY the JSON object
"""


async def analyse(
    *,
    sheet_name: str,
    columns: list[str],
    sample_rows: list[list[str]],
) -> dict[str, Any]:
    """Ask Dr.Doc to identify the schema of a CSV sheet."""
    settings = get_settings()

    header = " | ".join(columns)
    sample = "\n".join(" | ".join(row) for row in sample_rows[:5])
    user_prompt = (
        f"Sheet name: {sheet_name}\n"
        f"Columns: {header}\n"
        f"Sample rows:\n{sample}"
    )

    async with httpx.AsyncClient(timeout=90.0) as client:
        resp = await client.post(
            f"{settings.ollama_base_url}/api/chat",
            json={
                "model": settings.dr_doc_model,
                "messages": [
                    {"role": "system", "content": _SYSTEM},
                    {"role": "user", "content": user_prompt},
                ],
                "stream": False,
                "options": {"temperature": 0.0},
            },
        )
        resp.raise_for_status()
        raw = resp.json()["message"]["content"]

    return _parse(raw)


def _parse(raw: str) -> dict[str, Any]:
    raw = raw.strip()
    for candidate in [raw, re.sub(r"```(?:json)?|```", "", raw).strip()]:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if m:
        try:
            return json.loads(m.group())
        except json.JSONDecodeError:
            pass
    raise ValueError(f"Dr.Doc returned non-JSON: {raw[:200]}")
