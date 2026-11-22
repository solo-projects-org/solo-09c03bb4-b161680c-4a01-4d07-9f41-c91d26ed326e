"""Unit tests for counting and edge vetting, without HTTP."""

from __future__ import annotations

import pytest

from app import service


def test_boundary_value_goes_to_higher_band():
    values = [54, 55, 55, 65, 66, 65]
    assert service.counts_from_minutes(values, [55, 65]) == [1, 2, 3]


def test_counts_always_sum_to_day_length():
    values = [50] * 100 + [55] * 200 + [60] * 300 + [65] * 400 + [70] * 440
    for edges in ([], [55], [55, 60], [55, 60, 65, 70], [0, 100, 1000]):
        assert sum(service.counts_from_minutes(values, edges)) == 1440


def test_intervals_only_match_kept_edges():
    intervals = [
        service.Interval(None, 55, 100),
        service.Interval(55, 65, 500),
        service.Interval(65, None, 840),
    ]
    assert service.counts_from_intervals(intervals, [55, 65]) == [100, 500, 840]
    assert service.counts_from_intervals(intervals, []) == [1440]
    # An edge strictly inside a stored bin is a caller bug, never a miscount.
    with pytest.raises(ValueError):
        service.counts_from_intervals(intervals, [40])


def test_intervals_can_spread_into_empty_bands():
    intervals = [
        service.Interval(None, 40, 0),
        service.Interval(40, 55, 100),
        service.Interval(55, 65, 0),
        service.Interval(65, 90, 500),
        service.Interval(90, None, 840),
    ]
    # <40, [40,55), [55,65), [65,90), >=90
    assert service.counts_from_intervals(intervals, [40, 55, 65, 90]) == [
        0,
        100,
        0,
        500,
        840,
    ]


def test_zero_minute_bin_does_not_block_its_edges():
    intervals = [
        service.Interval(None, 55, 200),
        service.Interval(55, 65, 0),
        service.Interval(65, 70, 400),
        service.Interval(70, None, 840),
    ]
    kept, rejected = service._reject_candidates([55, 60, 65, 70], [("d", intervals)])
    assert kept == [55, 60, 65, 70]
    assert rejected == []


def test_edge_touching_bin_border_is_kept():
    intervals = [
        service.Interval(None, 55, 200),
        service.Interval(55, 65, 400),
        service.Interval(65, None, 840),
    ]
    kept, rejected = service._reject_candidates([55, 65], [("d", intervals)])
    assert kept == [55, 65]
    assert rejected == []


def test_edge_inside_bin_is_rejected_with_day_and_bin():
    intervals = [
        service.Interval(None, 55, 200),
        service.Interval(55, 65, 400),
        service.Interval(65, None, 840),
    ]
    kept, rejected = service._reject_candidates([60], [("2024-05-05", intervals)])
    assert kept == []
    assert rejected == [
        {
            "boundary": 60,
            "reason": "would_split_original_bin",
            "severed_total": 1,
            "severed_truncated": False,
            "severed": [
                {
                    "date": "2024-05-05",
                    "original_bin": {"lower": 55, "upper": 65, "minutes": 400},
                }
            ],
        }
    ]


def test_edge_on_zero_minute_bin_is_not_rejected():
    intervals = [
        service.Interval(None, 55, 200),
        service.Interval(55, 65, 0),
        service.Interval(65, None, 1240),
    ]
    kept, _ = service._reject_candidates([60], [("d", intervals)])
    assert kept == [60]


@pytest.mark.parametrize(
    "doc",
    [
        # not contiguous
        [service.Interval(None, 55, 100), service.Interval(60, None, 1340)],
        # open at bottom on the first bin
        [service.Interval(40, 55, 100), service.Interval(55, None, 1340)],
        # open at top only on the last bin
        [service.Interval(None, 55, 100), service.Interval(55, 60, 1340)],
        # wrong total
        [service.Interval(None, 55, 100), service.Interval(55, None, 1000)],
    ],
)
def test_invalid_bins_are_rejected(doc):
    with pytest.raises(service.ServiceError) as info:
        service.normalize_intervals(doc)
    assert info.value.status == 422


def test_minutes_need_exactly_1440_ints():
    with pytest.raises(service.ServiceError) as info:
        service.normalize_minutes([1, 2, 3])
    assert info.value.code == "wrong_minute_count"


def test_boundaries_compact_form_requires_ascending_edges():
    with pytest.raises(service.ServiceError) as info:
        service.intervals_from_boundaries([65, 55], [1, 1, 1438])
    assert info.value.code == "boundaries_not_ascending"


def test_bin_definitions_labels():
    assert [b["label"] for b in service.bin_definitions([55, 65])] == [
        "<55",
        "[55,65)",
        ">=65",
    ]
    assert [b["label"] for b in service.bin_definitions([])] == ["(-inf, +inf)"]


def test_rejection_list_is_capped_but_total_is_exact():
    days = [
        (f"2024-{i:04d}-01", [service.Interval(None, 60, 1440)])
        for i in range(1, 61)
    ]
    kept, rejected = service._reject_candidates([30], days)
    assert kept == []
    (entry,) = rejected
    assert entry["severed_total"] == 60
    assert entry["severed_truncated"] is True
    assert len(entry["severed"]) == service.SEVERED_LIST_LIMIT
    assert entry["severed"][0]["date"] == "2024-0001-01"


def test_open_lowest_bin_rejects_every_edge_below_its_top():
    intervals = [service.Interval(None, 70, 1440)]
    kept, rejected = service._reject_candidates([-50, 0, 69, 70, 71], [("d", intervals)])
    assert kept == [70, 71]
    assert [r["boundary"] for r in rejected] == [-50, 0, 69]


def test_compact_boundaries_require_matching_counts():
    with pytest.raises(service.ServiceError) as info:
        service.intervals_from_boundaries([55], [1, 2, 3])
    assert info.value.code == "count_mismatch"


def test_edge_equal_to_original_edge_puts_bin_in_higher_band():
    intervals = [
        service.Interval(None, 55, 100),
        service.Interval(55, 65, 500),
        service.Interval(65, None, 840),
    ]
    # Stored bins never straddle a kept edge, so the higher-band rule only
    # applies to the bin that starts exactly on the edge.
    assert service.counts_from_intervals(intervals, [55]) == [100, 1340]
    assert service.counts_from_intervals(intervals, [65]) == [600, 840]
    assert service.counts_from_intervals(intervals, [55, 65]) == [100, 500, 840]
