from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List


@dataclass
class MigrationHazard:
    """PostgreSQL DDL migration lock hazard detected before production deployment."""

    rule_id: str
    lock_mode: str  # e.g., 'ACCESS EXCLUSIVE', 'SHARE', 'SHARE ROW EXCLUSIVE'
    statement: str
    hazard_description: str
    safe_rewrite: str


@dataclass
class AstViolation:
    """Static analysis security or reliability defect detected via Python AST traversal."""

    rule_id: str
    severity: str
    line: int
    message: str


@dataclass
class AgentPatchCandidate:
    """Candidate pull request submitted by a parallel coding agent in an isolated git worktree."""

    pr_id: str
    agent_id: str
    worktree_branch: str
    defect_id: str
    prod_lines_changed: int
    test_lines_changed: int
    test_fails_on_base: bool
    test_passes_on_head: bool
    python_patch_code: str = ""
    sql_migration_sql: str = ""
    min_test_ratio: float = 1.0


@dataclass
class ReleaseGateVerdict:
    """Deterministic verdict from the 5-stage Aegis-CI Zero-Rollback Release Gate."""

    pr_id: str
    agent_id: str
    worktree_branch: str
    approved: bool
    stage_failed: str | None
    test_to_code_ratio: float
    red_to_green_verified: bool
    sql_hazards: List[MigrationHazard] = field(default_factory=list)
    ast_violations: List[AstViolation] = field(default_factory=list)
    audit_ledger_idempotency_key: str = ""
    evaluation_ms: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
