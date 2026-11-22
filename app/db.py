"""SQLite schema and connection helpers.

One database file, one station.  ``PRAGMA journal_mode=WAL`` lets a paged
reader work while a day is being corrected.  Every write runs inside an
explicit ``BEGIN IMMEDIATE`` transaction, and a whole request (a day write or
the creation of a query snapshot) is one transaction, so it lands completely
or not at all.

Query snapshots are materialised: ``query_log`` holds the boundaries and the
rejection report, ``query_day`` the per-day counts, and ``query_page`` the
frozen page layout.  That is what makes paging resumable across corrections,
late submissions and restarts.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS day (
    day_id      INTEGER PRIMARY KEY,
    date        TEXT NOT NULL UNIQUE,        -- ISO date, sortable as text
    revision    INTEGER NOT NULL,
    source      TEXT NOT NULL,               -- 'minutes' | 'bins'
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

-- One row per minute of a minutes-submitted day.
CREATE TABLE IF NOT EXISTS reading (
    day_id  INTEGER NOT NULL REFERENCES day(day_id) ON DELETE CASCADE,
    minute  INTEGER NOT NULL CHECK (minute BETWEEN 0 AND 1439),
    value   INTEGER NOT NULL,
    PRIMARY KEY (day_id, minute)
) WITHOUT ROWID;

-- Bins of a bins-submitted day, as half-open intervals.  NULL lower/upper
-- means unbounded.  ``idx`` keeps submission order.
CREATE TABLE IF NOT EXISTS bin_interval (
    day_id     INTEGER NOT NULL REFERENCES day(day_id) ON DELETE CASCADE,
    idx        INTEGER NOT NULL,
    lower      INTEGER,
    upper      INTEGER,
    minutes    INTEGER NOT NULL CHECK (minutes >= 0),
    PRIMARY KEY (day_id, idx)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS query_log (
    query_id             TEXT PRIMARY KEY,   -- opaque, e.g. q-<32 hex>
    created_at           TEXT NOT NULL,
    start_date           TEXT NOT NULL,
    end_date             TEXT NOT NULL,
    requested_boundaries TEXT NOT NULL,      -- JSON list[int], deduplicated
    ignored_duplicates   TEXT NOT NULL,      -- JSON list[int]
    effective_boundaries TEXT NOT NULL,      -- JSON list[int], ascending
    bin_definitions      TEXT NOT NULL,      -- JSON list[object]
    rejected             TEXT NOT NULL,      -- JSON list[object]
    missing_dates        TEXT NOT NULL,      -- JSON list[str]
    page_size            INTEGER NOT NULL,
    days_total           INTEGER NOT NULL,
    pages_total          INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS query_day (
    query_id      TEXT NOT NULL REFERENCES query_log(query_id) ON DELETE CASCADE,
    date          TEXT NOT NULL,
    day_id        INTEGER NOT NULL,
    revision      INTEGER NOT NULL,
    source        TEXT NOT NULL,
    counts        TEXT NOT NULL,             -- JSON list[int]
    minutes_total INTEGER NOT NULL,
    PRIMARY KEY (query_id, date)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS query_page (
    query_id TEXT NOT NULL REFERENCES query_log(query_id) ON DELETE CASCADE,
    page_no  INTEGER NOT NULL,
    dates    TEXT NOT NULL,                  -- JSON list[str]
    PRIMARY KEY (query_id, page_no)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS reading_day ON reading(day_id);
"""


def default_db_path() -> Path:
    """Local default; the container sets NOISE_DB=/data/noise.db explicitly."""
    return Path(os.environ.get("NOISE_DB", "./data/noise.db"))


def connect(path: Path | str) -> sqlite3.Connection:
    """Open a connection with the pragmas every request needs.

    Each request owns its connection, so ``check_same_thread=False`` is safe;
    a connection is never shared between requests.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        target, timeout=30.0, isolation_level=None, check_same_thread=False
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db(path: Path | str) -> None:
    conn = connect(path)
    try:
        conn.executescript(SCHEMA)
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    finally:
        conn.close()


@contextlib.contextmanager
def write_txn(conn: sqlite3.Connection):
    """One immediate write transaction: all-or-nothing."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


@contextlib.contextmanager
def read_txn(conn: sqlite3.Connection):
    """A consistent snapshot for one read request."""
    conn.execute("BEGIN")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
