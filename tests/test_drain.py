"""Drain lifecycle acceptance: admission gate, waiting, idempotency.

A test-controlled threading gate injected into the inspection pipeline
(via monkeypatch) makes inspections block on demand, so these tests prove
that POST /drain waits for previously admitted requests, rejects new ones
with a structured 503, and is released by every finally path — success,
validation rejection, internal error and client cancellation — while the
plain inspection contract stays intact when drain is never called.
"""
from __future__ import annotations

import asyncio
import json
import threading

import httpx
import pytest

import app.main as main_module
from app.drain import DrainCoordinator, InspectionAdmissionMiddleware
from app.main import create_app
from app.models import DrainState

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

FAIL_BODY = {
    "result": "FAIL",
    "failures": [
        {"exit_id": "EXIT-A", "evidence": {"row": 2, "col": 4}},
        {"exit_id": "EXIT-B", "evidence": {"row": 0, "col": 4}},
    ],
}

# Two power sources violate the input contract -> structured 422.
INVALID_PAYLOAD = {"grid": [["P", "W", "P"]], "labels": [[None, None, None]]}

DRAINED_BODY = {"state": "DRAINED"}


async def wait_until(predicate, timeout=5.0, interval=0.005):
    """Poll a thread-safe condition until it holds or the timeout expires."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() >= deadline:
            raise AssertionError("condition was not met within the timeout")
        await asyncio.sleep(interval)


def asgi_client(app, **transport_kwargs):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, **transport_kwargs),
        base_url="http://testserver",
        timeout=10.0,
    )


def gate_pipeline(monkeypatch, attribute, outcome):
    """Block an app.main pipeline step on a test-controlled gate.

    Returns the gate: the patched step waits for it inside the worker
    thread, then applies ``outcome`` (the original callable, or an
    exception instance to raise).
    """
    gate = threading.Event()

    def gated(grid, labels):
        if not gate.wait(timeout=10):
            raise RuntimeError("test gate was never opened")
        if isinstance(outcome, Exception):
            raise outcome
        return outcome(grid, labels)

    monkeypatch.setattr(main_module, attribute, gated)
    return gate


def gate_inspect(monkeypatch):
    """Hold admitted inspections inside the connectivity search."""
    return gate_pipeline(monkeypatch, "inspect", main_module.inspect)


def gate_validation(monkeypatch):
    """Hold admitted inspections before the input contract is checked."""
    return gate_pipeline(monkeypatch, "validate_payload", main_module.validate_payload)


class TestDrainCoordinator:
    def test_starts_accepting_with_no_in_flight(self):
        coordinator = DrainCoordinator()
        assert coordinator.state is DrainState.ACCEPTING
        assert coordinator.in_flight == 0

    def test_admission_and_release_track_the_in_flight_count(self):
        coordinator = DrainCoordinator()
        assert coordinator.try_admit()
        assert coordinator.try_admit()
        assert coordinator.in_flight == 2
        coordinator.release()
        assert coordinator.in_flight == 1
        coordinator.release()
        assert coordinator.in_flight == 0
        assert coordinator.state is DrainState.ACCEPTING

    def test_release_without_admission_raises(self):
        coordinator = DrainCoordinator()
        with pytest.raises(RuntimeError, match="release without a matching admission"):
            coordinator.release()

    def test_drain_completes_immediately_when_idle(self):
        async def scenario():
            coordinator = DrainCoordinator()
            assert await coordinator.drain() is DrainState.DRAINED
            assert coordinator.state is DrainState.DRAINED

        asyncio.run(scenario())

    def test_drain_waits_for_the_last_admitted_inspection(self):
        async def scenario():
            coordinator = DrainCoordinator()
            assert coordinator.try_admit()
            assert coordinator.try_admit()
            waiter = asyncio.create_task(coordinator.drain())
            await asyncio.sleep(0)
            assert coordinator.state is DrainState.DRAINING
            assert not waiter.done()

            coordinator.release()
            await asyncio.sleep(0)
            assert not waiter.done(), "one inspection is still in flight"

            coordinator.release()
            assert await asyncio.wait_for(waiter, 1) is DrainState.DRAINED
            assert coordinator.state is DrainState.DRAINED
            assert coordinator.in_flight == 0

        asyncio.run(scenario())

    def test_concurrent_drains_share_one_transition_and_result(self):
        async def scenario():
            coordinator = DrainCoordinator()
            assert coordinator.try_admit()
            waiters = [asyncio.create_task(coordinator.drain()) for _ in range(3)]
            await asyncio.sleep(0)
            assert coordinator.state is DrainState.DRAINING
            assert all(not waiter.done() for waiter in waiters)

            coordinator.release()
            results = await asyncio.gather(*waiters)
            assert results == [DrainState.DRAINED] * 3

        asyncio.run(scenario())

    def test_admission_stops_once_draining_and_never_resumes(self):
        async def scenario():
            coordinator = DrainCoordinator()
            assert coordinator.try_admit()
            waiter = asyncio.create_task(coordinator.drain())
            await asyncio.sleep(0)
            assert not coordinator.try_admit(), "draining rejects new admissions"
            coordinator.release()
            assert await waiter is DrainState.DRAINED
            assert not coordinator.try_admit(), "drained rejects new admissions"
            # The state is terminal: only a process restart accepts again.
            assert coordinator.state is DrainState.DRAINED
            assert await coordinator.drain() is DrainState.DRAINED

        asyncio.run(scenario())

    def test_admit_and_release_survive_thread_contention(self):
        coordinator = DrainCoordinator()

        def worker():
            for _ in range(1000):
                assert coordinator.try_admit()
                coordinator.release()

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert coordinator.in_flight == 0
        assert coordinator.state is DrainState.ACCEPTING


def http_scope(method="POST", path="/inspect"):
    return {"type": "http", "method": method, "path": path, "headers": []}


def responding_app(status):
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": status, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    return app


def collect_sent():
    sent = []

    async def send(message):
        sent.append(message)

    return sent, send


class TestInspectionAdmissionMiddleware:
    def test_releases_the_slot_after_a_success(self):
        async def scenario():
            coordinator = DrainCoordinator()
            middleware = InspectionAdmissionMiddleware(responding_app(200), coordinator)
            sent, send = collect_sent()
            await middleware(http_scope(), None, send)
            assert sent[0]["status"] == 200
            assert coordinator.in_flight == 0

        asyncio.run(scenario())

    def test_releases_the_slot_after_a_validation_rejection(self):
        async def scenario():
            coordinator = DrainCoordinator()
            middleware = InspectionAdmissionMiddleware(responding_app(422), coordinator)
            sent, send = collect_sent()
            await middleware(http_scope(), None, send)
            assert sent[0]["status"] == 422
            assert coordinator.in_flight == 0

        asyncio.run(scenario())

    def test_releases_the_slot_after_an_internal_error(self):
        async def scenario():
            coordinator = DrainCoordinator()

            async def broken_app(scope, receive, send):
                raise RuntimeError("boom")

            middleware = InspectionAdmissionMiddleware(broken_app, coordinator)
            with pytest.raises(RuntimeError, match="boom"):
                await middleware(http_scope(), None, None)
            assert coordinator.in_flight == 0

        asyncio.run(scenario())

    def test_releases_the_slot_when_the_request_is_cancelled(self):
        async def scenario():
            coordinator = DrainCoordinator()

            async def cancelled_app(scope, receive, send):
                raise asyncio.CancelledError()

            middleware = InspectionAdmissionMiddleware(cancelled_app, coordinator)
            with pytest.raises(asyncio.CancelledError):
                await middleware(http_scope(), None, None)
            assert coordinator.in_flight == 0

        asyncio.run(scenario())

    def test_rejects_new_inspections_with_a_structured_503(self):
        async def scenario():
            coordinator = DrainCoordinator()
            assert coordinator.try_admit()  # hold one slot so the state sits at DRAINING
            waiter = asyncio.create_task(coordinator.drain())
            await asyncio.sleep(0)

            async def forbidden_app(scope, receive, send):
                pytest.fail("a rejected request must never reach the application")

            middleware = InspectionAdmissionMiddleware(forbidden_app, coordinator)
            sent, send = collect_sent()
            await middleware(http_scope(), None, send)

            start, body = sent
            assert start["status"] == 503
            payload = json.loads(body["body"])
            assert payload["detail"]["code"] == "SERVICE_DRAINING"
            assert payload["detail"]["state"] == "DRAINING"
            assert payload["detail"]["message"]
            assert coordinator.in_flight == 1, "a rejected request holds no slot"

            coordinator.release()
            assert await waiter is DrainState.DRAINED

        asyncio.run(scenario())

    def test_non_inspection_traffic_passes_through_uncounted(self):
        async def scenario():
            coordinator = DrainCoordinator()
            middleware = InspectionAdmissionMiddleware(responding_app(200), coordinator)
            for scope in (
                http_scope(method="GET", path="/inspect"),
                http_scope(method="POST", path="/healthz"),
                {"type": "lifespan"},
            ):
                sent, send = collect_sent()
                await middleware(scope, None, send)
                assert coordinator.in_flight == 0

        asyncio.run(scenario())


class TestDrainApi:
    def test_inspection_contract_is_intact_when_drain_is_never_called(self):
        async def scenario():
            app = create_app(DrainCoordinator())
            async with asgi_client(app) as client:
                health = await client.get("/healthz")
                assert health.status_code == 200
                assert health.json() == {"status": "ok"}

                passed = await client.post("/inspect", json=PASS_PAYLOAD)
                assert passed.status_code == 200
                assert passed.json() == {"result": "PASS"}

                failed = await client.post("/inspect", json=FAIL_PAYLOAD)
                assert failed.status_code == 200
                assert failed.json() == FAIL_BODY

                invalid = await client.post("/inspect", json=INVALID_PAYLOAD)
                assert invalid.status_code == 422
                detail = invalid.json()["detail"]
                assert detail["code"] == "INPUT_REJECTED"
                assert "POWER_SOURCE_COUNT" in [i["code"] for i in detail["issues"]]

                assert (await client.get("/inspect")).status_code == 405

        asyncio.run(scenario())

    @pytest.mark.parametrize(
        "payload,expected_body",
        [
            (PASS_PAYLOAD, {"result": "PASS"}),
            (FAIL_PAYLOAD, FAIL_BODY),
        ],
    )
    def test_drain_waits_for_admitted_inspection_and_rejects_new_ones(
        self, monkeypatch, payload, expected_body
    ):
        async def scenario():
            coordinator = DrainCoordinator()
            app = create_app(coordinator)
            gate = gate_inspect(monkeypatch)
            async with asgi_client(app) as client:
                inspection = asyncio.create_task(client.post("/inspect", json=payload))
                await wait_until(lambda: coordinator.in_flight == 1)

                drain_call = asyncio.create_task(client.post("/drain"))
                await wait_until(lambda: coordinator.state is DrainState.DRAINING)

                # New inspections are refused while the old one is still inside.
                rejected = await client.post("/inspect", json=PASS_PAYLOAD)
                assert rejected.status_code == 503
                detail = rejected.json()["detail"]
                assert detail["code"] == "SERVICE_DRAINING"
                assert detail["state"] == "DRAINING"

                await asyncio.sleep(0.05)
                assert not drain_call.done(), "drain must wait for the admitted request"

                gate.set()
                inspection_response = await inspection
                assert inspection_response.status_code == 200
                assert inspection_response.json() == expected_body

                drain_response = await asyncio.wait_for(drain_call, timeout=5)
                assert drain_response.status_code == 200
                assert drain_response.json() == DRAINED_BODY
            assert coordinator.in_flight == 0

        asyncio.run(scenario())

    def test_admitted_request_still_gets_its_422_during_drain(self, monkeypatch):
        async def scenario():
            coordinator = DrainCoordinator()
            app = create_app(coordinator)
            gate = gate_validation(monkeypatch)
            async with asgi_client(app) as client:
                inspection = asyncio.create_task(client.post("/inspect", json=INVALID_PAYLOAD))
                await wait_until(lambda: coordinator.in_flight == 1)

                drain_call = asyncio.create_task(client.post("/drain"))
                await wait_until(lambda: coordinator.state is DrainState.DRAINING)

                gate.set()
                response = await inspection
                assert response.status_code == 422
                detail = response.json()["detail"]
                assert detail["code"] == "INPUT_REJECTED"
                assert "POWER_SOURCE_COUNT" in [i["code"] for i in detail["issues"]]

                drain_response = await asyncio.wait_for(drain_call, timeout=5)
                assert drain_response.json() == DRAINED_BODY
            assert coordinator.in_flight == 0

        asyncio.run(scenario())

    def test_internal_error_still_releases_the_drain(self, monkeypatch):
        async def scenario():
            coordinator = DrainCoordinator()
            app = create_app(coordinator)
            gate = gate_pipeline(
                monkeypatch, "inspect", RuntimeError("inspection exploded")
            )
            async with asgi_client(app, raise_app_exceptions=False) as client:
                inspection = asyncio.create_task(client.post("/inspect", json=PASS_PAYLOAD))
                await wait_until(lambda: coordinator.in_flight == 1)

                drain_call = asyncio.create_task(client.post("/drain"))
                await wait_until(lambda: coordinator.state is DrainState.DRAINING)

                gate.set()
                response = await inspection
                assert response.status_code == 500

                drain_response = await asyncio.wait_for(drain_call, timeout=5)
                assert drain_response.json() == DRAINED_BODY
            assert coordinator.in_flight == 0

        asyncio.run(scenario())

    def test_client_cancellation_releases_the_drain(self, monkeypatch):
        async def scenario():
            coordinator = DrainCoordinator()
            app = create_app(coordinator)
            gate = gate_inspect(monkeypatch)
            async with asgi_client(app) as client:
                inspection = asyncio.create_task(client.post("/inspect", json=PASS_PAYLOAD))
                await wait_until(lambda: coordinator.in_flight == 1)

                drain_call = asyncio.create_task(client.post("/drain"))
                await wait_until(lambda: coordinator.state is DrainState.DRAINING)

                inspection.cancel()
                gate.set()  # let the abandoned worker thread finish
                with pytest.raises(asyncio.CancelledError):
                    await inspection

                drain_response = await asyncio.wait_for(drain_call, timeout=5)
                assert drain_response.json() == DRAINED_BODY
            assert coordinator.in_flight == 0

        asyncio.run(scenario())

    def test_concurrent_drain_calls_get_the_same_result(self, monkeypatch):
        async def scenario():
            coordinator = DrainCoordinator()
            app = create_app(coordinator)
            gate = gate_inspect(monkeypatch)
            async with asgi_client(app) as client:
                inspection = asyncio.create_task(client.post("/inspect", json=PASS_PAYLOAD))
                await wait_until(lambda: coordinator.in_flight == 1)

                drains = [asyncio.create_task(client.post("/drain")) for _ in range(3)]
                await wait_until(lambda: coordinator.state is DrainState.DRAINING)
                await asyncio.sleep(0.05)
                assert all(not drain.done() for drain in drains)

                gate.set()
                assert (await inspection).status_code == 200
                responses = await asyncio.gather(*drains)
                assert [r.status_code for r in responses] == [200, 200, 200]
                bodies = [r.json() for r in responses]
                assert bodies == [DRAINED_BODY] * 3, "every caller shares one result"

                # Drained is terminal: later drains return the same body at once.
                again = await client.post("/drain")
                assert again.status_code == 200
                assert again.json() == DRAINED_BODY

        asyncio.run(scenario())

    def test_503_reports_the_current_state_while_draining_and_after_drained(
        self, monkeypatch
    ):
        async def scenario():
            coordinator = DrainCoordinator()
            app = create_app(coordinator)
            gate = gate_inspect(monkeypatch)
            async with asgi_client(app) as client:
                inspection = asyncio.create_task(client.post("/inspect", json=PASS_PAYLOAD))
                await wait_until(lambda: coordinator.in_flight == 1)
                drain_call = asyncio.create_task(client.post("/drain"))
                await wait_until(lambda: coordinator.state is DrainState.DRAINING)

                while_draining = await client.post("/inspect", json=PASS_PAYLOAD)
                assert while_draining.status_code == 503
                assert while_draining.json()["detail"]["state"] == "DRAINING"

                gate.set()
                assert (await inspection).status_code == 200
                assert (await drain_call).json() == DRAINED_BODY

                after_drained = await client.post("/inspect", json=PASS_PAYLOAD)
                assert after_drained.status_code == 503
                detail = after_drained.json()["detail"]
                assert detail["code"] == "SERVICE_DRAINED"
                assert detail["state"] == "DRAINED"

        asyncio.run(scenario())

    def test_drained_service_keeps_health_and_method_contract(self):
        async def scenario():
            app = create_app(DrainCoordinator())
            async with asgi_client(app) as client:
                drained = await client.post("/drain")
                assert drained.status_code == 200
                assert drained.json() == DRAINED_BODY

                health = await client.get("/healthz")
                assert health.status_code == 200
                assert health.json() == {"status": "ok"}

                assert (await client.get("/inspect")).status_code == 405

                rejected = await client.post("/inspect", json=PASS_PAYLOAD)
                assert rejected.status_code == 503
                assert rejected.json()["detail"]["code"] == "SERVICE_DRAINED"

        asyncio.run(scenario())
