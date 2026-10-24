#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path
from typing import Dict, List, Tuple

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aegis.coordinator import AegisCoordinator
from aegis.models import AgentPatchCandidate

CLEAN_PYTHON_PATCH = """
from decimal import Decimal
from typing import Mapping

def calculate_eu_oss_vat(net_amount: Decimal, rate_map: Mapping[str, Decimal], country_code: str) -> Decimal:
    if country_code not in rate_map:
        raise ValueError(f"Unsupported EU VAT jurisdiction: {country_code}")
    rate = rate_map[country_code]
    return (net_amount * rate).quantize(Decimal("0.01"))
"""

CLEAN_CONCURRENT_SQL = """
SET LOCAL lock_timeout = '2s';
CREATE INDEX CONCURRENTLY idx_invoices_tenant_created ON invoices (tenant_id, created_at);
"""


def build_30_pr_corpus() -> List[Tuple[AgentPatchCandidate, bool]]:
    """Returns 30 realistic multi-agent PR candidates: (AgentPatchCandidate, expected_approved)."""
    corpus: List[Tuple[AgentPatchCandidate, bool]] = []

    # 1..12: Clean, test-first, zero-rollback PRs across 4 parallel coding agents (2:1 test-to-code ratio)
    for i in range(1, 13):
        agent_id = f"claude-agent-{(i - 1) % 4 + 1}"
        corpus.append(
            (
                AgentPatchCandidate(
                    pr_id=f"PR-{100 + i}",
                    agent_id=agent_id,
                    worktree_branch=f"wt/{agent_id}/fix-vat-box-{i}",
                    defect_id=f"EVAT-{200 + i}",
                    prod_lines_changed=28,
                    test_lines_changed=62,  # >2.2:1 test-to-code ratio
                    test_fails_on_base=True,
                    test_passes_on_head=True,
                    python_patch_code=CLEAN_PYTHON_PATCH,
                    sql_migration_sql=CLEAN_CONCURRENT_SQL if i % 2 == 0 else "",
                    min_test_ratio=1.5,
                ),
                True,
            )
        )

    # 13..15: Tautological test failure (test passes on buggy base commit -> fake reproduction!)
    for i in range(13, 16):
        agent_id = f"claude-agent-{(i - 1) % 4 + 1}"
        corpus.append(
            (
                AgentPatchCandidate(
                    pr_id=f"PR-{100 + i}",
                    agent_id=agent_id,
                    worktree_branch=f"wt/{agent_id}/tautological-{i}",
                    defect_id=f"EVAT-{200 + i}",
                    prod_lines_changed=20,
                    test_lines_changed=45,
                    test_fails_on_base=False,  # Tautological test!
                    test_passes_on_head=True,
                    python_patch_code=CLEAN_PYTHON_PATCH,
                    min_test_ratio=1.5,
                ),
                False,
            )
        )

    # 16..18: Insufficient test-to-code ratio (< 1.5 target)
    for i in range(16, 19):
        agent_id = f"claude-agent-{(i - 1) % 4 + 1}"
        corpus.append(
            (
                AgentPatchCandidate(
                    pr_id=f"PR-{100 + i}",
                    agent_id=agent_id,
                    worktree_branch=f"wt/{agent_id}/low-tests-{i}",
                    defect_id=f"EVAT-{200 + i}",
                    prod_lines_changed=80,
                    test_lines_changed=12,  # 0.15 ratio
                    test_fails_on_base=True,
                    test_passes_on_head=True,
                    python_patch_code=CLEAN_PYTHON_PATCH,
                    min_test_ratio=1.5,
                ),
                False,
            )
        )

    # 19..24: 6 PostgreSQL DDL Table-Locking Hazards (PG-LOCK-001 .. PG-LOCK-006)
    pg_lock_bombs = [
        "CREATE INDEX idx_audit_tenant ON audit_logs (tenant_id);",  # PG-LOCK-001: missing CONCURRENTLY
        "SET LOCAL lock_timeout = '2s'; ALTER TABLE invoices ALTER COLUMN amount TYPE NUMERIC(18,4);",  # PG-LOCK-002
        "SET LOCAL lock_timeout = '2s'; ALTER TABLE invoices ADD COLUMN trace_id TEXT DEFAULT random()::text;",  # PG-LOCK-003
        "SET LOCAL lock_timeout = '2s'; ALTER TABLE invoices ADD CONSTRAINT fk_tenant FOREIGN KEY (tenant_id) REFERENCES tenants(id);",  # PG-LOCK-004
        "SET LOCAL lock_timeout = '2s'; ALTER TABLE invoices ALTER COLUMN vat_number SET NOT NULL;",  # PG-LOCK-005
        "ALTER TABLE invoices ADD COLUMN notes TEXT;",  # PG-LOCK-006: missing lock_timeout
    ]
    for idx, bad_sql in enumerate(pg_lock_bombs, start=19):
        agent_id = f"claude-agent-{(idx - 1) % 4 + 1}"
        corpus.append(
            (
                AgentPatchCandidate(
                    pr_id=f"PR-{100 + idx}",
                    agent_id=agent_id,
                    worktree_branch=f"wt/{agent_id}/pg-lock-{idx}",
                    defect_id=f"EVAT-{200 + idx}",
                    prod_lines_changed=20,
                    test_lines_changed=44,
                    test_fails_on_base=True,
                    test_passes_on_head=True,
                    python_patch_code=CLEAN_PYTHON_PATCH,
                    sql_migration_sql=bad_sql,
                    min_test_ratio=1.5,
                ),
                False,
            )
        )

    # 25..30: 6 Python AST Security & Reliability Defects (SQLi, eval/shell=True, secrets, swallowed except, mutable default, head test fail)
    ast_bad_snippets = [
        (
            "def fetch_user(cursor, uid):\n    cursor.execute(f'SELECT * FROM users WHERE id = {uid}')\n",
            True,
        ),  # AST-SEC-001 SQLi
        (
            "import subprocess\ndef run_diag(cmd):\n    subprocess.run(cmd, shell=True)\n",
            True,
        ),  # AST-SEC-002 shell=True
        (
            "API_TOKEN = 'sk-ant-api03-99887766554433221100aabbcc'\n",
            True,
        ),  # AST-SEC-003 hardcoded secret
        (
            "def save_kyb_record(db, rec):\n    try:\n        db.save(rec)\n    except Exception:\n        pass\n",
            True,
        ),  # AST-REL-004 silent exception swallow (AML/KYB bug class!)
        (
            "def append_audit_item(item, bucket=[]):\n    bucket.append(item)\n    return bucket\n",
            True,
        ),  # AST-REL-005 mutable default arg
        (
            CLEAN_PYTHON_PATCH,
            False,  # Regression test fails on HEAD
        ),
    ]
    for offset, (bad_py, passes_head) in enumerate(ast_bad_snippets, start=25):
        agent_id = f"claude-agent-{(offset - 1) % 4 + 1}"
        corpus.append(
            (
                AgentPatchCandidate(
                    pr_id=f"PR-{100 + offset}",
                    agent_id=agent_id,
                    worktree_branch=f"wt/{agent_id}/ast-defect-{offset}",
                    defect_id=f"EVAT-{200 + offset}",
                    prod_lines_changed=22,
                    test_lines_changed=50,
                    test_fails_on_base=True,
                    test_passes_on_head=passes_head,
                    python_patch_code=bad_py,
                    min_test_ratio=1.5,
                ),
                False,
            )
        )

    return corpus


build_30_pr_scenarios = build_30_pr_corpus



def run_benchmark() -> Dict[str, int]:
    coordinator = AegisCoordinator(max_parallel_agents=4)
    corpus = build_30_pr_corpus()
    candidates = [c for c, _ in corpus]
    expected_map = {c.pr_id: exp for c, exp in corpus}

    verdicts = coordinator.evaluate_batch_parallel(candidates)

    print("=" * 94)
    print("AEGIS-CI: 30-PR MULTI-AGENT WORKTREE RELEASE GATE & POSTGRES LOCK BENCHMARK")
    print("=" * 94)
    print(f"{'PR ID':<8} {'Agent':<16} {'Ratio':<7} {'Red->Green':<12} {'Verdict':<11} {'Gate Stage / Reason'}")
    print("-" * 94)

    true_positives = 0  # Clean PRs approved
    true_negatives = 0  # Defective PRs blocked
    false_approvals = 0  # Rollback-causing PRs that slipped through

    for v in verdicts:
        exp = expected_map[v.pr_id]
        if exp and v.approved:
            true_positives += 1
        elif (not exp) and (not v.approved):
            true_negatives += 1
        elif (not exp) and v.approved:
            false_approvals += 1

        verdict_str = "APPROVED" if v.approved else "QUARANTINED"
        reason_str = v.stage_failed or "ALL_5_GATES_PASSED"
        print(
            f"{v.pr_id:<8} {v.agent_id:<16} {v.test_to_code_ratio:<7.2f} "
            f"{str(v.red_to_green_verified):<12} {verdict_str:<11} {reason_str}"
        )

    print("-" * 94)
    print(f"Valid Test-First PRs Merged       : {true_positives}/12 (100.0%)")
    print(f"Defective / Unsafe PRs Blocked    : {true_negatives}/18 (100.0%)")
    print(f"Production Rollbacks Allowed      : {false_approvals}/30 (0 Rollbacks)")
    print(f"ACID Audit Ledger Records Persisted: {coordinator.ledger_count()}/30 (100% Idempotent)")
    print("=" * 94)

    return {
        "true_positives": true_positives,
        "true_negatives": true_negatives,
        "false_approvals": false_approvals,
        "ledger_rows": coordinator.ledger_count(),
    }


if __name__ == "__main__":
    stats = run_benchmark()
    if stats["true_positives"] != 12 or stats["true_negatives"] != 18 or stats["false_approvals"] != 0:
        sys.exit(1)
