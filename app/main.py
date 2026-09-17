"""FastAPI entrypoint for the passage light-strip inspector."""
from __future__ import annotations

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app import __version__
from app.drain import (
    InspectionRejectedWhileDraining,
    coordinator as drain_coordinator,
)
from app.inspector import inspect
from app.models import (
    DrainRejectionResponse,
    DrainResponse,
    ErrorDetail,
    ErrorResponse,
    InspectionRequest,
    InspectionResponse,
    Issue,
)
from app.validation import InputRejected, validate_payload

app = FastAPI(
    title="Passage Light-Strip Inspector",
    version=__version__,
    summary="Verifies that every exit light is energized by the single power source.",
)


@app.exception_handler(InputRejected)
async def handle_input_rejected(_: Request, exc: InputRejected) -> JSONResponse:
    detail = ErrorDetail(
        code="INPUT_REJECTED",
        message="The diagram violates the input contract; see issues for every problem found.",
        issues=exc.issues,
    )
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": detail.model_dump()},
    )


@app.exception_handler(RequestValidationError)
async def handle_schema_error(_: Request, exc: RequestValidationError) -> JSONResponse:
    issues = [
        Issue(
            code="SCHEMA_ERROR",
            message=f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}",
        )
        for error in exc.errors()
    ]
    detail = ErrorDetail(
        code="SCHEMA_ERROR",
        message="The request body does not match the expected JSON schema.",
        issues=issues,
    )
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": detail.model_dump()},
    )


class DrainAdmissionMiddleware:
    """Admission gate for POST /inspect backed by the drain coordinator.

    Pure ASGI so the release sits in a plain try/finally around the wrapped
    app: success, a 422 produced inside the endpoint, an unhandled exception
    and request-task cancellation all take the finally path and release the
    in-flight slot, which keeps an in-progress drain from waiting forever.
    Requests that arrive after the ACCEPTING -> DRAINING transition get a
    structured 503 naming the state observed on arrival; requests admitted
    before it run untouched and complete with their usual PASS/FAIL/422.
    """

    def __init__(self, app: ASGIApp, coordinator=drain_coordinator) -> None:
        self.app = app
        self.coordinator = coordinator

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") != "POST" or scope.get("path") != "/inspect":
            await self.app(scope, receive, send)
            return

        try:
            self.coordinator.acquire()
        except InspectionRejectedWhileDraining as exc:
            response = JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={"detail": exc.detail.model_dump()},
            )
            await response(scope, receive, send)
            return

        try:
            await self.app(scope, receive, send)
        finally:
            self.coordinator.release()


app.add_middleware(DrainAdmissionMiddleware)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post(
    "/inspect",
    response_model=InspectionResponse,
    response_model_exclude_none=True,
    responses={
        422: {
            "model": ErrorResponse,
            "description": "The diagram violates the input contract.",
        },
        503: {
            "model": DrainRejectionResponse,
            "description": "The service is draining or drained and admits no new inspections.",
        },
    },
)
def inspect_endpoint(payload: InspectionRequest) -> InspectionResponse:
    validate_payload(payload.grid, payload.labels)
    return inspect(payload.grid, payload.labels)


@app.post(
    "/drain",
    response_model=DrainResponse,
    summary="Drain in-flight inspections before a rolling update.",
)
def drain_endpoint() -> DrainResponse:
    """Stop admitting inspections and answer once all previously admitted
    POST /inspect calls have finished.

    Concurrent callers join the same transition and receive the same
    DRAINED result; the drained state is terminal until the process restarts.
    """
    return drain_coordinator.begin_drain()
