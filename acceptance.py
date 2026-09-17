"""One-shot acceptance suite against a running API instance.

Used by the `verify` docker-compose service, but also runnable directly:

    API_BASE_URL=http://localhost:8000 python acceptance.py

Exits 0 when every check passes, 1 otherwise.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE_URL = os.environ.get("API_BASE_URL", "http://localhost:8000").rstrip("/")
TIMEOUT_SECONDS = 10

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

DIAGONAL_PAYLOAD = {
    "grid": [["P", "X"], ["X", "E"]],
    "labels": [[None, None], [None, "EXIT-D"]],
}

SORT_PAYLOAD = {
    "grid": [
        ["P", "X", "E"],
        ["X", "X", "X"],
        ["E", "X", "E"],
    ],
    "labels": [
        [None, None, "b"],
        [None, None, None],
        ["A", None, "1"],
    ],
}


def _request(method, path, payload=None):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        BASE_URL + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def wait_until_ready(attempts=30, delay=1.0):
    for _ in range(attempts):
        try:
            status, _ = _request("GET", "/healthz")
            if status == 200:
                return True
        except (urllib.error.URLError, ConnectionError, json.JSONDecodeError, OSError):
            pass
        time.sleep(delay)
    return False


def expect_post(payload, status, body=None, predicate=None):
    def run():
        got_status, got_body = _request("POST", "/inspect", payload)
        if got_status != status:
            return False, f"expected HTTP {status}, got {got_status}: {got_body}"
        if body is not None and got_body != body:
            return False, f"unexpected body: {got_body}"
        if predicate is not None:
            return predicate(got_body)
        return True, ""

    return run


def has_issue(code):
    def predicate(body):
        detail = body.get("detail", {})
        if detail.get("code") != "INPUT_REJECTED":
            return False, f"unexpected detail.code: {detail.get('code')!r} in {body}"
        codes = [issue.get("code") for issue in detail.get("issues", [])]
        if code not in codes:
            return False, f"missing issue {code!r}; got {codes}"
        return True, ""

    return predicate


def check_health():
    status, body = _request("GET", "/healthz")
    if status == 200 and body == {"status": "ok"}:
        return True, ""
    return False, f"healthz returned {status}: {body}"


# Drain checks run last: once drained the process only admits inspections
# again after a restart, so no further /inspect checks may follow them.
def check_drain_while_idle():
    status, body = _request("POST", "/drain")
    if status != 200:
        return False, f"expected HTTP 200, got {status}: {body}"
    if body != {"status": "DRAINED", "state": "DRAINED", "in_flight": 0}:
        return False, f"unexpected drain body: {body}"
    # A second call joins the same completed transition.
    again_status, again_body = _request("POST", "/drain")
    if again_status != 200 or again_body != body:
        return False, f"repeated drain is not idempotent: {again_status} {again_body}"
    return True, ""


def check_inspect_rejected_after_drain():
    status, body = _request("POST", "/inspect", PASS_PAYLOAD)
    if status != 503:
        return False, f"expected HTTP 503, got {status}: {body}"
    detail = body.get("detail", {})
    if detail.get("code") != "SERVICE_UNAVAILABLE":
        return False, f"unexpected detail.code: {detail.get('code')!r}"
    if detail.get("state") != "DRAINED" or detail.get("in_flight") != 0:
        return False, f"503 must name the terminal state and zero count: {detail}"
    return True, ""


CHECKS = [
    ("health endpoint responds", check_health),
    (
        "fully wired diagram returns PASS",
        expect_post(PASS_PAYLOAD, 200, body={"result": "PASS"}),
    ),
    (
        "severed corridor returns FAIL with sorted exits and evidence",
        expect_post(
            FAIL_PAYLOAD,
            200,
            body={
                "result": "FAIL",
                "failures": [
                    {"exit_id": "EXIT-A", "evidence": {"row": 2, "col": 4}},
                    {"exit_id": "EXIT-B", "evidence": {"row": 0, "col": 4}},
                ],
            },
        ),
    ),
    (
        "diagonal contact does not conduct",
        expect_post(
            DIAGONAL_PAYLOAD,
            200,
            body={
                "result": "FAIL",
                "failures": [{"exit_id": "EXIT-D", "evidence": {"row": 1, "col": 1}}],
            },
        ),
    ),
    (
        "failures are ordered by Unicode code point",
        expect_post(
            SORT_PAYLOAD,
            200,
            body={
                "result": "FAIL",
                "failures": [
                    {"exit_id": "1", "evidence": {"row": 2, "col": 2}},
                    {"exit_id": "A", "evidence": {"row": 2, "col": 0}},
                    {"exit_id": "b", "evidence": {"row": 0, "col": 2}},
                ],
            },
        ),
    ),
    (
        "two power sources are rejected",
        expect_post(
            {"grid": [["P", "W", "P"]], "labels": [[None, None, None]]},
            422,
            predicate=has_issue("POWER_SOURCE_COUNT"),
        ),
    ),
    (
        "ragged rows are rejected",
        expect_post(
            {"grid": [["P", "W"], ["E"]], "labels": [[None, None], [None]]},
            422,
            predicate=has_issue("GRID_RAGGED"),
        ),
    ),
    (
        "duplicate exit ids are rejected",
        expect_post(
            {"grid": [["P", "E", "E"]], "labels": [[None, "A", "A"]]},
            422,
            predicate=has_issue("EXIT_ID_DUPLICATE"),
        ),
    ),
    (
        "misplaced labels are rejected",
        expect_post(
            {"grid": [["P", "X"]], "labels": [[None, "A"]]},
            422,
            predicate=has_issue("EXIT_ID_MISPLACED"),
        ),
    ),
    (
        "missing exit ids are rejected",
        expect_post(
            {"grid": [["P", "E"]], "labels": [[None, None]]},
            422,
            predicate=has_issue("EXIT_ID_MISSING"),
        ),
    ),
    # Drain checks run last: once drained the process only accepts inspections
    # again after a restart, so no further /inspect checks may follow them.
    ("drain completes while idle", check_drain_while_idle),
    ("inspections are rejected with 503 after drain", check_inspect_rejected_after_drain),
]


def main():
    if not wait_until_ready():
        print(f"[FAIL] API not reachable at {BASE_URL}")
        return 1
    failures = 0
    for name, check in CHECKS:
        try:
            ok, detail = check()
        except Exception as exc:  # noqa: BLE001 - report any failure and continue
            ok, detail = False, f"exception: {exc!r}"
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + ("" if ok else f" -- {detail}"))
        failures += 0 if ok else 1
    print(f"{len(CHECKS) - failures}/{len(CHECKS)} acceptance checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
