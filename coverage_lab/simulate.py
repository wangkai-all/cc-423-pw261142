"""Fictional acquisition logs and a scripted scenario that exercises the model.

The logs are synthetic (clearly labelled) but the interval arithmetic is real:
nothing here pre-computes coverage.  Events go through the same ingestion path
as an agent upload, manifests through the same versioning rules, and the
scenario deliberately includes the awkward cases:

* a node restart that re-uploads a batch (duplicates must be ignored),
* a late manifest backfill that closes a gap,
* a manifest revision that *withdraws* a range (gap re-opens),
* an operator retraction (evidence exists but must not count),
* a node with no declared window (state must stay ``unknown``),
* overlapping reports inside one tick (must count once).

Run ``python -m coverage_lab.simulate`` to write ``data/node-*.jsonl`` and
populate ``data/coverage.db``.
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence

from .store import Store

TICK_SECONDS = 60  # one tick == one minute of acquisition

SCENARIO: Sequence[Dict[str, Any]] = (
    {
        "node_id": "n-alpha-01",
        "name": "Alpha ridge",
        "group": "north",
        "window": [0, 240],
        "batches": [
            # 0..60 covered multiple times over: overlapping reports must not
            # inflate coverage.
            [(0, 20), (10, 30), (25, 60), (30, 45)],
            [(60, 90), (90, 120)],
            # 120..180 missing on purpose
            [(180, 200), (190, 210)],
            # restart: the same batch is re-sent below
        ],
        "restart_resend": 2,  # index of the batch that is re-uploaded
    },
    {
        "node_id": "n-bravo-02",
        "name": "Bravo basin",
        "group": "north",
        "window": [0, 240],
        "batches": [
            [(0, 120)],
            [(140, 200)],
        ],
        "manifests": [
            # v1 claims 120..160, v2 withdraws it again.
            {
                "name": "local-ring-buffer",
                "revision": 1,
                "intervals": [(120, 160)],
                "note": "ring buffer snapshot before restart",
            },
            {
                "name": "local-ring-buffer",
                "revision": 2,
                "intervals": [(120, 130)],
                "note": "checksum failure: 130-160 withdrawn",
            },
        ],
    },
    {
        "node_id": "n-charlie-03",
        "name": "Charlie shelf",
        "group": "south",
        "window": [0, 180],
        "batches": [
            [(0, 90)],
            [(120, 180)],  # 90..120 later retracted
        ],
        "retraction": {
            "start": 90,
            "end": 120,
            "reason": "operator: buffer corruption after power loss",
        },
    },
    {
        "node_id": "n-delta-04",
        "name": "Delta trench",
        "group": "south",
        "window": [0, 180],
        "batches": [
            [(0, 60)],
            [(60, 120)],
            # 120..180 backfilled late by manifest
        ],
        "manifests": [
            {
                "name": "backfill-after-outage",
                "revision": 1,
                "intervals": [(120, 180)],
                "note": "replayed from node-local spool",
            }
        ],
    },
    {
        "node_id": "n-echo-05",
        "name": "Echo pass",
        "group": "south",
        "window": None,  # no declared window -> coverage ratio must stay unknown
        "batches": [
            [(0, 100)],
        ],
    },
    {
        "node_id": "n-foxtrot-06",
        "name": "Foxtrot moor",
        "group": "west",
        "window": [0, 120],
        "batches": [
            [(0, 120)],
        ],
    },
)


def build_log_lines(scenario: Sequence[Dict[str, Any]] = SCENARIO) -> Dict[str, List[Dict[str, Any]]]:
    """Expand the scenario into per-node log records (the 'fictional logs')."""
    rng = random.Random(20271120)
    logs: Dict[str, List[Dict[str, Any]]] = {}
    for spec in scenario:
        node_id = spec["node_id"]
        lines: List[Dict[str, Any]] = []
        seq = 0
        for batch_index, batch in enumerate(spec["batches"]):
            batch_id = f"{node_id}-b{batch_index:02d}"
            for start, end in batch:
                # Overlapping raw reports: each event's interval lies inside the
                # batch span.  Because they are merged before measuring, N
                # overlapping reports cover the span once, not N times.
                mid = (start + end) // 2
                offsets = [(start, end)]
                if end - start >= 4:
                    offsets.append((start, mid))
                    offsets.append((mid, end))
                for off_start, off_end in offsets:
                    seq += 1
                    lines.append(
                        {
                            "event_id": f"{node_id}-e{seq:05d}",
                            "batch_id": batch_id,
                            "start": off_start,
                            "end": off_end,
                            "sensor": rng.choice(["hydrophone", "tiltmeter", "geophone"]),
                            "level": rng.choice(["debug", "info", "warn"]),
                            "msg": "window committed to local spool",
                        }
                    )
        logs[node_id] = lines
    return logs


def write_logs(directory: Path, logs: Dict[str, List[Dict[str, Any]]]) -> List[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    paths = []
    for node_id, lines in logs.items():
        path = directory / f"{node_id}.jsonl"
        path.write_text(
            "\n".join(json.dumps(line, sort_keys=True) for line in lines) + "\n",
            encoding="utf-8",
        )
        paths.append(path)
    return paths


def seed_store(store: Store, scenario: Sequence[Dict[str, Any]] = SCENARIO) -> Dict[str, Any]:
    """Populate an empty store and return a summary of what was ingested."""
    logs = build_log_lines(scenario)
    summary: Dict[str, Any] = {"nodes": {}, "duplicate_retries": 0}
    for spec in scenario:
        store.create_node(
            node_id=spec["node_id"],
            name=spec["name"],
            group_name=spec["group"],
            window=None if spec["window"] is None else tuple(spec["window"]),
        )

    # Ingest in batch order, node by node, so the resend lands after the
    # original batch (like a real retry).
    for spec in scenario:
        node_id = spec["node_id"]
        by_batch: Dict[str, List[Dict[str, Any]]] = {}
        order: List[str] = []
        for line in logs[node_id]:
            if line["batch_id"] not in by_batch:
                by_batch[line["batch_id"]] = []
                order.append(line["batch_id"])
            by_batch[line["batch_id"]].append(
                {"event_id": line["event_id"], "start": line["start"], "end": line["end"]}
            )
        accepted = 0
        duplicates = 0
        for batch_id in order:
            result = store.add_events(node_id, by_batch[batch_id], batch_id=batch_id)
            accepted += result["accepted"]
            duplicates += result["duplicates"]
        if "restart_resend" in spec:
            retry_batch = order[spec["restart_resend"]]
            result = store.add_events(
                node_id, by_batch[retry_batch], batch_id=retry_batch + "-retry"
            )
            duplicates += result["duplicates"]
            summary["duplicate_retries"] += result["duplicates"]
        for manifest in spec.get("manifests", []):
            store.submit_manifest(
                node_id,
                manifest["name"],
                manifest["revision"],
                manifest["intervals"],
                note=manifest["note"],
            )
        if "retraction" in spec:
            r = spec["retraction"]
            store.add_retraction(node_id, r["start"], r["end"], r["reason"])
        summary["nodes"][node_id] = {"accepted": accepted, "duplicates": duplicates}

    # Snapshot revision R1, then demonstrate the ways a conclusion moves:
    # a node is stopped (its status is part of the report), a late manifest
    # revision withdraws a range (gap re-opens), and a node is restarted.
    from . import analysis

    analysis.analyze(store, "R1: initial ingest", persist=True)
    store.set_node_status("n-charlie-03", "stopped")
    store.submit_manifest(
        "n-bravo-02",
        "local-ring-buffer",
        3,
        [(120, 125)],
        note="operator review: 125-160 also withdrawn",
    )
    store.add_retraction(
        "n-delta-04", 150, 180, "operator: spool replay failed integrity check"
    )
    store.set_node_status("n-charlie-03", "running")
    analysis.analyze(store, "R2: revisions + retractions", persist=True)

    # Compress raw storage, then prove coverage did not move.
    before = {n: store.covered_ticks(n) for n in summary["nodes"]}
    pruned = {n: store.prune_events(n, keep_last=2) for n in summary["nodes"]}
    after = {n: store.covered_ticks(n) for n in summary["nodes"]}
    summary["pruned"] = pruned
    summary["coverage_before_prune"] = before
    summary["coverage_after_prune"] = after
    summary["prune_was_lossless"] = before == after
    return summary


def default_paths(root: Path) -> Dict[str, Path]:
    return {"logs": root / "data" / "logs", "db": root / "data" / "coverage.db"}


def main(argv: Iterable[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Generate fictional logs and seed the store")
    parser.add_argument("--root", default=str(Path.cwd()), help="project root")
    parser.add_argument("--reset", action="store_true", help="delete an existing database first")
    args = parser.parse_args(list(argv) if argv is not None else None)
    root = Path(args.root).resolve()
    paths = default_paths(root)
    if args.reset and paths["db"].exists():
        paths["db"].unlink()
    store = Store(paths["db"])
    if store.list_nodes():
        print(f"store already has {len(store.list_nodes())} nodes; use --reset to rebuild")
        return 0
    logs = build_log_lines()
    written = write_logs(paths["logs"], logs)
    summary = seed_store(store)
    print(f"wrote {len(written)} fictional log files to {paths['logs']}")
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
