"""Render report rows as an aligned text table, CSV or JSON."""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Any


class OutputFormat(StrEnum):
    TABLE = "table"
    CSV = "csv"
    JSON = "json"


MAX_CELL_WIDTH = 64


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def _truncate(value: str, width: int) -> str:
    return value if len(value) <= width else value[: width - 3] + "..."


def render_table(rows: Sequence[Mapping[str, Any]], columns: Sequence[str]) -> str:
    if not rows:
        return "No findings."
    cells = [[_truncate(_cell(row.get(c)), MAX_CELL_WIDTH) for c in columns] for row in rows]
    headers = [c.upper() for c in columns]
    widths = [max(len(h), *(len(r[i]) for r in cells)) for i, h in enumerate(headers)]
    lines = ["  ".join(h.ljust(w) for h, w in zip(headers, widths, strict=True)).rstrip()]
    lines.append("  ".join("-" * w for w in widths))
    lines.extend(
        "  ".join(v.ljust(w) for v, w in zip(row, widths, strict=True)).rstrip() for row in cells
    )
    return "\n".join(lines)


def render_csv(rows: Sequence[Mapping[str, Any]], columns: Sequence[str]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(columns), extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({c: _cell(row.get(c)) for c in columns})
    return buffer.getvalue()


def render_json(rows: Sequence[Mapping[str, Any]], summary: Mapping[str, Any] | None = None) -> str:
    payload: Any = list(rows) if summary is None else {"summary": summary, "items": list(rows)}
    return json.dumps(payload, indent=2, default=str)


def render(
    rows: Sequence[Mapping[str, Any]],
    *,
    fmt: OutputFormat,
    table_columns: Sequence[str],
    summary: Mapping[str, Any] | None = None,
) -> str:
    """Render ``rows``.

    The table view shows ``table_columns`` only, to stay readable in a
    terminal. CSV and JSON always carry every column (including full resource
    IDs) because they feed spreadsheets, tickets and automation.
    """
    all_columns = list(rows[0].keys()) if rows else list(table_columns)
    if fmt is OutputFormat.TABLE:
        return render_table(rows, table_columns)
    if fmt is OutputFormat.CSV:
        return render_csv(rows, all_columns)
    return render_json(rows, summary)
