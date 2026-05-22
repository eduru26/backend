"""
data_mapper.py — pure pandas transformations driven by Dr.Doc's schema JSON.

Takes a DataFrame (from one CSV sheet) + the schema dict from Dr.Doc.
Returns dicts ready for our existing repositories.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from io import StringIO
from typing import Any

import pandas as pd


# ── Public entry point ────────────────────────────────────

def map_sheet(df: pd.DataFrame, schema: dict[str, Any]) -> dict[str, Any]:
    """
    Returns {students, attendance, assessments} based on Dr.Doc's schema.
    Each list element is a plain dict matching the relevant repository's expected format.
    """
    data_type = schema.get("data_type", "unknown")
    fmt = schema.get("format", "long")

    if data_type == "students":
        return {"students": _students(df, schema), "attendance": [], "assessments": []}

    if data_type == "attendance":
        if fmt == "wide":
            return {"students": [], "attendance": _attendance_wide(df, schema), "assessments": []}
        return {"students": [], "attendance": _attendance_long(df, schema), "assessments": []}

    if data_type == "marks":
        if fmt == "wide_subjects":
            return {"students": [], "attendance": [], "assessments": _marks_wide_subjects(df, schema)}
        if fmt == "wide_questions":
            return {"students": [], "attendance": [], "assessments": [_marks_wide_questions(df, schema)]}
        return {"students": [], "attendance": [], "assessments": [_marks_long(df, schema)]}

    return {"students": [], "attendance": [], "assessments": []}


# ── Students ──────────────────────────────────────────────

def _students(df: pd.DataFrame, schema: dict) -> list[dict]:
    col_map: dict[str, str] = schema.get("column_map", {})
    df = df.rename(columns=col_map)
    records = []
    for _, row in df.iterrows():
        handle = _s(row.get("handle"))
        name   = _s(row.get("full_name"))
        if not handle or not name:
            continue
        records.append({
            "handle":       handle[:64],
            "full_name":    name[:200],
            "grade":        _i(row.get("grade"), 1),
            "section":      _s(row.get("section", "A"))[:8] or "A",
            "parent_email": _nullable(row.get("parent_email")),
        })
    return records


# ── Attendance ────────────────────────────────────────────

def _attendance_long(df: pd.DataFrame, schema: dict) -> list[dict]:
    col_map: dict[str, str] = schema.get("column_map", {})
    df = df.rename(columns=col_map)
    records = []
    for _, row in df.iterrows():
        handle = _s(row.get("student_handle"))
        d      = _date(_s(row.get("date")))
        if not handle or not d:
            continue
        records.append({
            "student_handle": handle,
            "date":           d,
            "status":         _status(_s(row.get("status", "present"))),
        })
    return records


def _attendance_wide(df: pd.DataFrame, schema: dict) -> list[dict]:
    id_col    = schema.get("id_col", "")
    date_cols = schema.get("date_cols", [])
    if not id_col or id_col not in df.columns:
        return []
    available = [c for c in date_cols if c in df.columns]
    if not available:
        return []
    melted = df.melt(id_vars=[id_col], value_vars=available,
                     var_name="date", value_name="status_raw")
    records = []
    for _, row in melted.iterrows():
        handle = _s(row[id_col])
        d      = _date(_s(row["date"]))
        if not handle or not d:
            continue
        records.append({
            "student_handle": handle,
            "date":           d,
            "status":         _status(_s(row.get("status_raw", ""))),
        })
    return records


# ── Marks ─────────────────────────────────────────────────

def _marks_wide_subjects(df: pd.DataFrame, schema: dict) -> list[dict]:
    """One assessment per subject column."""
    id_col       = schema.get("id_col", "")
    subject_cols = schema.get("subject_cols", [])
    meta         = schema.get("assessment", {})
    assessments  = []

    for sc in subject_cols:
        csv_col   = sc.get("csv_col", "")
        subject   = sc.get("subject", csv_col)
        max_marks = float(sc.get("max_marks", 100))
        if not csv_col or csv_col not in df.columns:
            continue
        marks = []
        for _, row in df.iterrows():
            handle = _s(row.get(id_col, ""))
            obtained = min(_f(row.get(csv_col, 0)), max_marks)
            if handle:
                marks.append({"student_handle": handle, "question_no": 1,
                               "marks_obtained": obtained})
        assessments.append({
            "title":     meta.get("title", f"{subject} Assessment"),
            "subject":   subject,
            "type":      meta.get("type", "unit_test"),
            "date":      meta.get("date", str(date.today())),
            "questions": [{"question_no": 1, "topic": subject,
                           "max_marks": max_marks, "difficulty": "medium"}],
            "marks":     marks,
        })
    return assessments


def _marks_wide_questions(df: pd.DataFrame, schema: dict) -> dict:
    """One assessment, multiple question columns."""
    id_col       = schema.get("id_col", "")
    q_cols       = schema.get("question_cols", [])
    meta         = schema.get("assessment", {})

    questions, all_marks = [], []
    for qc in q_cols:
        csv_col   = qc.get("csv_col", "")
        qno       = int(qc.get("question_no", 1))
        topic     = qc.get("topic", "General")
        max_marks = float(qc.get("max_marks", 10))
        if not csv_col or csv_col not in df.columns:
            continue
        questions.append({"question_no": qno, "topic": topic,
                          "max_marks": max_marks, "difficulty": "medium"})
        for _, row in df.iterrows():
            handle = _s(row.get(id_col, ""))
            obtained = min(_f(row.get(csv_col, 0)), max_marks)
            if handle:
                all_marks.append({"student_handle": handle,
                                   "question_no": qno, "marks_obtained": obtained})

    return {
        "title":     meta.get("title", "Assessment"),
        "subject":   meta.get("subject", "General"),
        "type":      meta.get("type", "unit_test"),
        "date":      meta.get("date", str(date.today())),
        "questions": questions,
        "marks":     all_marks,
    }


def _marks_long(df: pd.DataFrame, schema: dict) -> dict:
    col_map = schema.get("column_map", {})
    meta    = schema.get("assessment", {})
    df = df.rename(columns=col_map)

    max_marks = float(meta.get("max_marks", 100))
    question_no = int(meta.get("question_no", 1))
    marks = []
    for _, row in df.iterrows():
        handle   = _s(row.get("student_handle"))
        obtained = min(_f(row.get("marks_obtained", 0)), max_marks)
        if handle:
            marks.append({"student_handle": handle,
                          "question_no": _i(row.get("question_no"), question_no),
                          "marks_obtained": obtained})
    return {
        "title":     meta.get("title", "Assessment"),
        "subject":   meta.get("subject", "General"),
        "type":      meta.get("type", "unit_test"),
        "date":      meta.get("date", str(date.today())),
        "questions": [{"question_no": question_no,
                       "topic":       meta.get("topic", "General"),
                       "max_marks":   max_marks,
                       "difficulty":  "medium"}],
        "marks":     marks,
    }


# ── Type coercions ────────────────────────────────────────

def _s(v: Any) -> str:
    s = str(v).strip() if v is not None else ""
    return "" if s.lower() in ("nan", "none", "nat") else s

def _nullable(v: Any) -> str | None:
    s = _s(v)
    return s if s else None

def _i(v: Any, default: int = 0) -> int:
    try:
        return int(str(v).strip().split(".")[0])
    except (ValueError, TypeError):
        return default

def _f(v: Any) -> float:
    try:
        return float(str(v).strip())
    except (ValueError, TypeError):
        return 0.0

_DATE_FMTS = ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y",
              "%Y/%m/%d", "%d %b %Y", "%d %B %Y")

def _date(s: str) -> date | None:
    for fmt in _DATE_FMTS:
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except ValueError:
            pass
    return None

_STATUS_MAP = {
    "p": "present", "present": "present", "yes": "present", "1": "present",
    "a": "absent",  "absent":  "absent",  "no":  "absent",  "0": "absent",
    "l": "late",    "late":    "late",
}

def _status(v: str) -> str:
    return _STATUS_MAP.get(v.strip().lower(), "present")
