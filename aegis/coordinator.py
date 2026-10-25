from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import List

from aegis.ast_auditor import PythonAstAuditor
from aegis.models import AgentPatchCandidate, ReleaseGateVerdict
from aegis.sql_analyzer import PostgresMigrationLockAnalyzer


class AegisCoordinator:
    """
    Multi-Agent Git-Worktree Release Gate Coordinator:
    Directs up to 4 parallel coding agents and enforces a 5-stage deterministic release gate:
      Stage 1: Test-to-Code Ratio Enforcement (default >= 1.0, strict standard 2:1)
      Stage 2: Strict Red-to-Green Defect Reproduction Gate (must fail on base, pass on head)
      Stage 3: Python AST Security & Reliability Gate (no SQLi, secrets, eval, swallowed exceptions, mutable defaults)
      Stage 4: PostgreSQL Zero-Downtime DDL Lock Analyzer (no unguarded ACCESS EXCLUSIVE / SHARE locks)
      Stage 5: Atomic ACID Audit Ledger Commit with idempotent event deduplication
    """

    def __init__(self, db_path: Path | str = ":memory:", max_parallel_agents: int = 4) -> None:
        self.max_parallel_agents = max_parallel_agents
        self._db_lock = threading.Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._init_acid_ledger()

    def _init_acid_ledger(self) -> None:
        with self._db_lock:
            self._conn.execute("PRAGMA foreign_keys = ON;")
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS release_gate_ledger (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pr_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    worktree_branch TEXT NOT NULL,
                    approved INTEGER NOT NULL CHECK (approved IN (0, 1)),
                    stage_failed TEXT,
                    test_ratio REAL NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    created_at_ms INTEGER NOT NULL
                );
                """
            )
            self._conn.commit()

    def evaluate_patch(self, candidate: AgentPatchCandidate) -> ReleaseGateVerdict:
        t0 = time.perf_counter()

        ratio = round(
            candidate.test_lines_changed / max(candidate.prod_lines_changed, 1),
            2,
        )
        red_to_green_ok = bool(candidate.test_fails_on_base and candidate.test_passes_on_head)
        ast_violations = PythonAstAuditor.audit_source(candidate.python_patch_code)
        sql_hazards = PostgresMigrationLockAnalyzer.analyze_sql(candidate.sql_migration_sql)

        stage_failed: str | None = None
        if ratio < candidate.min_test_ratio:
            stage_failed = "STAGE_1_INSUFFICIENT_TEST_RATIO"
        elif not candidate.test_fails_on_base:
            stage_failed = "STAGE_2_TAUTOLOGICAL_TEST_PASSED_ON_BUGGY_BASE"
        elif not candidate.test_passes_on_head:
            stage_failed = "STAGE_2_REGRESSION_TEST_FAILED_ON_HEAD"
        elif ast_violations:
            stage_failed = f"STAGE_3_AST_VIOLATION ({ast_violations[0].rule_id})"
        elif sql_hazards:
            stage_failed = f"STAGE_4_POSTGRES_LOCK_HAZARD ({sql_hazards[0].rule_id})"

        approved = stage_failed is None
        idem_payload = (
            f"{candidate.pr_id}:{candidate.agent_id}:{candidate.worktree_branch}:"
            f"{approved}:{stage_failed}:{ratio}"
        )
        idem_key = hashlib.sha256(idem_payload.encode("utf-8")).hexdigest()[:20]

        # Stage 5: Atomic ACID persistence with idempotent conflict handling
        with self._db_lock:
            self._conn.execute(
                """
                INSERT INTO release_gate_ledger (
                    pr_id, agent_id, worktree_branch, approved, stage_failed, test_ratio, idempotency_key, created_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(idempotency_key) DO NOTHING;
                """,
                (
                    candidate.pr_id,
                    candidate.agent_id,
                    candidate.worktree_branch,
                    1 if approved else 0,
                    stage_failed,
                    ratio,
                    idem_key,
                    int(time.time() * 1000),
                ),
            )
            self._conn.commit()

        elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 3)
        return ReleaseGateVerdict(
            pr_id=candidate.pr_id,
            agent_id=candidate.agent_id,
            worktree_branch=candidate.worktree_branch,
            approved=approved,
            stage_failed=stage_failed,
            test_to_code_ratio=ratio,
            red_to_green_verified=red_to_green_ok,
            sql_hazards=sql_hazards,
            ast_violations=ast_violations,
            audit_ledger_idempotency_key=idem_key,
            evaluation_ms=elapsed_ms,
        )

    def evaluate_batch_parallel(self, candidates: List[AgentPatchCandidate]) -> List[ReleaseGateVerdict]:
        """Dispatches candidate PR evaluations across up to 4 parallel coding agent workers."""
        with ThreadPoolExecutor(max_workers=self.max_parallel_agents) as executor:
            futures = [executor.submit(self.evaluate_patch, c) for c in candidates]
            return [f.result() for f in futures]

    def ledger_count(self) -> int:
        with self._db_lock:
            row = self._conn.execute("SELECT COUNT(*) FROM release_gate_ledger;").fetchone()
            return int(row[0]) if row else 0
