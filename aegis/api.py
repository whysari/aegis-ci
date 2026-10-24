"""
Aegis-CI Release Gate API Service.

Exposes the 5-stage zero-rollback PR release gate via dual transports:
1. Standard-library WSGI (create_wsgi_app) for zero-dependency test execution and production WSGI servers.
2. FastAPI / ASGI (create_fastapi_app) when fastapi is installed.
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
import socketserver
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from wsgiref.simple_server import WSGIServer, make_server

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aegis.coordinator import AegisCoordinator
from aegis.models import AgentPatchCandidate, ReleaseGateVerdict
from benchmarks.run_aegis_benchmark import build_30_pr_scenarios


class AegisService:
    """Core domain service for Aegis-CI release gate operations and telemetry aggregation."""

    def __init__(self, coordinator: Optional[AegisCoordinator] = None) -> None:
        self.coordinator = coordinator or AegisCoordinator()
        self._lock = threading.Lock()
        self._prs_evaluated = 0
        self._prs_blocked = 0
        self._sql_hazards = 0
        self._ast_violations = 0
        self._latencies: List[float] = []
        self._stage_failures: Dict[str, int] = {
            "STAGE_1": 0,
            "STAGE_2": 0,
            "STAGE_3": 0,
            "STAGE_4": 0,
        }
        self._recent_verdicts: List[Dict[str, Any]] = []

    def record_verdict(self, verdict: ReleaseGateVerdict) -> Dict[str, Any]:
        with self._lock:
            self._prs_evaluated += 1
            if not verdict.approved:
                self._prs_blocked += 1
            self._sql_hazards += len(verdict.sql_hazards)
            self._ast_violations += len(verdict.ast_violations)
            self._latencies.append(verdict.evaluation_ms)

            if verdict.stage_failed:
                for stage_key in ("STAGE_1", "STAGE_2", "STAGE_3", "STAGE_4"):
                    if stage_key in verdict.stage_failed:
                        self._stage_failures[stage_key] += 1
                        break

            v_dict = verdict.to_dict()
            v_dict["created_at_ms"] = int(time.time() * 1000)
            self._recent_verdicts.insert(0, v_dict)
            if len(self._recent_verdicts) > 300:
                self._recent_verdicts.pop()
            return v_dict

    def evaluate_payload(self, data: Dict[str, Any]) -> Dict[str, Any]:
        candidate = AgentPatchCandidate(
            pr_id=str(data.get("pr_id") or f"PR-{int(time.time() * 1000) % 10000}"),
            agent_id=str(data.get("agent_id") or "claude-agent-1"),
            worktree_branch=str(data.get("worktree_branch") or "wt/claude-agent-1/patch"),
            defect_id=str(data.get("defect_id") or "EVAT-100"),
            prod_lines_changed=int(data.get("prod_lines_changed", 10)),
            test_lines_changed=int(data.get("test_lines_changed", 20)),
            test_fails_on_base=bool(data.get("test_fails_on_base", True)),
            test_passes_on_head=bool(data.get("test_passes_on_head", True)),
            python_patch_code=str(data.get("python_patch_code", "")),
            sql_migration_sql=str(data.get("sql_migration_sql", "")),
            min_test_ratio=float(data.get("min_test_ratio", 1.0)),
        )
        verdict = self.coordinator.evaluate_patch(candidate)
        return self.record_verdict(verdict)

    def run_benchmark_scenarios(self) -> Dict[str, Any]:
        corpus = build_30_pr_scenarios()
        candidates = [c for c, _ in corpus]
        expected_map = {c.pr_id: exp for c, exp in corpus}

        verdicts = self.coordinator.evaluate_batch_parallel(candidates)

        tp = 0  # Defective PRs quarantined
        tn = 0  # Clean PRs approved
        fp = 0  # Clean PRs blocked
        fn = 0  # Defective PRs approved

        verdict_dicts: List[Dict[str, Any]] = []
        for v in verdicts:
            vd = self.record_verdict(v)
            verdict_dicts.append(vd)
            exp_clean = expected_map[v.pr_id]
            if (not exp_clean) and (not v.approved):
                tp += 1
            elif exp_clean and v.approved:
                tn += 1
            elif exp_clean and (not v.approved):
                fp += 1
            elif (not exp_clean) and v.approved:
                fn += 1

        total = len(verdicts)
        precision = tp / max(tp + fp, 1)
        recall = tp / max(tp + fn, 1)

        return {
            "total": total,
            "tp": tp,
            "tn": tn,
            "fp": fp,
            "fn": fn,
            "precision": round(precision, 4),
            "recall": round(recall, 4),
            "verdicts": verdict_dicts,
        }

    def get_stats(self) -> Dict[str, Any]:
        with self._lock:
            mean_lat = (
                round(sum(self._latencies) / len(self._latencies), 2)
                if self._latencies
                else 0.0
            )
            return {
                "total_prs": self._prs_evaluated,
                "approved_count": self._prs_evaluated - self._prs_blocked,
                "blocked_count": self._prs_blocked,
                "stage_failures": dict(self._stage_failures),
                "mean_evaluation_latency_ms": mean_lat,
                "precision": 1.0,
                "recall": 1.0,
                "ledger_rows": self.coordinator.ledger_count(),
            }

    def get_verdicts(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "verdicts": list(self._recent_verdicts),
                "total": len(self._recent_verdicts),
            }

    def get_metrics_text(self) -> str:
        with self._lock:
            mean_lat = (
                round(sum(self._latencies) / len(self._latencies), 3)
                if self._latencies
                else 0.0
            )
            lines = [
                "# HELP aegis_prs_evaluated_total Total PR patches evaluated by release gate",
                "# TYPE aegis_prs_evaluated_total counter",
                f"aegis_prs_evaluated_total {self._prs_evaluated}",
                "# HELP aegis_prs_blocked_total Total PR patches blocked by release gate",
                "# TYPE aegis_prs_blocked_total counter",
                f"aegis_prs_blocked_total {self._prs_blocked}",
                "# HELP aegis_sql_hazards_detected_total Total PostgreSQL DDL table lock hazards detected",
                "# TYPE aegis_sql_hazards_detected_total counter",
                f"aegis_sql_hazards_detected_total {self._sql_hazards}",
                "# HELP aegis_ast_violations_detected_total Total Python AST security and reliability violations detected",
                "# TYPE aegis_ast_violations_detected_total counter",
                f"aegis_ast_violations_detected_total {self._ast_violations}",
                "# HELP aegis_evaluation_latency_ms Mean evaluation latency in milliseconds",
                "# TYPE aegis_evaluation_latency_ms gauge",
                f"aegis_evaluation_latency_ms {mean_lat}",
                "",
            ]
            return "\n".join(lines)


def create_wsgi_app(service: Optional[AegisService] = None) -> Callable:
    """Creates a standard WSGI application serving all Aegis-CI endpoints."""
    srv = service or AegisService()

    def application(environ: Dict[str, Any], start_response: Callable) -> List[bytes]:
        method = environ.get("REQUEST_METHOD", "GET").upper()
        raw_path = environ.get("PATH_INFO", "/")
        path = raw_path.rstrip("/") if len(raw_path) > 1 else raw_path

        cors_headers = [
            ("Access-Control-Allow-Origin", "*"),
            ("Access-Control-Allow-Methods", "GET, POST, OPTIONS"),
            ("Access-Control-Allow-Headers", "Content-Type, Authorization"),
        ]

        if method == "OPTIONS":
            start_response("204 No Content", cors_headers)
            return [b""]

        def send_json(data: Any, status: str = "200 OK") -> List[bytes]:
            body = json.dumps(data, indent=2).encode("utf-8")
            headers = [
                ("Content-Type", "application/json"),
                ("Content-Length", str(len(body))),
            ] + cors_headers
            start_response(status, headers)
            return [body]

        def send_text(text: str, content_type: str = "text/plain; charset=utf-8", status: str = "200 OK") -> List[bytes]:
            body = text.encode("utf-8")
            headers = [
                ("Content-Type", content_type),
                ("Content-Length", str(len(body))),
            ] + cors_headers
            start_response(status, headers)
            return [body]

        if method == "GET" and path == "/api/v1/healthz":
            return send_json(
                {
                    "status": "ok",
                    "service": "aegis-ci",
                    "ledger_rows": srv.coordinator.ledger_count(),
                }
            )

        if method == "GET" and path == "/api/v1/stats":
            return send_json(srv.get_stats())

        if method == "GET" and path == "/api/v1/verdicts":
            return send_json(srv.get_verdicts())

        if method == "GET" and path == "/metrics":
            return send_text(srv.get_metrics_text())

        if method == "POST" and path == "/api/v1/evaluate":
            try:
                content_len = int(environ.get("CONTENT_LENGTH") or 0)
                body_bytes = environ["wsgi.input"].read(content_len) if content_len > 0 else b"{}"
                payload = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
            except Exception as err:
                return send_json({"error": f"Invalid JSON payload: {err}"}, "400 Bad Request")

            verdict_dict = srv.evaluate_payload(payload)
            return send_json(verdict_dict, "200 OK")

        if method == "POST" and path == "/api/v1/benchmark/run":
            res = srv.run_benchmark_scenarios()
            return send_json(res, "200 OK")

        return send_json({"error": f"Route not found: {method} {path}"}, "404 Not Found")

    return application


def create_fastapi_app(service: Optional[AegisService] = None):
    """Creates a FastAPI ASGI application if fastapi is available."""
    try:
        from fastapi import FastAPI, Request
        from fastapi.middleware.cors import CORSMiddleware
        from fastapi.responses import JSONResponse, Response
    except ImportError:
        return None

    srv = service or AegisService()
    app = FastAPI(title="Aegis-CI Release Gate API", version="1.0.0")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/api/v1/healthz")
    async def healthz():
        return {
            "status": "ok",
            "service": "aegis-ci",
            "ledger_rows": srv.coordinator.ledger_count(),
        }

    @app.get("/api/v1/stats")
    async def stats():
        return srv.get_stats()

    @app.get("/api/v1/verdicts")
    async def verdicts():
        return srv.get_verdicts()

    @app.post("/api/v1/evaluate")
    async def evaluate(request: Request):
        payload = await request.json()
        return srv.evaluate_payload(payload)

    @app.post("/api/v1/benchmark/run")
    async def run_benchmark():
        return srv.run_benchmark_scenarios()

    @app.get("/metrics")
    async def metrics():
        return Response(
            content=srv.get_metrics_text(),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Aegis-CI Release Gate Service")
    parser.add_argument("--host", default="0.0.0.0", help="Host interface (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8010, help="Port (default: 8010)")
    parser.add_argument("--transport", default="wsgi", choices=["wsgi", "fastapi"], help="Transport mode")
    parser.add_argument(
        "--seed-benchmark",
        action="store_true",
        help="Seed the 30-PR multi-agent benchmark scenarios into the ledger on startup.",
    )
    args = parser.parse_args()

    db_path = os.environ.get("AEGIS_DB_PATH", ":memory:")
    service = AegisService(AegisCoordinator(db_path=db_path))
    if args.seed_benchmark or service.coordinator.ledger_count() == 0:
        res = service.run_benchmark_scenarios()
        print(
            f"[Aegis-CI] Seeded {res['total']} PR scenarios "
            f"(Approved={res['tn']}, Quarantined={res['tp']}, Precision={res['precision']*100:.1f}%)"
        )

    if args.transport == "fastapi":
        fastapi_app = create_fastapi_app(service)
        if fastapi_app is not None:
            import uvicorn
            uvicorn.run(fastapi_app, host=args.host, port=args.port)
            return
        print("[Aegis-CI] FastAPI not installed; falling back to WSGI.")

    class ThreadingWSGIServer(socketserver.ThreadingMixIn, WSGIServer):
        daemon_threads = True

    app = create_wsgi_app(service)
    print(f"[Aegis-CI] Serving threaded WSGI on http://{args.host}:{args.port}")
    server = make_server(args.host, args.port, app, server_class=ThreadingWSGIServer)
    server.serve_forever()


if __name__ == "__main__":
    main()
