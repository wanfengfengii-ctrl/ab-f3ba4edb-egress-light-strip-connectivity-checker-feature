"""Single-process drain coordinator.

Before a rolling update the operator calls POST /drain: the service stops
admitting new inspection requests and answers DRAINED only after every
inspection admitted before the transition has finished. The coordinator
guards the whole transition with one threading.Condition (FastAPI runs sync
endpoints in a worker threadpool) and tracks:

* state     - ACCEPTING -> DRAINING -> DRAINED (terminal until restart)
* in_flight - number of inspections admitted before the drain still running

Every admitted inspection MUST release its slot in a finally block, so a
successful check, a 422 rejection raised inside the endpoint, an internal
exception or a cancelled request all unblock the drain instead of making it
wait forever.

Concurrent /drain calls join the same transition: exactly one performs the
ACCEPTING -> DRAINING flip, and all of them wait on the one Condition and
return the same DRAINED result after the count reaches zero.
"""
from __future__ import annotations

import threading

from app.models import DrainRejectionDetail, DrainResponse

ACCEPTING = "ACCEPTING"
DRAINING = "DRAINING"
DRAINED = "DRAINED"


class InspectionRejectedWhileDraining(Exception):
    """Raised when an inspection arrives after the ACCEPTING phase ended."""

    def __init__(self, detail: DrainRejectionDetail) -> None:
        super().__init__(detail.message)
        self.detail = detail


class DrainCoordinator:
    """State machine plus in-flight counter behind a single lock/condition."""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._state: str = ACCEPTING
        self._in_flight = 0

    def snapshot(self) -> tuple[str, int]:
        """Return the current ``(state, in_flight)`` pair."""
        with self._cond:
            return self._state, self._in_flight

    def begin_drain(self) -> DrainResponse:
        """Move into (or join) the drain and block until the count is zero.

        Returns the same DrainResponse for every caller of the same
        transition. The drained state is terminal for the process lifetime.
        """
        with self._cond:
            if self._state == ACCEPTING:
                self._state = DRAINING
                if self._in_flight == 0:
                    self._state = DRAINED
            self._cond.wait_for(lambda: self._state == DRAINED)
            return DrainResponse(state=DRAINED, in_flight=self._in_flight)

    def acquire(self) -> None:
        """Admit one inspection and count it, or reject a post-flip arrival.

        The state check and the counter increment are one atomic step: a
        request is either fully counted before any drain transition can be
        observed, or it is rejected with the state observed on arrival.
        """
        with self._cond:
            if self._state != ACCEPTING:
                raise InspectionRejectedWhileDraining(
                    DrainRejectionDetail(
                        code="SERVICE_UNAVAILABLE",
                        message=(
                            "The inspection service is draining or drained and "
                            "no longer accepts new inspection requests."
                        ),
                        state=self._state,
                        in_flight=self._in_flight,
                    )
                )
            self._in_flight += 1

    def release(self) -> None:
        """Release one admitted inspection slot.

        Must be called from the finally path of every admitted request. When
        the last slot closes while a drain is in progress the transition
        completes and every waiting /drain caller is woken.
        """
        with self._cond:
            self._in_flight -= 1
            if self._state == DRAINING and self._in_flight == 0:
                self._state = DRAINED
                self._cond.notify_all()

    def reset(self) -> None:
        """Restore the initial ACCEPTING state.

        Test support only: in production the DRAINED state is cleared solely
        by a process restart, which creates a brand-new coordinator.
        """
        with self._cond:
            self._state = ACCEPTING
            self._in_flight = 0


# Process-wide coordinator shared by the admission middleware and /drain.
coordinator = DrainCoordinator()
