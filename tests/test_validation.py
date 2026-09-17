"""Input-contract rejections: every malformed diagram gets a structured 422."""
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def post(payload):
    return client.post("/inspect", json=payload)


def issue_codes(response):
    return [issue["code"] for issue in response.json()["detail"]["issues"]]


def assert_rejected(response, *codes):
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "INPUT_REJECTED"
    found = issue_codes(response)
    for code in codes:
        assert code in found, f"expected issue {code}, got {found}"
    return detail


class TestStructure:
    def test_empty_grid(self):
        assert_rejected(post({"grid": [], "labels": []}), "GRID_EMPTY")

    def test_grid_too_tall(self):
        grid = [["P"]] + [["X"]] * 200
        labels = [[None]] * 201
        assert_rejected(post({"grid": grid, "labels": labels}), "GRID_TOO_TALL")

    def test_row_too_wide(self):
        grid = [["P"] + ["X"] * 200]
        labels = [[None] * 201]
        assert_rejected(post({"grid": grid, "labels": labels}), "ROW_TOO_WIDE")

    def test_empty_row(self):
        assert_rejected(post({"grid": [[]], "labels": [[]]}), "ROW_EMPTY")

    def test_ragged_rows(self):
        payload = {"grid": [["P", "W"], ["E"]], "labels": [[None, None], [None]]}
        detail = assert_rejected(post(payload), "GRID_RAGGED")
        assert "row 1" in detail["issues"][0]["message"]

    def test_labels_row_count_mismatch(self):
        assert_rejected(
            post({"grid": [["P"]], "labels": [[None], [None]]}),
            "LABELS_ROW_COUNT_MISMATCH",
        )

    def test_labels_row_length_mismatch(self):
        assert_rejected(
            post({"grid": [["P", "W"]], "labels": [[None]]}),
            "LABELS_ROW_LENGTH_MISMATCH",
        )

    def test_structural_failure_skips_content_checks(self):
        # Ragged grid with an unknown symbol: only the structural issue is reported.
        payload = {"grid": [["P", "?"], ["X"]], "labels": [[None, None], [None]]}
        response = post(payload)
        assert issue_codes(response) == ["GRID_RAGGED"]


class TestContent:
    def test_unknown_cell_reports_location(self):
        payload = {"grid": [["P", "Q"]], "labels": [[None, None]]}
        detail = assert_rejected(post(payload), "UNKNOWN_CELL")
        issue = next(i for i in detail["issues"] if i["code"] == "UNKNOWN_CELL")
        assert issue["locations"] == [{"row": 0, "col": 1}]
        assert "'Q'" in issue["message"]

    def test_power_source_missing(self):
        payload = {"grid": [["W", "E"]], "labels": [[None, "A"]]}
        assert_rejected(post(payload), "POWER_SOURCE_COUNT")

    def test_power_source_duplicated(self):
        payload = {"grid": [["P", "P"]], "labels": [[None, None]]}
        detail = assert_rejected(post(payload), "POWER_SOURCE_COUNT")
        issue = next(i for i in detail["issues"] if i["code"] == "POWER_SOURCE_COUNT")
        assert "found 2" in issue["message"]
        assert issue["locations"] == [{"row": 0, "col": 0}, {"row": 0, "col": 1}]

    def test_exit_id_missing(self):
        payload = {"grid": [["P", "E"]], "labels": [[None, None]]}
        detail = assert_rejected(post(payload), "EXIT_ID_MISSING")
        issue = next(i for i in detail["issues"] if i["code"] == "EXIT_ID_MISSING")
        assert issue["locations"] == [{"row": 0, "col": 1}]

    def test_exit_id_empty_string(self):
        payload = {"grid": [["P", "E"]], "labels": [[None, ""]]}
        assert_rejected(post(payload), "EXIT_ID_EMPTY")

    def test_label_misplaced_on_each_non_exit_symbol(self):
        for symbol in ("P", "W", "X"):
            grid = [["P", symbol]] if symbol != "P" else [[symbol, "W"]]
            labels = [[None, "A"]] if symbol != "P" else [["A", None]]
            detail = assert_rejected(post({"grid": grid, "labels": labels}), "EXIT_ID_MISPLACED")
            issue = next(i for i in detail["issues"] if i["code"] == "EXIT_ID_MISPLACED")
            assert issue["locations"] == [{"row": 0, "col": 0 if symbol == "P" else 1}]

    def test_duplicate_exit_ids(self):
        payload = {"grid": [["P", "E", "E"]], "labels": [[None, "A", "A"]]}
        detail = assert_rejected(post(payload), "EXIT_ID_DUPLICATE")
        issue = next(i for i in detail["issues"] if i["code"] == "EXIT_ID_DUPLICATE")
        assert "'A'" in issue["message"]
        assert issue["locations"] == [{"row": 0, "col": 1}, {"row": 0, "col": 2}]

    def test_all_content_problems_collected_together(self):
        payload = {"grid": [["?", "E"]], "labels": [[None, None]]}
        assert_rejected(
            post(payload),
            "UNKNOWN_CELL",
            "POWER_SOURCE_COUNT",
            "EXIT_ID_MISSING",
        )


class TestSchema:
    def assert_schema_error(self, response):
        assert response.status_code == 422, response.text
        detail = response.json()["detail"]
        assert detail["code"] == "SCHEMA_ERROR"
        assert detail["issues"]

    def test_non_string_cell(self):
        self.assert_schema_error(post({"grid": [["P", 1]], "labels": [[None, None]]}))

    def test_non_string_label(self):
        self.assert_schema_error(post({"grid": [["P", "E"]], "labels": [[None, 7]]}))

    def test_missing_labels_field(self):
        self.assert_schema_error(post({"grid": [["P"]]}))

    def test_extra_field_forbidden(self):
        self.assert_schema_error(post({"grid": [["P"]], "labels": [[None]], "extra": 1}))

    def test_malformed_json(self):
        response = client.post(
            "/inspect", content="{not json", headers={"Content-Type": "application/json"}
        )
        self.assert_schema_error(response)
