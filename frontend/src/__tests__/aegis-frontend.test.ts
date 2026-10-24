import { afterAll, beforeAll, describe, expect, it } from "bun:test";
import { spawn } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import {
  AegisStats,
  BenchmarkRunResponse,
  ReleaseGateVerdict,
  isAegisStats,
  isBenchmarkRunResponse,
  isReleaseGateVerdict,
} from "../types/aegis";
import { AegisApiClient } from "../lib/api-client";

const TEST_DIR = dirname(fileURLToPath(import.meta.url));
const FRONTEND_ROOT = resolve(TEST_DIR, "../..");
const PROJECT_ROOT = resolve(FRONTEND_ROOT, "..");

const SERVER_SOURCE = `
import io
import json
import sys
import threading
import socketserver

from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

from aegis.api import AegisService, create_wsgi_app
from aegis.coordinator import AegisCoordinator


def dispatch_wsgi(wsgi_app, req: dict) -> dict:
    method = str(req.get("method") or "GET").upper()
    path = str(req.get("path") or "/")
    query = str(req.get("query") or "")
    raw_body = req.get("body") or ""
    body_bytes = raw_body.encode("utf-8") if isinstance(raw_body, str) else b""
    headers = req.get("headers") or {}

    environ = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "QUERY_STRING": query,
        "CONTENT_TYPE": headers.get("content-type", "application/json"),
        "CONTENT_LENGTH": str(len(body_bytes)),
        "SERVER_NAME": "127.0.0.1",
        "SERVER_PORT": "8010",
        "wsgi.version": (1, 0),
        "wsgi.url_scheme": "http",
        "wsgi.input": io.BytesIO(body_bytes),
        "wsgi.errors": sys.stderr,
        "wsgi.multithread": True,
        "wsgi.multiprocess": False,
        "wsgi.run_once": False,
    }

    status_holder = ["200 OK", []]

    def start_response(status, response_headers, exc_info=None):
        status_holder[0] = status
        status_holder[1] = list(response_headers)

    chunks = []
    result_iter = wsgi_app(environ, start_response)
    try:
        for chunk in result_iter:
            if isinstance(chunk, bytes):
                chunks.append(chunk)
            elif isinstance(chunk, str):
                chunks.append(chunk.encode("utf-8"))
    finally:
        if hasattr(result_iter, "close"):
            result_iter.close()

    status_code = int(str(status_holder[0]).split(" ", 1)[0])
    resp_headers = {str(k).lower(): str(v) for k, v in status_holder[1]}
    return {
        "id": req.get("id"),
        "status": status_code,
        "headers": resp_headers,
        "body": b"".join(chunks).decode("utf-8", errors="replace"),
    }


def main() -> None:
    db_path = sys.argv[1]
    service = AegisService(AegisCoordinator(db_path=db_path))
    wsgi_app = create_wsgi_app(service)

    port = 0
    try:
        class ThreadingWSGIServer(socketserver.ThreadingMixIn, WSGIServer):
            daemon_threads = True

        class QuietHandler(WSGIRequestHandler):
            def log_message(self, fmt, *args):
                pass

        httpd = make_server(
            "127.0.0.1",
            0,
            wsgi_app,
            server_class=ThreadingWSGIServer,
            handler_class=QuietHandler,
        )
        port = httpd.server_port
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
    except OSError:
        port = 0

    sys.stdout.write("PORT=%d\\n" % port)
    sys.stdout.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            resp = dispatch_wsgi(wsgi_app, req)
        except Exception as exc:
            resp = {
                "id": None,
                "status": 500,
                "headers": {"content-type": "application/json"},
                "body": json.dumps({"error": str(exc)}),
            }
        sys.stdout.write("WSGI_RPC=" + json.dumps(resp) + "\\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
`;

interface PendingRpc {
  resolve: (value: { status: number; headers: Record<string, string>; body: string }) => void;
  reject: (reason: Error) => void;
}

let serverProcess: ReturnType<typeof spawn> | null = null;
let workDir = "";
let baseUrl = "";
let rpcCounter = 0;
const pendingRpcs = new Map<number, PendingRpc>();
let liveClient: AegisApiClient;

async function callWsgiRpc(input: string | URL | Request, init?: RequestInit): Promise<Response> {
  if (!serverProcess || !serverProcess.stdin) {
    throw new Error("Python Aegis WSGI server process is not running");
  }
  const rawUrl = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
  const parsed = new URL(rawUrl, "http://127.0.0.1:8010");
  const method = (init?.method ?? (input instanceof Request ? input.method : "GET")).toUpperCase();
  const bodyText = typeof init?.body === "string" ? init.body : "";

  const id = ++rpcCounter;
  const payload = JSON.stringify({
    id,
    method,
    path: parsed.pathname,
    query: parsed.search.startsWith("?") ? parsed.search.slice(1) : parsed.search,
    body: bodyText,
    headers: { "content-type": "application/json" },
  });

  const rpcResult = await new Promise<{ status: number; headers: Record<string, string>; body: string }>(
    (resolveRpc, rejectRpc) => {
      pendingRpcs.set(id, { resolve: resolveRpc, reject: rejectRpc });
      serverProcess!.stdin!.write(payload + "\n", "utf8");
    },
  );

  return new Response(rpcResult.body, {
    status: rpcResult.status,
    headers: rpcResult.headers,
  });
}

const hybridFetch: typeof fetch = async (input, init) => {
  if (!baseUrl.endsWith(":0")) {
    try {
      return await fetch(input, init);
    } catch (error) {
      const code = (error as { code?: string })?.code ?? "";
      const message = error instanceof Error ? error.message : String(error);
      if (!code.includes("EPERM") && !message.includes("EPERM")) {
        throw error;
      }
    }
  }
  return callWsgiRpc(input as string | URL | Request, init);
};

beforeAll(async () => {
  workDir = mkdtempSync(join(tmpdir(), "aegis-console-test-"));
  const scriptPath = join(workDir, "serve_aegis.py");
  const dbPath = join(workDir, "aegis-test.db");
  writeFileSync(scriptPath, SERVER_SOURCE, "utf8");

  const child = spawn("python3", ["-u", scriptPath, dbPath], {
    cwd: PROJECT_ROOT,
    stdio: ["pipe", "pipe", "pipe"],
    env: { ...process.env, PYTHONPATH: PROJECT_ROOT, PYTHONDONTWRITEBYTECODE: "1" },
  });
  serverProcess = child;

  let stdoutBuffer = "";
  let bootLog = "";
  child.stdout?.on("data", (chunk: Buffer) => {
    const text = chunk.toString("utf8");
    bootLog += text;
    stdoutBuffer += text;
    let idx = stdoutBuffer.indexOf("\n");
    while (idx !== -1) {
      const line = stdoutBuffer.slice(0, idx).trim();
      stdoutBuffer = stdoutBuffer.slice(idx + 1);
      if (line.startsWith("WSGI_RPC=")) {
        try {
          const parsed = JSON.parse(line.slice("WSGI_RPC=".length)) as {
            id: number;
            status: number;
            headers: Record<string, string>;
            body: string;
          };
          const pending = pendingRpcs.get(parsed.id);
          if (pending) {
            pendingRpcs.delete(parsed.id);
            pending.resolve(parsed);
          }
        } catch {
          // ignore
        }
      }
      idx = stdoutBuffer.indexOf("\n");
    }
  });

  const deadline = Date.now() + 15_000;
  while (Date.now() < deadline) {
    const match = /PORT=(\d+)/.exec(bootLog);
    if (match?.[1]) {
      baseUrl = `http://127.0.0.1:${match[1]}`;
      break;
    }
    await new Promise((r) => setTimeout(r, 20));
  }
  liveClient = new AegisApiClient({ baseUrl, fetchImpl: hybridFetch });
});

afterAll(() => {
  if (serverProcess) {
    serverProcess.kill("SIGKILL");
    serverProcess = null;
  }
  if (workDir) {
    rmSync(workDir, { recursive: true, force: true });
  }
});

describe("Aegis-CI Runtime Type Guards", () => {
  const validVerdict: ReleaseGateVerdict = {
    pr_id: "PR-101",
    agent_id: "claude-agent-1",
    worktree_branch: "wt/claude-agent-1/patch",
    approved: true,
    stage_failed: null,
    test_to_code_ratio: 2.2,
    red_to_green_verified: true,
    sql_hazards: [],
    ast_violations: [],
    audit_ledger_idempotency_key: "idem-key-101",
    evaluation_ms: 1.05,
    created_at_ms: Date.now(),
  };

  it("validates valid ReleaseGateVerdict", () => {
    expect(isReleaseGateVerdict(validVerdict)).toBe(true);
  });

  it("rejects invalid ReleaseGateVerdict", () => {
    expect(isReleaseGateVerdict(null)).toBe(false);
    expect(isReleaseGateVerdict({})).toBe(false);
    expect(isReleaseGateVerdict({ ...validVerdict, approved: "not-bool" as unknown as boolean })).toBe(false);
    expect(isReleaseGateVerdict({ ...validVerdict, evaluation_ms: "invalid" as unknown as number })).toBe(false);
  });

  const validBenchmark: BenchmarkRunResponse = {
    total: 30,
    tp: 18,
    tn: 12,
    fp: 0,
    fn: 0,
    precision: 1.0,
    recall: 1.0,
    verdicts: [validVerdict],
  };

  it("validates valid BenchmarkRunResponse", () => {
    expect(isBenchmarkRunResponse(validBenchmark)).toBe(true);
  });

  it("rejects invalid BenchmarkRunResponse", () => {
    expect(isBenchmarkRunResponse(null)).toBe(false);
    expect(isBenchmarkRunResponse({ ...validBenchmark, total: "30" as unknown as number })).toBe(false);
    expect(
      isBenchmarkRunResponse({ ...validBenchmark, verdicts: [{ invalid: true } as unknown as ReleaseGateVerdict] }),
    ).toBe(false);
  });

  const validStats: AegisStats = {
    total_prs: 30,
    approved_count: 12,
    blocked_count: 18,
    stage_failures: { STAGE_1: 3, STAGE_2: 4, STAGE_3: 5, STAGE_4: 6 },
    mean_evaluation_latency_ms: 1.15,
    precision: 1.0,
    recall: 1.0,
    ledger_rows: 30,
  };

  it("validates valid AegisStats", () => {
    expect(isAegisStats(validStats)).toBe(true);
  });

  it("rejects invalid AegisStats", () => {
    expect(isAegisStats(null)).toBe(false);
    expect(isAegisStats({ ...validStats, stage_failures: null as unknown as Record<string, number> })).toBe(false);
    expect(isAegisStats({ ...validStats, total_prs: "invalid" as unknown as number })).toBe(false);
  });
});

describe("AegisApiClient Live Python Backend Contract", () => {
  it("GET /api/v1/healthz returns ok from live Python WSGI backend", async () => {
    const health = await liveClient.healthz();
    expect(health.status).toBe("ok");
    expect(health.service).toBe("aegis-ci");
  });

  it("POST /api/v1/evaluate approves a clean 2:1 test-ratio PR with CONCURRENTLY DDL", async () => {
    const verdict = await liveClient.evaluate({
      pr_id: "PR-LIVE-001",
      agent_id: "claude-agent-1",
      worktree_branch: "wt/claude-agent-1/clean-live",
      defect_id: "EVAT-901",
      prod_lines_changed: 20,
      test_lines_changed: 48,
      test_fails_on_base: true,
      test_passes_on_head: true,
      python_patch_code: "def compute_vat(amount: float) -> float:\n    return round(amount * 0.2, 2)\n",
      sql_migration_sql: "SET LOCAL lock_timeout = '2s'; CREATE INDEX CONCURRENTLY idx_orders_vat ON orders(id);",
      min_test_ratio: 1.5,
    });
    expect(isReleaseGateVerdict(verdict)).toBe(true);
    expect(verdict.approved).toBe(true);
    expect(verdict.stage_failed).toBeNull();
    expect(verdict.audit_ledger_idempotency_key.length).toBe(20);
  });

  it("POST /api/v1/evaluate quarantines a non-concurrent CREATE INDEX (PG-LOCK-001)", async () => {
    const verdict = await liveClient.evaluate({
      pr_id: "PR-LIVE-002",
      agent_id: "claude-agent-2",
      worktree_branch: "wt/claude-agent-2/lock-bomb",
      defect_id: "EVAT-902",
      prod_lines_changed: 20,
      test_lines_changed: 48,
      test_fails_on_base: true,
      test_passes_on_head: true,
      python_patch_code: "def ok() -> int:\n    return 1\n",
      sql_migration_sql: "CREATE INDEX idx_blocking ON invoices (customer_id);",
      min_test_ratio: 1.5,
    });
    expect(isReleaseGateVerdict(verdict)).toBe(true);
    expect(verdict.approved).toBe(false);
    expect(verdict.stage_failed).toContain("STAGE_4_POSTGRES_LOCK_HAZARD");
    expect(verdict.sql_hazards[0]?.rule_id).toBe("PG-LOCK-001");
  });

  it("POST /api/v1/benchmark/run executes all 30 PR scenarios across 4 parallel agent threads", async () => {
    const benchmark = await liveClient.runBenchmark();
    expect(isBenchmarkRunResponse(benchmark)).toBe(true);
    expect(benchmark.total).toBe(30);
    expect(benchmark.tp).toBe(18);
    expect(benchmark.tn).toBe(12);
    expect(benchmark.fp).toBe(0);
    expect(benchmark.fn).toBe(0);
    expect(benchmark.precision).toBe(1.0);
    expect(benchmark.recall).toBe(1.0);

    const stats = await liveClient.stats();
    expect(isAegisStats(stats)).toBe(true);
    expect(stats.total_prs).toBeGreaterThanOrEqual(32);
    expect(stats.ledger_rows).toBeGreaterThanOrEqual(32);
  });
});

describe("AegisApiClient Offline & Fallback Engine", () => {
  const client = new AegisApiClient({ offline: true });

  it("evaluates clean PR, SQL lock hazard, and AST SQLi defect in offline mode", async () => {
    const clean = await client.evaluate({
      pr_id: "PR-801",
      agent_id: "claude-agent-1",
      worktree_branch: "wt/claude-agent-1/clean",
      defect_id: "EVAT-801",
      prod_lines_changed: 20,
      test_lines_changed: 50,
      test_fails_on_base: true,
      test_passes_on_head: true,
      python_patch_code: "def add(a, b): return a + b",
      sql_migration_sql: "SET LOCAL lock_timeout = '2s'; CREATE INDEX CONCURRENTLY idx_a ON t(a);",
      min_test_ratio: 1.5,
    });
    expect(clean.approved).toBe(true);

    const sqlBomb = await client.evaluate({
      pr_id: "PR-802",
      agent_id: "claude-agent-2",
      worktree_branch: "wt/claude-agent-2/pg-bomb",
      defect_id: "EVAT-802",
      prod_lines_changed: 20,
      test_lines_changed: 50,
      test_fails_on_base: true,
      test_passes_on_head: true,
      python_patch_code: "def add(a, b): return a + b",
      sql_migration_sql: "CREATE INDEX idx_audit ON audit_logs (tenant_id);",
      min_test_ratio: 1.5,
    });
    expect(sqlBomb.approved).toBe(false);
    expect(sqlBomb.sql_hazards[0]?.rule_id).toBe("PG-LOCK-001");

    const sqli = await client.evaluate({
      pr_id: "PR-803",
      agent_id: "claude-agent-3",
      worktree_branch: "wt/claude-agent-3/sqli",
      defect_id: "EVAT-803",
      prod_lines_changed: 20,
      test_lines_changed: 50,
      test_fails_on_base: true,
      test_passes_on_head: true,
      python_patch_code: "cursor.execute(f'SELECT * FROM users WHERE id = {uid}')",
      sql_migration_sql: "",
      min_test_ratio: 1.5,
    });
    expect(sqli.approved).toBe(false);
    expect(sqli.ast_violations[0]?.rule_id).toBe("AST-SEC-001");
  });
});
