export interface MigrationHazard {
  rule_id: string;
  lock_mode: string;
  statement: string;
  hazard_description: string;
  safe_rewrite: string;
}

export interface AstViolation {
  rule_id: string;
  severity: string;
  line: number;
  message: string;
}

export interface AgentPatchCandidateInput {
  pr_id: string;
  agent_id: string;
  worktree_branch: string;
  defect_id: string;
  prod_lines_changed: number;
  test_lines_changed: number;
  test_fails_on_base: boolean;
  test_passes_on_head: boolean;
  python_patch_code?: string;
  sql_migration_sql?: string;
  min_test_ratio?: number;
}

export interface ReleaseGateVerdict {
  pr_id: string;
  agent_id: string;
  worktree_branch: string;
  approved: boolean;
  stage_failed: string | null;
  test_to_code_ratio: number;
  red_to_green_verified: boolean;
  sql_hazards: MigrationHazard[];
  ast_violations: AstViolation[];
  audit_ledger_idempotency_key: string;
  evaluation_ms: number;
  created_at_ms?: number;
}

export interface BenchmarkRunResponse {
  total: number;
  tp: number;
  tn: number;
  fp: number;
  fn: number;
  precision: number;
  recall: number;
  verdicts: ReleaseGateVerdict[];
}

export interface AegisStats {
  total_prs: number;
  approved_count: number;
  blocked_count: number;
  stage_failures: Record<string, number>;
  mean_evaluation_latency_ms: number;
  precision: number;
  recall: number;
  ledger_rows?: number;
}

export function isReleaseGateVerdict(obj: unknown): obj is ReleaseGateVerdict {
  if (typeof obj !== "object" || obj === null) return false;
  const v = obj as Record<string, unknown>;
  return (
    typeof v.pr_id === "string" &&
    typeof v.agent_id === "string" &&
    typeof v.worktree_branch === "string" &&
    typeof v.approved === "boolean" &&
    (v.stage_failed === null || typeof v.stage_failed === "string") &&
    typeof v.test_to_code_ratio === "number" &&
    typeof v.red_to_green_verified === "boolean" &&
    Array.isArray(v.sql_hazards) &&
    Array.isArray(v.ast_violations) &&
    typeof v.audit_ledger_idempotency_key === "string" &&
    typeof v.evaluation_ms === "number"
  );
}

export function isBenchmarkRunResponse(obj: unknown): obj is BenchmarkRunResponse {
  if (typeof obj !== "object" || obj === null) return false;
  const b = obj as Record<string, unknown>;
  return (
    typeof b.total === "number" &&
    typeof b.tp === "number" &&
    typeof b.tn === "number" &&
    typeof b.fp === "number" &&
    typeof b.fn === "number" &&
    typeof b.precision === "number" &&
    typeof b.recall === "number" &&
    Array.isArray(b.verdicts) &&
    b.verdicts.every(isReleaseGateVerdict)
  );
}

export function isAegisStats(obj: unknown): obj is AegisStats {
  if (typeof obj !== "object" || obj === null) return false;
  const s = obj as Record<string, unknown>;
  return (
    typeof s.total_prs === "number" &&
    typeof s.approved_count === "number" &&
    typeof s.blocked_count === "number" &&
    typeof s.stage_failures === "object" &&
    s.stage_failures !== null &&
    typeof s.mean_evaluation_latency_ms === "number" &&
    typeof s.precision === "number" &&
    typeof s.recall === "number"
  );
}
