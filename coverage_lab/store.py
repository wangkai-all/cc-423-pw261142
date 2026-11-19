r"""Persistence for the coverage lab.

The store keeps four independent things apart, because conflating them is how
coverage dashboards end up lying:

``events``
    Raw, per-node evidence uploaded by agents.  Deduplicated by
    ``(node_id, event_id)`` so a retry after a crash/restart can be replayed
    safely.  Rows may later be archived (zlib-compressed) to reclaim space, but
    archived claims remain queryable on demand.
``manifests``
    Local coverage manifests, versioned per ``(node, name)``.  Only the highest
    revision contributes coverage; older revisions are retained so the UI can
    explain *why* a gap opened ("a later manifest withdrew this range").
``retractions``
    Operator withdrawals of tick ranges (bad sensors, corrupt buffers).  They
    always win, regardless of how many claims cover the range.

        coverage(node) = ( events ∪ latest manifests ) \ retractions

``coverage_segments``
    A materialized, canonical (sorted/disjoint/merged) copy of
    ``coverage(node)``.  It is recomputed transactionally from live *and*
    archived events, so pruning raw rows cannot change or double count
    coverage, while gap location stays exact.

All mutations that can change coverage bump a monotonic ``revision``.  Replays
that change nothing (duplicate events, identical manifest resubmission) do not
bump it, so failure-recovery retries are idempotent.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import zlib
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from . import intervals as iv


class CoverageError(Exception):
    """Base class for domain errors mapped to HTTP 4xx responses."""

    status = 400


class ValidationError(CoverageError):
    status = 400


class NotFound(CoverageError):
    status = 404


class Conflict(CoverageError):
    status = 409


class NodeStopped(Conflict):
    pass


DEFAULT_T0 = 1_800_000_000  # 2027-01-15T08:00:00Z, the timeline origin for ticks


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _canonical_json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def _normalize_pairs(pairs: Iterable[Sequence[int]]) -> Tuple[iv.Interval, ...]:
    try:
        return iv.normalize(pairs)
    except ValueError as exc:  # pragma: no cover - message passthrough
        raise ValidationError(str(exc)) from exc


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS nodes (
    node_id      TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    group_name   TEXT NOT NULL,
    window_start INTEGER,
    window_end   INTEGER,
    status       TEXT NOT NULL DEFAULT 'running',
    created_at   TEXT NOT NULL,
    CHECK (status IN ('running', 'stopped')),
    CHECK ((window_start IS NULL) = (window_end IS NULL)),
    CHECK (window_end IS NULL OR window_start < window_end)
);
CREATE TABLE IF NOT EXISTS events (
    node_id     TEXT NOT NULL REFERENCES nodes(node_id) ON DELETE CASCADE,
    event_id    TEXT NOT NULL,
    start_tick  INTEGER NOT NULL,
    end_tick    INTEGER NOT NULL,
    batch_id    TEXT,
    received_at TEXT NOT NULL,
    PRIMARY KEY (node_id, event_id),
    CHECK (start_tick >= 0 AND end_tick > start_tick)
);
CREATE INDEX IF NOT EXISTS events_by_node ON events(node_id, start_tick, end_tick);
CREATE TABLE IF NOT EXISTS event_archives (
    archive_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id     TEXT NOT NULL REFERENCES nodes(node_id) ON DELETE CASCADE,
    payload     BLOB NOT NULL,
    event_count INTEGER NOT NULL,
    range_start INTEGER NOT NULL,
    range_end   INTEGER NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS manifests (
    manifest_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id        TEXT NOT NULL REFERENCES nodes(node_id) ON DELETE CASCADE,
    name           TEXT NOT NULL,
    revision       INTEGER NOT NULL,
    note           TEXT NOT NULL DEFAULT '',
    submitted_at   TEXT NOT NULL,
    content_hash   TEXT NOT NULL,
    intervals_json TEXT NOT NULL,
    UNIQUE (node_id, name, revision)
);
CREATE TABLE IF NOT EXISTS retractions (
    retraction_id INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id       TEXT NOT NULL REFERENCES nodes(node_id) ON DELETE CASCADE,
    start_tick    INTEGER NOT NULL,
    end_tick      INTEGER NOT NULL,
    reason        TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    UNIQUE (node_id, start_tick, end_tick, reason),
    CHECK (start_tick >= 0 AND end_tick > start_tick)
);
CREATE TABLE IF NOT EXISTS coverage_segments (
    node_id    TEXT NOT NULL REFERENCES nodes(node_id) ON DELETE CASCADE,
    start_tick INTEGER NOT NULL,
    end_tick   INTEGER NOT NULL,
    PRIMARY KEY (node_id, start_tick),
    CHECK (start_tick >= 0 AND end_tick > start_tick)
);
CREATE TABLE IF NOT EXISTS analysis_runs (
    run_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    revision   INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    note       TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS analysis_node_rows (
    run_id             INTEGER NOT NULL REFERENCES analysis_runs(run_id) ON DELETE CASCADE,
    node_id            TEXT NOT NULL,
    group_name         TEXT NOT NULL,
    window_start       INTEGER,
    window_end         INTEGER,
    declared_ticks     INTEGER NOT NULL,
    covered_ticks      INTEGER NOT NULL,
    ratio              REAL,
    ratio_fraction     TEXT,
    out_of_window_ticks INTEGER NOT NULL,
    gap_count          INTEGER NOT NULL,
    gap_ticks          INTEGER NOT NULL,
    state              TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS analysis_group_rows (
    run_id         INTEGER NOT NULL REFERENCES analysis_runs(run_id) ON DELETE CASCADE,
    group_name     TEXT NOT NULL,
    window_start   INTEGER,
    window_end     INTEGER,
    declared_ticks INTEGER NOT NULL,
    covered_ticks  INTEGER NOT NULL,
    ratio          REAL,
    ratio_fraction TEXT,
    node_count     INTEGER NOT NULL,
    unknown_nodes  TEXT NOT NULL,
    gap_count      INTEGER NOT NULL,
    gap_ticks      INTEGER NOT NULL,
    state          TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
    audit_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    at         TEXT NOT NULL,
    action     TEXT NOT NULL,
    node_id    TEXT,
    detail     TEXT NOT NULL
);
"""


class Store:
    """SQLite-backed repository.  Safe to share across threads: every
    operation uses its own short-lived connection (WAL mode)."""

    def __init__(self, path: str | Path, t0: int = DEFAULT_T0) -> None:
        self.path = Path(path)
        self.t0 = int(t0)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES ('revision', '0')"
            )
            conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES ('t0', ?)",
                (str(self.t0),),
            )
            row = conn.execute("SELECT value FROM meta WHERE key = 't0'").fetchone()
            self.t0 = int(row["value"])

    # ------------------------------------------------------------------ plumbing
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    @contextmanager
    def snapshot_read(self) -> Iterator[sqlite3.Connection]:
        """A consistent read transaction (analysis reads many tables)."""
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    # ------------------------------------------------------------------ meta
    def revision(self, conn: Optional[sqlite3.Connection] = None) -> int:
        if conn is not None:
            row = conn.execute("SELECT value FROM meta WHERE key = 'revision'").fetchone()
            return int(row["value"])
        with self.snapshot_read() as own:
            return self.revision(own)

    def _bump_revision(self, conn: sqlite3.Connection) -> int:
        row = conn.execute("SELECT value FROM meta WHERE key = 'revision'").fetchone()
        value = int(row["value"]) + 1
        conn.execute("UPDATE meta SET value = ? WHERE key = 'revision'", (str(value),))
        return value

    def _audit(
        self, conn: sqlite3.Connection, action: str, node_id: Optional[str], detail: Any
    ) -> None:
        conn.execute(
            "INSERT INTO audit_log(at, action, node_id, detail) VALUES (?, ?, ?, ?)",
            (utcnow(), action, node_id, _canonical_json(detail)),
        )

    # ------------------------------------------------------------------ nodes
    def create_node(
        self,
        node_id: str,
        name: str,
        group_name: str,
        window: Optional[iv.Interval] = None,
        status: str = "running",
    ) -> Dict[str, Any]:
        node_id = (node_id or "").strip()
        if not node_id or len(node_id) > 64:
            raise ValidationError("node_id must be 1-64 characters")
        if status not in ("running", "stopped"):
            raise ValidationError("status must be 'running' or 'stopped'")
        window_start = window_end = None
        if window is not None:
            try:
                window_start, window_end = iv.validate(window)
            except (TypeError, ValueError) as exc:
                raise ValidationError(f"invalid declared window: {exc}") from exc
        with self.transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM nodes WHERE node_id = ?", (node_id,)
            ).fetchone()
            if existing:
                same = (
                    existing["name"] == name
                    and existing["group_name"] == group_name
                    and existing["window_start"] == window_start
                    and existing["window_end"] == window_end
                )
                if not same:
                    raise Conflict(f"node {node_id!r} already exists with different config")
                return self._node_row(existing)
            conn.execute(
                "INSERT INTO nodes(node_id, name, group_name, window_start, window_end,"
                " status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (node_id, name, group_name, window_start, window_end, status, utcnow()),
            )
            self._audit(conn, "node.created", node_id, {"group": group_name})
            return self._node_row(
                conn.execute("SELECT * FROM nodes WHERE node_id = ?", (node_id,)).fetchone()
            )

    @staticmethod
    def _node_row(row: sqlite3.Row) -> Dict[str, Any]:
        window = (
            None
            if row["window_start"] is None
            else [row["window_start"], row["window_end"]]
        )
        return {
            "node_id": row["node_id"],
            "name": row["name"],
            "group": row["group_name"],
            "window": window,
            "status": row["status"],
        }

    def get_node(self, node_id: str, conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
        def query(c: sqlite3.Connection) -> sqlite3.Row:
            row = c.execute("SELECT * FROM nodes WHERE node_id = ?", (node_id,)).fetchone()
            if row is None:
                raise NotFound(f"unknown node {node_id!r}")
            return row

        if conn is not None:
            return self._node_row(query(conn))
        with self.snapshot_read() as own:
            return self._node_row(query(own))

    def list_nodes(self, conn: Optional[sqlite3.Connection] = None) -> List[Dict[str, Any]]:
        if conn is not None:
            rows = conn.execute("SELECT * FROM nodes ORDER BY group_name, node_id").fetchall()
            return [self._node_row(r) for r in rows]
        with self.snapshot_read() as own:
            return self.list_nodes(own)

    def set_node_status(self, node_id: str, status: str) -> Dict[str, Any]:
        if status not in ("running", "stopped"):
            raise ValidationError("status must be 'running' or 'stopped'")
        with self.transaction() as conn:
            node = conn.execute("SELECT * FROM nodes WHERE node_id = ?", (node_id,)).fetchone()
            if node is None:
                raise NotFound(f"unknown node {node_id!r}")
            if node["status"] != status:
                conn.execute(
                    "UPDATE nodes SET status = ? WHERE node_id = ?", (status, node_id)
                )
                self._audit(conn, "node.status", node_id, {"from": node["status"], "to": status})
            return self._node_row(
                conn.execute("SELECT * FROM nodes WHERE node_id = ?", (node_id,)).fetchone()
            )

    # ------------------------------------------------------------------ events
    def add_events(
        self,
        node_id: str,
        events: Sequence[Dict[str, Any]],
        batch_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Ingest a batch idempotently.

        Returns ``{"accepted", "duplicates", "rejected", "revision"}``.
        Invalid individual events are reported back, never silently dropped.
        A stopped node rejects the whole batch with :class:`NodeStopped`.
        """
        if not isinstance(events, Sequence) or isinstance(events, (str, bytes)):
            raise ValidationError("events must be a list")
        if len(events) > 10_000:
            raise ValidationError("at most 10000 events per batch")
        received_at = utcnow()
        accepted = 0
        duplicates = 0
        rejected: List[Dict[str, Any]] = []
        with self.transaction() as conn:
            node = conn.execute("SELECT * FROM nodes WHERE node_id = ?", (node_id,)).fetchone()
            if node is None:
                raise NotFound(f"unknown node {node_id!r}")
            if node["status"] == "stopped":
                raise NodeStopped(
                    f"node {node_id!r} is stopped; live events are not accepted "
                    "(use a manifest to backfill after restart)"
                )
            for raw in events:
                event_id = str((raw or {}).get("event_id", "")).strip()
                if not event_id or len(event_id) > 128:
                    rejected.append(
                        {"event_id": event_id or None, "reason": "event_id must be 1-128 chars"}
                    )
                    continue
                try:
                    start, end = iv.validate((raw.get("start"), raw.get("end")))
                except (ValueError, TypeError) as exc:
                    rejected.append({"event_id": event_id, "reason": str(exc)})
                    continue
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO events(node_id, event_id, start_tick, end_tick,"
                    " batch_id, received_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (node_id, event_id, start, end, batch_id, received_at),
                )
                if cursor.rowcount == 1:
                    accepted += 1
                else:
                    duplicates += 1
            revision = self.revision(conn)
            if accepted:
                revision = self._bump_revision(conn)
                self._recompute_segments(conn, node_id)
            self._audit(
                conn,
                "events.ingest",
                node_id,
                {
                    "batch_id": batch_id,
                    "accepted": accepted,
                    "duplicates": duplicates,
                    "rejected": len(rejected),
                },
            )
        return {
            "node_id": node_id,
            "accepted": accepted,
            "duplicates": duplicates,
            "rejected": rejected,
            "revision": revision,
        }

    # ------------------------------------------------------------------ manifests
    def submit_manifest(
        self,
        node_id: str,
        name: str,
        revision: int,
        intervals: Sequence[Sequence[int]],
        note: str = "",
    ) -> Dict[str, Any]:
        """Submit a revision of a local coverage manifest.

        * same revision, same content  -> ``{"state": "duplicate"}``
        * same revision, other content -> 409 conflict
        * older revision                -> 409 stale (never silently ignored)
        * newer revision                -> replaces older ones for coverage,
          but older rows are retained for evidence.
        """
        if not name or len(name) > 64:
            raise ValidationError("manifest name must be 1-64 characters")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise ValidationError("revision must be a positive integer")
        normalized = _normalize_pairs(intervals or [])
        content_hash = _sha256([list(p) for p in normalized])
        with self.transaction() as conn:
            if conn.execute("SELECT 1 FROM nodes WHERE node_id = ?", (node_id,)).fetchone() is None:
                raise NotFound(f"unknown node {node_id!r}")
            existing = conn.execute(
                "SELECT * FROM manifests WHERE node_id = ? AND name = ? AND revision = ?",
                (node_id, name, revision),
            ).fetchone()
            if existing is not None:
                if existing["content_hash"] == content_hash and existing["note"] == note:
                    return {
                        "state": "duplicate",
                        "manifest": self._manifest_row(existing),
                        "revision": self.revision(conn),
                    }
                raise Conflict(
                    f"manifest {name!r} revision {revision} already exists with different content"
                )
            newest = conn.execute(
                "SELECT MAX(revision) AS rev FROM manifests WHERE node_id = ? AND name = ?",
                (node_id, name),
            ).fetchone()["rev"]
            if newest is not None and revision <= newest:
                raise Conflict(
                    f"stale manifest {name!r} revision {revision}; current revision is {newest}"
                )
            inserted = conn.execute(
                "INSERT INTO manifests(node_id, name, revision, note, submitted_at,"
                " content_hash, intervals_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    node_id,
                    name,
                    revision,
                    note,
                    utcnow(),
                    content_hash,
                    _canonical_json([list(p) for p in normalized]),
                ),
            )
            new_revision = self._bump_revision(conn)
            self._recompute_segments(conn, node_id)
            self._audit(
                conn,
                "manifest.submit",
                node_id,
                {
                    "name": name,
                    "revision": revision,
                    "intervals": len(normalized),
                    "ticks": iv.measure(normalized),
                },
            )
            row = conn.execute(
                "SELECT * FROM manifests WHERE manifest_id = ?", (inserted.lastrowid,)
            ).fetchone()
        return {"state": "accepted", "manifest": self._manifest_row(row), "revision": new_revision}

    @staticmethod
    def _manifest_row(row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "manifest_id": row["manifest_id"],
            "node_id": row["node_id"],
            "name": row["name"],
            "revision": row["revision"],
            "note": row["note"],
            "submitted_at": row["submitted_at"],
            "intervals": [list(p) for p in json.loads(row["intervals_json"])],
        }

    def manifest_versions(
        self, node_id: str, conn: Optional[sqlite3.Connection] = None
    ) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM manifests WHERE node_id = ? ORDER BY name, revision"
        if conn is not None:
            return [self._manifest_row(r) for r in conn.execute(sql, (node_id,)).fetchall()]
        with self.snapshot_read() as own:
            return self.manifest_versions(node_id, own)

    def latest_manifests(
        self, node_id: str, conn: Optional[sqlite3.Connection] = None
    ) -> List[Dict[str, Any]]:
        if conn is not None:
            return list(self._latest_manifests(node_id, conn))
        with self.snapshot_read() as own:
            return list(self._latest_manifests(node_id, own))

    def _latest_manifests(
        self, node_id: str, conn: sqlite3.Connection
    ) -> Iterator[Dict[str, Any]]:
        rows = conn.execute(
            "SELECT m.* FROM manifests m JOIN ("
            "  SELECT name, MAX(revision) AS rev FROM manifests WHERE node_id = ? GROUP BY name"
            ") top ON m.name = top.name AND m.revision = top.rev WHERE m.node_id = ?"
            " ORDER BY m.name",
            (node_id, node_id),
        ).fetchall()
        for row in rows:
            yield self._manifest_row(row)

    # ------------------------------------------------------------------ retractions
    def add_retraction(
        self, node_id: str, start: int, end: int, reason: str
    ) -> Dict[str, Any]:
        try:
            start, end = iv.validate((start, end))
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"invalid retraction range: {exc}") from exc
        reason = (reason or "").strip()
        if not reason or len(reason) > 500:
            raise ValidationError("reason must be 1-500 characters")
        with self.transaction() as conn:
            if conn.execute("SELECT 1 FROM nodes WHERE node_id = ?", (node_id,)).fetchone() is None:
                raise NotFound(f"unknown node {node_id!r}")
            cursor = conn.execute(
                "INSERT OR IGNORE INTO retractions(node_id, start_tick, end_tick, reason,"
                " created_at) VALUES (?, ?, ?, ?, ?)",
                (node_id, start, end, reason, utcnow()),
            )
            created = cursor.rowcount == 1
            revision = self.revision(conn)
            if created:
                revision = self._bump_revision(conn)
                self._recompute_segments(conn, node_id)
                self._audit(
                    conn, "retraction.add", node_id,
                    {"start": start, "end": end, "reason": reason},
                )
        return {
            "created": created,
            "node_id": node_id,
            "start": start,
            "end": end,
            "reason": reason,
            "revision": revision,
        }

    def retractions(
        self, node_id: str, conn: Optional[sqlite3.Connection] = None
    ) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM retractions WHERE node_id = ? ORDER BY start_tick, end_tick"
        if conn is not None:
            rows = conn.execute(sql, (node_id,)).fetchall()
        else:
            with self.snapshot_read() as own:
                rows = own.execute(sql, (node_id,)).fetchall()
        return [
            {
                "retraction_id": r["retraction_id"],
                "start": r["start_tick"],
                "end": r["end_tick"],
                "reason": r["reason"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    # ------------------------------------------------------------------ segments
    def _claim_union(
        self, conn: sqlite3.Connection, node_id: str, include_archives: bool = True
    ) -> Tuple[iv.Interval, ...]:
        claims: List[Sequence[int]] = [
            (r["start_tick"], r["end_tick"])
            for r in conn.execute(
                "SELECT start_tick, end_tick FROM events WHERE node_id = ?", (node_id,)
            )
        ]
        for manifest in self._latest_manifests(node_id, conn):
            claims.extend(manifest["intervals"])
        if include_archives:
            for archive in self._archive_rows(conn, node_id):
                claims.extend(archive["intervals"])
        return iv.normalize(claims)

    def _archive_rows(
        self, conn: sqlite3.Connection, node_id: str
    ) -> Iterator[Dict[str, Any]]:
        for row in conn.execute(
            "SELECT * FROM event_archives WHERE node_id = ? ORDER BY archive_id",
            (node_id,),
        ):
            payload = json.loads(zlib.decompress(row["payload"]).decode())
            yield {
                "archive_id": row["archive_id"],
                "created_at": row["created_at"],
                "events": payload,
                "intervals": [(e["start"], e["end"]) for e in payload],
            }

    def _recompute_segments(self, conn: sqlite3.Connection, node_id: str) -> None:
        claims = self._claim_union(conn, node_id)
        retracted = [
            (r["start_tick"], r["end_tick"])
            for r in conn.execute(
                "SELECT start_tick, end_tick FROM retractions WHERE node_id = ?", (node_id,)
            )
        ]
        segments = iv.subtract(claims, retracted)
        conn.execute("DELETE FROM coverage_segments WHERE node_id = ?", (node_id,))
        conn.executemany(
            "INSERT INTO coverage_segments(node_id, start_tick, end_tick) VALUES (?, ?, ?)",
            [(node_id, start, end) for start, end in segments],
        )

    def segments(
        self, node_id: str, conn: Optional[sqlite3.Connection] = None
    ) -> Tuple[iv.Interval, ...]:
        sql = (
            "SELECT start_tick, end_tick FROM coverage_segments"
            " WHERE node_id = ? ORDER BY start_tick"
        )
        if conn is not None:
            rows = conn.execute(sql, (node_id,)).fetchall()
        else:
            with self.snapshot_read() as own:
                rows = own.execute(sql, (node_id,)).fetchall()
        return tuple((r["start_tick"], r["end_tick"]) for r in rows)

    def covered_ticks(self, node_id: str) -> int:
        return iv.measure(self.segments(node_id))

    # ------------------------------------------------------------------ archives
    def prune_events(self, node_id: str, keep_last: int = 200) -> Dict[str, Any]:
        """Archive all but the newest ``keep_last`` raw events for a node.

        Coverage is unaffected: archived claims still feed the materialized
        segments and gap evidence reads them on demand.  This is lossless
        compression of storage, not of meaning.
        """
        if isinstance(keep_last, bool) or not isinstance(keep_last, int) or keep_last < 0:
            raise ValidationError("keep_last must be a non-negative integer")
        with self.transaction() as conn:
            if conn.execute("SELECT 1 FROM nodes WHERE node_id = ?", (node_id,)).fetchone() is None:
                raise NotFound(f"unknown node {node_id!r}")
            before = self.segments(node_id, conn)
            rows = conn.execute(
                "SELECT rowid AS rid, * FROM events WHERE node_id = ?"
                " ORDER BY received_at, rowid",
                (node_id,),
            ).fetchall()
            to_archive = rows[: max(0, len(rows) - keep_last)]
            if not to_archive:
                return {"archived": 0, "remaining": len(rows), "revision": self.revision(conn)}
            payload = [
                {
                    "event_id": r["event_id"],
                    "start": r["start_tick"],
                    "end": r["end_tick"],
                    "batch_id": r["batch_id"],
                    "received_at": r["received_at"],
                }
                for r in to_archive
            ]
            conn.execute(
                "INSERT INTO event_archives(node_id, payload, event_count, range_start,"
                " range_end, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    node_id,
                    zlib.compress(_canonical_json(payload).encode(), level=9),
                    len(payload),
                    min(e["start"] for e in payload),
                    max(e["end"] for e in payload),
                    utcnow(),
                ),
            )
            conn.executemany(
                "DELETE FROM events WHERE node_id = ? AND event_id = ?",
                [(node_id, e["event_id"]) for e in payload],
            )
            self._recompute_segments(conn, node_id)
            after = self.segments(node_id, conn)
            self._audit(conn, "events.prune", node_id, {"archived": len(payload), "keep_last": keep_last})
            if before != after:  # pragma: no cover - would indicate a real bug
                raise Conflict("pruning changed coverage; refusing to commit")
        return {
            "archived": len(payload),
            "remaining": len(rows) - len(payload),
            "revision": self.revision(),
        }

    def live_events(
        self,
        node_id: str,
        start: Optional[int] = None,
        end: Optional[int] = None,
        limit: int = 200,
        conn: Optional[sqlite3.Connection] = None,
    ) -> List[Dict[str, Any]]:
        clauses = ["node_id = ?"]
        params: List[Any] = [node_id]
        if end is not None:
            clauses.append("start_tick < ?")
            params.append(end)
        if start is not None:
            clauses.append("end_tick > ?")
            params.append(start)
        sql = (
            "SELECT * FROM events WHERE " + " AND ".join(clauses)
            + " ORDER BY start_tick, event_id LIMIT ?"
        )
        params.append(limit)
        if conn is not None:
            rows = conn.execute(sql, params).fetchall()
        else:
            with self.snapshot_read() as own:
                rows = own.execute(sql, params).fetchall()
        return [
            {
                "event_id": r["event_id"],
                "start": r["start_tick"],
                "end": r["end_tick"],
                "batch_id": r["batch_id"],
                "received_at": r["received_at"],
            }
            for r in rows
        ]

    def archived_events(
        self,
        node_id: str,
        start: Optional[int] = None,
        end: Optional[int] = None,
        limit: int = 200,
        conn: Optional[sqlite3.Connection] = None,
    ) -> List[Dict[str, Any]]:
        """Decompress archived event chunks on demand (never for counting)."""

        def collect(c: sqlite3.Connection) -> List[Dict[str, Any]]:
            out: List[Dict[str, Any]] = []
            for archive in self._archive_rows(c, node_id):
                for event in archive["events"]:
                    if end is not None and event["start"] >= end:
                        continue
                    if start is not None and event["end"] <= start:
                        continue
                    out.append({**event, "archive_id": archive["archive_id"]})
                    if len(out) >= limit:
                        return out
            return out

        if conn is not None:
            return collect(conn)
        with self.snapshot_read() as own:
            return collect(own)

    def archive_stats(self, node_id: str) -> Dict[str, Any]:
        with self.snapshot_read() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS chunks, COALESCE(SUM(event_count), 0) AS events,"
                " COALESCE(SUM(LENGTH(payload)), 0) AS bytes"
                " FROM event_archives WHERE node_id = ?",
                (node_id,),
            ).fetchone()
            live = conn.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(LENGTH(event_id) + 24), 0) AS bytes"
                " FROM events WHERE node_id = ?",
                (node_id,),
            ).fetchone()
        return {
            "archived_chunks": row["chunks"],
            "archived_events": row["events"],
            "archived_bytes": row["bytes"],
            "live_events": live["n"],
            "live_approx_bytes": live["bytes"],
        }

    # ------------------------------------------------------------------ analysis rows
    def save_analysis_run(
        self,
        revision: int,
        note: str,
        node_rows: Sequence[Dict[str, Any]],
        group_rows: Sequence[Dict[str, Any]],
    ) -> int:
        with self.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO analysis_runs(revision, created_at, note) VALUES (?, ?, ?)",
                (revision, utcnow(), note),
            )
            run_id = int(cursor.lastrowid)
            conn.executemany(
                "INSERT INTO analysis_node_rows(run_id, node_id, group_name, window_start,"
                " window_end, declared_ticks, covered_ticks, ratio, ratio_fraction,"
                " out_of_window_ticks, gap_count, gap_ticks, state)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        run_id, r["node_id"], r["group"], _w0(r["window"]),
                        _w1(r["window"]), r["declared_ticks"], r["covered_ticks"],
                        r["ratio"], r["ratio_fraction"], r["out_of_window_ticks"],
                        r["gap_count"], r["gap_ticks"], r["state"],
                    )
                    for r in node_rows
                ],
            )
            conn.executemany(
                "INSERT INTO analysis_group_rows(run_id, group_name, window_start,"
                " window_end, declared_ticks, covered_ticks, ratio, ratio_fraction,"
                " node_count, unknown_nodes, gap_count, gap_ticks, state)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        run_id, r["group"], _w0(r["window"]), _w1(r["window"]),
                        r["declared_ticks"], r["covered_ticks"], r["ratio"],
                        r["ratio_fraction"], r["node_count"],
                        _canonical_json(r["unknown_nodes"]), r["gap_count"],
                        r["gap_ticks"], r["state"],
                    )
                    for r in group_rows
                ],
            )
            return run_id

    def analysis_run(self, run_id: int) -> Dict[str, Any]:
        with self.snapshot_read() as conn:
            run = conn.execute("SELECT * FROM analysis_runs WHERE run_id = ?", (run_id,)).fetchone()
            if run is None:
                raise NotFound(f"unknown analysis run {run_id}")
            nodes = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM analysis_node_rows WHERE run_id = ? ORDER BY group_name, node_id",
                    (run_id,),
                )
            ]
            groups = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM analysis_group_rows WHERE run_id = ? ORDER BY group_name",
                    (run_id,),
                )
            ]
        for g in groups:
            g["unknown_nodes"] = json.loads(g["unknown_nodes"])
            g["window"] = _window_of(g.pop("window_start"), g.pop("window_end"))
        for n in nodes:
            n["window"] = _window_of(n.pop("window_start"), n.pop("window_end"))
        return {"run": dict(run), "nodes": nodes, "groups": groups}

    def analysis_runs(self, limit: int = 20) -> List[Dict[str, Any]]:
        with self.snapshot_read() as conn:
            rows = conn.execute(
                "SELECT * FROM analysis_runs ORDER BY run_id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]


def _w0(window: Optional[Sequence[int]]) -> Optional[int]:
    return None if window is None else int(window[0])


def _w1(window: Optional[Sequence[int]]) -> Optional[int]:
    return None if window is None else int(window[1])


def _window_of(start: Optional[int], end: Optional[int]) -> Optional[List[int]]:
    return None if start is None or end is None else [start, end]
