"""HTTP-level tests: day writing, edge vetting, frozen queries, paging."""

from __future__ import annotations

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
