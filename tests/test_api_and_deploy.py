from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union
import unittest

from aegis.api import AegisService, create_wsgi_app
from aegis.coordinator import AegisCoordinator

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class TestApiAndDeploy(unittest.TestCase):
    def setUp(self) -> None:
        self.service = AegisService(AegisCoordinator(db_path=":memory:"))
        self.app = create_wsgi_app(self.service)

    def _request(
        self, method: str, path: str, body: Optional[Dict[str, Any]] = None
    ) -> Tuple[int, Union[Dict[str, Any], str]]:
        body_bytes = json.dumps(body).encode("utf-8") if body is not None else b""
        status_code = [200]
        response_headers: List[Tuple[str, str]] = []

        def start_response(status: str, headers: List[Tuple[str, str]]) -> None:
            status_code[0] = int(status.split()[0])
            response_headers.extend(headers)

        environ = {
            "REQUEST_METHOD": method,
            "PATH_INFO": path,
            "CONTENT_LENGTH": str(len(body_bytes)),
            "wsgi.input": io.BytesIO(body_bytes),
        }
        res_iter = self.app(environ, start_response)
        res_bytes = b"".join(res_iter)
        content_type = next((v for k, v in response_headers if k.lower() == "content-type"), "")
        if "application/json" in content_type:
            return status_code[0], json.loads(res_bytes.decode("utf-8"))
        return status_code[0], res_bytes.decode("utf-8")

    def test_healthz_endpoint(self) -> None:
        status, data = self._request("GET", "/api/v1/healthz")
        self.assertEqual(status, 200)
        self.assertIsInstance(data, dict)
        self.assertEqual(data.get("status"), "ok")
        self.assertEqual(data.get("service"), "aegis-ci")
        self.assertGreaterEqual(data.get("ledger_rows", -1), 0)

    def test_stats_endpoint(self) -> None:
        status, data = self._request("GET", "/api/v1/stats")
        self.assertEqual(status, 200)
        self.assertIsInstance(data, dict)
        self.assertIn("total_prs", data)
        self.assertIn("approved_count", data)
        self.assertIn("blocked_count", data)
        self.assertIn("stage_failures", data)
        self.assertEqual(data.get("precision"), 1.0)
        self.assertEqual(data.get("recall"), 1.0)

    def test_evaluate_clean_patch_approved(self) -> None:
        payload = {
            "pr_id": "PR-501",
            "agent_id": "claude-agent-1",
            "worktree_branch": "wt/claude-agent-1/clean-501",
            "defect_id": "EVAT-501",
            "prod_lines_changed": 15,
            "test_lines_changed": 35,
            "test_fails_on_base": True,
            "test_passes_on_head": True,
            "python_patch_code": "def compute_tax(amt: float) -> float:\n    return amt * 0.2\n",
            "sql_migration_sql": "SET LOCAL lock_timeout = '2s'; CREATE INDEX CONCURRENTLY idx_clean ON orders(id);",
            "min_test_ratio": 1.5,
        }
        status, data = self._request("POST", "/api/v1/evaluate", payload)
        self.assertEqual(status, 200)
        self.assertIsInstance(data, dict)
        self.assertTrue(data.get("approved"))
        self.assertIsNone(data.get("stage_failed"))
        self.assertGreaterEqual(data.get("test_to_code_ratio", 0), 1.5)

    def test_evaluate_sql_lock_hazard_blocked(self) -> None:
        payload = {
            "pr_id": "PR-502",
            "agent_id": "claude-agent-2",
            "worktree_branch": "wt/claude-agent-2/pg-lock-502",
            "defect_id": "EVAT-502",
            "prod_lines_changed": 15,
            "test_lines_changed": 35,
            "test_fails_on_base": True,
            "test_passes_on_head": True,
            "python_patch_code": "def noop(): pass\n",
            "sql_migration_sql": "CREATE INDEX idx_bomb ON orders (user_id);",
            "min_test_ratio": 1.5,
        }
        status, data = self._request("POST", "/api/v1/evaluate", payload)
        self.assertEqual(status, 200)
        self.assertIsInstance(data, dict)
        self.assertFalse(data.get("approved"))
        self.assertIn("STAGE_4_POSTGRES_LOCK_HAZARD", str(data.get("stage_failed")))
        hazards = data.get("sql_hazards", [])
        self.assertTrue(any(h.get("rule_id") == "PG-LOCK-001" for h in hazards))

    def test_evaluate_ast_violation_blocked(self) -> None:
        payload = {
            "pr_id": "PR-503",
            "agent_id": "claude-agent-3",
            "worktree_branch": "wt/claude-agent-3/sqli-503",
            "defect_id": "EVAT-503",
            "prod_lines_changed": 15,
            "test_lines_changed": 35,
            "test_fails_on_base": True,
            "test_passes_on_head": True,
            "python_patch_code": "def get_user(db, uid):\n    db.execute(f'SELECT * FROM users WHERE id = {uid}')\n",
            "sql_migration_sql": "",
            "min_test_ratio": 1.5,
        }
        status, data = self._request("POST", "/api/v1/evaluate", payload)
        self.assertEqual(status, 200)
        self.assertIsInstance(data, dict)
        self.assertFalse(data.get("approved"))
        self.assertIn("STAGE_3_AST_VIOLATION", str(data.get("stage_failed")))
        violations = data.get("ast_violations", [])
        self.assertTrue(any(v.get("rule_id") == "AST-SEC-001" for v in violations))

    def test_benchmark_run_endpoint(self) -> None:
        status, data = self._request("POST", "/api/v1/benchmark/run")
        self.assertEqual(status, 200)
        self.assertIsInstance(data, dict)
        self.assertEqual(data.get("total"), 30)
        self.assertEqual(data.get("tp"), 18)
        self.assertEqual(data.get("tn"), 12)
        self.assertEqual(data.get("fp"), 0)
        self.assertEqual(data.get("fn"), 0)
        self.assertEqual(data.get("precision"), 1.0)
        self.assertEqual(data.get("recall"), 1.0)
        self.assertEqual(len(data.get("verdicts", [])), 30)

    def test_metrics_endpoint(self) -> None:
        status, text = self._request("GET", "/metrics")
        self.assertEqual(status, 200)
        self.assertIsInstance(text, str)
        self.assertIn("aegis_prs_evaluated_total", text)
        self.assertIn("aegis_prs_blocked_total", text)
        self.assertIn("aegis_sql_hazards_detected_total", text)
        self.assertIn("aegis_ast_violations_detected_total", text)
        self.assertIn("aegis_evaluation_latency_ms", text)

    def test_deployment_files_exist_and_non_empty(self) -> None:
        files = [
            "Dockerfile.backend",
            "frontend/Dockerfile",
            "docker-compose.yml",
            "deploy/postgres/init.sql",
        ]
        for rel in files:
            p = PROJECT_ROOT / rel
            self.assertTrue(p.exists(), f"Missing file: {rel}")
            self.assertGreater(p.stat().st_size, 0, f"Empty file: {rel}")


if __name__ == "__main__":
    unittest.main()
