# Coverage lab

Interval-exact coverage accounting for a fleet of reporting nodes.

Many nodes each report **events** (what they observed) and **local coverage
manifests** (what they claim to have stored). Those two streams are not the
same claim, and neither is a count of messages received. This project merges
the claims into exact tick sets, so coverage is always a length of a merged
interval set — never a message count — and anything the stored evidence cannot
establish is reported as `unknown` instead of being guessed.

```
node agents ──events──┐
             ──manifests (versioned)──┤  ingester  ──►  SQLite store
             ──retractions (operator)─┘                   │
                                                          ├─ interval merger (transactional, materialized segments)
                                                          └─ analyzer ──► HTTP API ──► dashboard
```

The ingester (`store.add_events` / `submit_manifest` / `add_retraction`), the
interval merger (`coverage_lab.intervals` + the materialized
`coverage_segments` table) and the analyzer (`coverage_lab.analysis`) are
separate modules. The web server is only a transport.

## Quick start

```bash
python3 -m coverage_lab.server --seed --reset --port 8765
# open http://127.0.0.1:8765
```

Or seed without serving:

```bash
python3 -m coverage_lab.simulate --reset
```

Tests (pytest; no third-party runtime dependency for the app itself):

```bash
python3 -m venv .venv && .venv/bin/pip install pytest
.venv/bin/python -m pytest -q         # 37 tests
```

The app requires only Python 3.11+ and SQLite.

## Data model

| Table | Holds | Notes |
|---|---|---|
| `nodes` | node id, group, declared window `[start, end)`, running/stopped | no window ⇒ coverage is `unknown` |
| `events` | raw per-node claims | PK `(node_id, event_id)`; retries are duplicates, not new coverage |
| `event_archives` | zlib-compressed chunks of older events | still counted in coverage; readable on demand for evidence |
| `manifests` | versioned local coverage claims | only the highest revision per `(node, name)` counts; older rows retained as evidence |
| `retractions` | operator-withdrawn ranges + reason | always win over events and manifests |
| `coverage_segments` | canonical merged coverage per node | recomputed transactionally from live **and** archived events |
| `analysis_runs` / `analysis_*_rows` | stored snapshots with their store revision | exact fractions, per node and per group |
| `audit_log` | who changed what | |

```
coverage(node) = ( live events ∪ archived events ∪ latest manifests ) \ retractions
```

Coverage is a union, so a tick covered by 40 overlapping reports counts once.
`intervals.measure` only ever runs on a normalized (sorted, disjoint) set.

## What the analyzer guarantees

* **Ratios need a declared window.** `covered / declared` is computed in-window
  only. A node with no window has `state = unknown`, ratio `null`. A group with
  any unknown member also has ratio `null` — no "known subset" percentage is
  fabricated.
* **Exact fractions.** Every ratio is stored both as a float and as a reduced
  fraction (`"19/24"`), so the UI never implies precision it doesn't have.
* **No confidence intervals.** Nothing is estimated: the model records exactly
  which ticks were claimed, so there is no sampling to summarise. The dashboard
  says so in its footer.
* **Staleness is explicit.** Every response carries the store `revision` that
  produced it. Mutations that change coverage bump it; replays that change
  nothing (duplicate events, identical manifest resubmission, identical
  retraction) do not.
* **Out-of-window claims stay visible.** They appear in the node detail and in
  tile form, but are excluded from the ratio (`out_of_window_ticks`).
* **Gaps distinguish "not reported" from "withdrawn".** Gap evidence shows
  claiming events (live and decompressed from the archive), manifest revisions
  that intersect the range, superseded revisions, and retractions with their
  reasons. If nothing was ever reported it says so instead of implying the
  ticks were observed uncovered.

## Revision impact

`POST /api/analyze` stores a snapshot. `GET /api/compare?a=&b=` diffs two
snapshots per group and per node: covered/gap tick deltas, exact ratio deltas,
state transitions, added/removed nodes. When a group is `unknown` in either run
its numeric ratio delta is `null` rather than computed from the known subset.

The dashboard shows this automatically for the two most recent stored runs and
lets you pick any older snapshot to compare against the current one.

## HTTP API

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/state` | current snapshot: nodes, groups, runs, auto-comparison |
| GET | `/api/nodes` · `/api/nodes/{id}` | node list / detail (segments, manifests, retractions, archives) |
| GET | `/api/gap?node=&start=&end=` | evidence for a range |
| GET | `/api/runs` · `/api/runs/{id}` | stored analysis runs |
| GET | `/api/compare?a=&b=` | revision impact |
| GET | `/api/analysis` | compute without storing |
| POST | `/api/nodes` | create a node (`window` nullable) |
| POST | `/api/nodes/{id}/stop` · `/start` | stop (rejects live events) / restart |
| POST | `/api/events` | `{node_id, batch_id?, events:[{event_id,start,end}]}` — idempotent |
| POST | `/api/manifests` | `{node_id, name, revision, intervals:"a-b, c-d", note}` — versioned |
| POST | `/api/retractions` | `{node_id, start, end, reason}` |
| POST | `/api/analyze` | store a snapshot (`note`) |
| POST | `/api/prune` | archive old raw events (`node_id`, `keep_last`) without changing coverage |

Errors are `{"error": "...", "status": n}`; validation is `400`, unknown
objects `404`, version/state conflicts `409` (including events to a stopped
node).

## Failure-recovery semantics

* **Restart + resend.** Events are keyed by `(node, event_id)`; a resent batch
  returns `duplicates: n`, changes no intervals and bumps no revision. The
  demo scenario does exactly this for `n-alpha-01`.
* **Backfill after outage.** Manifests are revisioned; submitting `r2` after
  `r1` replaces `r1` as a claim and keeps `r1` as evidence. Older revisions are
  rejected with `409` rather than silently applied.
* **Withdrawal.** A later revision with fewer intervals re-opens the gap, and
  gap evidence names the revision that withdrew it.
* **Retraction.** Removes the range no matter how many claims cover it, and the
  gap shows the operator's reason.
* **Storage compression.** `prune_events` moves old rows into compressed
  chunks, recomputes segments inside the same transaction, and refuses to
  commit if coverage changed. Archived claims are still counted (once) and
  still readable for evidence.

## The demo scenario

Six nodes are fabricated in `coverage_lab/simulate.py` and written as JSONL
logs to `data/logs/`. The logs are fictional; the arithmetic on them is not.
The scenario deliberately contains overlapping reports, a resent batch, a
manifest withdrawal, a retraction, an out-of-window claim, and a node without a
declared window. Seeding also stores two analysis runs (R1 ingest, R2
revisions/retractions) so the comparison view has history.

## Dashboard

* Stat tiles: covered/declared ticks (exact union), gaps, unknown nodes,
  stopped nodes, change since the previous stored run.
* Group rows: segmented covered/uncovered bar, exact fraction, explicit
  `unknown` state with the offending node ids.
* Node timelines: covered segments, clickable gaps, retraction overlays,
  declared-window band, round tick axis, per-node ratio.
* Inspector: gap evidence (claims, withdrawn manifest revisions, retractions,
  archive reads) plus forms to submit a manifest revision or retract a range.
* Both charts have a table view; tooltips mirror the same values.
* Stop/Start per node, "Store snapshot" and "Compare with current" in one
  control row above the charts.

Colours: one categorical hue for covered ticks, reserved status colours for
gap/retraction/unknown states, always with a label; validated for both themes.

## Layout

```
coverage_lab/
  intervals.py   pure interval algebra (normalize/union/subtract/gaps)
  store.py       SQLite persistence, ingestion, materialized segments, archives
  analysis.py    coverage math, gap evidence, snapshot comparison
  simulate.py    fictional logs + scripted scenario
  server.py      HTTP API + static files
  web/           dashboard (plain HTML/CSS/JS, no build step)
tests/
  test_intervals.py  properties: overlaps count once, idempotent normalization
  test_store.py      ingestion, versions, retractions, pruning, analysis
```
