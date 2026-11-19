"""End-to-end tests of the store + analyzer semantics.

Each test targets a failure mode that a naive "count the events" dashboard
would get wrong.
"""

from __future__ import annotations

import pytest

from coverage_lab import analysis
from coverage_lab.store import Conflict, NodeStopped, Store, ValidationError


@pytest.fixture()
def store(tmp_path):
    return Store(tmp_path / "coverage.db")


def make_node(store, node_id="n1", window=(0, 100), group="g1"):
    return store.create_node(node_id, node_id, group, window)


def event(event_id, start, end):
    return {"event_id": event_id, "start": start, "end": end}


# --------------------------------------------------------------------- counting


def test_overlapping_events_do_not_inflate_coverage(store):
    make_node(store)
    store.add_events("n1", [event("a", 0, 50), event("b", 20, 70), event("c", 65, 70)])
    result = analysis.analyze(store, persist=False)
    node = result["nodes"][0]
    assert node["covered_ticks"] == 70
    assert node["coverage"] == [[0, 70]]
    assert node["gap_count"] == 1
    assert [(g["start"], g["end"], g["length"]) for g in node["gaps"]] == [(70, 100, 30)]
    assert node["gaps"][0]["retractions"] == []


def test_retried_batch_is_idempotent(store):
    make_node(store)
    batch = [event("a", 0, 10), event("b", 10, 20)]
    first = store.add_events("n1", batch, batch_id="b1")
    second = store.add_events("n1", batch, batch_id="b1-retry")
    assert first["accepted"] == 2 and first["duplicates"] == 0
    assert second["accepted"] == 0 and second["duplicates"] == 2
    assert store.covered_ticks("n1") == 20
    # Revision bumped once for the real change, not for the replay.
    assert first["revision"] == second["revision"]


def test_invalid_events_are_reported_not_dropped(store):
    make_node(store)
    result = store.add_events(
        "n1",
        [event("ok", 0, 5), event("bad", 10, 5), {"start": 0, "end": 1}],
    )
    assert result["accepted"] == 1
    assert len(result["rejected"]) == 2
    reasons = " ".join(r["reason"] for r in result["rejected"])
    assert "event_id" in reasons and "start < end" in reasons


# --------------------------------------------------------------------- windows


def test_no_declared_window_keeps_everything_unknown(store):
    store.create_node("n1", "n1", "g1", None)
    store.add_events("n1", [event("a", 0, 10)])
    result = analysis.analyze(store, persist=False)
    node = result["nodes"][0]
    assert node["state"] == "unknown"
    assert node["ratio"] is None
    assert node["coverage"] == [[0, 10]]  # evidence is still shown
    assert node["covered_ticks_total"] == 10
    group = result["groups"][0]
    assert group["state"] == "unknown"
    assert group["ratio"] is None
    assert group["unknown_nodes"] == ["n1"]


def test_out_of_window_claims_are_visible_but_excluded(store):
    make_node(store, window=(10, 20))
    store.add_events("n1", [event("a", 0, 30)])
    node = analysis.analyze(store, persist=False)["nodes"][0]
    assert node["covered_ticks"] == 10
    assert node["declared_ticks"] == 10
    assert node["out_of_window_ticks"] == 20
    assert node["ratio"] == 1.0
    assert node["out_of_window"] == [[0, 10], [20, 30]]


# --------------------------------------------------------------------- manifests


def test_latest_manifest_revision_wins_and_history_is_kept(store):
    make_node(store)
    store.add_events("n1", [event("a", 0, 50)])
    store.submit_manifest("n1", "spool", 1, [(50, 100)], note="initial")
    assert store.covered_ticks("n1") == 100
    store.submit_manifest("n1", "spool", 2, [(50, 60)], note="withdraw 60-100")
    assert store.covered_ticks("n1") == 60
    versions = store.manifest_versions("n1")
    assert [v["revision"] for v in versions] == [1, 2]
    assert store.latest_manifests("n1")[0]["revision"] == 2


def test_manifest_duplicate_is_noop_and_conflict_is_rejected(store):
    make_node(store)
    first = store.submit_manifest("n1", "spool", 1, [(0, 10)], note="x")
    again = store.submit_manifest("n1", "spool", 1, [(0, 10)], note="x")
    assert first["state"] == "accepted"
    assert again["state"] == "duplicate"
    assert again["revision"] == first["revision"]
    with pytest.raises(Conflict):
        store.submit_manifest("n1", "spool", 1, [(0, 20)], note="x")
    with pytest.raises(Conflict):
        store.submit_manifest("n1", "spool", 1, [(0, 10)], note="different note")


def test_stale_manifest_revision_is_rejected(store):
    make_node(store)
    store.submit_manifest("n1", "spool", 2, [(0, 10)])
    with pytest.raises(Conflict):
        store.submit_manifest("n1", "spool", 1, [(0, 5)])


def test_manifest_intervals_are_merged_before_storage(store):
    make_node(store)
    store.submit_manifest("n1", "spool", 1, [(0, 30), (10, 50), (45, 60)])
    manifest = store.latest_manifests("n1")[0]
    assert manifest["intervals"] == [[0, 60]]
    assert store.covered_ticks("n1") == 60


# --------------------------------------------------------------------- retractions


def test_retraction_beats_events_and_manifests(store):
    make_node(store)
    store.add_events("n1", [event("a", 0, 100)])
    store.submit_manifest("n1", "spool", 1, [(0, 100)])
    store.add_retraction("n1", 40, 60, "corrupt buffer")
    node = analysis.analyze(store, persist=False)["nodes"][0]
    assert node["coverage"] == [[0, 40], [60, 100]]
    assert [(g["start"], g["end"], g["length"]) for g in node["gaps"]] == [(40, 60, 20)]
    assert node["gaps"][0]["retractions"] == [
        {"start": 40, "end": 60, "reason": "corrupt buffer"}
    ]
    evidence = analysis.gap_evidence(store, "n1", 40, 60)
    assert evidence["is_fully_covered"] is False
    assert any("corrupt buffer" in line for line in evidence["explanation"])


def test_identical_retraction_does_not_bump_revision(store):
    make_node(store)
    store.add_events("n1", [event("a", 0, 10)])
    first = store.add_retraction("n1", 2, 4, "noise")
    revision = store.revision()
    second = store.add_retraction("n1", 2, 4, "noise")
    assert first["created"] is True
    assert second["created"] is False
    assert store.revision() == revision


# --------------------------------------------------------------------- node lifecycle


def test_stopped_node_rejects_events_but_accepts_manifests(store):
    make_node(store)
    store.set_node_status("n1", "stopped")
    with pytest.raises(NodeStopped):
        store.add_events("n1", [event("a", 0, 10)])
    store.submit_manifest("n1", "spool", 1, [(0, 10)], note="backfill while stopped")
    assert store.covered_ticks("n1") == 10
    store.set_node_status("n1", "running")
    assert store.add_events("n1", [event("b", 10, 20)])["accepted"] == 1


def test_node_config_conflict(store):
    make_node(store, window=(0, 100))
    with pytest.raises(Conflict):
        store.create_node("n1", "n1", "g1", (0, 200))
    # identical config is a harmless replay
    again = store.create_node("n1", "n1", "g1", (0, 100))
    assert again["node_id"] == "n1"


# --------------------------------------------------------------------- storage


def test_pruning_to_archive_is_lossless(store):
    make_node(store)
    events = [event(f"e{i}", i, i + 5) for i in range(0, 100, 4)]
    store.add_events("n1", events)
    before = store.segments("n1")
    result = store.prune_events("n1", keep_last=3)
    assert result["archived"] == len(events) - 3
    assert store.segments("n1") == before
    # archived claims are still readable on demand
    archived = store.archived_events("n1", 0, 100, limit=500)
    assert archived
    assert all("archive_id" in a for a in archived)


def test_archived_evidence_explains_a_gap_after_pruning(store):
    make_node(store)
    store.add_events("n1", [event("a", 0, 50), event("b", 50, 100)])
    store.add_retraction("n1", 40, 60, "corrupt buffer")
    store.prune_events("n1", keep_last=0)
    evidence = analysis.gap_evidence(store, "n1", 40, 60)
    assert evidence["uncovered"] == [[40, 60]]
    # Both current claims now live only in the compressed archive, yet the
    # explanation can still show them and the retraction that removed them.
    live = sum(1 for e in evidence["live_events"] if e["start"] < 60 and e["end"] > 40)
    archived = sum(1 for e in evidence["archived_events"] if e["start"] < 60 and e["end"] > 40)
    assert live + archived >= 1
    assert evidence["retractions"][0]["reason"] == "corrupt buffer"


def test_gap_evidence_names_withdrawn_manifest(store):
    make_node(store)
    store.add_events("n1", [event("a", 0, 50)])
    store.submit_manifest("n1", "ring", 1, [(50, 100)], note="before restart")
    store.submit_manifest("n1", "ring", 2, [], note="checksum failure")
    evidence = analysis.gap_evidence(store, "n1", 60, 90)
    assert evidence["uncovered"] == [[60, 90]]
    assert evidence["withdrawn_manifests"][0]["revision"] == 1
    assert any("withdrew" in line for line in evidence["explanation"])


def test_gap_evidence_distinguishes_never_reported(store):
    make_node(store)
    store.add_events("n1", [event("a", 0, 50)])
    evidence = analysis.gap_evidence(store, "n1", 50, 100)
    assert any("not reported" in line for line in evidence["explanation"])


# --------------------------------------------------------------------- analysis runs


def test_compare_runs_reports_group_impact(store):
    store.create_node("n1", "n1", "north", (0, 100))
    store.create_node("n2", "n2", "north", (0, 100))
    store.create_node("n3", "n3", "south", None)
    store.add_events("n1", [event("a", 0, 50)])
    store.add_events("n2", [event("b", 0, 100)])
    store.add_events("n3", [event("c", 0, 10)])
    run1 = analysis.analyze(store, "before")["run_id"]

    store.add_events("n1", [event("d", 50, 100)])
    run2 = analysis.analyze(store, "after backfill")["run_id"]

    diff = analysis.compare_runs(store, run1, run2)
    north = next(g for g in diff["groups"] if g["group"] == "north")
    assert north["covered_delta"] == 50
    assert north["state"] == "known"
    assert north["ratio_delta_fraction"] == "1/4"

    south = next(g for g in diff["groups"] if g["group"] == "south")
    assert south["state"] == "unknown"
    assert south["ratio_delta"] is None
    assert south["ratio_delta_fraction"] is None


def test_compare_runs_reports_added_nodes(store):
    store.create_node("n1", "n1", "g", None)
    store.add_events("n1", [event("a", 0, 10)])
    run1 = analysis.analyze(store, "one unknown node")["run_id"]
    store.create_node("n2", "n2", "g", (0, 10))
    store.add_events("n2", [event("b", 0, 10)])
    run2 = analysis.analyze(store, "one known node added")["run_id"]
    diff = analysis.compare_runs(store, run1, run2)
    added = next(n for n in diff["nodes"] if n["node_id"] == "n2")
    assert added["change"] == "added"
    # The group still contains an unknown node, so no group ratio is invented.
    group = diff["groups"][0]
    assert group["state"] == "unknown"
    assert group["ratio_delta"] is None


def test_analysis_run_persists_exact_fractions(store):
    store.create_node("n1", "n1", "g", (0, 3))
    store.add_events("n1", [event("a", 0, 2)])
    result = analysis.analyze(store, "fractions")
    stored = store.analysis_run(result["run_id"])
    assert stored["groups"][0]["ratio_fraction"] == "2/3"
    assert stored["groups"][0]["ratio"] == round(2 / 3, 10)


def test_every_mutation_carries_revision(store):
    make_node(store)
    r0 = store.revision()
    r1 = store.add_events("n1", [event("a", 0, 10)])["revision"]
    r2 = store.submit_manifest("n1", "m", 1, [(10, 20)])["revision"]
    r3 = store.add_retraction("n1", 15, 20, "x")["revision"]
    assert r0 < r1 < r2 < r3


# --------------------------------------------------------------------- validation


def test_manifest_validation(store):
    make_node(store)
    with pytest.raises(ValidationError):
        store.submit_manifest("n1", "", 1, [(0, 1)])
    with pytest.raises(ValidationError):
        store.submit_manifest("n1", "m", 0, [(0, 1)])
    with pytest.raises(ValidationError):
        store.submit_manifest("n1", "m", True, [(0, 1)])
    with pytest.raises(ValidationError):
        store.submit_manifest("n1", "m", 1, [(5, 5)])


def test_retraction_validation(store):
    make_node(store)
    with pytest.raises(ValidationError):
        store.add_retraction("n1", 5, 5, "reason")
    with pytest.raises(ValidationError):
        store.add_retraction("n1", 0, 5, "   ")
