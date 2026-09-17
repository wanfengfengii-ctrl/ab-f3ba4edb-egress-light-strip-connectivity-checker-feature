"""Drain coordination for graceful rolling updates.

POST /drain moves the service through a one-way ACCEPTING -> DRAINING ->
DRAINED lifecycle: admission of new inspections stops the moment draining
begins, and the drain completes only when every previously admitted
inspection has released its slot. The coordinator, the admission
middleware, the routes and the response models all share the DrainState
contract defined in app.models.
"""
from __future__ import annotations

import asyncio
import threading

from starlette.types import ASGIApp, Receive, Scope, Send

from app.models import DrainRejectionDetail, DrainRejectionResponse, DrainState

INSPECT_PATH = "/inspect"


class DrainCoordinator:
    """Single-process, concurrency-safe drain state machine.

    A threading.Lock guards every state/counter mutation, so the atomic
    boundary around admission, release and the drain transition holds no
    matter which thread (event loop or worker) calls in; the lock is never
    held across an await. Waiters are per-call asyncio futures, so the
    coordinator itself never binds to a specific event loop.
    """

    def __init__(self) -> None:
        self._state = DrainState.ACCEPTING
        self._in_flight = 0
        self._lock = threading.Lock()
        self._waiters: list[tuple[asyncio.AbstractEventLoop, asyncio.Future[DrainState]]] = []

    @property
    def state(self) -> DrainState:
        with self._lock:
            return self._state

    @property
    def in_flight(self) -> int:
        with self._lock:
            return self._in_flight

    def try_admit(self) -> bool:
        """Admit one inspection iff the service is still ACCEPTING.

        The state check and the counter increment are one atomic step, so
        no inspection slips in after draining begins.
        """
        with self._lock:
            if self._state is not DrainState.ACCEPTING:
                return False
            self._in_flight += 1
            return True

    def release(self) -> None:
        """Release one admitted inspection.

        When the last in-flight inspection leaves while DRAINING, the
        transition to DRAINED completes and every waiting drain caller is
        resolved with the same result.
        """
        with self._lock:
            if self._in_flight == 0:
                raise RuntimeError("drain coordinator: release without a matching admission")
            self._in_flight -= 1
            if self._in_flight == 0 and self._state is DrainState.DRAINING:
                self._state = DrainState.DRAINED
                waiters, self._waiters = self._waiters, []
            else:
                waiters = []
        for loop, future in waiters:
            try:
                loop.call_soon_threadsafe(self._settle, future)
            except RuntimeError:
                pass  # the waiting loop is already gone; nothing left to wake

    @staticmethod
    def _settle(future: asyncio.Future[DrainState]) -> None:
        if not future.done():  # a cancelled drain caller needs no result
            future.set_result(DrainState.DRAINED)

    async def drain(self) -> DrainState:
        """Transition to DRAINING and wait for the in-flight count to reach zero.

        Concurrent callers share the single transition: each registers its
        own waiter and every one of them resolves to DRAINED once the last
        admitted inspection releases. Callers that go away (cancelled)
        never affect the transition. The state is terminal — only a
        process restart returns to ACCEPTING.
        """
        with self._lock:
            if self._state is DrainState.DRAINED:
                return DrainState.DRAINED
            self._state = DrainState.DRAINING
            if self._in_flight == 0:
                self._state = DrainState.DRAINED
                return DrainState.DRAINED
            loop = asyncio.get_running_loop()
            future: asyncio.Future[DrainState] = loop.create_future()
            self._waiters.append((loop, future))
        return await future


class InspectionAdmissionMiddleware:
    """Pure-ASGI admission gate around POST /inspect.

    While the coordinator is ACCEPTING the request is admitted and its
    in-flight slot is released in a finally block — so success, validation
    rejections, internal errors and client cancellations all let a waiting
    drain complete instead of blocking it forever. Once draining begins,
    new inspections are answered directly with a structured 503 carrying
    the current drain state. All other traffic passes through untouched.
    """

    def __init__(self, app: ASGIApp, coordinator: DrainCoordinator) -> None:
        self.app = app
        self.coordinator = coordinator

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope["method"] != "POST"
            or scope["path"] != INSPECT_PATH
        ):
            await self.app(scope, receive, send)
            return
        if not self.coordinator.try_admit():
            await self._reject(send)
            return
        try:
            await self.app(scope, receive, send)
        finally:
            self.coordinator.release()

    async def _reject(self, send: Send) -> None:
        detail = DrainRejectionDetail.for_state(self.coordinator.state)
        body = DrainRejectionResponse(detail=detail).model_dump_json().encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": 503,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        })
        await send({"type": "http.response.body", "body": body})
