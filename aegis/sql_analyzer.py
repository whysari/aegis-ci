from __future__ import annotations

import re
from typing import List

from aegis.models import MigrationHazard

VOLATILE_DEFAULT_RE = re.compile(
    r"\bDEFAULT\s+(random\s*\(|clock_timestamp\s*\(|timeofday\s*\(|uuid_generate_v1\s*\()",
    re.IGNORECASE,
)


class PostgresMigrationLockAnalyzer:
    """
    Static analyzer for PostgreSQL DDL migrations.
    Detects statements that acquire ACCESS EXCLUSIVE, SHARE, or SHARE ROW EXCLUSIVE locks
    without zero-downtime expand/contract patterns or lock_timeout guards.
    """

    @classmethod
    def analyze_sql(cls, sql_text: str) -> List[MigrationHazard]:
        if not sql_text or not sql_text.strip():
            return []

        hazards: List[MigrationHazard] = []
        raw_statements = [s.strip() for s in sql_text.split(";") if s.strip()]

        has_lock_timeout = any(
            re.search(r"\bSET\s+(LOCAL\s+)?lock_timeout\s*=", stmt, re.IGNORECASE)
            for stmt in raw_statements
        )

        has_validated_check = any(
            re.search(r"\bVALIDATE\s+CONSTRAINT\b", stmt, re.IGNORECASE)
            for stmt in raw_statements
        )

        for stmt in raw_statements:
            compact = " ".join(stmt.split())
            upper = compact.upper()

            # PG-LOCK-001: CREATE [UNIQUE] INDEX without CONCURRENTLY
            if re.search(r"\bCREATE\s+(UNIQUE\s+)?INDEX\b", upper) and "CONCURRENTLY" not in upper:
                rewrite = re.sub(
                    r"(?i)\bCREATE\s+(UNIQUE\s+)?INDEX\b",
                    lambda m: f"CREATE {m.group(1) or ''}INDEX CONCURRENTLY",
                    compact,
                    count=1,
                )
                hazards.append(
                    MigrationHazard(
                        rule_id="PG-LOCK-001",
                        lock_mode="SHARE (Blocks Writes)",
                        statement=compact,
                        hazard_description="CREATE INDEX without CONCURRENTLY acquires a SHARE lock, blocking all INSERT/UPDATE/DELETE operations for the duration of the index build.",
                        safe_rewrite=f"{rewrite};",
                    )
                )

            # PG-LOCK-002: ALTER TABLE ... ALTER COLUMN ... TYPE (Full Table Rewrite)
            if re.search(r"\bALTER\s+TABLE\s+\S+\s+ALTER\s+(COLUMN\s+)?\S+\s+(SET\s+DATA\s+)?TYPE\b", upper):
                hazards.append(
                    MigrationHazard(
                        rule_id="PG-LOCK-002",
                        lock_mode="ACCESS EXCLUSIVE (Full Table Rewrite)",
                        statement=compact,
                        hazard_description="Changing column type rewrites the entire heap table under an ACCESS EXCLUSIVE lock, blocking all reads and writes.",
                        safe_rewrite="-- Expand/Contract: 1) ADD COLUMN new_col <type>; 2) Backfill in batches; 3) Dual-write via trigger; 4) Swap column names.",
                    )
                )

            # PG-LOCK-003: ADD COLUMN with volatile DEFAULT
            if re.search(r"\bADD\s+(COLUMN\s+)?\S+.*\bDEFAULT\b", upper) and VOLATILE_DEFAULT_RE.search(compact):
                hazards.append(
                    MigrationHazard(
                        rule_id="PG-LOCK-003",
                        lock_mode="ACCESS EXCLUSIVE (Volatile Rewrite)",
                        statement=compact,
                        hazard_description="Adding a column with a volatile DEFAULT evaluates the expression for every existing row under an ACCESS EXCLUSIVE lock.",
                        safe_rewrite="-- 1) ADD COLUMN col <type> NULL; 2) ALTER COLUMN col SET DEFAULT ...; 3) Backfill existing rows in batches.",
                    )
                )

            # PG-LOCK-004: ADD CONSTRAINT FOREIGN KEY or CHECK without NOT VALID
            if (
                re.search(r"\bADD\s+CONSTRAINT\s+\S+\s+(FOREIGN\s+KEY|CHECK)\b", upper)
                and "NOT VALID" not in upper
            ):
                hazards.append(
                    MigrationHazard(
                        rule_id="PG-LOCK-004",
                        lock_mode="SHARE ROW EXCLUSIVE (Full Table Scan)",
                        statement=compact,
                        hazard_description="Adding a FOREIGN KEY or CHECK constraint without NOT VALID scans the entire table under a blocking lock.",
                        safe_rewrite=f"{compact} NOT VALID; ALTER TABLE ... VALIDATE CONSTRAINT ...;",
                    )
                )

            # PG-LOCK-005: ALTER COLUMN ... SET NOT NULL without prior VALIDATE CONSTRAINT
            if re.search(r"\bALTER\s+(COLUMN\s+)?\S+\s+SET\s+NOT\s+NULL\b", upper) and not has_validated_check:
                hazards.append(
                    MigrationHazard(
                        rule_id="PG-LOCK-005",
                        lock_mode="ACCESS EXCLUSIVE (Sequential Scan)",
                        statement=compact,
                        hazard_description="SET NOT NULL scans the entire table under ACCESS EXCLUSIVE unless a NOT VALID check constraint is validated first.",
                        safe_rewrite="ALTER TABLE t ADD CONSTRAINT chk_nn CHECK (col IS NOT NULL) NOT VALID; ALTER TABLE t VALIDATE CONSTRAINT chk_nn; ALTER TABLE t ALTER COLUMN col SET NOT NULL;",
                    )
                )

            # PG-LOCK-006: ALTER TABLE / DROP TABLE without SET lock_timeout
            if re.search(r"^\s*(ALTER|DROP)\s+TABLE\b", upper) and not has_lock_timeout:
                hazards.append(
                    MigrationHazard(
                        rule_id="PG-LOCK-006",
                        lock_mode="LOCK QUEUE AMPLIFICATION",
                        statement=compact,
                        hazard_description="DDL statement acquiring table locks executed without SET lock_timeout; if blocked by a long query, all subsequent SELECTs queue behind the DDL.",
                        safe_rewrite=f"SET LOCAL lock_timeout = '2s'; {compact};",
                    )
                )

        return hazards
