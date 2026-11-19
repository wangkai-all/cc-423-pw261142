"""Canonical, merge-only interval algebra.

Conventions
-----------
* Every interval is a half-open ``[start, end)`` interval over integer ticks.
* An *interval set* is a sorted tuple of canonical, disjoint, non-adjacent
  intervals.  Construction goes through :func:`normalize`, which merges every
  overlap *before* any length is summed.  There is no code path that adds the
  lengths of overlapping intervals: :func:`measure` is only ever called on
  normalized sets.
* Normalization is idempotent, so a set can be persisted, reloaded and
  re-normalized without inflating any figure (failure recovery / replay).

The module is deliberately free of I/O so that the coverage semantics it
implements can be tested exhaustively and reused by both the ingester and the
analyzer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Iterator, List, Sequence, Tuple

Interval = Tuple[int, int]


def validate(interval: Interval) -> Interval:
    """Return a validated ``(start, end)`` half-open pair.

    Raises :class:`ValueError` for non-integer, negative or empty intervals.
    An empty interval (``start == end``) is rejected rather than silently
    treated as "no data", because the caller must be explicit about it.
    """
    start, end = interval
    if isinstance(start, bool) or isinstance(end, bool):
        raise ValueError(f"interval bounds must be integers, got {interval!r}")
    if not isinstance(start, int) or not isinstance(end, int):
        raise ValueError(f"interval bounds must be integers, got {interval!r}")
    if start < 0 or end < 0:
        raise ValueError(f"interval bounds must be non-negative, got {interval!r}")
    if end <= start:
        raise ValueError(f"interval must satisfy start < end, got {interval!r}")
    return (start, end)


def normalize(intervals: Iterable[Sequence[int]]) -> Tuple[Interval, ...]:
    """Merge and sort intervals into a canonical disjoint set.

    Overlapping *and* touching intervals are merged, so that
    ``[(0, 10), (10, 20)]`` becomes ``[(0, 20)]`` and is neither counted as two
    separate spans nor double counted.  Invalid intervals raise ``ValueError``
    instead of being dropped: silently discarding data would turn a reporting
    bug into a phantom coverage gap.
    """
    validated: List[Interval] = sorted(validate((int(i[0]), int(i[1]))) for i in intervals)
    if not validated:
        return ()
    merged: List[List[int]] = [[validated[0][0], validated[0][1]]]
    for start, end in validated[1:]:
        last = merged[-1]
        if start <= last[1]:  # overlap or exact adjacency
            if end > last[1]:
                last[1] = end
        else:
            merged.append([start, end])
    return tuple((start, end) for start, end in merged)


def union(*sets: Iterable[Sequence[int]]) -> Tuple[Interval, ...]:
    """Union of any number of interval sets, normalized once."""
    combined: List[Interval] = []
    for group in sets:
        combined.extend((int(i[0]), int(i[1])) for i in group)
    return normalize(combined)


def intersection(
    left: Iterable[Sequence[int]], right: Iterable[Sequence[int]]
) -> Tuple[Interval, ...]:
    """Exact intersection of two normalized-or-not interval sets."""
    a = normalize(left)
    b = normalize(right)
    out: List[Interval] = []
    i = j = 0
    while i < len(a) and j < len(b):
        start = max(a[i][0], b[j][0])
        end = min(a[i][1], b[j][1])
        if start < end:
            out.append((start, end))
        if a[i][1] < b[j][1]:
            i += 1
        else:
            j += 1
    return tuple(out)


def subtract(
    left: Iterable[Sequence[int]], right: Iterable[Sequence[int]]
) -> Tuple[Interval, ...]:
    """Exact difference ``left \\ right``."""
    a = normalize(left)
    b = normalize(right)
    out: List[Interval] = []
    j = 0
    for start, end in a:
        cursor = start
        while j < len(b) and b[j][1] <= cursor:
            j += 1
        k = j
        while k < len(b) and b[k][0] < end:
            if b[k][0] > cursor:
                out.append((cursor, min(b[k][0], end)))
            cursor = max(cursor, b[k][1])
            if cursor >= end:
                break
            k += 1
        if cursor < end:
            out.append((cursor, end))
    return normalize(out)


def measure(intervals: Iterable[Sequence[int]]) -> int:
    """Total covered tick count of a set, counting overlaps exactly once."""
    return sum(end - start for start, end in normalize(intervals))


def is_canonical(intervals: Sequence[Sequence[int]]) -> bool:
    """True when ``intervals`` is already sorted, disjoint and non-touching."""
    previous_end = None
    for start, end in intervals:
        if end <= start:
            return False
        if previous_end is not None and start <= previous_end:
            return False
        previous_end = end
    return True


def covers_point(intervals: Iterable[Sequence[int]], point: int) -> bool:
    for start, end in normalize(intervals):
        if start <= point < end:
            return True
        if point < start:
            break
    return False


def clip(intervals: Iterable[Sequence[int]], window: Interval) -> Tuple[Interval, ...]:
    """Restrict an interval set to ``window`` (half-open)."""
    low, high = validate(window)
    return intersection(intervals, [(low, high)])


def iter_ticks(intervals: Iterable[Sequence[int]], limit: int | None = None) -> Iterator[int]:
    """Iterate ticks in ascending order; ``limit`` bounds the iteration."""
    emitted = 0
    for start, end in normalize(intervals):
        for tick in range(start, end):
            if limit is not None and emitted >= limit:
                return
            yield tick
            emitted += 1


@dataclass(frozen=True)
class Gap:
    """A maximal uncovered run inside a declared window."""

    start: int
    end: int

    @property
    def length(self) -> int:
        return self.end - self.start


def gaps(
    intervals: Iterable[Sequence[int]], window: Interval
) -> Tuple[Gap, ...]:
    """Maximal uncovered runs of ``window`` given covered ``intervals``."""
    low, high = validate(window)
    covered = clip(intervals, window)
    missing = subtract([(low, high)], covered)
    return tuple(Gap(start, end) for start, end in missing)
