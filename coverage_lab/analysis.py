"""Coverage computation: the analyzer half of the system.

Rules implemented here (and only here):

1. Coverage is computed from **merged intervals**, never from an event count.
   A thousand overlapping event reports inside one tick still cover one tick.
2. A ratio is only reported over a **declared window**.  A node with no
   declared window (or a group containing one) is reported as ``unknown``;
   we never extrapolate a percentage from the covered part alone.
3. Retractions are applied before measuring, so withdrawn ranges surface as
   gaps with their operator-supplied reason.
4. Every figure carries the store ``revision`` it was computed from, and every
   analysis run is persisted so two revisions can be compared later.

No confidence intervals or "estimated" coverage are ever produced: the data
model records exactly which ticks were reported, so there is no statistical
inference to make.
"""

from __future__ import annotations

from fractions import Fraction
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import intervals as iv
from .store import Store
STATE_COMPLETE = "complete"
STATE_PARTIAL = "partial"
STATE_EMPTY = "empty"
STATE_UNKNOWN = "unknown"


def _ratio(covered: int, declared: int) -> Tuple[Optional[float], Optional[str]]:
    if declared <= 0:
        return None, None
    fraction = Fraction(covered, declared).limit_denominator(10**9)
    return round(float(fraction), 10), f"{fraction.numerator}/{fraction.denominator}"


def _ratio_delta(
    a_covered: int, a_declared: int, b_covered: int, b_declared: int
) -> Dict[str, Any]:
    if a_declared <= 0 or b_declared <= 0:
        return {"ratio_delta": None, "ratio_delta_fraction": None}
    delta = Fraction(b_covered, b_declared) - Fraction(a_covered, a_declared)
    return {
        "ratio_delta": round(float(delta), 10),
        "ratio_delta_fraction": f"{delta.numerator}/{delta.denominator}",
    }


def analyze(
    store: Store, note: str = "current", persist: bool = True
) -> Dict[str, Any]:
    """Compute (and by default persist) coverage for every node and group."""
    with store.snapshot_read() as conn:
        revision = store.revision(conn)
        nodes = store.list_nodes(conn)
        details = []
        for node in nodes:
            segments = store.segments(node["node_id"], conn)
            details.append(_analyze_node(store, conn, node, segments))

    groups = _analyze_groups(details)
    result = {
        "revision": revision,
        "generated_at": _now(),
        "nodes": details,
        "groups": groups,
    }
    if persist:
        result["run_id"] = store.save_analysis_run(revision, note, details, groups)
    return result


def _analyze_node(
    store: Store,
    conn: Any,
    node: Dict[str, Any],
    segments: Tuple[iv.Interval, ...],
) -> Dict[str, Any]:
    window: Optional[Sequence[int]] = node["window"]
    retractions = store.retractions(node["node_id"], conn)
    covered_all = iv.measure(segments)
    notes: List[str] = []
    if retractions:
        notes.append(
            f"{len(retractions)} operator retraction(s) already applied; "
            "retracted ticks are excluded even if claims cover them."
        )
    base: Dict[str, Any] = {
        "node_id": node["node_id"],
        "name": node["name"],
        "group": node["group"],
        "status": node["status"],
        "window": None if window is None else list(window),
        "coverage": [list(p) for p in segments],
        "covered_ticks_total": covered_all,
        "retraction_count": len(retractions),
        "retractions": [
            {"start": r["start"], "end": r["end"], "reason": r["reason"]} for r in retractions
        ],
    }
    if window is None:
        base.update(
            {
                "state": STATE_UNKNOWN,
                "declared_ticks": 0,
                "covered_ticks": 0,
                "ratio": None,
                "ratio_fraction": None,
                "out_of_window_ticks": 0,
                "gap_count": 0,
                "gap_ticks": 0,
                "gaps": [],
                "unknown_reason": "node has no declared coverage window",
            }
        )
        return base

    in_window = iv.clip(segments, tuple(window))
    out_of_window = iv.subtract(segments, [tuple(window)])
    gap_list = iv.gaps(segments, tuple(window))
    declared = window[1] - window[0]
    covered = iv.measure(in_window)
    ratio, ratio_fraction = _ratio(covered, declared)
    if covered == 0:
        state = STATE_EMPTY
    elif gap_list:
        state = STATE_PARTIAL
    else:
        state = STATE_COMPLETE
    out_ticks = iv.measure(out_of_window)
    base.update(
        {
            "state": state,
            "declared_ticks": declared,
            "covered_ticks": covered,
            "ratio": ratio,
            "ratio_fraction": ratio_fraction,
            "out_of_window_ticks": out_ticks,
            "out_of_window": [list(p) for p in out_of_window],
            "gap_count": len(gap_list),
            "gap_ticks": sum(g.length for g in gap_list),
            "gaps": [
                {
                    "start": g.start,
                    "end": g.end,
                    "length": g.length,
                    # Reasons are attached here so the UI can explain a gap
                    # without a second request; they are evidence, not counts.
                    "retractions": [
                        {"start": r["start"], "end": r["end"], "reason": r["reason"]}
                        for r in retractions
                        if r["start"] < g.end and r["end"] > g.start
                    ],
                }
                for g in gap_list
            ],
            "unknown_reason": None,
        }
    )
    if out_ticks:
        notes.append(
            "Claimed ticks outside the declared window are excluded from the ratio "
            "but kept visible; they are not evidence of window coverage."
        )
    if notes:
        base["notes"] = notes
    return base


def _analyze_groups(nodes: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_group: Dict[str, List[Dict[str, Any]]] = {}
    for node in nodes:
        by_group.setdefault(node["group"], []).append(node)

    groups: List[Dict[str, Any]] = []
    for name in sorted(by_group):
        members = by_group[name]
        unknown = sorted(n["node_id"] for n in members if n["state"] == STATE_UNKNOWN)
        declared = sum(n["declared_ticks"] for n in members)
        covered = sum(n["covered_ticks"] for n in members)
        gap_count = sum(n["gap_count"] for n in members)
        gap_ticks = sum(n["gap_ticks"] for n in members)
        # Group windows are per-node: each node must cover its own window, so
        # ticks are summed across nodes and never interval-unioned across nodes.
        window = _common_window(members)
        row: Dict[str, Any] = {
            "group": name,
            "window": window,
            "nodes": [
                {
                    "node_id": n["node_id"],
                    "state": n["state"],
                    "covered_ticks": n["covered_ticks"],
                    "declared_ticks": n["declared_ticks"],
                    "ratio_fraction": n["ratio_fraction"],
                    "status": n["status"],
                }
                for n in members
            ],
            "node_count": len(members),
            "unknown_nodes": unknown,
            "declared_ticks": declared,
            "covered_ticks": covered,
            "gap_count": gap_count,
            "gap_ticks": gap_ticks,
        }
        if unknown or declared == 0:
            row.update(
                {
                    "state": STATE_UNKNOWN,
                    "ratio": None,
                    "ratio_fraction": None,
                    "unknown_reason": (
                        f"{len(unknown)} node(s) without a declared window: "
                        + ", ".join(unknown)
                    ),
                    "known_covered_ticks": covered,
                }
            )
        else:
            ratio, ratio_fraction = _ratio(covered, declared)
            row.update(
                {
                    "state": STATE_COMPLETE if gap_count == 0 else STATE_PARTIAL,
                    "ratio": ratio,
                    "ratio_fraction": ratio_fraction,
                    "unknown_reason": None,
                }
            )
        groups.append(row)
    return groups


def _common_window(members: Sequence[Dict[str, Any]]) -> Optional[List[int]]:
    windows = [n["window"] for n in members]
    if not windows or any(w is None for w in windows):
        return None
    start = min(w[0] for w in windows)
    end = max(w[1] for w in windows)
    return [start, end]


# --------------------------------------------------------------------- evidence


def gap_evidence(store: Store, node_id: str, start: int, end: int) -> Dict[str, Any]:
    """Explain why ``[start, end)`` is (or is not) a gap for a node.

    Returns the overlapping raw/archived event claims, manifest revisions that
    intersect, and retractions that bite, plus a plain-language explanation
    built from those facts.  Anything the stored evidence cannot establish is
    labelled ``unknown`` instead of guessed.
    """
    start, end = iv.validate((start, end))
    node = store.get_node(node_id)
    with store.snapshot_read() as conn:
        revision = store.revision(conn)
        segments = store.segments(node_id, conn)
        live = store.live_events(node_id, start, end, limit=500, conn=conn)
        archived = store.archived_events(node_id, start, end, limit=500, conn=conn)
        manifests = store.manifest_versions(node_id, conn)
        retractions = [
            r for r in store.retractions(node_id, conn) if r["start"] < end and r["end"] > start
        ]
        latest_by_name = {}
        for m in manifests:
            current = latest_by_name.get(m["name"])
            if current is None or m["revision"] > current["revision"]:
                latest_by_name[m["name"]] = m

    window = node["window"]
    if window is None:
        in_window: Optional[bool] = None
    else:
        in_window = start >= window[0] and end <= window[1]

    covered = iv.intersection(segments, [(start, end)])
    uncovered = iv.subtract([(start, end)], segments)
    fully_covered = len(uncovered) == 0

    intersecting_events = [
        e for e in live + archived if e["start"] < end and e["end"] > start
    ]
    intersecting_manifests = [
        m for m in manifests if any(s < end and e > start for s, e in m["intervals"])
    ]
    withdrawn_manifests = []
    for m in intersecting_manifests:
        latest = latest_by_name.get(m["name"])
        if latest and m["revision"] < latest["revision"]:
            withdrawn_manifests.append(
                {
                    "name": m["name"],
                    "revision": m["revision"],
                    "superseded_by": latest["revision"],
                    "superseded_at": latest["submitted_at"],
                    "note": latest["note"],
                }
            )
    withdrawn_events = [
        e
        for e in intersecting_events
        if any(r["start"] < e["end"] and r["end"] > e["start"] for r in retractions)
    ]

    explanation: List[str] = []
    if fully_covered:
        explanation.append(
            f"All {end - start} ticks are covered by current claims."
        )
    else:
        explanation.append(
            f"{iv.measure(covered)} of {end - start} ticks are covered by current claims."
        )
        explanation.append(
            "Uncovered sub-ranges: " + ", ".join(f"[{s},{e})" for s, e in uncovered)
        )
        if retractions:
            explanation.append(
                "An operator retraction overlaps it: "
                + "; ".join(f"[{r['start']},{r['end']}) {r['reason']}" for r in retractions)
            )
        if withdrawn_manifests:
            explanation.append(
                "A later manifest revision withdrew a claim that covered it: "
                + "; ".join(
                    f"{w['name']} r{w['revision']} -> r{w['superseded_by']}"
                    for w in withdrawn_manifests
                )
            )
        if not retractions and not withdrawn_manifests and not intersecting_events:
            explanation.append(
                "No event or manifest was ever reported for these ticks; this is "
                "'not reported', not 'observed uncovered'."
            )

    return {
        "node_id": node_id,
        "range": [start, end],
        "revision": revision,
        "node_window": window,
        "in_declared_window": in_window,
        "is_fully_covered": fully_covered,
        "is_fully_uncovered": len(covered) == 0,
        "covered": [list(p) for p in covered],
        "uncovered": [list(p) for p in uncovered],
        "events": intersecting_events,
        "live_events": live,
        "archived_events": archived,
        "manifest_revisions": intersecting_manifests,
        "withdrawn_manifests": withdrawn_manifests,
        "retractions": retractions,
        "withdrawn_events": withdrawn_events,
        "explanation": explanation,
        "unknown": []
        if node["window"] is not None
        else ["no declared window: outside-window membership is unknown"],
    }


# --------------------------------------------------------------------- revision diff


def compare_runs(store: Store, run_a: int, run_b: int) -> Dict[str, Any]:
    """Per-group and per-node impact of moving from run A to run B."""
    a = store.analysis_run(run_a)
    b = store.analysis_run(run_b)
    a_nodes = {n["node_id"]: n for n in a["nodes"]}
    b_nodes = {n["node_id"]: n for n in b["nodes"]}
    a_groups = {g["group_name"]: g for g in a["groups"]}
    b_groups = {g["group_name"]: g for g in b["groups"]}

    group_rows = []
    for name in sorted(set(a_groups) | set(b_groups)):
        ga = a_groups.get(name)
        gb = b_groups.get(name)
        row: Dict[str, Any] = {"group": name}
        if ga is None or gb is None:
            row.update(
                {
                    "state": "unknown",
                    "unknown_reason": "group present in only one of the two runs",
                    "covered_delta": (gb or ga)["covered_ticks"] if (gb or ga) else 0,
                    "gap_delta": (gb or ga)["gap_ticks"] if (gb or ga) else 0,
                }
            )
            group_rows.append(row)
            continue
        row.update(
            {
                "state_a": ga["state"],
                "state_b": gb["state"],
                "declared_ticks": gb["declared_ticks"],
                "covered_a": ga["covered_ticks"],
                "covered_b": gb["covered_ticks"],
                "covered_delta": gb["covered_ticks"] - ga["covered_ticks"],
                "gap_a": ga["gap_ticks"],
                "gap_b": gb["gap_ticks"],
                "gap_delta": gb["gap_ticks"] - ga["gap_ticks"],
                "nodes_a": ga["node_count"],
                "nodes_b": gb["node_count"],
                "unknown_nodes": gb["unknown_nodes"],
                "ratio_a_fraction": ga["ratio_fraction"],
                "ratio_b_fraction": gb["ratio_fraction"],
                "state": "unknown"
                if ga["state"] == STATE_UNKNOWN or gb["state"] == STATE_UNKNOWN
                else "known",
            }
        )
        if row["state"] == "known":
            row.update(
                _ratio_delta(
                    ga["covered_ticks"], ga["declared_ticks"],
                    gb["covered_ticks"], gb["declared_ticks"],
                )
            )
        else:
            # A group with an unknown member has no ratio in either run; a
            # numeric delta here would invent one.
            row.update({"ratio_delta": None, "ratio_delta_fraction": None})
        group_rows.append(row)

    node_changes = []
    for node_id in sorted(set(a_nodes) | set(b_nodes)):
        na = a_nodes.get(node_id)
        nb = b_nodes.get(node_id)
        if na is None or nb is None:
            node_changes.append({"node_id": node_id, "change": "added" if na is None else "removed"})
            continue
        if (
            na["covered_ticks"] == nb["covered_ticks"]
            and na["declared_ticks"] == nb["declared_ticks"]
            and na["state"] == nb["state"]
        ):
            continue
        change: Dict[str, Any] = {
            "node_id": node_id,
            "group": nb["group_name"],
            "state_a": na["state"],
            "state_b": nb["state"],
            "covered_a": na["covered_ticks"],
            "covered_b": nb["covered_ticks"],
            "covered_delta": nb["covered_ticks"] - na["covered_ticks"],
            "gap_a": na["gap_ticks"],
            "gap_b": nb["gap_ticks"],
            "gap_delta": nb["gap_ticks"] - na["gap_ticks"],
            "ratio_a_fraction": na["ratio_fraction"],
            "ratio_b_fraction": nb["ratio_fraction"],
        }
        change.update(
            _ratio_delta(
                na["covered_ticks"], na["declared_ticks"],
                nb["covered_ticks"], nb["declared_ticks"],
            )
        )
        change["change"] = _classify_change(na, nb)
        node_changes.append(change)

    return {
        "run_a": {k: a["run"][k] for k in ("run_id", "revision", "created_at", "note")},
        "run_b": {k: b["run"][k] for k in ("run_id", "revision", "created_at", "note")},
        "groups": group_rows,
        "nodes": node_changes,
    }


def _now() -> str:
    from .store import utcnow

    return utcnow()


def _classify_change(a: Dict[str, Any], b: Dict[str, Any]) -> str:
    """A conservative label; never implies more certainty than the numbers."""
    if a["state"] == STATE_UNKNOWN or b["state"] == STATE_UNKNOWN:
        return "unknown"
    delta = b["covered_ticks"] - a["covered_ticks"]
    if delta > 0:
        return "gained"
    if delta < 0:
        return "lost"
    return "unchanged"


def node_detail(store: Store, node_id: str) -> Dict[str, Any]:
    """Everything the inspector needs for one node, from one snapshot.

    Manifests are returned both as full version history and as the latest
    revision per manifest name, because historical rows are *evidence of what
    was withdrawn* and must not be mistaken for active claims.
    """
    with store.snapshot_read() as conn:
        node = store.get_node(node_id, conn)
        segments = store.segments(node_id, conn)
        revision = store.revision(conn)
        manifests = store.manifest_versions(node_id, conn)
        latest = store.latest_manifests(node_id, conn)
        retractions = store.retractions(node_id, conn)
        live = store.live_events(node_id, limit=20, conn=conn)
        archived = store.archived_events(node_id, limit=20, conn=conn)
    analysis = _analyze_node(store, None, node, segments)
    return {
        "revision": revision,
        "node": node,
        "analysis": analysis,
        "segments": [list(p) for p in segments],
        "manifests": manifests,
        "latest_manifests": latest,
        "retractions": retractions,
        "live_events": live,
        "archived_events": archived,
        "archive_stats": store.archive_stats(node_id),
    }
