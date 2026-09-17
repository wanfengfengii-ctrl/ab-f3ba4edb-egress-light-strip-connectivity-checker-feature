"""Pydantic schemas for the inspection API."""
from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class InspectionRequest(BaseModel):
    """Request body: the wiring grid plus the exit-id label matrix."""

    model_config = ConfigDict(extra="forbid")

    grid: list[list[str]] = Field(
        description=(
            "Rectangular cell matrix, 1..200 rows by 1..200 columns. "
            "Cells: 'P' power source (exactly one), 'W' wire, 'E' exit light, 'X' empty."
        )
    )
    labels: list[list[str | None]] = Field(
        description=(
            "Matrix with the same shape as `grid`: a unique non-empty exit id "
            "at every 'E' cell, null everywhere else."
        )
    )


class Coordinate(BaseModel):
    """Zero-based cell coordinate."""

    row: int = Field(ge=0)
    col: int = Field(ge=0)


class Failure(BaseModel):
    """One unreachable exit plus the evidence coordinate for its component."""

    exit_id: str
    evidence: Coordinate


class InspectionResponse(BaseModel):
    """PASS when every exit is energized, otherwise FAIL with per-exit evidence."""

    result: Literal["PASS", "FAIL"]
    failures: list[Failure] | None = None


class Issue(BaseModel):
    """A single input-validation problem."""

    code: str
    message: str
    locations: list[Coordinate] = []


class ErrorDetail(BaseModel):
    """Structured rejection payload carried in the `detail` field."""

    code: str
    message: str
    issues: list[Issue] = []


class ErrorResponse(BaseModel):
    detail: ErrorDetail


class DrainState(str, Enum):
    """Lifecycle of the inspection-admission gate.

    ACCEPTING -> DRAINING -> DRAINED is one-way: once draining begins the
    service never resumes acceptance within the same process; only a
    restart returns it to ACCEPTING.
    """

    ACCEPTING = "ACCEPTING"
    DRAINING = "DRAINING"
    DRAINED = "DRAINED"


class DrainResponse(BaseModel):
    """POST /drain result. Every concurrent caller receives this same body
    once the last admitted inspection has finished."""

    state: Literal[DrainState.DRAINED]


_REJECTION_MESSAGES = {
    DrainState.DRAINING: (
        "The service is draining for a rolling update: new inspections are "
        "rejected while previously admitted ones finish."
    ),
    DrainState.DRAINED: (
        "The service is drained: new inspections are rejected and only a "
        "process restart resumes acceptance."
    ),
}


class DrainRejectionDetail(BaseModel):
    """Structured 503 body for inspections refused after draining began."""

    code: Literal["SERVICE_DRAINING", "SERVICE_DRAINED"]
    message: str
    state: DrainState

    @classmethod
    def for_state(cls, state: DrainState) -> "DrainRejectionDetail":
        """Build the rejection that matches the coordinator's current state."""
        if state is DrainState.ACCEPTING:
            raise ValueError("an accepting service does not reject inspections")
        return cls(
            code="SERVICE_DRAINED" if state is DrainState.DRAINED else "SERVICE_DRAINING",
            message=_REJECTION_MESSAGES[state],
            state=state,
        )


class DrainRejectionResponse(BaseModel):
    detail: DrainRejectionDetail
