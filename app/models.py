"""Pydantic schemas for the inspection API."""
from __future__ import annotations

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


# Lifecycle of the single-process drain coordinator.
#   ACCEPTING - inspections are admitted and counted as in-flight
#   DRAINING  - POST /drain has been observed; new inspections get a 503 while
#               already-admitted ones run to completion
#   DRAINED   - the in-flight count reached zero; terminal until process restart
DrainState = Literal["ACCEPTING", "DRAINING", "DRAINED"]


class DrainResponse(BaseModel):
    """Shared contract for POST /drain: returned once every old check finished."""

    status: Literal["DRAINED"] = "DRAINED"
    state: DrainState
    in_flight: int = Field(ge=0)


class DrainRejectionDetail(BaseModel):
    """Structured 503 payload naming the drain state observed on arrival."""

    code: Literal["SERVICE_UNAVAILABLE"]
    message: str
    state: DrainState
    in_flight: int = Field(ge=0)


class DrainRejectionResponse(BaseModel):
    detail: DrainRejectionDetail
