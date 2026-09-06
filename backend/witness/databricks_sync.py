"""Databricks mirror of the audit-ledger.

CLAUDE.md section 8: Delta tables mirrored from the ledger on every commit, and
Databricks SQL / Genie powering the dashboard off those tables. Databricks is
the storage/analytics/hosting layer - reasoning never routes through model
serving (Free Edition has no provisioned throughput; that's a mid-demo quota
risk).

Two backends behind one interface:
  * databricks  - real Delta tables via databricks-sql-connector
  * sqlite      - local mirror with the identical schema and identical SQL

The local mirror is not a mock of the data: it is fed from the same ledger
records by the same code path. It exists so the dashboard and the Genie-style
queries stay demoable if the free-tier warehouse is asleep or out of quota, and
so the query text can be developed without burning warehouse uptime.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .ledger import AuditLedger
from .privacy import assert_no_raw_text, redact

CATALOG = os.environ.get("DATABRICKS_CATALOG", "workspace")
SCHEMA = os.environ.get("DATABRICKS_SCHEMA", "witness")

# One schema, two dialects. Delta gets STRING/TIMESTAMP, SQLite gets TEXT.
TABLES: dict[str, list[tuple[str, str]]] = {
    # Columns are structured fields only. Free-text columns that used to live
    # here - checkpoint summary, requirement rationale, evidence quotes, risk
    # detail - were removed by the privacy boundary: Databricks is an external
    # service and may not receive prose derived from a transcript. What remains
    # is enough to answer the analytics questions and nothing more.
    "checkpoints": [
        ("id", "STRING"), ("session_id", "STRING"), ("branch", "STRING"),
        ("author", "STRING"), ("created_at", "STRING"),
        ("files_touched", "STRING"), ("context_state", "STRING"),
        ("ledger_commit", "STRING"), ("synced_at", "STRING"),
    ],
    "requirements": [
        ("id", "STRING"), ("checkpoint_id", "STRING"),
        ("requirement_text", "STRING"), ("status", "STRING"),
        ("evidence_ref", "STRING"), ("context_state", "STRING"),
        ("ledger_commit", "STRING"), ("synced_at", "STRING"),
    ],
    "assumptions": [
        ("id", "STRING"), ("checkpoint_id", "STRING"),
        ("assumption_text", "STRING"), ("category", "STRING"),
        ("confidence", "DOUBLE"), ("decayed_confidence", "DOUBLE"),
        ("owner", "STRING"), ("status", "STRING"), ("symbol", "STRING"),
        ("created_at", "STRING"), ("expires_at", "STRING"),
        ("evidence_ref", "STRING"), ("context_state", "STRING"),
        ("synced_at", "STRING"),
    ],
    "risk_reports": [
        ("id", "STRING"), ("checkpoint_id", "STRING"), ("score", "DOUBLE"),
        ("verdict", "STRING"), ("risk_flags", "STRING"),
        ("risk_count", "BIGINT"), ("critical_count", "BIGINT"),
        ("context_state", "STRING"), ("generated_at", "STRING"),
        ("synced_at", "STRING"),
    ],
    "graph_snapshots": [
        ("id", "STRING"), ("symbol", "STRING"), ("checkpoint_id", "STRING"),
        ("blast_radius_json", "STRING"), ("head_sha", "STRING"),
        ("evidence_ref", "STRING"), ("recheck_status", "STRING"),
        ("captured_at", "STRING"), ("synced_at", "STRING"),
    ],
}

# Genie-style questions, pre-written as SQL so the demo has a guaranteed path
# even if Genie's NL parse wanders. CLAUDE.md section 8 asks for at least one
# working live; these are the candidates.
GENIE_QUERIES: dict[str, dict[str, str]] = {
    # Pre-written so the demo has a guaranteed path even if Genie's NL parse
    # wanders. They select structured columns only - the free-text columns these
    # once joined on were removed by the privacy boundary.
    "unvalidated_security_assumptions": {
        "question": "Show checkpoints with unvalidated security assumptions",
        "sql": """
SELECT c.id AS checkpoint_id, c.branch, a.assumption_text,
       a.category, a.confidence, a.decayed_confidence, a.expires_at,
       a.context_state
FROM {schema}.assumptions a
JOIN {schema}.checkpoints c ON c.id = a.checkpoint_id
WHERE a.status != 'validated'
  AND a.category IN ('environment', 'dependency', 'contract')
ORDER BY a.decayed_confidence ASC
""",
    },
    "stale_claims": {
        "question": "Which verified claims have gone stale since their checkpoint?",
        "sql": """
SELECT g.symbol, g.checkpoint_id, g.recheck_status, g.captured_at, c.branch
FROM {schema}.graph_snapshots g
JOIN {schema}.checkpoints c ON c.id = g.checkpoint_id
WHERE g.recheck_status IN ('stale', 'contradicted')
ORDER BY g.captured_at DESC
""",
    },
    "partial_context_reports": {
        "question": "Which reports were built on redacted context?",
        "sql": """
SELECT r.checkpoint_id, r.verdict, r.score, r.context_state, r.generated_at
FROM {schema}.risk_reports r
WHERE r.context_state = 'partial' OR r.score IS NULL
ORDER BY r.generated_at DESC
""",
    },
    "release_readiness": {
        "question": "What is the release-readiness trend across checkpoints?",
        "sql": """
SELECT r.checkpoint_id, r.score, r.verdict, r.critical_count,
       r.context_state, r.generated_at
FROM {schema}.risk_reports r
ORDER BY r.generated_at DESC
""",
    },
    "unsatisfied_requirements": {
        "question": "Which requirements are contradicted, unverified or redacted?",
        "sql": """
SELECT checkpoint_id, requirement_text, status, evidence_ref, context_state
FROM {schema}.requirements
WHERE status IN ('contradicted', 'unverified', 'redacted')
ORDER BY CASE status WHEN 'contradicted' THEN 0 WHEN 'redacted' THEN 1 ELSE 2 END,
         checkpoint_id
""",
    },
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class SyncResult:
    backend: str
    ok: bool
    rows_written: dict[str, int]
    error: str | None = None
    warehouse: str | None = None
    synced_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend, "ok": self.ok,
            "rows_written": self.rows_written, "total_rows": sum(self.rows_written.values()),
            "error": self.error, "warehouse": self.warehouse,
            "synced_at": self.synced_at or _now(),
        }


class DatabricksSync:
    def __init__(self, ledger: AuditLedger, *, local_db: str | Path = "audit-ledger/.mirror.db") -> None:
        self.ledger = ledger
        self.local_db = Path(local_db)
        self.host = os.environ.get("DATABRICKS_SERVER_HOSTNAME")
        self.http_path = os.environ.get("DATABRICKS_HTTP_PATH")
        self.token = os.environ.get("DATABRICKS_TOKEN")

    @property
    def configured(self) -> bool:
        return bool(self.host and self.http_path and self.token)

    @property
    def backend(self) -> str:
        return "databricks" if self.configured else "sqlite"

    @property
    def schema(self) -> str:
        return f"{CATALOG}.{SCHEMA}" if self.configured else "main"

    def status(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "configured": self.configured,
            "host": self.host,
            "catalog": CATALOG, "schema": SCHEMA,
            "local_mirror": str(self.local_db),
            "local_mirror_exists": self.local_db.exists(),
            "note": (
                "Delta tables in Unity Catalog"
                if self.configured
                else "DATABRICKS_* env vars not set - mirroring to local SQLite "
                     "with identical schema and identical query text"
            ),
        }

    # -- connections ---------------------------------------------------------
    def _connect(self):
        if self.configured:
            from databricks import sql as dbsql
            return dbsql.connect(
                server_hostname=self.host, http_path=self.http_path,
                access_token=self.token,
            )
        self.local_db.parent.mkdir(parents=True, exist_ok=True)
        return sqlite3.connect(self.local_db)

    def _ddl(self, table: str, cols: list[tuple[str, str]]) -> str:
        if self.configured:
            body = ", ".join(f"{n} {t}" for n, t in cols)
            return (f"CREATE TABLE IF NOT EXISTS {self.schema}.{table} "
                    f"({body}) USING DELTA")
        sqlite_type = {"STRING": "TEXT", "DOUBLE": "REAL", "BIGINT": "INTEGER"}
        body = ", ".join(f"{n} {sqlite_type.get(t, 'TEXT')}" for n, t in cols)
        return f"CREATE TABLE IF NOT EXISTS {table} ({body})"

    def _qualified(self, table: str) -> str:
        return f"{self.schema}.{table}" if self.configured else table

    def create_tables(self) -> dict[str, Any]:
        conn = self._connect()
        made = []
        try:
            cur = conn.cursor()
            if self.configured:
                cur.execute(f"CREATE CATALOG IF NOT EXISTS {CATALOG}")
                cur.execute(f"CREATE SCHEMA IF NOT EXISTS {self.schema}")
            for table, cols in TABLES.items():
                cur.execute(self._ddl(table, cols))
                made.append(table)
            conn.commit() if not self.configured else None
        finally:
            conn.close()
        return {"backend": self.backend, "schema": self.schema, "tables": made}

    # -- the mirror ----------------------------------------------------------
    def sync(self) -> SyncResult:
        """Mirror the current ledger contents into the tables.

        Replace-by-table, not append: the ledger's git history is the audit
        trail, so the warehouse only ever needs to hold current state. This also
        makes the sync idempotent, which matters when it runs on every commit.
        """
        if not self.ledger.exists:
            return SyncResult(self.backend, False, {}, error="audit-ledger not initialised")

        rows = self._collect_rows()
        written: dict[str, int] = {}
        try:
            self.create_tables()
            conn = self._connect()
            try:
                cur = conn.cursor()
                for table, cols in TABLES.items():
                    data = rows.get(table, [])
                    cur.execute(f"DELETE FROM {self._qualified(table)}")
                    if not data:
                        written[table] = 0
                        continue
                    names = [n for n, _ in cols]
                    placeholders = ", ".join("?" for _ in names)
                    stmt = (f"INSERT INTO {self._qualified(table)} "
                            f"({', '.join(names)}) VALUES ({placeholders})")
                    payload = [tuple(r.get(n) for n in names) for r in data]
                    cur.executemany(stmt, payload)
                    written[table] = len(payload)
                if not self.configured:
                    conn.commit()
            finally:
                conn.close()
        except Exception as exc:                       # noqa: BLE001
            return SyncResult(self.backend, False, written,
                              error=f"{type(exc).__name__}: {exc}")

        return SyncResult(self.backend, True, written,
                          warehouse=self.http_path, synced_at=_now())

    def _collect_rows(self) -> dict[str, list[dict[str, Any]]]:
        """Flatten ledger records into table rows, through the privacy boundary.

        Every row is built by `redact(destination="databricks")`, the same
        function the Claude API path calls. Rows are then re-checked by
        `assert_no_raw_text` before they can be written - Databricks is an
        external service, and a schema change upstream must not be able to
        reopen this path silently.
        """
        now = _now()
        commit = self.ledger.head()
        out: dict[str, list[dict[str, Any]]] = {t: [] for t in TABLES}

        def emit(table: str, payload: dict[str, Any], extra: dict[str, Any]) -> None:
            safe, report = redact(payload, destination="databricks")
            row = {**safe, **extra}
            row.setdefault("context_state", report.context_state)
            assert_no_raw_text(row, where=f"Databricks table {table}")
            # Columns the table declares but this row lacks must still exist.
            for name, _ in TABLES[table]:
                row.setdefault(name, None)
            out[table].append({k: row.get(k) for k, _ in TABLES[table]})

        for cp in self.ledger.read_all("checkpoints"):
            emit("checkpoints", {
                "checkpoint_id": cp.get("id"), "branch": cp.get("branch"),
                "author": cp.get("author"), "created_at": cp.get("created_at"),
                "files_touched": json.dumps(cp.get("files_touched") or []),
            }, {
                "id": cp.get("id"), "session_id": cp.get("session_id"),
                "ledger_commit": commit, "synced_at": now,
            })

        for rec in self.ledger.read_all("requirements"):
            cid = rec.get("checkpoint_id")
            state = rec.get("context_state") or "full"
            for req in (rec.get("requirements") or []):
                emit("requirements", {
                    "checkpoint_id": cid,
                    "requirement_text": req.get("text") or req.get("requirement_text"),
                    "status": req.get("status"),
                    "evidence_ref": req.get("evidence_id"),
                    "context_state": state,
                }, {
                    "id": f"{cid}:{req.get('id')}",
                    "ledger_commit": commit, "synced_at": now,
                })

        for rec in self.ledger.read_all("assumptions"):
            state = rec.get("context_state") or "full"
            for a in (rec.get("assumptions") or []):
                if not isinstance(a, dict) or not a.get("text"):
                    continue
                emit("assumptions", {
                    "checkpoint_id": rec.get("checkpoint_id"),
                    "assumption_text": a.get("text"),
                    "category": a.get("category"),
                    "confidence": _f(a.get("confidence")),
                    "owner": a.get("owner"),
                    "status": a.get("status") or a.get("decay_status") or "unvalidated",
                    "symbol": a.get("symbol"),
                    "created_at": a.get("created_at"), "expires_at": a.get("expires_at"),
                    "evidence_ref": a.get("evidence_id"),
                    "context_state": state,
                }, {
                    "id": a.get("id"),
                    "decayed_confidence": _f(a.get("decayed_confidence")),
                    "synced_at": now,
                })

        for rec in self.ledger.read_all("risk-reports"):
            risks = rec.get("risks") or []
            flags = sorted({r.get("band") for r in risks if r.get("band")})
            emit("risk_reports", {
                "checkpoint_id": rec.get("checkpoint_id"),
                "score": _f(rec.get("score")), "verdict": rec.get("verdict"),
                "risk_flags": json.dumps(flags),
                "context_state": rec.get("context_state") or "full",
            }, {
                "id": rec.get("id"), "risk_count": len(risks),
                "critical_count": sum(1 for r in risks if r.get("severity") == "critical"),
                "generated_at": rec.get("generated_at"), "synced_at": now,
            })

        for rec in self.ledger.read_all("graph-snapshots"):
            emit("graph_snapshots", {
                "checkpoint_id": rec.get("checkpoint_id"),
                "symbol": rec.get("symbol"),
                "evidence_ref": rec.get("evidence_id"),
            }, {
                "id": rec.get("id"),
                "blast_radius_json": json.dumps(rec.get("surface") or {}),
                "head_sha": rec.get("head_sha"),
                "recheck_status": (rec.get("recheck") or {}).get("status"),
                "captured_at": rec.get("captured_at"), "synced_at": now,
            })

        return out

    # -- queries -------------------------------------------------------------
    def query(self, sql: str) -> dict[str, Any]:
        """Run SQL against whichever backend is live. Same text both ways."""
        rendered = sql.format(schema=self.schema).strip()
        try:
            conn = self._connect()
            try:
                cur = conn.cursor()
                cur.execute(rendered)
                cols = [d[0] for d in cur.description] if cur.description else []
                rows = [dict(zip(cols, r)) for r in cur.fetchall()]
            finally:
                conn.close()
            return {"ok": True, "backend": self.backend, "sql": rendered,
                    "columns": cols, "rows": rows, "row_count": len(rows)}
        except Exception as exc:                       # noqa: BLE001
            return {"ok": False, "backend": self.backend, "sql": rendered,
                    "error": f"{type(exc).__name__}: {exc}", "rows": []}

    def genie(self, key: str) -> dict[str, Any]:
        spec = GENIE_QUERIES.get(key)
        if not spec:
            return {"ok": False, "error": f"unknown query: {key}",
                    "available": list(GENIE_QUERIES)}
        result = self.query(spec["sql"])
        result["question"] = spec["question"]
        result["key"] = key
        return result


def _f(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
