"""Domain logic: day storage, candidate-edge vetting, frozen queries.

Vocabulary
----------
* A **day** is submitted either as 1440 integer readings ("minutes") or as
  counts per original bin ("bins": half-open intervals carrying their
  original edges).
* A **query** takes a date range plus candidate band edges.  Every candidate
  that would cut through a non-empty stored bin of a bins-day anywhere in
  the range is discarded and reported with the offending day and bin.
  Per-day per-band minute counts are then computed under the surviving
  edges and frozen.
* A reading exactly on an edge belongs to the higher band, i.e. bands are
  ``[lower, upper)`` and the lowest band is ``(-inf, first)``.

Every function takes an open connection; the API layer owns connections and
transactions are opened here at the granularity of one operation.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from bisect import bisect_right
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from .db import read_txn, write_txn

MINUTES_PER_DAY = 1440
MAX_RANGE_DAYS = 3660
MAX_BOUNDARIES = 100
MAX_PAGE_SIZE = 366
MIN_LEVEL = -100
MAX_LEVEL = 300
MIN_BOUNDARY = -1000
MAX_BOUNDARY = 1000
SEVERED_LIST_LIMIT = 50


class ServiceError(Exception):
    """A request-level failure with a stable machine-readable code."""

    def __init__(self, status: int, code: str, message: str, **details: Any):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def iso(day: date) -> str:
    return day.isoformat()


def daterange(start: date, end: date) -> list[date]:
    return [date.fromordinal(n) for n in range(start.toordinal(), end.toordinal() + 1)]


# ---------------------------------------------------------------------------
# Submission normalisation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Interval:
    """A half-open band ``[lower, upper)`` with ``None`` meaning unbounded."""

    lower: int | None
    upper: int | None
    minutes: int

    def to_json(self) -> dict[str, Any]:
        return {"lower": self.lower, "upper": self.upper, "minutes": self.minutes}

    def contains_strictly(self, boundary: int) -> bool:
        """True if ``boundary`` lies strictly inside this non-empty bin."""
        if self.minutes <= 0:
            return False
        above_lower = self.lower is None or self.lower < boundary
        below_upper = self.upper is None or self.upper > boundary
        return above_lower and below_upper


@dataclass(frozen=True)
class NormalizedDay:
    source: str  # 'minutes' | 'bins'
    values: tuple[int, ...] | None = None
    intervals: tuple[Interval, ...] | None = None


def _check_int(value: Any, what: str, code: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ServiceError(422, code, f"{what} must be an integer")
    return value


def _check_level(value: Any, what: str) -> int:
    _check_int(value, what, "invalid_level")
    if not MIN_LEVEL <= value <= MAX_LEVEL:
        raise ServiceError(
            422,
            "invalid_level",
            f"{what} must be between {MIN_LEVEL} and {MAX_LEVEL}",
            value=value,
        )
    return value


def _check_boundary(value: Any, what: str) -> int:
    _check_int(value, what, "invalid_boundary")
    if not MIN_BOUNDARY <= value <= MAX_BOUNDARY:
        raise ServiceError(
            422,
            "invalid_boundary",
            f"{what} must be between {MIN_BOUNDARY} and {MAX_BOUNDARY}",
            value=value,
        )
    return value


def normalize_minutes(values: Iterable[int]) -> NormalizedDay:
    vals = [_check_level(v, f"minute {i}") for i, v in enumerate(values)]
    if len(vals) != MINUTES_PER_DAY:
        raise ServiceError(
            422,
            "wrong_minute_count",
            f"expected exactly {MINUTES_PER_DAY} minute values, got {len(vals)}",
            expected=MINUTES_PER_DAY,
            got=len(vals),
        )
    return NormalizedDay(source="minutes", values=tuple(vals))


def normalize_intervals(intervals: Iterable[Interval]) -> NormalizedDay:
    items = list(intervals)
    if not items:
        raise ServiceError(422, "empty_bins", "at least one bin is required")
    if len(items) > MAX_BOUNDARIES + 1:
        raise ServiceError(
            422, "too_many_bins", f"at most {MAX_BOUNDARIES + 1} bins are allowed"
        )

    total = 0
    for i, item in enumerate(items):
        _check_int(item.minutes, f"bin {i} minutes", "invalid_bin_minutes")
        if not 0 <= item.minutes <= MINUTES_PER_DAY:
            raise ServiceError(
                422,
                "invalid_bin_minutes",
                f"bin {i} minutes must be between 0 and {MINUTES_PER_DAY}",
                bin=i,
            )
        if item.lower is not None:
            _check_boundary(item.lower, f"bin {i} lower")
        if item.upper is not None:
            _check_boundary(item.upper, f"bin {i} upper")
        if item.lower is not None and item.upper is not None and item.lower >= item.upper:
            raise ServiceError(
                422, "invalid_bin_range", f"bin {i} lower must be below upper", bin=i
            )
        total += item.minutes

    if items[0].lower is not None:
        raise ServiceError(
            422,
            "bins_do_not_cover_day",
            "the first bin must be open at the bottom (lower = null)",
        )
    if items[-1].upper is not None:
        raise ServiceError(
            422,
            "bins_do_not_cover_day",
            "the last bin must be open at the top (upper = null)",
        )
    for i in range(1, len(items)):
        if items[i].lower != items[i - 1].upper:
            raise ServiceError(
                422,
                "bins_not_contiguous",
                f"bin {i} must start exactly where bin {i - 1} ends",
                previous_upper=items[i - 1].upper,
                lower=items[i].lower,
            )
    if total != MINUTES_PER_DAY:
        raise ServiceError(
            422,
            "bin_total_mismatch",
            f"bin minutes must sum to {MINUTES_PER_DAY}, got {total}",
            expected=MINUTES_PER_DAY,
            got=total,
        )
    return NormalizedDay(source="bins", intervals=tuple(items))


def intervals_from_boundaries(boundaries: list[int], counts: list[int]) -> list[Interval]:
    """Build contiguous bins from ``n`` original edges and ``n + 1`` counts."""
    for i, boundary in enumerate(boundaries):
        _check_boundary(boundary, f"boundary {i}")
    for i in range(1, len(boundaries)):
        if boundaries[i] <= boundaries[i - 1]:
            raise ServiceError(
                422,
                "boundaries_not_ascending",
                "original boundaries must be strictly ascending",
                bin=i,
            )
    if len(counts) != len(boundaries) + 1:
        raise ServiceError(
            422,
            "count_mismatch",
            "counts must have exactly one more entry than boundaries",
            boundaries=len(boundaries),
            counts=len(counts),
        )
    edges: list[int | None] = [None, *boundaries, None]
    return [
        Interval(lower=edges[i], upper=edges[i + 1], minutes=counts[i])
        for i in range(len(edges) - 1)
    ]


# ---------------------------------------------------------------------------
# Day storage
# ---------------------------------------------------------------------------

FINGERPRINT_SCHEME = "v1"


def _fingerprint(
    source: str,
    values: Iterable[int] | None,
    intervals: Iterable[Interval] | None,
) -> str:
    """Content hash of one day: source plus the complete content.

    The canonical JSON covers everything downstream considers "the same
    day", so two submissions are interchangeable exactly when their
    fingerprints match.  Equivalent spellings of a bins-day (explicit
    intervals vs. boundaries+counts) normalise to the same intervals and
    therefore share one fingerprint.
    """
    if source == "minutes":
        body: dict[str, Any] = {"source": "minutes", "values": list(values or ())}
    else:
        body = {
            "source": "bins",
            "intervals": [b.to_json() for b in intervals or ()],
        }
    canonical = json.dumps(body, separators=(",", ":"), sort_keys=True)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"{FINGERPRINT_SCHEME}:{digest}"


def fingerprint_normalized(norm: NormalizedDay) -> str:
    return _fingerprint(norm.source, norm.values, norm.intervals)


def _fingerprint_stored(conn: sqlite3.Connection, day_id: int, source: str) -> str:
    """Fingerprint of an existing row, derived from its actual content.

    Rows written before schema v2 have no stored fingerprint; their first
    comparison must be against what is really in ``reading``/``bin_interval``,
    so the hash is computed from those rows (the caller then backfills it).
    """
    if source == "minutes":
        values = [
            int(r["value"])
            for r in conn.execute(
                "SELECT value FROM reading WHERE day_id = ? ORDER BY minute",
                (day_id,),
            )
        ]
        return _fingerprint("minutes", values, None)
    return _fingerprint("bins", None, _load_intervals(conn, day_id))


def _insert_details(conn: sqlite3.Connection, day_id: int, norm: NormalizedDay) -> None:
    if norm.source == "minutes":
        conn.executemany(
            "INSERT INTO reading (day_id, minute, value) VALUES (?, ?, ?)",
            [(day_id, i, v) for i, v in enumerate(norm.values or ())],
        )
    else:
        conn.executemany(
            "INSERT INTO bin_interval (day_id, idx, lower, upper, minutes)"
            " VALUES (?, ?, ?, ?, ?)",
            [
                (day_id, i, b.lower, b.upper, b.minutes)
                for i, b in enumerate(norm.intervals or ())
            ],
        )


def upsert_day(conn: sqlite3.Connection, day: date, norm: NormalizedDay) -> dict:
    """Store one day, deduplicating exact re-submissions.

    The comparison and the write share one immediate transaction, so
    concurrent submissions serialise and the outcome matches commit order:

    * same source and complete content -> the stored revision is returned
      unchanged and nothing is rewritten (``deduplicated: true``);
    * changed content or source -> the day is atomically replaced and its
      revision bumped, all-or-nothing, never half a day.
    """
    day_iso = iso(day)
    now = utcnow()
    new_fp = fingerprint_normalized(norm)
    with write_txn(conn):
        row = conn.execute(
            "SELECT day_id, revision, source, updated_at, fingerprint FROM day"
            " WHERE date = ?",
            (day_iso,),
        ).fetchone()
        if row is not None:
            day_id = int(row["day_id"])
            stored_fp = row["fingerprint"]
            if stored_fp is None:
                # Pre-v2 row without a fingerprint: compare against the actual
                # stored content, then backfill so later checks are cheap.
                stored_fp = _fingerprint_stored(conn, day_id, row["source"])
                conn.execute(
                    "UPDATE day SET fingerprint = ? WHERE day_id = ?",
                    (stored_fp, day_id),
                )
            if stored_fp == new_fp:
                return {
                    "date": day_iso,
                    "revision": int(row["revision"]),
                    "source": row["source"],
                    "minutes_total": MINUTES_PER_DAY,
                    "updated_at": row["updated_at"],
                    "deduplicated": True,
                }
            revision = int(row["revision"]) + 1
            conn.execute(
                "UPDATE day SET revision = ?, source = ?, fingerprint = ?,"
                " updated_at = ? WHERE day_id = ?",
                (revision, norm.source, new_fp, now, day_id),
            )
            conn.execute("DELETE FROM reading WHERE day_id = ?", (day_id,))
            conn.execute("DELETE FROM bin_interval WHERE day_id = ?", (day_id,))
        else:
            cur = conn.execute(
                "INSERT INTO day (date, revision, source, fingerprint,"
                " created_at, updated_at) VALUES (?, 1, ?, ?, ?, ?)",
                (day_iso, norm.source, new_fp, now, now),
            )
            day_id = int(cur.lastrowid)
            revision = 1
        _insert_details(conn, day_id, norm)
    return {
        "date": day_iso,
        "revision": revision,
        "source": norm.source,
        "minutes_total": MINUTES_PER_DAY,
        "updated_at": now,
        "deduplicated": False,
    }


def _load_intervals(conn: sqlite3.Connection, day_id: int) -> list[Interval]:
    rows = conn.execute(
        "SELECT lower, upper, minutes FROM bin_interval WHERE day_id = ? ORDER BY idx",
        (day_id,),
    ).fetchall()
    return [
        Interval(lower=r["lower"], upper=r["upper"], minutes=int(r["minutes"]))
        for r in rows
    ]


def day_payload(conn: sqlite3.Connection, day: date, station: str) -> dict:
    with read_txn(conn):
        row = conn.execute(
            "SELECT day_id, revision, source, updated_at FROM day WHERE date = ?",
            (iso(day),),
        ).fetchone()
        if row is None:
            raise ServiceError(
                404, "day_not_found", f"no data stored for {iso(day)}", date=iso(day)
            )
        payload = {
            "station": station,
            "date": iso(day),
            "revision": int(row["revision"]),
            "source": row["source"],
            "minutes_total": MINUTES_PER_DAY,
            "updated_at": row["updated_at"],
            "values": None,
            "intervals": None,
        }
        if row["source"] == "minutes":
            payload["values"] = [
                int(r["value"])
                for r in conn.execute(
                    "SELECT value FROM reading WHERE day_id = ? ORDER BY minute",
                    (row["day_id"],),
                )
            ]
        else:
            payload["intervals"] = [
                b.to_json() for b in _load_intervals(conn, int(row["day_id"]))
            ]
        return payload


def list_days(conn: sqlite3.Connection, start: date, end: date) -> list[dict]:
    with read_txn(conn):
        rows = conn.execute(
            "SELECT date, revision, source, updated_at FROM day"
            " WHERE date BETWEEN ? AND ? ORDER BY date",
            (iso(start), iso(end)),
        ).fetchall()
    return [
        {
            "date": r["date"],
            "revision": int(r["revision"]),
            "source": r["source"],
            "updated_at": r["updated_at"],
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Bands and counts
# ---------------------------------------------------------------------------


def bin_definitions(boundaries: list[int]) -> list[dict]:
    """Bands implied by the surviving edges, lowest first."""
    defs = []
    for i in range(len(boundaries) + 1):
        lower = boundaries[i - 1] if i > 0 else None
        upper = boundaries[i] if i < len(boundaries) else None
        if lower is None and upper is None:
            label = "(-inf, +inf)"
        elif lower is None:
            label = f"<{upper}"
        elif upper is None:
            label = f">={lower}"
        else:
            label = f"[{lower},{upper})"
        defs.append({"index": i, "lower": lower, "upper": upper, "label": label})
    return defs


def counts_from_minutes(values: Iterable[int], boundaries: list[int]) -> list[int]:
    """Equal to an edge goes to the higher band (``value >= edge``).

    ``bisect_right`` returns the number of edges ``<= value``, which is
    exactly the index of the band a reading on an edge falls into.
    """
    counts = [0] * (len(boundaries) + 1)
    for value in values:
        counts[bisect_right(boundaries, value)] += 1
    return counts


def counts_from_intervals(intervals: list[Interval], boundaries: list[int]) -> list[int]:
    counts = [0] * (len(boundaries) + 1)
    for interval in intervals:
        if interval.minutes == 0:
            continue
        # Callers must pass only vetted edges.  Checking this is cheap and
        # turns a silent miscount into a loud failure.
        if any(interval.contains_strictly(b) for b in boundaries):
            raise ValueError(f"edge splits stored bin {interval!r}")
        # No surviving edge lies strictly inside a non-empty stored bin, so
        # each non-empty bin sits wholly inside exactly one target band.
        index = 0 if interval.lower is None else bisect_right(boundaries, interval.lower)
        counts[index] += interval.minutes
    return counts


def _reject_candidates(
    candidates: list[int], bins_days: list[tuple[str, list[Interval]]]
) -> tuple[list[int], list[dict]]:
    """Discard candidates that split a non-empty stored bin; keep the rest.

    A bins-day only knows counts per original bin, so an edge inside such a
    bin cannot be honoured.  Each rejection names every offending day and the
    original bin (with its minutes); the per-rejection list is capped and
    flagged with ``severed_truncated`` plus ``severed_total``.

    Only ``bins`` days constrain the edges: a day submitted as minutes carries
    its full resolution, so any edge can be computed for it exactly.
    """
    rejected: list[dict] = []
    kept: list[int] = []
    for boundary in candidates:
        hits: list[dict] = []
        total = 0
        for day_iso, intervals in bins_days:
            for interval in intervals:
                if not interval.contains_strictly(boundary):
                    continue
                total += 1
                if len(hits) < SEVERED_LIST_LIMIT:
                    hits.append(
                        {"date": day_iso, "original_bin": interval.to_json()}
                    )
        if total:
            rejected.append(
                {
                    "boundary": boundary,
                    "reason": "would_split_original_bin",
                    "severed_total": total,
                    "severed_truncated": total > len(hits),
                    "severed": hits,
                }
            )
        else:
            kept.append(boundary)
    return kept, rejected


# ---------------------------------------------------------------------------
# Queries: freeze edges + per-day data, then page through the snapshot
# ---------------------------------------------------------------------------


def _snapshot_rows(conn: sqlite3.Connection, start: date, end: date):
    """Load every stored day in range, one day at a time.

    Readings are streamed per day through a cursor rather than materialised
    for the whole range, so a multi-year query stays within bounded memory.
    """
    rows = conn.execute(
        "SELECT day_id, date, revision, source, updated_at FROM day"
        " WHERE date BETWEEN ? AND ? ORDER BY date",
        (iso(start), iso(end)),
    ).fetchall()
    days: list[tuple[sqlite3.Row, Iterable[int] | None, list[Interval] | None]] = []
    bins_days: list[tuple[str, list[Interval]]] = []
    for row in rows:
        if row["source"] == "minutes":
            values: Iterable[int] = (
                int(r["value"])
                for r in conn.execute(
                    "SELECT value FROM reading WHERE day_id = ? ORDER BY minute",
                    (row["day_id"],),
                )
            )
            days.append((row, values, None))
        else:
            intervals = _load_intervals(conn, int(row["day_id"]))
            days.append((row, None, intervals))
            bins_days.append((row["date"], intervals))
    return days, bins_days


def create_query(
    conn: sqlite3.Connection,
    *,
    start: date,
    end: date,
    candidate_boundaries: list[int],
    page_size: int,
    station: str,
) -> dict:
    """Vet edges and freeze the whole result in one transaction."""
    if start > end:
        raise ServiceError(422, "invalid_range", "start_date must not be after end_date")
    span = (end - start).days + 1
    if span > MAX_RANGE_DAYS:
        raise ServiceError(
            422,
            "range_too_long",
            f"date range may span at most {MAX_RANGE_DAYS} days",
            days=span,
        )
    if len(candidate_boundaries) > MAX_BOUNDARIES:
        raise ServiceError(
            422,
            "too_many_boundaries",
            f"at most {MAX_BOUNDARIES} candidate boundaries are allowed",
        )
    if not 1 <= page_size <= MAX_PAGE_SIZE:
        raise ServiceError(
            422,
            "invalid_page_size",
            f"page_size must be between 1 and {MAX_PAGE_SIZE}",
            page_size=page_size,
        )

    requested: list[int] = []
    duplicates: list[int] = []
    for boundary in candidate_boundaries:
        _check_boundary(boundary, "candidate boundary")
        if boundary in requested:
            if boundary not in duplicates:
                duplicates.append(boundary)
        else:
            requested.append(boundary)

    query_id = "q-" + uuid.uuid4().hex
    created_at = utcnow()

    with write_txn(conn):
        days, bins_days = _snapshot_rows(conn, start, end)
        available = {row["date"] for row, _, _ in days}

        effective, rejected = _reject_candidates(requested, bins_days)
        effective = sorted(effective)
        definitions = bin_definitions(effective)

        all_dates = [iso(d) for d in daterange(start, end)]
        missing = [d for d in all_dates if d not in available]

        day_rows = []
        for row, values, intervals in days:
            if values is not None:
                counts = counts_from_minutes(values, effective)
            else:
                counts = counts_from_intervals(intervals or [], effective)
            day_rows.append(
                (
                    query_id,
                    row["date"],
                    int(row["day_id"]),
                    int(row["revision"]),
                    row["source"],
                    json.dumps(counts),
                    sum(counts),
                )
            )

        pages = [
            all_dates[i : i + page_size] for i in range(0, len(all_dates), page_size)
        ]

        conn.execute(
            "INSERT INTO query_log (query_id, created_at, start_date, end_date,"
            " requested_boundaries, ignored_duplicates, effective_boundaries,"
            " bin_definitions, rejected, missing_dates, page_size, days_total,"
            " pages_total) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                query_id,
                created_at,
                iso(start),
                iso(end),
                json.dumps(requested),
                json.dumps(duplicates),
                json.dumps(effective),
                json.dumps(definitions),
                json.dumps(rejected),
                json.dumps(missing),
                page_size,
                len(all_dates),
                len(pages),
            ),
        )
        conn.executemany(
            "INSERT INTO query_day (query_id, date, day_id, revision, source,"
            " counts, minutes_total) VALUES (?, ?, ?, ?, ?, ?, ?)",
            day_rows,
        )
        conn.executemany(
            "INSERT INTO query_page (query_id, page_no, dates) VALUES (?, ?, ?)",
            [(query_id, i + 1, json.dumps(dates)) for i, dates in enumerate(pages)],
        )

    return {
        "station": station,
        "query_id": query_id,
        "created_at": created_at,
        "start_date": iso(start),
        "end_date": iso(end),
        "requested_boundaries": requested,
        "ignored_duplicate_boundaries": duplicates,
        "effective_boundaries": effective,
        "rejected": rejected,
        "bins": definitions,
        "missing_dates": missing,
        "page_size": page_size,
        "pages_total": len(pages),
        "days_total": len(all_dates),
        "days_available": len(all_dates) - len(missing),
        "data_version": f"snapshot frozen at {created_at}",
    }


def _load_query_row(conn: sqlite3.Connection, query_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM query_log WHERE query_id = ?", (query_id,)).fetchone()
    if row is None:
        raise ServiceError(
            404, "query_not_found", f"unknown query_id {query_id!r}", query_id=query_id
        )
    return row


def _query_meta(row: sqlite3.Row, station: str) -> dict:
    missing = json.loads(row["missing_dates"])
    return {
        "station": station,
        "query_id": row["query_id"],
        "created_at": row["created_at"],
        "start_date": row["start_date"],
        "end_date": row["end_date"],
        "requested_boundaries": json.loads(row["requested_boundaries"]),
        "ignored_duplicate_boundaries": json.loads(row["ignored_duplicates"]),
        "effective_boundaries": json.loads(row["effective_boundaries"]),
        "rejected": json.loads(row["rejected"]),
        "bins": json.loads(row["bin_definitions"]),
        "missing_dates": missing,
        "page_size": int(row["page_size"]),
        "pages_total": int(row["pages_total"]),
        "days_total": int(row["days_total"]),
        "days_available": int(row["days_total"]) - len(missing),
        "data_version": f"snapshot frozen at {row['created_at']}",
    }


def get_query(conn: sqlite3.Connection, query_id: str, station: str) -> dict:
    with read_txn(conn):
        row = _load_query_row(conn, query_id)
        meta = _query_meta(row, station)
        meta["pages"] = [
            {"page_no": int(p["page_no"]), "dates": json.loads(p["dates"])}
            for p in conn.execute(
                "SELECT page_no, dates FROM query_page WHERE query_id = ?"
                " ORDER BY page_no",
                (query_id,),
            )
        ]
        return meta


def get_page(conn: sqlite3.Connection, query_id: str, page_no: int, station: str) -> dict:
    with read_txn(conn):
        row = _load_query_row(conn, query_id)
        page_row = conn.execute(
            "SELECT dates FROM query_page WHERE query_id = ? AND page_no = ?",
            (query_id, page_no),
        ).fetchone()
        if page_row is None:
            raise ServiceError(
                404,
                "page_not_found",
                f"query {query_id} has no page {page_no}",
                query_id=query_id,
                page_no=page_no,
                pages_total=int(row["pages_total"]),
            )
        dates = json.loads(page_row["dates"])
        placeholders = ",".join("?" for _ in dates)
        stored = {
            r["date"]: r
            for r in conn.execute(
                "SELECT date, revision, source, counts, minutes_total"
                f" FROM query_day WHERE query_id = ? AND date IN ({placeholders})",
                (query_id, *dates),
            )
        }
        days = []
        for day_iso in dates:
            day = stored.get(day_iso)
            if day is None:
                days.append(
                    {
                        "date": day_iso,
                        "available": False,
                        "revision": None,
                        "source": None,
                        "counts": None,
                        "minutes_total": None,
                    }
                )
            else:
                days.append(
                    {
                        "date": day_iso,
                        "available": True,
                        "revision": int(day["revision"]),
                        "source": day["source"],
                        "counts": json.loads(day["counts"]),
                        "minutes_total": int(day["minutes_total"]),
                    }
                )
        return {
            "station": station,
            "query_id": query_id,
            "page_no": page_no,
            "page_size": int(row["page_size"]),
            "pages_total": int(row["pages_total"]),
            "days_total": int(row["days_total"]),
            "effective_boundaries": json.loads(row["effective_boundaries"]),
            "bins": json.loads(row["bin_definitions"]),
            "days": days,
        }


def get_query_day(
    conn: sqlite3.Connection, query_id: str, day: date, station: str
) -> dict:
    with read_txn(conn):
        row = _load_query_row(conn, query_id)
        day_iso = iso(day)
        if not (row["start_date"] <= day_iso <= row["end_date"]):
            raise ServiceError(
                404,
                "date_outside_query",
                f"{day_iso} is outside the query range",
                query_id=query_id,
                date=day_iso,
            )
        stored = conn.execute(
            "SELECT date, revision, source, counts, minutes_total FROM query_day"
            " WHERE query_id = ? AND date = ?",
            (query_id, day_iso),
        ).fetchone()
        if stored is None:
            raise ServiceError(
                404,
                "day_missing_in_query",
                f"{day_iso} had no data when the query was frozen",
                query_id=query_id,
                date=day_iso,
            )
        return {
            "station": station,
            "query_id": query_id,
            "date": stored["date"],
            "available": True,
            "revision": int(stored["revision"]),
            "source": stored["source"],
            "counts": json.loads(stored["counts"]),
            "minutes_total": int(stored["minutes_total"]),
            "effective_boundaries": json.loads(row["effective_boundaries"]),
            "bins": json.loads(row["bin_definitions"]),
        }
