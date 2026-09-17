"""End-to-end API behaviour for the canonical passage scenarios."""
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

PASS_PAYLOAD = {
    "grid": [
        ["P", "W", "E"],
        ["X", "X", "W"],
        ["E", "W", "W"],
    ],
    "labels": [
        [None, None, "EXIT-01"],
        [None, None, None],
        ["EXIT-02", None, None],
    ],
}

# Corridor with a severed section: EXIT-A sits alone at (2, 4); EXIT-B at
# (0, 5) belongs to the dead component {(0, 4), (0, 5)} whose row-major
# first cell is the wire at (0, 4).
FAIL_PAYLOAD = {
    "grid": [
        ["P", "W", "W", "X", "W", "E"],
        ["X", "X", "W", "X", "X", "X"],
        ["W", "W", "W", "X", "E", "X"],
    ],
    "labels": [
        [None, None, None, None, None, "EXIT-B"],
        [None, None, None, None, None, None],
        [None, None, None, None, "EXIT-A", None],
    ],
}

FAIL_BODY = {
    "result": "FAIL",
    "failures": [
        {"exit_id": "EXIT-A", "evidence": {"row": 2, "col": 4}},
        {"exit_id": "EXIT-B", "evidence": {"row": 0, "col": 4}},
    ],
}


def test_healthz():
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_pass_response_is_exact_and_omits_failures():
    response = client.post("/inspect", json=PASS_PAYLOAD)
    assert response.status_code == 200
    assert response.json() == {"result": "PASS"}


def test_fail_response_carries_sorted_exits_and_evidence():
    response = client.post("/inspect", json=FAIL_PAYLOAD)
    assert response.status_code == 200
    assert response.json() == FAIL_BODY


def test_get_on_inspect_is_not_allowed():
    assert client.get("/inspect").status_code == 405
