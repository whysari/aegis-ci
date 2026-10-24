from __future__ import annotations

import unittest

from aegis.ast_auditor import PythonAstAuditor
from aegis.coordinator import AegisCoordinator
from aegis.models import AgentPatchCandidate
from aegis.sql_analyzer import PostgresMigrationLockAnalyzer
from benchmarks.run_aegis_benchmark import CLEAN_PYTHON_PATCH, run_benchmark


class TestAegisCiPlatform(unittest.TestCase):
    def test_postgres_ddl_lock_analyzer_catches_all_6_hazards(self) -> None:
        sql = """
        CREATE INDEX idx_orders_user ON orders (user_id);
        ALTER TABLE orders ALTER COLUMN total TYPE NUMERIC(14,2);
        ALTER TABLE orders ADD COLUMN rnd TEXT DEFAULT random()::text;
        ALTER TABLE orders ADD CONSTRAINT fk_user FOREIGN KEY (user_id) REFERENCES users(id);
        ALTER TABLE orders ALTER COLUMN status SET NOT NULL;
        """
        hazards = PostgresMigrationLockAnalyzer.analyze_sql(sql)
        rule_ids = {h.rule_id for h in hazards}
        self.assertIn("PG-LOCK-001", rule_ids)
        self.assertIn("PG-LOCK-002", rule_ids)
        self.assertIn("PG-LOCK-003", rule_ids)
        self.assertIn("PG-LOCK-004", rule_ids)
        self.assertIn("PG-LOCK-005", rule_ids)
        self.assertIn("PG-LOCK-006", rule_ids)

    def test_safe_concurrent_migration_passes_with_zero_hazards(self) -> None:
        safe_sql = """
        SET LOCAL lock_timeout = '2s';
        CREATE INDEX CONCURRENTLY idx_orders_created ON orders (created_at);
        ALTER TABLE orders ADD CONSTRAINT fk_org FOREIGN KEY (org_id) REFERENCES orgs(id) NOT VALID;
        ALTER TABLE orders VALIDATE CONSTRAINT fk_org;
        """
        hazards = PostgresMigrationLockAnalyzer.analyze_sql(safe_sql)
        self.assertEqual(len(hazards), 0)

    def test_python_ast_auditor_catches_sqli_swallowed_except_and_mutable_default(self) -> None:
        bad_code = """
def bad_handler(db, user_id, cache={}):
    try:
        db.execute(f"SELECT * FROM accounts WHERE user_id = {user_id}")
    except Exception:
        pass
"""
        violations = PythonAstAuditor.audit_source(bad_code)
        rules = {v.rule_id for v in violations}
        self.assertIn("AST-SEC-001", rules)
        self.assertIn("AST-REL-004", rules)
        self.assertIn("AST-REL-005", rules)

    def test_idempotent_acid_ledger_deduplication(self) -> None:
        coord = AegisCoordinator()
        candidate = AgentPatchCandidate(
            pr_id="PR-999",
            agent_id="claude-agent-1",
            worktree_branch="wt/claude-agent-1/fix-999",
            defect_id="EVAT-999",
            prod_lines_changed=15,
            test_lines_changed=35,
            test_fails_on_base=True,
            test_passes_on_head=True,
            python_patch_code=CLEAN_PYTHON_PATCH,
        )
        v1 = coord.evaluate_patch(candidate)
        v2 = coord.evaluate_patch(candidate)  # Duplicate retry
        self.assertTrue(v1.approved)
        self.assertEqual(v1.audit_ledger_idempotency_key, v2.audit_ledger_idempotency_key)
        self.assertEqual(coord.ledger_count(), 1)

    def test_full_30_pr_benchmark_zero_rollbacks(self) -> None:
        stats = run_benchmark()
        self.assertEqual(stats["true_positives"], 12)
        self.assertEqual(stats["true_negatives"], 18)
        self.assertEqual(stats["false_approvals"], 0)
        self.assertEqual(stats["ledger_rows"], 30)


if __name__ == "__main__":
    unittest.main()
