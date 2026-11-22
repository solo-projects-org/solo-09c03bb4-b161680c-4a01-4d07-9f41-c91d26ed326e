"""Pydantic request/response schemas (also drives the Swagger demo)."""

from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

LEVEL_MIN = -100
LEVEL_MAX = 300
BOUNDARY_MIN = -1000
BOUNDARY_MAX = 1000
MINUTES_PER_DAY = 1440
PAGE_SIZE_MAX = 366
MAX_BOUNDARIES = 100

Level = Annotated[int, Field(strict=True, ge=LEVEL_MIN, le=LEVEL_MAX)]
Boundary = Annotated[int, Field(strict=True, ge=BOUNDARY_MIN, le=BOUNDARY_MAX)]
Minutes = Annotated[int, Field(strict=True, ge=0, le=MINUTES_PER_DAY)]
PageSize = Annotated[int, Field(strict=True, ge=1, le=PAGE_SIZE_MAX)]


# ---------------------------------------------------------------------------
# Day submission
# ---------------------------------------------------------------------------


class BinIntervalIn(BaseModel):
    """One original bin, half-open ``[lower, upper)``; null = unbounded."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {"lower": None, "upper": 55, "minutes": 210},
                {"lower": 55, "upper": 65, "minutes": 690},
                {"lower": 65, "upper": None, "minutes": 540},
            ]
        }
    )

    lower: Boundary | None = Field(
        default=None, description="Inclusive lower edge; null means -infinity"
    )
    upper: Boundary | None = Field(
        default=None, description="Exclusive upper edge; null means +infinity"
    )
    minutes: Minutes = Field(description="Minutes that fell into this bin")


class MinutesDayIn(BaseModel):
    mode: Literal["minutes"]
    values: list[Level] = Field(
        description=(
            f"Exactly {MINUTES_PER_DAY} integer dB readings, minute 0 through 1439."
        ),
        examples=[[*([55] * 600), *([62] * 600), *([70] * 240)]],
    )


class BinsDayIn(BaseModel):
    """A day submitted as counts per original bin.

    Give either ``intervals`` (explicit original edges) or the compact
    ``boundaries`` + ``counts`` pair (edges implied, first/last bins open).
    """

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "mode": "bins",
                    "intervals": [
                        {"lower": None, "upper": 55, "minutes": 210},
                        {"lower": 55, "upper": 65, "minutes": 690},
                        {"lower": 65, "upper": None, "minutes": 540},
                    ],
                },
                {
                    "mode": "bins",
                    "boundaries": [55, 65],
                    "counts": [210, 690, 540],
                },
            ]
        }
    )

    mode: Literal["bins"]
    intervals: list[BinIntervalIn] | None = None
    boundaries: list[Boundary] | None = Field(
        default=None, description="Ascending original edges, all interior"
    )
    counts: list[Minutes] | None = Field(
        default=None, description="One more count than boundaries"
    )

    @model_validator(mode="after")
    def _exactly_one_representation(self):
        has_intervals = self.intervals is not None
        has_edges = self.boundaries is not None or self.counts is not None
        if has_intervals and has_edges:
            raise ValueError("use either intervals or boundaries+counts, not both")
        if not has_intervals and not has_edges:
            raise ValueError("provide intervals or boundaries+counts")
        if has_edges and (self.boundaries is None or self.counts is None):
            raise ValueError("boundaries and counts must be provided together")
        return self


DaySubmission = Annotated[
    MinutesDayIn | BinsDayIn, Field(discriminator="mode")
]


# ---------------------------------------------------------------------------
# Day responses
# ---------------------------------------------------------------------------


class BinIntervalOut(BaseModel):
    lower: int | None
    upper: int | None
    minutes: int


class DayWriteOut(BaseModel):
    station: str
    date: str
    revision: int
    source: Literal["minutes", "bins"]
    minutes_total: int
    updated_at: str
    deduplicated: bool = Field(
        description=(
            "true when the same source and complete content were already "
            "stored: nothing was rewritten and the stored revision is "
            "returned unchanged"
        )
    )


class DayOut(BaseModel):
    station: str
    date: str
    revision: int
    source: Literal["minutes", "bins"]
    minutes_total: int
    updated_at: str
    values: list[int] | None
    intervals: list[BinIntervalOut] | None


class DaySummaryOut(BaseModel):
    date: str
    revision: int
    source: Literal["minutes", "bins"]
    updated_at: str


class DayListOut(BaseModel):
    station: str
    start_date: str
    end_date: str
    days: list[DaySummaryOut]


# ---------------------------------------------------------------------------
# Query creation
# ---------------------------------------------------------------------------


class QueryIn(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "start_date": "2023-01-01",
                    "end_date": "2025-12-31",
                    "boundaries": [55, 60, 65, 70],
                    "page_size": 31,
                }
            ]
        }
    )

    start_date: date = Field(description="First day of the range (inclusive)")
    end_date: date = Field(description="Last day of the range (inclusive)")
    boundaries: list[Boundary] = Field(
        default_factory=list,
        max_length=MAX_BOUNDARIES,
        description=(
            "Candidate band edges in dB. Each edge that would cut through a "
            "non-empty original bin on any day in range is discarded."
        ),
    )
    page_size: PageSize = Field(
        default=31, description=f"Days per page, 1..{PAGE_SIZE_MAX}"
    )


class BinOut(BaseModel):
    index: int
    lower: int | None
    upper: int | None
    label: str


class SeveredHitOut(BaseModel):
    date: str
    original_bin: BinIntervalOut


class RejectionOut(BaseModel):
    boundary: int
    reason: str
    severed_total: int
    severed_truncated: bool
    severed: list[SeveredHitOut]


class QuerySummaryOut(BaseModel):
    station: str
    query_id: str
    created_at: str
    start_date: str
    end_date: str
    requested_boundaries: list[int]
    effective_boundaries: list[int]
    ignored_duplicate_boundaries: list[int]
    rejected: list[RejectionOut]
    bins: list[BinOut]
    missing_dates: list[str]
    page_size: int
    pages_total: int
    days_total: int
    days_available: int
    data_version: str


class QueryCreatedOut(QuerySummaryOut):
    pass


class PageIndexOut(BaseModel):
    page_no: int
    dates: list[str]


class QueryOut(QuerySummaryOut):
    pages: list[PageIndexOut]


class PageDayOut(BaseModel):
    date: str
    available: bool
    revision: int | None
    source: Literal["minutes", "bins"] | None
    counts: list[int] | None
    minutes_total: int | None


class PageOut(BaseModel):
    station: str
    query_id: str
    page_no: int
    page_size: int
    pages_total: int
    days_total: int
    effective_boundaries: list[int]
    bins: list[BinOut]
    days: list[PageDayOut]


class QueryDayOut(BaseModel):
    station: str
    query_id: str
    date: str
    available: bool
    revision: int
    source: Literal["minutes", "bins"]
    counts: list[int]
    minutes_total: int
    effective_boundaries: list[int]
    bins: list[BinOut]


class HealthOut(BaseModel):
    status: str
    station: str
    database: str
    schema_version: int
    days_stored: int
