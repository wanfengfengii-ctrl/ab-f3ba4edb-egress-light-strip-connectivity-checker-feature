"""Inspection pipeline: reachability from the power source plus evidence."""
from __future__ import annotations

from typing import cast

from app.connectivity import Cell, flood_fill
from app.models import Coordinate, Failure, InspectionResponse


def inspect(grid: list[list[str]], labels: list[list[str | None]]) -> InspectionResponse:
    """Inspect a validated diagram.

    Searches outward from the single power source 'P'. Every exit light 'E'
    outside the reached set is reported with the row-major first cell of its
    own (unreachable) connected component as evidence. Failures are ordered
    by exit id in Unicode code-point order.
    """
    power = next(
        (r, c)
        for r, row in enumerate(grid)
        for c, symbol in enumerate(row)
        if symbol == "P"
    )
    reachable = flood_fill(grid, power)

    exits: list[tuple[str, Cell]] = [
        (cast(str, labels[r][c]), (r, c))
        for r, row in enumerate(grid)
        for c, symbol in enumerate(row)
        if symbol == "E"
    ]

    # Cache each unreachable component's row-major head so exits sharing a
    # component cost one search in total, not one per exit.
    component_head: dict[Cell, Cell] = {}
    failures: list[Failure] = []
    for exit_id, cell in exits:
        if cell in reachable:
            continue
        head = component_head.get(cell)
        if head is None:
            component = flood_fill(grid, cell)
            head = min(component)  # tuples order by (row, col): row-major first cell
            for member in component:
                component_head[member] = head
        failures.append(Failure(
            exit_id=exit_id,
            evidence=Coordinate(row=head[0], col=head[1]),
        ))

    failures.sort(key=lambda failure: failure.exit_id)
    if failures:
        return InspectionResponse(result="FAIL", failures=failures)
    return InspectionResponse(result="PASS")
