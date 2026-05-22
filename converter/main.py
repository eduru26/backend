"""
Converter microservice — converts any spreadsheet format to CSV.
Every sheet becomes a separate CSV payload. No LLM involved; pure format conversion.
"""
from __future__ import annotations

from io import BytesIO

import pandas as pd
from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="Eduru — File Converter", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# pandas engine per extension
_ENGINES: dict[str, str] = {
    "xlsx": "openpyxl",
    "xlsm": "openpyxl",
    "xlsb": "openpyxl",
    "xls":  "xlrd",
    "ods":  "odf",
}


class SheetOut(BaseModel):
    sheet: str
    csv: str
    rows: int
    columns: list[str]


class ConvertOut(BaseModel):
    filename: str
    sheets: list[SheetOut]


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/convert", response_model=ConvertOut)
async def convert(file: UploadFile) -> ConvertOut:
    filename = file.filename or "upload"
    data = await file.read()
    if not data:
        raise HTTPException(400, "Empty file")

    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

    try:
        frames = _to_dataframes(BytesIO(data), ext, filename)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    except Exception as exc:
        raise HTTPException(500, f"Conversion failed: {exc}")

    sheets: list[SheetOut] = []
    for name, df in frames:
        # Normalise column names and drop fully-empty rows
        df.columns = [str(c).strip() for c in df.columns]
        df = df.dropna(how="all").reset_index(drop=True)
        sheets.append(SheetOut(
            sheet=name,
            csv=df.to_csv(index=False),
            rows=len(df),
            columns=list(df.columns),
        ))

    return ConvertOut(filename=filename, sheets=sheets)


def _to_dataframes(buf: BytesIO, ext: str, filename: str) -> list[tuple[str, pd.DataFrame]]:
    if ext in ("csv", "tsv", "txt"):
        sep = "\t" if ext in ("tsv", "txt") else ","
        try:
            df = pd.read_csv(buf, sep=sep, dtype=str, encoding_errors="replace")
        except Exception:
            buf.seek(0)
            df = pd.read_csv(buf, sep=None, engine="python", dtype=str, encoding_errors="replace")
        return [(filename, df)]

    engine = _ENGINES.get(ext)
    if not engine:
        raise ValueError(
            f"Unsupported format '.{ext}'. "
            "Supported: .xlsx, .xlsm, .xlsb, .xls, .ods, .csv, .tsv"
        )

    try:
        xl = pd.ExcelFile(buf, engine=engine)
    except Exception as exc:
        raise ValueError(f"Cannot open file: {exc}") from exc

    return [(name, xl.parse(name, dtype=str)) for name in xl.sheet_names]
