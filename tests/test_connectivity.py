"""Boundary criteria for the four-neighbourhood connectivity algorithm."""
from app.connectivity import flood_fill
from app.inspector import inspect

MAX = 200


def make_labels(grid, exits):
    """Build a labels matrix from a {(row, col): exit_id} mapping."""
    return [[exits.get((r, c)) for c in range(len(row))] for r, row in enumerate(grid)]


class TestFloodFill:
    def test_spreads_through_shared_edges(self):
        grid = [
            ["P", "W", "X"],
            ["X", "W", "X"],
        ]
        assert flood_fill(grid, (0, 0)) == {(0, 0), (0, 1), (1, 1)}

    def test_diagonal_contact_does_not_conduct(self):
        grid = [
            ["P", "X"],
            ["X", "W"],
        ]
        assert flood_fill(grid, (0, 0)) == {(0, 0)}

    def test_empty_cells_block_current(self):
        grid = [["P", "X", "W"]]
        assert flood_fill(grid, (0, 0)) == {(0, 0)}

    def test_never_wraps_around_grid_edges(self):
        # Without explicit bounds checks, Python negative indexing would let
        # the search wrap from row 0 to the last row, or from column 0 to the
        # last column, energizing cells that share no edge with the source.
        vertical = [
            ["P", "W"],
            ["X", "X"],
            ["W", "X"],
        ]
        assert flood_fill(vertical, (0, 0)) == {(0, 0), (0, 1)}

        horizontal = [["P", "X", "W"]]
        assert flood_fill(horizontal, (0, 0)) == {(0, 0)}

    def test_single_cell_grid(self):
        assert flood_fill([["P"]], (0, 0)) == {(0, 0)}

    def test_maximum_size_grid_is_fully_swept(self):
        grid = [["W"] * MAX for _ in range(MAX)]
        grid[0][0] = "P"
        assert len(flood_fill(grid, (0, 0))) == MAX * MAX


class TestInspect:
    def test_all_exits_reachable_passes(self):
        grid = [
            ["P", "W", "E"],
            ["X", "X", "W"],
            ["E", "W", "W"],
        ]
        labels = make_labels(grid, {(0, 2): "EXIT-01", (2, 0): "EXIT-02"})
        result = inspect(grid, labels)
        assert result.result == "PASS"
        assert result.failures is None

    def test_no_exits_passes_vacuously(self):
        grid = [["P", "W", "X"]]
        result = inspect(grid, make_labels(grid, {}))
        assert result.result == "PASS"

    def test_unreachable_exit_reports_component_first_cell(self):
        grid = [
            ["P", "W", "X"],
            ["X", "X", "W"],
            ["X", "W", "E"],
        ]
        # The exit's component is {(1,2), (2,1), (2,2)}; its row-major first
        # cell is the wire at (1,2), not the exit itself at (2,2).
        labels = make_labels(grid, {(2, 2): "EXIT-9"})
        result = inspect(grid, labels)
        assert result.result == "FAIL"
        assert [(f.exit_id, f.evidence.row, f.evidence.col) for f in result.failures] == [
            ("EXIT-9", 1, 2)
        ]

    def test_failures_sorted_by_unicode_code_point(self):
        grid = [
            ["P", "X", "E"],
            ["X", "X", "X"],
            ["E", "X", "E"],
        ]
        labels = make_labels(grid, {(0, 2): "b", (2, 0): "A", (2, 2): "1"})
        result = inspect(grid, labels)
        assert [f.exit_id for f in result.failures] == ["1", "A", "b"]

    def test_exits_sharing_a_component_share_its_evidence(self):
        grid = [
            ["P", "X", "W"],
            ["X", "X", "E"],
            ["X", "X", "E"],
        ]
        labels = make_labels(grid, {(1, 2): "E1", (2, 2): "E2"})
        result = inspect(grid, labels)
        assert [(f.exit_id, f.evidence.row, f.evidence.col) for f in result.failures] == [
            ("E1", 0, 2),
            ("E2", 0, 2),
        ]

    def test_diagonal_exit_fails_with_its_own_coordinates(self):
        grid = [
            ["P", "X"],
            ["X", "E"],
        ]
        labels = make_labels(grid, {(1, 1): "EXIT-D"})
        result = inspect(grid, labels)
        assert result.result == "FAIL"
        assert (result.failures[0].evidence.row, result.failures[0].evidence.col) == (1, 1)

    def test_max_grid_cut_by_empty_column(self):
        grid = [["W"] * MAX for _ in range(MAX)]
        grid[0][0] = "P"
        for r in range(MAX):
            grid[r][100] = "X"
        grid[MAX - 1][MAX - 1] = "E"
        labels = make_labels(grid, {(MAX - 1, MAX - 1): "EXIT-FAR"})
        result = inspect(grid, labels)
        assert result.result == "FAIL"
        failure = result.failures[0]
        assert failure.exit_id == "EXIT-FAR"
        # Right-hand component spans rows 0..199, cols 101..199.
        assert (failure.evidence.row, failure.evidence.col) == (0, 101)
