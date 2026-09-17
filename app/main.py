"""FastAPI entrypoint for the passage light-strip inspector."""
from __future__ import annotations

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app import __version__
from app.drain import DrainCoordinator, InspectionAdmissionMiddleware
from app.inspector import inspect
from app.models import (
    DrainResponse,
    DrainRejectionResponse,
    ErrorDetail,
    ErrorResponse,
    InspectionRequest,
    InspectionResponse,
    Issue,
)
from app.validation import InputRejected, validate_payload


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


def create_app(coordinator: DrainCoordinator | None = None) -> FastAPI:
    """Build the application around one drain coordinator.

    The coordinator owns the ACCEPTING -> DRAINING -> DRAINED lifecycle
    shared by the admission middleware and the routes. Tests pass a fresh
    coordinator per app so draining one instance never leaks into another;
    uvicorn serves the module-level `app` below.
    """
    coordinator = coordinator if coordinator is not None else DrainCoordinator()

    app = FastAPI(
        title="Passage Light-Strip Inspector",
        version=__version__,
        summary="Verifies that every exit light is energized by the single power source.",
    )
    app.add_middleware(InspectionAdmissionMiddleware, coordinator=coordinator)
    app.add_exception_handler(InputRejected, handle_input_rejected)
    app.add_exception_handler(RequestValidationError, handle_schema_error)

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
                "description": "The service is draining and rejects new inspections.",
            },
        },
    )
    def inspect_endpoint(payload: InspectionRequest) -> InspectionResponse:
        validate_payload(payload.grid, payload.labels)
        return inspect(payload.grid, payload.labels)

    @app.post(
        "/drain",
        response_model=DrainResponse,
        summary="Stop admitting inspections and wait for in-flight ones to finish.",
    )
    async def drain_endpoint() -> DrainResponse:
        return DrainResponse(state=await coordinator.drain())

    return app


app = create_app()
