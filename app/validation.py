"""Domain validation for inspection payloads.

Every rule that rejects a whole diagram lives here so the API answers with a
structured, complete list of problems instead of a bare 4xx. Validation runs
in two stages: structural checks (shape and size) must pass before content
checks (symbols, power source, exit ids), because content positions are only
meaningful once both matrices are known to be aligned.
"""
from __future__ import annotations

from app.models import Coordinate, Issue

MAX_DIMENSION = 200
CELL_SYMBOLS = ("P", "W", "E", "X")
SAMPLE_LIMIT = 20


class InputRejected(Exception):
    """Raised when a payload violates the diagram contract."""

    def __init__(self, issues: list[Issue]) -> None:
        super().__init__("; ".join(issue.message for issue in issues))
        self.issues = issues


def _coords(cells: list[tuple[int, int]]) -> list[Coordinate]:
    return [Coordinate(row=r, col=c) for r, c in cells[:SAMPLE_LIMIT]]


def _suffix(total: int) -> str:
    extra = total - SAMPLE_LIMIT
    return f" (showing first {SAMPLE_LIMIT} of {total})" if extra > 0 else ""


def _row_listing(rows: list[int]) -> str:
    shown = ", ".join(str(r) for r in rows[:SAMPLE_LIMIT])
    return shown + (f", ... (+{len(rows) - SAMPLE_LIMIT} more)" if len(rows) > SAMPLE_LIMIT else "")


def validate_payload(grid: list[list[str]], labels: list[list[str | None]]) -> None:
    """Validate the diagram contract.

    Raises InputRejected carrying every problem found. Returns None when the
    payload is a well-formed diagram.
    """
    structural: list[Issue] = []

    row_count = len(grid)
    if row_count == 0:
        raise InputRejected([
            Issue(code="GRID_EMPTY", message="Grid must contain between 1 and 200 rows, found 0.")
        ])
    if row_count > MAX_DIMENSION:
        structural.append(Issue(
            code="GRID_TOO_TALL",
            message=f"Grid has {row_count} rows; the maximum is {MAX_DIMENSION}.",
        ))

    expected_width = len(grid[0])
    empty_rows = [r for r, row in enumerate(grid) if len(row) == 0]
    if empty_rows:
        structural.append(Issue(
            code="ROW_EMPTY",
            message=f"Rows must contain between 1 and {MAX_DIMENSION} cells; "
                    f"empty rows at indexes: {_row_listing(empty_rows)}.",
        ))
    wide_rows = [(r, len(row)) for r, row in enumerate(grid) if len(row) > MAX_DIMENSION]
    if wide_rows:
        listing = ", ".join(f"row {r} has {w}" for r, w in wide_rows[:SAMPLE_LIMIT])
        structural.append(Issue(
            code="ROW_TOO_WIDE",
            message=f"Rows exceed the maximum width of {MAX_DIMENSION}: {listing}"
                    f"{_suffix(len(wide_rows))}.",
        ))
    ragged = [(r, len(row)) for r, row in enumerate(grid) if len(row) != expected_width]
    if ragged:
        listing = ", ".join(f"row {r} has {w}" for r, w in ragged[:SAMPLE_LIMIT])
        structural.append(Issue(
            code="GRID_RAGGED",
            message=f"All rows must share the same width; row 0 has {expected_width} "
                    f"cells but {listing}{_suffix(len(ragged))}.",
        ))

    if len(labels) != row_count:
        structural.append(Issue(
            code="LABELS_ROW_COUNT_MISMATCH",
            message=f"Labels matrix has {len(labels)} rows but the grid has {row_count}.",
        ))
    else:
        misaligned = [
            (r, len(labels[r]), len(grid[r]))
            for r in range(row_count)
            if len(labels[r]) != len(grid[r])
        ]
        if misaligned:
            listing = ", ".join(
                f"row {r} has {label_width} label(s) for {cell_width} cell(s)"
                for r, label_width, cell_width in misaligned[:SAMPLE_LIMIT]
            )
            structural.append(Issue(
                code="LABELS_ROW_LENGTH_MISMATCH",
                message=f"Labels rows must match their grid rows; {listing}"
                        f"{_suffix(len(misaligned))}.",
            ))

    if structural:
        raise InputRejected(structural)

    issues: list[Issue] = []

    power_cells: list[tuple[int, int]] = []
    unknown: list[tuple[int, int, str]] = []
    for r, row in enumerate(grid):
        for c, symbol in enumerate(row):
            if symbol == "P":
                power_cells.append((r, c))
            elif symbol in ("W", "E", "X"):
                pass
            else:
                unknown.append((r, c, symbol))
    if unknown:
        sample = ", ".join(
            f"{symbol!r} at (row {r}, col {c})" for r, c, symbol in unknown[:SAMPLE_LIMIT]
        )
        issues.append(Issue(
            code="UNKNOWN_CELL",
            message=f"Found {len(unknown)} cell(s) outside the allowed symbols "
                    f"'P', 'W', 'E', 'X': {sample}{_suffix(len(unknown))}.",
            locations=_coords([(r, c) for r, c, _ in unknown]),
        ))
    if len(power_cells) != 1:
        issues.append(Issue(
            code="POWER_SOURCE_COUNT",
            message=f"Expected exactly one power source 'P', found {len(power_cells)}.",
            locations=_coords(power_cells),
        ))

    missing: list[tuple[int, int]] = []
    empty_ids: list[tuple[int, int]] = []
    misplaced: list[tuple[int, int]] = []
    id_cells: dict[str, list[tuple[int, int]]] = {}
    for r, row in enumerate(grid):
        for c, symbol in enumerate(row):
            label = labels[r][c]
            if symbol == "E":
                if label is None:
                    missing.append((r, c))
                elif label == "":
                    empty_ids.append((r, c))
                else:
                    id_cells.setdefault(label, []).append((r, c))
            elif label is not None:
                misplaced.append((r, c))

    if missing:
        issues.append(Issue(
            code="EXIT_ID_MISSING",
            message=f"Found {len(missing)} exit light(s) 'E' without an id; "
                    f"every 'E' cell needs a non-empty label{_suffix(len(missing))}.",
            locations=_coords(missing),
        ))
    if empty_ids:
        issues.append(Issue(
            code="EXIT_ID_EMPTY",
            message=f"Found {len(empty_ids)} exit id(s) that are empty strings; "
                    f"ids must be non-empty{_suffix(len(empty_ids))}.",
            locations=_coords(empty_ids),
        ))
    if misplaced:
        issues.append(Issue(
            code="EXIT_ID_MISPLACED",
            message=f"Found {len(misplaced)} label(s) on cells that are not 'E'; "
                    f"non-exit cells must be null{_suffix(len(misplaced))}.",
            locations=_coords(misplaced),
        ))
    for exit_id, cells in id_cells.items():
        if len(cells) > 1:
            issues.append(Issue(
                code="EXIT_ID_DUPLICATE",
                message=f"Exit id {exit_id!r} labels {len(cells)} exits; ids must be "
                        f"unique{_suffix(len(cells))}.",
                locations=_coords(cells),
            ))

    if issues:
        raise InputRejected(issues)
