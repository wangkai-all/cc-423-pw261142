"""Exhaustive-ish tests for the interval algebra.

These are the properties everything else depends on: if merging double counts,
every downstream ratio is wrong.
"""

from __future__ import annotations

import random

import pytest

from coverage_lab import intervals as iv


def test_normalize_merges_overlap_and_adjacency():
    assert iv.normalize([(0, 10), (5, 15), (15, 20)]) == ((0, 20),)
    assert iv.normalize([(10, 20), (0, 10)]) == ((0, 20),)
    assert iv.normalize([(0, 5), (6, 10)]) == ((0, 5), (6, 10))


def test_overlapping_reports_count_once():
    overlapping = [(0, 60), (10, 80), (30, 100), (40, 100)]
    assert iv.measure(overlapping) == 100


def test_duplicate_events_add_nothing():
    once = iv.normalize([(0, 10), (10, 20)])
    twice = iv.normalize(list(once) * 5)
    assert iv.measure(once) == iv.measure(twice) == 20


def test_normalize_is_idempotent():
    random.seed(7)
    for _ in range(200):
        raw = []
        for _ in range(random.randint(0, 12)):
            start = random.randint(0, 100)
            raw.append((start, start + random.randint(1, 30)))
        merged = iv.normalize(raw)
        assert iv.normalize(merged) == merged
        assert iv.is_canonical(merged)
        assert iv.measure(merged) == iv.measure(raw)


def test_measure_equals_union_of_points():
    random.seed(11)
    for _ in range(100):
        raw = []
        for _ in range(random.randint(1, 10)):
            start = random.randint(0, 60)
            raw.append((start, start + random.randint(1, 20)))
        points = {t for start, end in raw for t in range(start, end)}
        assert iv.measure(raw) == len(points)


def test_intersection_and_subtract():
    left = [(0, 10), (20, 30)]
    right = [(5, 25)]
    assert iv.intersection(left, right) == ((5, 10), (20, 25))
    assert iv.subtract(left, right) == ((0, 5), (25, 30))
    assert iv.subtract(left, left) == ()


def test_subtract_full_cover():
    assert iv.subtract([(0, 10)], [(0, 100)]) == ()
    assert iv.subtract([(0, 10)], [(2, 4), (6, 8)]) == ((0, 2), (4, 6), (8, 10))


def test_gaps_cover_exactly_the_window():
    covered = [(0, 10), (15, 40)]
    gaps = iv.gaps(covered, (0, 50))
    assert [(g.start, g.end) for g in gaps] == [(10, 15), (40, 50)]
    assert iv.measure(covered) + sum(g.length for g in gaps) == 50


def test_clip_excludes_outside_ticks():
    assert iv.clip([(0, 100)], (20, 40)) == ((20, 40),)
    assert iv.clip([(0, 10)], (20, 40)) == ()


@pytest.mark.parametrize(
    "bad",
    [
        (5, 5),
        (10, 5),
        (-1, 5),
        (0, 0),
    ],
)
def test_invalid_intervals_raise(bad):
    with pytest.raises(ValueError):
        iv.normalize([bad])


def test_non_integer_intervals_raise():
    with pytest.raises(ValueError):
        iv.validate((0.5, 3))
    with pytest.raises(ValueError):
        iv.validate((True, 3))
