"""
excel_processor.py — orchestrates the three-stage pipeline:

  1. Converter service  → any file format → CSV(s)
  2. Dr.Doc LLM         → CSV headers + sample → schema JSON
  3. data_mapper        → pandas transforms → DB-ready dicts
"""
from __future__ import annotations

import csv
from io import StringIO
from typing import Any

import httpx

from backend.core.config import get_settings
from backend.llm import dr_doc, data_mapper


async def process_file(filename: str, data: bytes) -> dict[str, Any]:
    settings = get_settings()

    # ── Stage 1: Convert any format to CSV(s) ────────────
    sheets = await _convert(filename, data, settings.converter_url)

    result: dict[str, Any] = {
        "summary": "",
        "students": [],
        "attendance": [],
        "assessments": [],
    }

    for sheet in sheets:
        if sheet["rows"] == 0:
            continue

        csv_text: str = sheet["csv"]
        columns: list[str] = sheet["columns"]
        sample_rows = _sample_rows(csv_text, n=5)

        # ── Stage 2: Dr.Doc identifies schema ────────────
        try:
            schema = await dr_doc.analyse(
                sheet_name=sheet["sheet"],
                columns=columns,
                sample_rows=sample_rows,
            )
        except Exception as exc:
            # If Dr.Doc fails for one sheet, skip it and continue
            result.setdefault("_warnings", []).append(
                f"Sheet '{sheet['sheet']}': Dr.Doc failed — {exc}"
            )
            continue

        # ── Stage 3: Pandas maps CSV → DB dicts ──────────
        import pandas as pd  # lazy import — only in upload flow
        df = pd.read_csv(StringIO(csv_text), dtype=str)
        mapped = data_mapper.map_sheet(df, schema)

        result["students"].extend(mapped.get("students", []))
        result["attendance"].extend(mapped.get("attendance", []))
        result["assessments"].extend(mapped.get("assessments", []))

    result["summary"] = _build_summary(result, len(sheets))
    return result


# A wedged converter shouldn't hold an upload request open for the full
# 30s timeout once it's clearly down. ValueError is a bad-file (4xx)
# business error — ignored so a run of bad uploads can't trip the breaker.
from backend.core.circuit_breaker import CircuitBreaker  # noqa: E402

_converter_breaker = CircuitBreaker("converter", ignore=(ValueError,))


async def _convert(filename: str, data: bytes, converter_url: str) -> list[dict[str, Any]]:
    return await _converter_breaker.call(_convert_impl, filename, data, converter_url)


async def _convert_impl(
    filename: str, data: bytes, converter_url: str
) -> list[dict[str, Any]]:
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{converter_url}/convert",
            files={"file": (filename, data)},
        )
    if not resp.is_success:
        detail = resp.json().get("detail", resp.text)
        raise ValueError(f"Converter: {detail}")
    payload = resp.json()
    return payload["sheets"]


def _sample_rows(csv_text: str, n: int) -> list[list[str]]:
    reader = csv.reader(StringIO(csv_text))
    rows = []
    for i, row in enumerate(reader):
        if i == 0:
            continue  # skip header
        rows.append(row)
        if len(rows) >= n:
            break
    return rows


def _build_summary(result: dict[str, Any], sheet_count: int) -> str:
    parts = []
    if result["students"]:
        parts.append(f"{len(result['students'])} students")
    if result["attendance"]:
        parts.append(f"{len(result['attendance'])} attendance records")
    if result["assessments"]:
        n_marks = sum(len(a.get("marks", [])) for a in result["assessments"])
        parts.append(f"{len(result['assessments'])} assessment(s), {n_marks} mark records")
    if not parts:
        return f"Processed {sheet_count} sheet(s) — no recognised school data"
    return ", ".join(parts) + f" across {sheet_count} sheet(s)"
