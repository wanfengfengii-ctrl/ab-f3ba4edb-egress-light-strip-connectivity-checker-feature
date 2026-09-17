"""Automated acceptance for the rolling-update drain contract.

A controllable blocking inspection (a threading.Event gate patched into the
request path) proves that POST /drain:

* waits for every inspection admitted before the transition,
* rejects inspections arriving after it with a structured 503 carrying the
  current state and in-flight count,
* is unblocked by every terminal path of an admitted request -- normal
  completion, a 422 validation rejection, an internal exception and even
  request-task cancellation -- instead of waiting forever,
* is idempotent under concurrent callers, who all observe one transition and
  get the same DRAINED result,
and leaves health checks, routing and ordinary PASS/FAIL/422 traffic
untouched when drain is never invoked.
"""
from __future__ import annotations

import asyncio
import threading
import time

import httpx
import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.drain import (
    ACCEPTING,
    DRAINED,
    DRAINING,
    DrainCoordinator,
    InspectionRejectedWhileDraining,
    coordinator,
)
from app.models import InspectionResponse
from app.validation import InputRejected, Issue

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
INVALID_PAYLOAD = {"grid": [["P", "W", "P"]], "labels": [[None, None, None]]}

DRAINED_BODY = {"status": "DRAINED", "state": "DRAINED", "in_flight": 0}
REJECTION_MESSAGE = (
    "The inspection service is draining or drained and no longer accepts "
    "new inspection requests."
)


class Gate:
    """Replace an inspection-stage callable with one that blocks on command.

    ``entered`` fires once the request thread has reached the gated stage;
    the test then decides when (and how) the request finishes via ``proceed``.
    """

    def __init__(self, action=None, raise_exc: BaseException | None = None) -> None:
        self.entered = threading.Event()
        self.proceed = threading.Event()
        self.calls = 0
        self._action = action
        self._raise_exc = raise_exc

    def __call__(self, *args, **kwargs):
        self.calls += 1
        self.entered.set()
        if not self.proceed.wait(timeout=10):
            raise RuntimeError("test gate was never released")
        if self._raise_exc is not None:
            raise self._raise_exc
        if self._action is not None:
            return self._action(*args, **kwargs)
        return InspectionResponse(result="PASS")


@pytest.fixture(autouse=True)
def fresh_coordinator():
    # The real coordinator is process-global and terminal; reset it around
    # every test the way a process restart would in production.
    coordinator.reset()
    yield
    coordinator.reset()


def wait_until(predicate, timeout: float = 5.0, interval: float = 0.005):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise AssertionError("timed out waiting for condition")


async def await_until(predicate, timeout: float = 5.0, interval: float = 0.005):
    """Event-loop-safe polling: blocking time.sleep would starve the request task."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(interval)
    raise AssertionError("timed out waiting for condition")


def spawn(fn):
    """Run fn() on a daemon thread, capturing its value or exception."""
    holder: dict = {}

    def run():
        try:
            holder["value"] = fn()
        except BaseException as exc:  # surfaced by the test via holder["error"]
            holder["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, holder


def test_drain_without_traffic_returns_drained_immediately():
    client = TestClient(main_module.app)
    response = client.post("/drain")
    assert response.status_code == 200
    assert response.json() == DRAINED_BODY


def test_drain_waits_for_old_request_and_rejects_new_ones(monkeypatch):
    gate = Gate()
    monkeypatch.setattr(main_module, "inspect", gate)

    old_client = TestClient(main_module.app)
    new_client = TestClient(main_module.app)
    drain_client = TestClient(main_module.app)

    old_thread, old_result = spawn(lambda: old_client.post("/inspect", json=PASS_PAYLOAD))
    wait_until(gate.entered.is_set)
    wait_until(lambda: coordinator.snapshot() == (ACCEPTING, 1))

    drain_thread, drain_result = spawn(lambda: drain_client.post("/drain"))
    wait_until(lambda: coordinator.snapshot()[0] == DRAINING)

    # The drain is still waiting for the admitted inspection to finish.
    drain_thread.join(timeout=0.25)
    assert drain_thread.is_alive(), "drain must not complete while an old inspection runs"

    # A check arriving after the transition is refused with state and count.
    rejected = new_client.post("/inspect", json=PASS_PAYLOAD)
    assert rejected.status_code == 503
    assert rejected.json() == {
        "detail": {
            "code": "SERVICE_UNAVAILABLE",
            "message": REJECTION_MESSAGE,
            "state": "DRAINING",
            "in_flight": 1,
        }
    }

    # Even a payload that would be a 422 is refused at the door; parsing and
    # validation only happen once a request has been admitted.
    malformed = new_client.post("/inspect", json=INVALID_PAYLOAD)
    assert malformed.status_code == 503

    gate.proceed.set()
    old_thread.join(timeout=5)
    drain_thread.join(timeout=5)
    assert not drain_thread.is_alive()

    assert "error" not in old_result
    assert old_result["value"].status_code == 200
    assert old_result["value"].json() == {"result": "PASS"}
    assert drain_result["value"].status_code == 200
    assert drain_result["value"].json() == DRAINED_BODY

    # DRAINED is terminal: new checks are refused naming DRAINED, and a later
    # drain call simply observes the completed transition.
    after = new_client.post("/inspect", json=PASS_PAYLOAD)
    assert after.status_code == 503
    assert after.json()["detail"]["state"] == "DRAINED"
    assert after.json()["detail"]["in_flight"] == 0

    second_drain = drain_client.post("/drain")
    assert second_drain.status_code == 200
    assert second_drain.json() == DRAINED_BODY


def test_admitted_request_still_completes_as_422_and_releases_drain(monkeypatch):
    # Admitted while ACCEPTING, its validation stage blocks until after the
    # drain starts and then rejects the diagram with the ordinary 422.
    gate = Gate(
        raise_exc=InputRejected([
            Issue(code="POWER_SOURCE_COUNT", message="Expected exactly one 'P'.")
        ])
    )
    monkeypatch.setattr(main_module, "validate_payload", gate)

    client = TestClient(main_module.app)
    drain_client = TestClient(main_module.app)

    request_thread, result = spawn(lambda: client.post("/inspect", json=INVALID_PAYLOAD))
    wait_until(gate.entered.is_set)

    drain_thread, drain_result = spawn(lambda: drain_client.post("/drain"))
    wait_until(lambda: coordinator.snapshot()[0] == DRAINING)
    drain_thread.join(timeout=0.25)
    assert drain_thread.is_alive()

    gate.proceed.set()
    request_thread.join(timeout=5)
    drain_thread.join(timeout=5)

    assert "error" not in result
    assert result["value"].status_code == 422
    body = result["value"].json()
    assert body["detail"]["code"] == "INPUT_REJECTED"
    assert body["detail"]["issues"][0]["code"] == "POWER_SOURCE_COUNT"
    assert drain_result["value"].json() == DRAINED_BODY
    assert coordinator.snapshot() == (DRAINED, 0)


def test_internal_exception_releases_the_in_flight_slot(monkeypatch):
    gate = Gate(raise_exc=RuntimeError("inspector exploded"))
    monkeypatch.setattr(main_module, "inspect", gate)

    # The unhandled RuntimeError must surface as a plain 500 instead of being
    # swallowed by the admission layer, and the slot must still be released.
    client = TestClient(main_module.app, raise_server_exceptions=False)
    drain_client = TestClient(main_module.app)

    request_thread, result = spawn(lambda: client.post("/inspect", json=PASS_PAYLOAD))
    wait_until(gate.entered.is_set)

    drain_thread, drain_result = spawn(lambda: drain_client.post("/drain"))
    wait_until(lambda: coordinator.snapshot()[0] == DRAINING)

    gate.proceed.set()
    request_thread.join(timeout=5)
    drain_thread.join(timeout=5)

    assert "error" not in result
    assert result["value"].status_code == 500
    assert drain_result["value"].status_code == 200
    assert drain_result["value"].json() == DRAINED_BODY
    assert coordinator.snapshot() == (DRAINED, 0)


def test_concurrent_drains_share_one_transition_and_return_equal_results(monkeypatch):
    gate = Gate()
    monkeypatch.setattr(main_module, "inspect", gate)

    inspect_client = TestClient(main_module.app)
    drain_client = TestClient(main_module.app)

    old_thread, old_result = spawn(
        lambda: inspect_client.post("/inspect", json=PASS_PAYLOAD)
    )
    wait_until(gate.entered.is_set)

    drain_bodies: list[dict] = []
    drain_statuses: list[int] = []
    barrier = threading.Barrier(5, timeout=5)

    def drain():
        barrier.wait()  # release all five callers together
        response = drain_client.post("/drain")
        drain_statuses.append(response.status_code)
        drain_bodies.append(response.json())

    drain_threads = [threading.Thread(target=drain, daemon=True) for _ in range(5)]
    for thread in drain_threads:
        thread.start()
    wait_until(lambda: coordinator.snapshot()[0] == DRAINING)
    drain_threads[0].join(timeout=0.25)
    assert drain_threads[0].is_alive(), "all drains must wait for the one old inspection"

    gate.proceed.set()
    old_thread.join(timeout=5)
    for thread in drain_threads:
        thread.join(timeout=5)
        assert not thread.is_alive()

    assert drain_statuses == [200] * 5
    assert drain_bodies == [DRAINED_BODY] * 5
    assert "error" not in old_result
    assert old_result["value"].json() == {"result": "PASS"}
    assert coordinator.snapshot() == (DRAINED, 0)


def test_request_cancellation_releases_the_slot(monkeypatch):
    gate = Gate()
    monkeypatch.setattr(main_module, "inspect", gate)

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main_module.app),
            base_url="http://test",
        ) as async_client:
            request_task = asyncio.create_task(
                async_client.post("/inspect", json=PASS_PAYLOAD)
            )
            await await_until(lambda: coordinator.snapshot() == (ACCEPTING, 1))

            drain_holder: dict = {}
            drain_thread = threading.Thread(
                target=lambda: drain_holder.setdefault("value", coordinator.begin_drain()),
                daemon=True,
            )
            drain_thread.start()
            await await_until(lambda: coordinator.snapshot()[0] == DRAINING)

            # The client goes away while the admitted check is still running.
            request_task.cancel()
            gate.proceed.set()  # let the worker thread land; the finally releases
            with pytest.raises(asyncio.CancelledError):
                await request_task

            drain_thread.join(timeout=5)
            assert not drain_thread.is_alive(), "cancellation must not strand the drain"
            assert drain_holder["value"].model_dump() == DRAINED_BODY

    asyncio.run(scenario())


def test_healthz_and_routing_keep_working_after_drain():
    client = TestClient(main_module.app)

    client.post("/drain")  # no in-flight traffic: drains immediately

    assert client.get("/healthz").json() == {"status": "ok"}
    # Method routing sits outside the admission gate.
    assert client.get("/inspect").status_code == 405
    # Unknown routes are not turned into 503s by the gate.
    assert client.get("/nonexistent").status_code == 404


def test_normal_inspection_traffic_is_unchanged_without_drain():
    client = TestClient(main_module.app)
    assert client.get("/healthz").status_code == 200

    passed = client.post("/inspect", json=PASS_PAYLOAD)
    assert passed.status_code == 200
    assert passed.json() == {"result": "PASS"}

    rejected = client.post("/inspect", json=INVALID_PAYLOAD)
    assert rejected.status_code == 422
    assert rejected.json()["detail"]["code"] == "INPUT_REJECTED"

    assert coordinator.snapshot() == (ACCEPTING, 0)


# ---------------------------------------------------------------------------
# Coordinator-level invariants, independent of HTTP plumbing.
# ---------------------------------------------------------------------------


def test_coordinator_counts_admitted_work_and_keeps_accepting_after_release():
    local = DrainCoordinator()
    assert local.snapshot() == (ACCEPTING, 0)

    local.acquire()
    local.acquire()
    assert local.snapshot() == (ACCEPTING, 2)

    local.release()
    assert local.snapshot() == (ACCEPTING, 1)

    local.release()
    assert local.snapshot() == (ACCEPTING, 0)
    local.acquire()  # still admitting with no drain in progress
    local.release()


def test_coordinator_rejects_acquisition_once_drained_and_needs_restart():
    local = DrainCoordinator()
    assert local.begin_drain().model_dump() == DRAINED_BODY

    with pytest.raises(InspectionRejectedWhileDraining) as exc_info:
        local.acquire()
    assert exc_info.value.detail.state == DRAINED
    assert exc_info.value.detail.in_flight == 0
    assert local.snapshot() == (DRAINED, 0)


def test_coordinator_concurrent_drains_return_after_the_last_slot_closes():
    local = DrainCoordinator()
    local.acquire()

    results: list = []
    threads = [
        threading.Thread(target=lambda: results.append(local.begin_drain()))
        for _ in range(3)
    ]
    for thread in threads:
        thread.start()

    wait_until(lambda: local.snapshot()[0] == DRAINING)
    assert all(thread.is_alive() for thread in threads)

    local.release()
    for thread in threads:
        thread.join(timeout=5)

    assert local.snapshot() == (DRAINED, 0)
    assert [result.model_dump() for result in results] == [DRAINED_BODY] * 3
