from __future__ import annotations

import ast
import re
from typing import List

from aegis.models import AstViolation

SECRET_LITERAL_RE = re.compile(
    r"(sk-ant-[a-zA-Z0-9_-]{12,}|AKIA[0-9A-Z]{16}|ghp_[a-zA-Z0-9]{20,}|postgres(?:ql)?://[^:\s]+:[^@\s]+@)",
)


class _SecurityAstVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.violations: List[AstViolation] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._check_mutable_defaults(node)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._check_mutable_defaults(node)
        self.generic_visit(node)

    def _check_mutable_defaults(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        all_defaults = list(node.args.defaults) + [
            d for d in node.args.kw_defaults if d is not None
        ]
        for default in all_defaults:
            if isinstance(default, (ast.List, ast.Dict, ast.Set)):
                self.violations.append(
                    AstViolation(
                        rule_id="AST-REL-005",
                        severity="HIGH",
                        line=node.lineno,
                        message=f"Function '{node.name}' uses a mutable default argument ({type(default).__name__}), causing shared cross-request state corruption.",
                    )
                )

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        # Detect silent exception swallowing: `except ...: pass` or `except ...: ...`
        if len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
            self.violations.append(
                AstViolation(
                    rule_id="AST-REL-004",
                    severity="CRITICAL",
                    line=node.lineno,
                    message="Silent exception swallowing (`except: pass`) hides persistence/compliance failures.",
                )
            )
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        func_name = self._resolve_call_name(node.func)

        # AST-SEC-002: eval() / exec() / subprocess(..., shell=True)
        if func_name in ("eval", "exec"):
            self.violations.append(
                AstViolation(
                    rule_id="AST-SEC-002",
                    severity="CRITICAL",
                    line=node.lineno,
                    message=f"Dynamic code execution via `{func_name}()` is prohibited.",
                )
            )
        elif func_name.startswith(("subprocess.", "os.system")):
            if func_name == "os.system":
                self.violations.append(
                    AstViolation(
                        rule_id="AST-SEC-002",
                        severity="CRITICAL",
                        line=node.lineno,
                        message="Unquoted shell invocation via `os.system()` is prohibited.",
                    )
                )
            for kw in node.keywords:
                if kw.arg == "shell" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
                    self.violations.append(
                        AstViolation(
                            rule_id="AST-SEC-002",
                            severity="CRITICAL",
                            line=node.lineno,
                            message=f"`{func_name}(..., shell=True)` enables shell command injection.",
                        )
                    )

        # AST-SEC-001: SQL Injection via f-string, % formatting, or .format() in .execute()
        if func_name.endswith((".execute", ".executemany", ".raw")):
            if node.args:
                first_arg = node.args[0]
                if isinstance(first_arg, ast.JoinedStr):
                    self.violations.append(
                        AstViolation(
                            rule_id="AST-SEC-001",
                            severity="CRITICAL",
                            line=node.lineno,
                            message="Unparameterized f-string passed to SQL `.execute()` creates SQL injection vulnerability.",
                        )
                    )
                elif isinstance(first_arg, ast.BinOp) and isinstance(first_arg.op, (ast.Mod, ast.Add)):
                    self.violations.append(
                        AstViolation(
                            rule_id="AST-SEC-001",
                            severity="CRITICAL",
                            line=node.lineno,
                            message="String interpolation/concatenation inside SQL `.execute()` creates SQL injection vulnerability.",
                        )
                    )
                elif (
                    isinstance(first_arg, ast.Call)
                    and isinstance(first_arg.func, ast.Attribute)
                    and first_arg.func.attr == "format"
                ):
                    self.violations.append(
                        AstViolation(
                            rule_id="AST-SEC-001",
                            severity="CRITICAL",
                            line=node.lineno,
                            message="`.format()` string interpolation inside SQL `.execute()` creates SQL injection vulnerability.",
                        )
                    )

        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str) and SECRET_LITERAL_RE.search(node.value):
            self.violations.append(
                AstViolation(
                    rule_id="AST-SEC-003",
                    severity="CRITICAL",
                    line=node.lineno,
                    message="Hardcoded credential or API secret literal detected in source tree.",
                )
            )

    @staticmethod
    def _resolve_call_name(func: ast.AST) -> str:
        if isinstance(func, ast.Name):
            return func.id
        if isinstance(func, ast.Attribute):
            prefix = _SecurityAstVisitor._resolve_call_name(func.value)
            return f"{prefix}.{func.attr}" if prefix else func.attr
        return ""


class PythonAstAuditor:
    """Runs AST static security and reliability inspection on Python patches."""

    @classmethod
    def audit_source(cls, source_code: str) -> List[AstViolation]:
        if not source_code or not source_code.strip():
            return []
        try:
            tree = ast.parse(source_code)
        except SyntaxError as exc:
            return [
                AstViolation(
                    rule_id="AST-SYNTAX-000",
                    severity="CRITICAL",
                    line=exc.lineno or 1,
                    message=f"SyntaxError in candidate patch: {exc.msg}",
                )
            ]
        visitor = _SecurityAstVisitor()
        visitor.visit(tree)
        return visitor.violations
