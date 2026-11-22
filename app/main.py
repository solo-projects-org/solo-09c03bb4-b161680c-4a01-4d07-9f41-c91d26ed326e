"""FastAPI surface for the single-station road-noise binning API.

Endpoints
---------
``PUT  /days/{date}``                    submit or fully replace one day
``GET  /days/{date}``                    read the stored day as submitted
``GET  /days?start_date=&end_date=``     list stored days in a range
``POST /queries``                        vet edges, freeze a query
``GET  /queries/{query_id}``             query metadata, bins, rejection report
``GET  /queries/{query_id}/pages/{n}``   page through the frozen snapshot
``GET  /queries/{query_id}/days/{date}`` one day of the frozen snapshot
``GET  /health``                         liveness + schema version

Every handler owns one SQLite connection for the duration of its request and
closes it afterwards.  Domain errors use a stable ``{"error": {...}}``
envelope; FastAPI validation errors are mapped onto the same shape.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Request
from fastapi import Path as PathParam
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from . import __version__, db, service
from .db import default_db_path, init_db
from .models import (
    DayListOut,
    DayOut,
    DaySubmission,
    DayWriteOut,
    HealthOut,
    MinutesDayIn,
    PageOut,
    QueryCreatedOut,
    QueryDayOut,
    QueryIn,
    QueryOut,
)

STATION = os.environ.get("NOISE_STATION", "station-01")
VALIDATION_PROBLEM_LIMIT = 10


def db_path() -> Path:
    """Resolved per call so tests and Compose can point NOISE_DB anywhere."""
    return Path(os.environ.get("NOISE_DB", str(default_db_path())))


def _conn() -> Iterator[sqlite3.Connection]:
    """Per-request connection, always closed."""
    conn = db.connect(db_path())
    try:
        yield conn
    finally:
        conn.close()


Conn = Annotated[sqlite3.Connection, Depends(_conn)]


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db(db_path())
    yield


app = FastAPI(
    title="Road Noise Bands (single station)",
    version=__version__,
    description=(
        "Store per-minute integer dB readings or per-day band counts for one "
        "road-noise station, then compare years under one consistent set of "
        "band edges. Candidate edges that would split an original band are "
        "rejected and reported with the offending day and original band. A "
        "query is a frozen snapshot: late submissions and corrections never "
        "change it, and paging resumes after restarts."
    ),
    lifespan=lifespan,
)


@app.exception_handler(service.ServiceError)
async def _service_error(_: Request, exc: service.ServiceError) -> JSONResponse:
    body = {"error": {"code": exc.code, "message": exc.message}}
    body["error"].update(exc.details)
    return JSONResponse(status_code=exc.status, content=body)


@app.exception_handler(RequestValidationError)
async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
    """Same error envelope as domain failures, plus the field-level detail."""
    problems = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error.get("loc", ()) if part != "body")
        problems.append({"field": location or "body", "message": error.get("msg", "")})
    total = len(problems)
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "invalid_request",
                "message": "request validation failed",
                "problems_total": total,
                "problems_truncated": total > VALIDATION_PROBLEM_LIMIT,
                "problems": problems[:VALIDATION_PROBLEM_LIMIT],
            }
        },
    )


@app.get("/health", response_model=HealthOut, tags=["ops"], summary="Liveness probe")
def health(conn: Conn) -> HealthOut:
    days = conn.execute("SELECT COUNT(*) AS n FROM day").fetchone()["n"]
    return HealthOut(
        status="ok",
        station=STATION,
        database=str(db_path()),
        schema_version=conn.execute("PRAGMA user_version").fetchone()[0],
        days_stored=int(days),
    )


@app.put(
    "/days/{day}",
    response_model=DayWriteOut,
    tags=["days"],
    summary="Submit or fully replace one day",
    description=(
        "Body is either ``{mode: 'minutes', values: [...]}`` (1440 integers) "
        "or ``{mode: 'bins', ...}`` with either explicit ``intervals`` or "
        "``boundaries`` + ``counts``. A repeated PUT fully replaces the day "
        "and increases its revision."
    ),
)
def put_day(
    submission: DaySubmission,
    day: Annotated[date, PathParam(description="ISO date, e.g. 2024-06-01")],
    conn: Conn,
) -> DayWriteOut:
    if isinstance(submission, MinutesDayIn):
        norm = service.normalize_minutes(submission.values)
    else:
        if submission.intervals is not None:
            intervals = [
                service.Interval(lower=b.lower, upper=b.upper, minutes=b.minutes)
                for b in submission.intervals
            ]
        else:
            intervals = service.intervals_from_boundaries(
                list(submission.boundaries or []), list(submission.counts or [])
            )
        norm = service.normalize_intervals(intervals)
    result = service.upsert_day(conn, day, norm)
    result["station"] = STATION
    return DayWriteOut(**result)


@app.get(
    "/days/{day}",
    response_model=DayOut,
    tags=["days"],
    summary="Read the day exactly as stored (latest revision)",
)
def get_day(day: date, conn: Conn) -> DayOut:
    return DayOut(**service.day_payload(conn, day, STATION))


@app.get(
    "/days",
    response_model=DayListOut,
    tags=["days"],
    summary="List stored days in a date range",
)
def list_days(start_date: date, end_date: date, conn: Conn) -> DayListOut:
    if start_date > end_date:
        raise service.ServiceError(
            422, "invalid_range", "start_date must not be after end_date"
        )
    return DayListOut(
        station=STATION,
        start_date=start_date.isoformat(),
        end_date=end_date.isoformat(),
        days=service.list_days(conn, start_date, end_date),
    )


@app.post(
    "/queries",
    response_model=QueryCreatedOut,
    status_code=201,
    tags=["queries"],
    summary="Create a frozen query: vet edges, count each day",
    description=(
        "Discards every candidate edge that would split a non-empty original "
        "band on any stored bins-day in range, reporting each such day and "
        "band. Counts are then frozen per day under the surviving edges; "
        "readings equal to an edge go to the higher band."
    ),
)
def create_query(body: QueryIn, conn: Conn) -> QueryCreatedOut:
    result = service.create_query(
        conn,
        start=body.start_date,
        end=body.end_date,
        candidate_boundaries=body.boundaries,
        page_size=body.page_size,
        station=STATION,
    )
    return QueryCreatedOut(**result)


@app.get(
    "/queries/{query_id}",
    response_model=QueryOut,
    tags=["queries"],
    summary="Metadata of a stored query (edges survive restarts)",
)
def get_query(query_id: str, conn: Conn) -> QueryOut:
    return QueryOut(**service.get_query(conn, query_id, STATION))


@app.get(
    "/queries/{query_id}/pages/{page_no}",
    response_model=PageOut,
    tags=["queries"],
    summary="Fetch one page of the frozen snapshot",
)
def get_page(
    query_id: str, page_no: Annotated[int, PathParam(ge=1)], conn: Conn
) -> PageOut:
    return PageOut(**service.get_page(conn, query_id, page_no, STATION))


@app.get(
    "/queries/{query_id}/days/{day}",
    response_model=QueryDayOut,
    tags=["queries"],
    summary="Fetch one day of the frozen snapshot",
)
def get_query_day(query_id: str, day: date, conn: Conn) -> QueryDayOut:
    return QueryDayOut(**service.get_query_day(conn, query_id, day, STATION))
