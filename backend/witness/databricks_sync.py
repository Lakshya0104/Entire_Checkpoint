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

CATALOG = os.environ.get("DATABRICKS_CATALOG", "workspace")
SCHEMA = os.environ.get("DATABRICKS_SCHEMA", "witness")

# One schema, two dialects. Delta gets STRING/TIMESTAMP, SQLite gets TEXT.
TABLES: dict[str, list[tuple[str, str]]] = {
    "checkpoints": [
        ("id", "STRING"), ("session_id", "STRING"), ("branch", "STRING"),
        ("author", "STRING"), ("created_at", "STRING"),
        ("files_touched", "STRING"), ("summary", "STRING"),
        ("ledger_commit", "STRING"), ("synced_at", "STRING"),
    ],
    "requirements": [
        ("id", "STRING"), ("checkpoint_id", "STRING"), ("text", "STRING"),
        ("status", "STRING"), ("rationale", "STRING"), ("evidence_ref", "STRING"),
        ("evidence_quote", "STRING"), ("ledger_commit", "STRING"),
        ("synced_at", "STRING"),
    ],
    "assumptions": [
        ("id", "STRING"), ("checkpoint_id", "STRING"), ("text", "STRING"),
        ("source", "STRING"), ("category", "STRING"), ("confidence", "DOUBLE"),
        ("decayed_confidence", "DOUBLE"), ("owner", "STRING"),
        ("status", "STRING"), ("symbol", "STRING"), ("validation", "STRING"),
        ("created_at", "STRING"), ("expires_at", "STRING"),
        ("evidence_ref", "STRING"), ("synced_at", "STRING"),
    ],
    "risk_reports": [
        ("id", "STRING"), ("checkpoint_id", "STRING"), ("score", "DOUBLE"),
        ("verdict", "STRING"), ("security_flags", "STRING"),
        ("risk_count", "BIGINT"), ("critical_count", "BIGINT"),
        ("generated_at", "STRING"), ("synced_at", "STRING"),
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
    "unvalidated_security_assumptions": {
        "question": "Show checkpoints with unvalidated security assumptions",
        "sql": """
SELECT c.id AS checkpoint_id, c.summary, a.text AS assumption,
       a.category, a.confidence, a.decayed_confidence, a.expires_at
FROM {schema}.assumptions a
JOIN {schema}.checkpoints c ON c.id = a.checkpoint_id
WHERE a.status != 'validated'
  AND (a.category IN ('environment', 'dependency', 'contract')
       OR lower(a.text) LIKE '%auth%' OR lower(a.text) LIKE '%token%'
       OR lower(a.text) LIKE '%permission%' OR lower(a.text) LIKE '%secret%')
ORDER BY a.decayed_confidence ASC
""",
    },
    "stale_claims": {
        "question": "Which verified claims have gone stale since their checkpoint?",
        "sql": """
SELECT g.symbol, g.checkpoint_id, g.recheck_status, g.captured_at, c.summary
FROM {schema}.graph_snapshots g
JOIN {schema}.checkpoints c ON c.id = g.checkpoint_id
WHERE g.recheck_status IN ('stale', 'contradicted')
ORDER BY g.captured_at DESC
""",
    },
    "release_readiness": {
        "question": "What is the release-readiness trend across checkpoints?",
        "sql": """
SELECT r.checkpoint_id, r.score, r.verdict, r.critical_count, r.generated_at
FROM {schema}.risk_reports r
ORDER BY r.generated_at DESC
""",
    },
    "unsatisfied_requirements": {
        "question": "Which requirements are contradicted or unverified?",
        "sql": """
SELECT checkpoint_id, text, status, rationale, evidence_ref
FROM {schema}.requirements
WHERE status IN ('contradicted', 'unverified')
ORDER BY CASE status WHEN 'contradicted' THEN 0 ELSE 1 END, checkpoint_id
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
        """Flatten ledger JSON records into table rows."""
        now = _now()
        commit = self.ledger.head()
        out: dict[str, list[dict[str, Any]]] = {t: [] for t in TABLES}

        for cp in self.ledger.read_all("checkpoints"):
            out["checkpoints"].append({
                "id": cp.get("id"), "session_id": cp.get("session_id"),
                "branch": cp.get("branch"), "author": cp.get("author"),
                "created_at": cp.get("created_at"),
                "files_touched": json.dumps(cp.get("files_touched") or []),
                "summary": cp.get("summary"), "ledger_commit": commit,
                "synced_at": now,
            })

        for rec in self.ledger.read_all("requirements"):
            cid = rec.get("checkpoint_id")
            for req in (rec.get("requirements") or []):
                out["requirements"].append({
                    "id": f"{cid}:{req.get('id')}", "checkpoint_id": cid,
                    "text": req.get("text"), "status": req.get("status"),
                    "rationale": req.get("rationale"),
                    "evidence_ref": req.get("evidence_id"),
                    "evidence_quote": req.get("evidence_quote"),
                    "ledger_commit": commit, "synced_at": now,
                })

        for rec in self.ledger.read_all("assumptions"):
            for a in (rec.get("assumptions") or [rec]):
                if not isinstance(a, dict) or not a.get("text"):
                    continue
                out["assumptions"].append({
                    "id": a.get("id"), "checkpoint_id": rec.get("checkpoint_id"),
                    "text": a.get("text"), "source": a.get("source"),
                    "category": a.get("category"),
                    "confidence": _f(a.get("confidence")),
                    "decayed_confidence": _f(a.get("decayed_confidence")),
                    "owner": a.get("owner"),
                    "status": a.get("status") or a.get("decay_status") or "unvalidated",
                    "symbol": a.get("symbol"), "validation": a.get("validation"),
                    "created_at": a.get("created_at"), "expires_at": a.get("expires_at"),
                    "evidence_ref": a.get("evidence_id"), "synced_at": now,
                })

        for rec in self.ledger.read_all("risk-reports"):
            risks = rec.get("risks") or []
            sec = [r for r in risks if r.get("band") == "security"]
            out["risk_reports"].append({
                "id": rec.get("id"), "checkpoint_id": rec.get("checkpoint_id"),
                "score": _f(rec.get("score")), "verdict": rec.get("verdict"),
                "security_flags": json.dumps([r.get("title") for r in sec]),
                "risk_count": len(risks),
                "critical_count": sum(1 for r in risks if r.get("severity") == "critical"),
                "generated_at": rec.get("generated_at"), "synced_at": now,
            })

        for rec in self.ledger.read_all("graph-snapshots"):
            out["graph_snapshots"].append({
                "id": rec.get("id"), "symbol": rec.get("symbol"),
                "checkpoint_id": rec.get("checkpoint_id"),
                "blast_radius_json": json.dumps(rec.get("surface") or {}),
                "head_sha": rec.get("head_sha"),
                "evidence_ref": rec.get("evidence_id"),
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
