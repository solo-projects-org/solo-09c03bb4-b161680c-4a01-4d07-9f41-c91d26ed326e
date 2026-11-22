"""HTTP-level tests: day writing, edge vetting, frozen queries, paging."""

from __future__ import annotations

import sqlite3
import threading
from datetime import date

from .conftest import minutes


def _bins(edges, counts):
    return {"mode": "bins", "boundaries": edges, "counts": counts}


def test_health(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["days_stored"] == 0


def test_write_and_read_minutes_day(client):
    values = minutes(l50=600, l62=600, l70=240)
    written = client.put("/days/2024-01-01", json={"mode": "minutes", "values": values})
    assert written.status_code == 200
    assert written.json()["revision"] == 1

    stored = client.get("/days/2024-01-01").json()
    assert stored["values"] == values
    assert stored["source"] == "minutes"


def test_correction_bumps_revision_and_replaces_previous_shape(client):
    client.put("/days/2024-01-01", json={"mode": "minutes", "values": minutes(l50=1440)})
    rewritten = client.put("/days/2024-01-01", json=_bins([60], [100, 1340]))
    assert rewritten.status_code == 200
    assert rewritten.json()["revision"] == 2

    stored = client.get("/days/2024-01-01").json()
    assert stored["source"] == "bins"
    assert stored["values"] is None
    assert stored["intervals"] == [
        {"lower": None, "upper": 60, "minutes": 100},
        {"lower": 60, "upper": None, "minutes": 1340},
    ]


def test_bad_minute_payload_rejected(client):
    response = client.put("/days/2024-01-01", json={"mode": "minutes", "values": [1, 2]})
    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "wrong_minute_count"
    assert client.get("/days/2024-01-01").status_code == 404


def test_bad_bin_payload_rejected(client):
    response = client.put(
        "/days/2024-01-01", json=_bins([60], [100, 100])  # does not sum to 1440
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "bin_total_mismatch"


def test_list_days(client):
    client.put("/days/2024-01-02", json={"mode": "minutes", "values": minutes(l50=1440)})
    client.put("/days/2024-01-05", json={"mode": "minutes", "values": minutes(l50=1440)})
    body = client.get(
        "/days", params={"start_date": "2024-01-01", "end_date": "2024-01-03"}
    ).json()
    assert [d["date"] for d in body["days"]] == ["2024-01-02"]


def test_query_rejects_edges_that_split_stored_bins(client):
    client.put(
        "/days/2024-03-01", json={"mode": "minutes", "values": minutes(l30=1440)}
    )
    client.put("/days/2024-03-02", json=_bins([55, 65], [100, 500, 840]))

    created = client.post(
        "/queries",
        json={
            "start_date": "2024-03-01",
            "end_date": "2024-03-02",
            "boundaries": [55, 60, 65, 70],
            "page_size": 1,
        },
    )
    assert created.status_code == 201
    body = created.json()
    assert body["effective_boundaries"] == [55, 65]
    by_edge = {r["boundary"]: r for r in body["rejected"]}
    assert set(by_edge) == {60, 70}
    assert by_edge[60]["severed"] == [
        {
            "date": "2024-03-02",
            "original_bin": {"lower": 55, "upper": 65, "minutes": 500},
        }
    ]
    assert by_edge[70]["severed"][0]["original_bin"]["upper"] is None


def test_query_counts_use_kept_edges_and_boundary_goes_up(client):
    # 100 min below 55, 200 exactly 55, 300 exactly 60, ... etc.
    values = [54] * 100 + [55] * 200 + [60] * 300 + [65] * 400 + [70] * 440
    client.put("/days/2024-04-01", json={"mode": "minutes", "values": values})
    created = client.post(
        "/queries",
        json={
            "start_date": "2024-04-01",
            "end_date": "2024-04-01",
            "boundaries": [55, 60, 65, 70],
            "page_size": 31,
        },
    ).json()
    page = client.get(f"/queries/{created['query_id']}/pages/1").json()
    assert page["days"][0]["counts"] == [100, 200, 300, 400, 440]
    assert page["days"][0]["minutes_total"] == 1440


def test_missing_days_are_listed_and_paged_as_unavailable(client):
    client.put("/days/2024-05-01", json={"mode": "minutes", "values": minutes(l50=1440)})
    created = client.post(
        "/queries",
        json={
            "start_date": "2024-05-01",
            "end_date": "2024-05-03",
            "boundaries": [],
            "page_size": 2,
        },
    ).json()
    assert created["missing_dates"] == ["2024-05-02", "2024-05-03"]
    assert created["days_available"] == 1
    assert created["pages_total"] == 2
    page2 = client.get(f"/queries/{created['query_id']}/pages/2").json()
    assert [d["available"] for d in page2["days"]] == [False]
    assert page2["days"][0]["date"] == "2024-05-03"
    assert page2["days"][0]["counts"] is None


def test_query_frozen_after_correction_and_new_query_sees_correction(client):
    client.put(
        "/days/2024-06-01", json={"mode": "minutes", "values": minutes(l50=1000, l70=440)}
    )
    first = client.post(
        "/queries",
        json={
            "start_date": "2024-06-01",
            "end_date": "2024-06-01",
            "boundaries": [60],
            "page_size": 10,
        },
    ).json()
    before = client.get(f"/queries/{first['query_id']}/pages/1").json()
    assert before["days"][0]["counts"] == [1000, 440]

    correction = client.put(
        "/days/2024-06-01", json={"mode": "minutes", "values": minutes(l50=1440)}
    )
    assert correction.json()["revision"] == 2

    after_old = client.get(f"/queries/{first['query_id']}/pages/1").json()
    assert after_old["days"][0]["counts"] == [1000, 440]  # frozen
    assert after_old["days"][0]["revision"] == 1

    second = client.post(
        "/queries",
        json={
            "start_date": "2024-06-01",
            "end_date": "2024-06-01",
            "boundaries": [60],
            "page_size": 10,
        },
    ).json()
    after_new = client.get(f"/queries/{second['query_id']}/pages/1").json()
    assert after_new["days"][0]["counts"] == [1440, 0]
    assert after_new["days"][0]["revision"] == 2


def test_resume_paging_across_restart(client, tmp_path, monkeypatch):
    client.put("/days/2024-07-01", json={"mode": "minutes", "values": minutes(l50=1440)})
    client.put("/days/2024-07-02", json={"mode": "minutes", "values": minutes(l60=1440)})
    created = client.post(
        "/queries",
        json={
            "start_date": "2024-07-01",
            "end_date": "2024-07-02",
            "boundaries": [55],
            "page_size": 1,
        },
    ).json()
    qid = created["query_id"]
    page1 = client.get(f"/queries/{qid}/pages/1").json()

    # Simulate a process restart: a brand-new app instance on the same file.
    from fastapi.testclient import TestClient

    import app.main as main_module

    with TestClient(main_module.app) as restarted:
        meta = restarted.get(f"/queries/{qid}").json()
        assert meta["effective_boundaries"] == [55]
        assert meta["pages_total"] == 2
        page1_again = restarted.get(f"/queries/{qid}/pages/1").json()
        page2 = restarted.get(f"/queries/{qid}/pages/2").json()
    assert page1_again == page1
    assert page2["days"][0]["date"] == "2024-07-02"
    assert page2["days"][0]["counts"] == [0, 1440]


def test_duplicate_boundaries_are_deduplicated(client):
    created = client.post(
        "/queries",
        json={
            "start_date": "2024-08-01",
            "end_date": "2024-08-01",
            "boundaries": [55, 55, 60, 60],
        },
    ).json()
    assert created["requested_boundaries"] == [55, 60]
    assert created["ignored_duplicate_boundaries"] == [55, 60]
    assert created["effective_boundaries"] == [55, 60]


def test_query_range_validation(client):
    response = client.post(
        "/queries",
        json={"start_date": "2024-02-02", "end_date": "2024-02-01"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_range"


def test_query_day_lookup(client):
    client.put("/days/2024-09-01", json={"mode": "minutes", "values": minutes(l50=1440)})
    created = client.post(
        "/queries",
        json={
            "start_date": "2024-09-01",
            "end_date": "2024-09-02",
            "boundaries": [50],
        },
    ).json()
    qid = created["query_id"]
    day = client.get(f"/queries/{qid}/days/2024-09-01").json()
    # every reading is exactly 50 and an edge value goes to the higher band
    assert day["counts"] == [0, 1440]
    assert client.get(f"/queries/{qid}/days/2024-09-02").status_code == 404
    assert client.get(f"/queries/{qid}/days/2024-10-01").status_code == 404


def test_unknown_query_and_page(client):
    assert client.get("/queries/q-nope").status_code == 404
    created = client.post(
        "/queries",
        json={"start_date": "2024-10-01", "end_date": "2024-10-01"},
    ).json()
    assert client.get(f"/queries/{created['query_id']}/pages/2").status_code == 404
    assert client.get(f"/queries/{created['query_id']}/pages/0").status_code == 422


# ---------------------------------------------------------------------------
# Deduplication of identical re-submissions
# ---------------------------------------------------------------------------


def test_identical_resubmission_returns_stored_revision_untouched(client):
    values = minutes(l50=600, l62=600, l70=240)
    first = client.put("/days/2024-01-10", json={"mode": "minutes", "values": values})
    assert first.status_code == 200
    assert first.json()["revision"] == 1
    assert first.json()["deduplicated"] is False

    again = client.put("/days/2024-01-10", json={"mode": "minutes", "values": values})
    assert again.status_code == 200
    body = again.json()
    assert body["deduplicated"] is True
    assert body["revision"] == 1
    # Nothing was rewritten: the stored timestamps and content are untouched.
    assert body["updated_at"] == first.json()["updated_at"]

    stored = client.get("/days/2024-01-10").json()
    assert stored["revision"] == 1
    assert stored["updated_at"] == first.json()["updated_at"]
    assert stored["values"] == values


def test_identical_bins_resubmission_across_representations(client):
    client.put("/days/2024-01-11", json=_bins([55, 65], [210, 690, 540]))
    again = client.put(
        "/days/2024-01-11",
        json={
            "mode": "bins",
            "intervals": [
                {"lower": None, "upper": 55, "minutes": 210},
                {"lower": 55, "upper": 65, "minutes": 690},
                {"lower": 65, "upper": None, "minutes": 540},
            ],
        },
    )
    assert again.json()["revision"] == 1
    assert again.json()["deduplicated"] is True
    stored = client.get("/days/2024-01-11").json()
    assert stored["revision"] == 1


def test_same_distribution_different_source_is_a_correction(client):
    client.put("/days/2024-01-12", json={"mode": "minutes", "values": minutes(l50=1440)})
    # Same distribution, different source: not a duplicate, revision bumps.
    rewritten = client.put("/days/2024-01-12", json=_bins([50], [0, 1440]))
    assert rewritten.json()["revision"] == 2
    assert rewritten.json()["deduplicated"] is False


def test_dedup_then_real_correction_bumps_only_once(client):
    client.put("/days/2024-01-13", json={"mode": "minutes", "values": minutes(l50=1440)})
    dup = client.put("/days/2024-01-13", json={"mode": "minutes", "values": minutes(l50=1440)})
    assert dup.json()["revision"] == 1
    assert dup.json()["deduplicated"] is True

    correction = client.put(
        "/days/2024-01-13", json={"mode": "minutes", "values": minutes(l60=1440)}
    )
    assert correction.json()["revision"] == 2
    assert correction.json()["deduplicated"] is False

    # Re-sending the corrected content is deduplicated against revision 2.
    again = client.put(
        "/days/2024-01-13", json={"mode": "minutes", "values": minutes(l60=1440)}
    )
    assert again.json()["revision"] == 2
    assert again.json()["deduplicated"] is True


def test_dedup_does_not_create_phantom_correction_for_queries(client):
    client.put(
        "/days/2024-01-14", json={"mode": "minutes", "values": minutes(l50=1000, l70=440)}
    )
    dup = client.put(
        "/days/2024-01-14", json={"mode": "minutes", "values": minutes(l50=1000, l70=440)}
    )
    assert dup.json()["deduplicated"] is True
    # A query created after the duplicate still pins revision 1.
    created = client.post(
        "/queries",
        json={"start_date": "2024-01-14", "end_date": "2024-01-14", "boundaries": [60]},
    ).json()
    page = client.get(f"/queries/{created['query_id']}/pages/1").json()
    assert page["days"][0]["revision"] == 1
    assert page["days"][0]["counts"] == [1000, 440]


def test_failed_correction_leaves_stored_day_untouched(client):
    client.put("/days/2024-01-15", json={"mode": "minutes", "values": minutes(l50=1440)})
    bad = client.put("/days/2024-01-15", json=_bins([60], [100, 100]))
    assert bad.status_code == 422
    stored = client.get("/days/2024-01-15").json()
    assert stored["revision"] == 1
    assert stored["source"] == "minutes"
    assert stored["values"] == minutes(l50=1440)


# ---------------------------------------------------------------------------
# Legacy databases without fingerprints
# ---------------------------------------------------------------------------

# The schema exactly as a v1 database has it: no day.fingerprint column.
_V1_SCHEMA = """
CREATE TABLE day (
    day_id      INTEGER PRIMARY KEY,
    date        TEXT NOT NULL UNIQUE,
    revision    INTEGER NOT NULL,
    source      TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE TABLE reading (
    day_id  INTEGER NOT NULL REFERENCES day(day_id) ON DELETE CASCADE,
    minute  INTEGER NOT NULL CHECK (minute BETWEEN 0 AND 1439),
    value   INTEGER NOT NULL,
    PRIMARY KEY (day_id, minute)
) WITHOUT ROWID;
CREATE TABLE bin_interval (
    day_id     INTEGER NOT NULL REFERENCES day(day_id) ON DELETE CASCADE,
    idx        INTEGER NOT NULL,
    lower      INTEGER,
    upper      INTEGER,
    minutes    INTEGER NOT NULL CHECK (minutes >= 0),
    PRIMARY KEY (day_id, idx)
) WITHOUT ROWID;
"""


def _seed_v1_db(path) -> None:
    """A v1 database holding one minutes-day at revision 3, no fingerprints."""
    conn = sqlite3.connect(path)
    try:
        conn.executescript(_V1_SCHEMA)
        conn.execute("PRAGMA user_version=1")
        conn.execute(
            "INSERT INTO day (date, revision, source, created_at, updated_at)"
            " VALUES ('2024-02-01', 3, 'minutes',"
            " '2024-02-02T00:00:00+00:00', '2024-02-02T00:00:00+00:00')"
        )
        conn.executemany(
            "INSERT INTO reading (day_id, minute, value) VALUES (1, ?, ?)",
            [(i, 55) for i in range(1440)],
        )
        conn.commit()
    finally:
        conn.close()


def test_legacy_db_dedups_against_actual_stored_content(tmp_path, monkeypatch):
    import importlib
    import sys

    from fastapi.testclient import TestClient

    db_file = tmp_path / "noise.db"
    _seed_v1_db(db_file)
    monkeypatch.setenv("NOISE_DB", str(db_file))
    if "app.main" in sys.modules:
        module = importlib.reload(sys.modules["app.main"])
    else:
        module = importlib.import_module("app.main")

    with TestClient(module.app) as client:
        # Startup migrated the schema in place.
        assert client.get("/health").json()["schema_version"] == 2

        # First comparison for the legacy row uses the actual stored readings.
        dup = client.put(
            "/days/2024-02-01", json={"mode": "minutes", "values": [55] * 1440}
        )
        assert dup.status_code == 200
        assert dup.json()["deduplicated"] is True
        assert dup.json()["revision"] == 3
        assert dup.json()["updated_at"] == "2024-02-02T00:00:00+00:00"

        # The fingerprint was backfilled on the legacy row.
        conn = sqlite3.connect(db_file)
        try:
            row = conn.execute(
                "SELECT fingerprint, revision FROM day WHERE date = '2024-02-01'"
            ).fetchone()
        finally:
            conn.close()
        assert row[0]
        assert row[1] == 3

        # A real change still replaces atomically and bumps the revision.
        correction = client.put(
            "/days/2024-02-01", json={"mode": "minutes", "values": [60] * 1440}
        )
        assert correction.json()["revision"] == 4
        assert correction.json()["deduplicated"] is False
        stored = client.get("/days/2024-02-01").json()
        assert stored["values"] == [60] * 1440


# ---------------------------------------------------------------------------
# Concurrent duplicates and corrections
# ---------------------------------------------------------------------------


def test_concurrent_identical_first_submissions_commit_once(tmp_path):
    from app import db, service

    path = tmp_path / "noise.db"
    db.init_db(path)
    norm = service.normalize_minutes([55] * 1440)
    results = []

    def worker():
        conn = db.connect(path)
        try:
            results.append(service.upsert_day(conn, date(2024, 3, 1), norm))
        finally:
            conn.close()

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    # Exactly one insert wins; everyone else deduplicates against it.
    assert len(results) == 6
    assert all(r["revision"] == 1 for r in results)
    assert sum(1 for r in results if not r["deduplicated"]) == 1

    conn = db.connect(path)
    try:
        stored = service.day_payload(conn, date(2024, 3, 1), "test")
    finally:
        conn.close()
    assert stored["revision"] == 1
    assert stored["values"] == [55] * 1440


def test_concurrent_duplicate_and_correction_match_commit_order(tmp_path):
    from app import db, service

    path = tmp_path / "noise.db"
    db.init_db(path)
    day = date(2024, 3, 2)
    norm_a = service.normalize_minutes([50] * 1440)
    norm_b = service.normalize_minutes([60] * 1440)

    conn = db.connect(path)
    try:
        assert service.upsert_day(conn, day, norm_a)["revision"] == 1
    finally:
        conn.close()

    results: list[tuple[str, dict]] = []
    lock = threading.Lock()

    def worker(tag, norm):
        conn = db.connect(path)
        try:
            outcome = service.upsert_day(conn, day, norm)
            with lock:
                results.append((tag, outcome))
        finally:
            conn.close()

    threads = [
        threading.Thread(target=worker, args=(tag, norm))
        for tag, norm in [("a", norm_a), ("b", norm_b), ("a", norm_a), ("b", norm_b), ("a", norm_a)]
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(results) == 5
    # Writes serialised: the bumped revisions are exactly 2..N, no gaps.
    writes = sorted(
        (r["revision"], tag) for tag, r in results if not r["deduplicated"]
    )
    assert [rev for rev, _ in writes] == list(range(2, 2 + len(writes)))

    # The last committed write wins and the stored day is complete, never half.
    final_revision, last_tag = writes[-1]
    conn = db.connect(path)
    try:
        stored = service.day_payload(conn, day, "test")
    finally:
        conn.close()
    assert stored["revision"] == final_revision
    assert stored["values"] == ([50] * 1440 if last_tag == "a" else [60] * 1440)
