"""Four-neighbourhood graph search over the wiring grid.

Only 'P', 'W' and 'E' conduct. Cells connect solely through shared
horizontal/vertical edges: diagonal contact and out-of-grid paths never
conduct, mirroring the on-site reality that angled touches carry no current.
"""
from __future__ import annotations

from collections import deque

CONDUCTIVE = frozenset({"P", "W", "E"})
_DIRECTIONS = ((1, 0), (-1, 0), (0, 1), (0, -1))

Cell = tuple[int, int]


def flood_fill(grid: list[list[str]], start: Cell) -> set[Cell]:
    """Return every conductive cell reachable from ``start`` (inclusive).

    Breadth-first search over the 4-neighbourhood. Bounds are checked
    explicitly so the search never wraps around grid edges, and diagonals
    are never visited because only edge-sharing neighbours are queued.
    """
    rows = len(grid)
    cols = len(grid[0])
    seen: set[Cell] = {start}
    queue: deque[Cell] = deque([start])
    while queue:
        r, c = queue.popleft()
        for dr, dc in _DIRECTIONS:
            nr, nc = r + dr, c + dc
            if (
                0 <= nr < rows
                and 0 <= nc < cols
                and (nr, nc) not in seen
                and grid[nr][nc] in CONDUCTIVE
            ):
                seen.add((nr, nc))
                queue.append((nr, nc))
    return seen
