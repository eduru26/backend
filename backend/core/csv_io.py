from __future__ import annotations

import csv
import hashlib
import io
from collections.abc import Iterator
from typing import TypeVar

from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)


def sha256_hex(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def read_csv_rows(payload: bytes) -> Iterator[dict[str, str]]:
    """Decode UTF-8 (BOM-tolerant) and yield row dicts via DictReader."""
    text = payload.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    yield from reader


def parse_csv_rows(payload: bytes, model: type[T]) -> list[T]:
    """Parse CSV bytes into a list of validated Pydantic models. Raises
    `CSVValidationError` on malformed rows with row-level context."""
    rows: list[T] = []
    for line_no, raw in enumerate(read_csv_rows(payload), start=2):  # 1 = header
        cleaned = {k: (v.strip() if isinstance(v, str) else v) for k, v in raw.items() if k}
        try:
            rows.append(model.model_validate(cleaned))
        except ValidationError as exc:
            raise CSVValidationError(line_no=line_no, errors=exc.errors()) from exc
    return rows


class CSVValidationError(Exception):
    def __init__(self, *, line_no: int, errors: list[dict]) -> None:
        self.line_no = line_no
        self.errors = errors
        super().__init__(f"CSV validation failed at line {line_no}: {errors}")
