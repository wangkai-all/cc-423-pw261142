"""HTTP API + static dashboard for the coverage lab.

The ingester, the interval merger (inside :mod:`coverage_lab.store`) and the
analyzer (:mod:`coverage_lab.analysis`) are separate modules; this server is
only a transport.  Every response includes the store ``revision`` so the UI can
tell that it is looking at a stale snapshot.

Run ``python -m coverage_lab.server --seed --port 8765``.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import re
import traceback
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from . import analysis
from . import intervals as iv
from .store import CoverageError, Conflict, NotFound, Store, ValidationError

WEB_ROOT = Path(__file__).resolve().parent / "web"
MAX_BODY = 2 * 1024 * 1024
INTERVAL_RE = re.compile(r"^\s*\d+\s*-\s*\d+\s*$")


def parse_intervals(text: str) -> Tuple[Tuple[int, int], ...]:
    """Parse ``"0-120, 180-210"`` into normalized half-open intervals."""
    if not isinstance(text, str) or not text.strip():
        raise ValidationError("intervals must be a non-empty 'a-b, c-d' string")
    pairs = []
    for chunk in text.split(","):
        if not INTERVAL_RE.match(chunk):
            raise ValidationError(f"cannot parse interval {chunk.strip()!r}; use 'start-end'")
        start_s, end_s = chunk.split("-")
        pairs.append((int(start_s), int(end_s)))
    return iv.normalize(pairs)


class Handler(BaseHTTPRequestHandler):
    server_version = "CoverageLab/1.0"
    store: Store  # injected by make_server

    # ------------------------------------------------------------------ helpers
    def log_message(self, fmt: str, *args: Any) -> None:  # quieter default logging
        print(f"{self.address_string()} {fmt % args}")

    def _send(self, status: int, payload: Any, content_type: str = "application/json") -> None:
        if content_type == "application/json":
            body = json.dumps(payload).encode()
        else:
            body = payload
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, message: str) -> None:
        self._send(status, {"error": message, "status": status})

    def _body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise ValidationError("request body too large")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode())
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationError(f"invalid JSON body: {exc}") from exc
        if not isinstance(payload, dict):
            raise ValidationError("JSON body must be an object")
        return payload

    # ------------------------------------------------------------------ verbs
    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = {k: v[-1] for k, v in parse_qs(parsed.query).items()}
        try:
            if path == "/" or path.startswith("/static/"):
                if method != "GET":
                    return self._error(405, "method not allowed")
                return self._static(path if path.startswith("/static/") else "/")
            route = self._route(method, path, query)
            if route is None:
                return self._error(404, f"no route for {method} {path}")
            status, payload = route
            self._send(status, payload)
        except CoverageError as exc:
            self._error(exc.status, str(exc))
        except (ValueError, KeyError) as exc:
            self._error(400, str(exc))
        except Exception:  # pragma: no cover - defensive
            traceback.print_exc()
            self._error(500, "internal error")

    def _route(
        self, method: str, path: str, query: Dict[str, str]
    ) -> Optional[Tuple[int, Any]]:
        s = self.store
        if method == "GET":
            if path == "/api/state":
                return 200, self._state()
            if path == "/api/nodes":
                return 200, {"revision": s.revision(), "nodes": s.list_nodes()}
            node_match = re.fullmatch(r"/api/nodes/([^/]+)", path)
            if node_match:
                return 200, analysis.node_detail(s, node_match.group(1))
            if path == "/api/analysis":
                return 200, analysis.analyze(s, note="ad hoc", persist=False)
            if path == "/api/runs":
                return 200, {"runs": s.analysis_runs(int(query.get("limit", "30")))}
            gap_match = re.fullmatch(r"/api/runs/(\d+)/gap", path)
            if gap_match:
                # Evidence as of *that stored run's* revision is not
                # reconstructible from today's rows; we answer from current
                # evidence and echo the run so the UI can show both revisions.
                return 200, {
                    "requested_run": s.analysis_run(int(gap_match.group(1)))["run"],
                    "evidence": analysis.gap_evidence(
                        s, query["node"], int(query["start"]), int(query["end"])
                    ),
                }
            if path == "/api/gap":
                return 200, analysis.gap_evidence(
                    s, query["node"], int(query["start"]), int(query["end"])
                )
            if path == "/api/compare":
                return 200, analysis.compare_runs(s, int(query["a"]), int(query["b"]))
            match = re.fullmatch(r"/api/runs/(\d+)", path)
            if match:
                return 200, s.analysis_run(int(match.group(1)))
            return None

        payload = self._body()
        if path == "/api/events":
            result = s.add_events(
                payload.get("node_id", ""),
                payload.get("events", []),
                batch_id=payload.get("batch_id"),
            )
            return 200, result
        if path == "/api/manifests":
            intervals = payload.get("intervals")
            if isinstance(intervals, str):
                intervals = list(parse_intervals(intervals))
            result = s.submit_manifest(
                payload.get("node_id", ""),
                payload.get("name", ""),
                payload.get("revision"),
                intervals or [],
                note=payload.get("note", ""),
            )
            return 200, result
        if path == "/api/retractions":
            result = s.add_retraction(
                payload.get("node_id", ""),
                payload.get("start"),
                payload.get("end"),
                payload.get("reason", ""),
            )
            return 200, result
        if path == "/api/nodes":
            try:
                parsed_window = None if payload.get("window") in (None, "") else iv.validate(
                    tuple(payload["window"])
                )
            except TypeError as exc:
                raise ValidationError(f"window must be [start, end]: {exc}") from exc
            result = s.create_node(
                payload.get("node_id", ""),
                payload.get("name", "") or payload.get("node_id", ""),
                payload.get("group", "") or "ungrouped",
                parsed_window,
            )
            return 200, {"node": result, "revision": s.revision()}
        if path.startswith("/api/nodes/") and path.endswith("/stop"):
            node_id = path.split("/")[3]
            return 200, s.set_node_status(node_id, "stopped")
        if path.startswith("/api/nodes/") and path.endswith("/start"):
            node_id = path.split("/")[3]
            return 200, s.set_node_status(node_id, "running")
        if path == "/api/analyze":
            return 200, analysis.analyze(s, note=payload.get("note", "manual"), persist=True)
        if path == "/api/prune":
            return 200, s.prune_events(payload.get("node_id", ""), int(payload.get("keep_last", 2)))
        return None

    # ------------------------------------------------------------------ data
    def _state(self) -> Dict[str, Any]:
        store = self.store
        runs = store.analysis_runs(30)
        latest = runs[0]["run_id"] if runs else None
        current = analysis.analyze(store, note="dashboard snapshot", persist=False)
        comparison = None
        if latest is not None and len(runs) > 1:
            comparison = analysis.compare_runs(store, runs[1]["run_id"], latest)
        nodes = store.list_nodes()
        return {
            "revision": current["revision"],
            "generated_at": current["generated_at"],
            "t0": store.t0,
            "nodes": current["nodes"],
            "groups": current["groups"],
            "runs": runs,
            "latest_run_id": latest,
            "comparison": comparison,
            "node_meta": {n["node_id"]: n for n in nodes},
        }

    # ------------------------------------------------------------------ static
    def _static(self, path: str) -> None:
        rel = "index.html" if path == "/" else path[len("/static/"):]
        candidate = (WEB_ROOT / rel).resolve()
        if WEB_ROOT.resolve() not in candidate.parents and candidate != WEB_ROOT.resolve():
            return self._error(403, "forbidden")
        if not candidate.is_file():
            return self._error(404, "not found")
        content_type = mimetypes.guess_type(str(candidate))[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in (
            "application/javascript",
            "application/json",
        ):
            content_type += "; charset=utf-8"
        self._send(200, candidate.read_bytes(), content_type)


def make_server(store: Store, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"store": store})
    return ThreadingHTTPServer((host, port), handler)


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="Coverage lab server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--db", default=str(Path.cwd() / "data" / "coverage.db"))
    parser.add_argument("--seed", action="store_true", help="seed the demo scenario if empty")
    parser.add_argument("--reset", action="store_true", help="delete the demo database first")
    args = parser.parse_args(argv)

    db_path = Path(args.db)
    if args.reset and db_path.exists():
        db_path.unlink()
    store = Store(db_path)
    if args.seed and not store.list_nodes():
        from . import simulate

        simulate.write_logs(
            Path(args.db).parent / "logs", simulate.build_log_lines()
        )
        summary = simulate.seed_store(store)
        print(json.dumps(summary, indent=2, sort_keys=True))
    httpd = make_server(store, args.host, args.port)
    print(f"coverage lab on http://{args.host}:{args.port}  (db: {db_path})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
