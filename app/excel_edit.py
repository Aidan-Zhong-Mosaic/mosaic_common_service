"""Adds one row to an .xlsx held in memory (the "download" Excel mode).

Used when Graph's Excel API is not available to the app (it often refuses apps that
only hold `Sites.Selected`). The caller downloads the workbook, this module adds the
row with openpyxl, and the caller uploads the result with an ETag precondition.

What re-saving with openpyxl does and does not keep:
  * Kept: cell values, formulas, number formats and styles, column widths, merged
    cells, defined names, tables and standard data validation / conditional formatting.
  * Not kept: charts, pivot tables, images and shapes, form controls, macros, and
    some newer extensions. Anything openpyxl reports as dropped is returned as a note.
  * Not extended: data validation and conditional-formatting ranges and calculated
    columns are not stretched to cover the new row (Excel does this on its own; this
    code does not). Calculated columns are reported as a note.

Text stays text: "2026-10-13" is written as the string "2026-10-13", not as a date.
"""
import io
import warnings
from collections.abc import Callable
from copy import copy
from typing import Any

import openpyxl
from openpyxl.utils import get_column_letter
from openpyxl.utils.cell import range_boundaries

# A sheet with more cells than this is refused rather than scanned.
MAX_SCAN_CELLS = 5_000_000


class ExcelEditError(Exception):
    """kind is one of: bad_request, not_found, conflict, not_excel."""

    def __init__(self, message: str, kind: str) -> None:
        super().__init__(message)
        self.kind = kind


def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and value.strip() == "")


def append_row(
    data: bytes,
    *,
    table: str | None,
    sheet: str | None,
    values: list[Any] | None,
    row: dict[str, Any] | None,
    prepare: Callable[[list[Any]], list[Any]],
) -> tuple[bytes, dict[str, Any], list[str]]:
    """Returns (new workbook bytes, result, notes). Raises ExcelEditError before changing anything."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        try:
            workbook = openpyxl.load_workbook(io.BytesIO(data))
        except Exception as exc:  # a zip that is not a workbook, a corrupt file, ...
            raise ExcelEditError(f"The file could not be opened as an Excel workbook ({exc}).", "not_excel") from exc
    notes = sorted({str(w.message) for w in caught})

    if table:
        result, extra = _append_to_table(workbook, table, values, row, prepare)
        notes += extra
    else:
        result = _append_below_data(workbook, sheet, values or [], prepare)

    out = io.BytesIO()
    workbook.save(out)
    return out.getvalue(), result, notes


def _find_table(workbook: openpyxl.Workbook, name: str):
    wanted = name.lower()
    for ws in workbook.worksheets:
        for tbl in ws.tables.values():
            if tbl.displayName.lower() == wanted or tbl.name.lower() == wanted:
                return ws, tbl
    return None, None


def _append_to_table(workbook, name, values, row, prepare):
    ws, tbl = _find_table(workbook, name)
    if tbl is None:
        raise ExcelEditError(f"Table '{name}' was not found in the workbook.", "not_found")
    if tbl.headerRowCount == 0:
        raise ExcelEditError(f"Table '{name}' has no header row, which this mode does not support.", "bad_request")
    if tbl.totalsRowCount:
        raise ExcelEditError(
            f"Table '{name}' has a totals row. Excel would insert above it; this mode cannot. "
            "Remove the totals row or use the Excel API mode.",
            "bad_request",
        )

    min_col, min_row, max_col, max_row = range_boundaries(tbl.ref)
    columns = [str(c.name) for c in tbl.tableColumns]

    if row is not None:
        unknown = [key for key in row if key not in columns]
        if unknown:
            raise ExcelEditError(f"Table '{name}' has no column {unknown}. Its columns are {columns}.", "bad_request")
        ordered = [row.get(column) for column in columns]
    else:
        if len(values or []) != len(columns):
            raise ExcelEditError(
                f"Table '{name}' has {len(columns)} columns {columns} but {len(values or [])} values were sent.",
                "bad_request",
            )
        ordered = list(values or [])
    cells = prepare(ordered)

    # Data rows are min_row+1..max_row. Fill the first blank row after the last row
    # that has data (a table often ends with an empty row); otherwise grow the table.
    last_with_data = min_row
    for r in range(max_row, min_row, -1):
        if any(not _blank(ws.cell(row=r, column=c).value) for c in range(min_col, max_col + 1)):
            last_with_data = r
            break
    target = last_with_data + 1
    grows = target > max_row

    if grows:
        for c in range(min_col, max_col + 1):
            if not _blank(ws.cell(row=target, column=c).value):
                raise ExcelEditError(
                    f"The row under table '{name}' (row {target}) already has data in {ws.cell(row=target, column=c).coordinate}. "
                    "Excel would push it down; this mode cannot. Move it, or use the Excel API mode.",
                    "conflict",
                )

    for offset, value in enumerate(cells):
        cell = ws.cell(row=target, column=min_col + offset)
        cell.value = value
        if grows and max_row > min_row:
            cell._style = copy(ws.cell(row=max_row, column=min_col + offset)._style)

    if grows:
        tbl.ref = f"{get_column_letter(min_col)}{min_row}:{get_column_letter(max_col)}{target}"
        if tbl.autoFilter is not None:
            tbl.autoFilter.ref = tbl.ref

    notes = []
    calculated = [str(c.name) for c in tbl.tableColumns if c.calculatedColumnFormula is not None]
    if calculated:
        notes.append(f"Calculated columns were not filled in for the new row: {calculated}.")

    result = {"mode": "table", "table": name, "sheet": None, "row_number": target - min_row, "address": None}
    return result, notes


def _append_below_data(workbook, sheet, values, prepare):
    if sheet is None:
        ws = workbook.worksheets[0] if workbook.worksheets else None
        if ws is None:
            raise ExcelEditError("The workbook has no worksheets.", "not_found")
    else:
        by_title = {w.title.lower(): w for w in workbook.worksheets}
        ws = by_title.get(sheet.lower())
        if ws is None:
            raise ExcelEditError(f"Worksheet '{sheet}' was not found in the workbook.", "not_found")

    cells = prepare(values)
    if not cells:
        raise ExcelEditError("'values' is empty.", "bad_request")
    if (ws.max_row or 0) * (ws.max_column or 0) > MAX_SCAN_CELLS:
        raise ExcelEditError(f"Worksheet '{ws.title}' is too large to scan for its last row.", "bad_request")

    last_row, first_col = 0, None
    for cells_in_row in ws.iter_rows():
        for cell in cells_in_row:
            if not _blank(cell.value):
                last_row = max(last_row, cell.row)
                first_col = cell.column if first_col is None else min(first_col, cell.column)

    row_number, start_col = (1, 1) if last_row == 0 else (last_row + 1, first_col)
    for offset, value in enumerate(cells):
        target = ws.cell(row=row_number, column=start_col + offset)
        target.value = value
        if last_row:
            target._style = copy(ws.cell(row=last_row, column=start_col + offset)._style)

    address = (
        f"{get_column_letter(start_col)}{row_number}:{get_column_letter(start_col + len(cells) - 1)}{row_number}"
    )
    return {"mode": "sheet", "table": None, "sheet": ws.title, "row_number": row_number, "address": address}
