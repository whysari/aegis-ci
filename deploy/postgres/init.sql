CREATE TABLE IF NOT EXISTS release_gate_ledger (
    id SERIAL PRIMARY KEY,
    pr_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    worktree_branch TEXT NOT NULL,
    approved INTEGER NOT NULL CHECK (approved IN (0, 1)),
    stage_failed TEXT,
    test_ratio DOUBLE PRECISION NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    created_at_ms BIGINT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS audit_violations_log (
    id SERIAL PRIMARY KEY,
    pr_id TEXT NOT NULL,
    rule_id TEXT NOT NULL,
    violation_type TEXT NOT NULL,
    details TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ledger_pr ON release_gate_ledger(pr_id);
CREATE INDEX IF NOT EXISTS idx_ledger_agent ON release_gate_ledger(agent_id);
CREATE INDEX IF NOT EXISTS idx_ledger_idempotency ON release_gate_ledger(idempotency_key);
CREATE INDEX IF NOT EXISTS idx_violations_pr ON audit_violations_log(pr_id);
