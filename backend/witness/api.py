"""HTTP API + static hosting for the Witness dashboard.

Section 9 of CLAUDE.md: whatever gets a public URL must be gated. Free-tier
hosting is often open by default, and this data includes reasoning traces and
transcript spans. If WITNESS_ACCESS_TOKEN is set, every /api route requires it.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .databricks_sync import GENIE_QUERIES
from .pipeline import Witness

REPO = os.environ.get("WITNESS_REPO", ".")
LEDGER = os.environ.get("WITNESS_LEDGER", "audit-ledger")
FIXTURES = os.environ.get("WITNESS_FIXTURES", "fixtures")
ACCESS_TOKEN = os.environ.get("WITNESS_ACCESS_TOKEN")

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"

app = FastAPI(title="Witness", description="Checkpoint-native release auditor",
              version="0.1.0")

_witness: Witness | None = None


def get_witness() -> Witness:
    global _witness
    if _witness is None:
        _witness = Witness(
            REPO, LEDGER,
            fixture_dir=FIXTURES if Path(FIXTURES).exists() else None,
            record=os.environ.get("WITNESS_RECORD") == "1",
        )
    return _witness


def require_token(x_witness_token: str | None = Header(default=None)) -> None:
    """Gate the API when a token is configured.

    Compared with compare_digest so a wrong token can't be recovered by timing.
    """
    if not ACCESS_TOKEN:
        return
    if not x_witness_token or not secrets.compare_digest(x_witness_token, ACCESS_TOKEN):
        raise HTTPException(status_code=401, detail="missing or invalid X-Witness-Token")


Gate = Depends(require_token)


# -- read ------------------------------------------------------------------

@app.get("/api/status")
def status(_: None = Gate) -> dict[str, Any]:
    w = get_witness()
    return {**w.status(), "auth_required": bool(ACCESS_TOKEN)}


@app.get("/api/personas")
def personas(_: None = Gate) -> dict[str, Any]:
    from .agents import CAST, PERSONAS
    return {"personas": PERSONAS, "cast": CAST}


@app.get("/api/context/{checkpoint_id}")
def context(checkpoint_id: str, _: None = Gate) -> dict[str, Any]:
    """The Full/Partial badge for a checkpoint's report screens."""
    return get_witness().context_report(checkpoint_id)



@app.get("/api/checkpoints")
def checkpoints(_: None = Gate) -> dict[str, Any]:
    return get_witness().list_checkpoints()


@app.get("/api/checkpoints/{checkpoint_id}")
def checkpoint_detail(checkpoint_id: str, _: None = Gate) -> dict[str, Any]:
    w = get_witness()
    record = w.ledger.read("checkpoints", checkpoint_id)
    if not record:
        raise HTTPException(404, f"checkpoint {checkpoint_id} not in ledger - ingest it first")
    return {
        "checkpoint": record,
        "requirements": w.ledger.read("requirements", checkpoint_id),
        "assumptions": w.ledger.read("assumptions", checkpoint_id),
    }


@app.get("/api/ledger")
def ledger(limit: int = Query(40, le=500), _: None = Gate) -> dict[str, Any]:
    w = get_witness()
    return {**w.ledger.stats(), "history": w.ledger.history(limit=limit)}


@app.get("/api/ledger/verify")
def ledger_verify(_: None = Gate) -> dict[str, Any]:
    return get_witness().ledger.verify_chain()


@app.get("/api/evidence/{evidence_id}")
def evidence(evidence_id: str, _: None = Gate) -> dict[str, Any]:
    w = get_witness()
    ev = w.store.get(evidence_id)
    if ev:
        return ev.to_dict()
    stored = w.ledger.read("evidence", evidence_id)
    if stored:
        return stored
    raise HTTPException(404, f"no evidence recorded with id {evidence_id}")


@app.get("/api/graph/{checkpoint_id}/{symbol}")
def graph_view(checkpoint_id: str, symbol: str, _: None = Gate) -> dict[str, Any]:
    from .graph_check import GraphSnapshot, to_subgraph
    w = get_witness()
    stored = w.ledger.read("graph-snapshots", f"{symbol}__{checkpoint_id}")
    if not stored:
        raise HTTPException(404, "no snapshot for this symbol/checkpoint")
    snap = GraphSnapshot.from_dict(stored)
    delta = (stored.get("recheck") or {}).get("delta", {})
    return {"snapshot": stored, "subgraph": to_subgraph(snap, delta),
            "recheck": stored.get("recheck")}


@app.get("/api/graph-snapshots")
def graph_snapshots(_: None = Gate) -> dict[str, Any]:
    w = get_witness()
    return {"snapshots": w.ledger.read_all("graph-snapshots")}


@app.get("/api/risk-reports")
def risk_reports(_: None = Gate) -> dict[str, Any]:
    return {"reports": get_witness().ledger.read_all("risk-reports")}


# -- act -------------------------------------------------------------------

class CheckpointBody(BaseModel):
    checkpoint_id: str
    symbol: str | None = None
    label: str | None = None


@app.post("/api/ingest")
def ingest(body: CheckpointBody, _: None = Gate) -> dict[str, Any]:
    return get_witness().ingest(body.checkpoint_id)


@app.post("/api/audit")
def audit(body: CheckpointBody, _: None = Gate) -> dict[str, Any]:
    return get_witness().audit(body.checkpoint_id, label=body.label or "primary")


@app.post("/api/watch")
def watch(body: CheckpointBody, _: None = Gate) -> dict[str, Any]:
    return get_witness().watch(body.checkpoint_id, symbol=body.symbol)


@app.post("/api/warden")
def warden(body: CheckpointBody, _: None = Gate) -> dict[str, Any]:
    """The Warden's report on what the privacy boundary withheld."""
    return get_witness().warden(body.checkpoint_id)


@app.post("/api/archive")
def archive(body: CheckpointBody, _: None = Gate) -> dict[str, Any]:
    return get_witness().archive(body.checkpoint_id)


@app.post("/api/haunt")
def haunt(body: CheckpointBody, _: None = Gate) -> dict[str, Any]:
    return get_witness().haunt(body.checkpoint_id)


@app.post("/api/handoff")
def handoff(body: CheckpointBody, redacted: bool = True, _: None = Gate) -> dict[str, Any]:
    return get_witness().handoff(body.checkpoint_id, redacted=redacted)


@app.post("/api/run-all")
def run_all(body: CheckpointBody, _: None = Gate) -> dict[str, Any]:
    return get_witness().run_all(body.checkpoint_id, symbol=body.symbol)


class SymbolBody(BaseModel):
    symbol: str
    checkpoint_id: str
    depth: int = 2


@app.post("/api/graph/snapshot")
def graph_snapshot(body: SymbolBody, _: None = Gate) -> dict[str, Any]:
    return get_witness().snapshot_symbol(body.symbol, body.checkpoint_id, body.depth)


@app.post("/api/graph/recheck")
def graph_recheck_route(body: SymbolBody, _: None = Gate) -> dict[str, Any]:
    result = get_witness().recheck_symbol(body.symbol, body.checkpoint_id)
    if not result.get("ok"):
        return JSONResponse(status_code=404, content=result)
    return result


class RefereeBody(BaseModel):
    checkpoint_id: str
    before_label: str = "pre-curveball"
    after_label: str = "post-curveball"


@app.post("/api/referee")
def referee(body: RefereeBody, _: None = Gate) -> dict[str, Any]:
    result = get_witness().referee(body.checkpoint_id, body.before_label, body.after_label)
    if not result.get("ok"):
        return JSONResponse(status_code=400, content=result)
    return result


# -- databricks ------------------------------------------------------------

@app.get("/api/databricks/status")
def databricks_status(_: None = Gate) -> dict[str, Any]:
    return get_witness().databricks.status()


@app.post("/api/databricks/sync")
def databricks_sync(_: None = Gate) -> dict[str, Any]:
    return get_witness().databricks.sync().to_dict()


@app.get("/api/databricks/queries")
def databricks_queries(_: None = Gate) -> dict[str, Any]:
    return {"queries": {k: v["question"] for k, v in GENIE_QUERIES.items()}}


@app.get("/api/databricks/genie/{key}")
def databricks_genie(key: str, _: None = Gate) -> dict[str, Any]:
    return get_witness().databricks.genie(key)


# -- static ----------------------------------------------------------------

if FRONTEND.exists():
    app.mount("/assets", StaticFiles(directory=FRONTEND / "assets"), name="assets")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(FRONTEND / "index.html")

    @app.get("/{path:path}")
    def static_files(path: str) -> FileResponse:
        # Resolve inside FRONTEND only - never serve a traversal target.
        target = (FRONTEND / path).resolve()
        if target.is_file() and FRONTEND.resolve() in target.parents:
            return FileResponse(target)
        return FileResponse(FRONTEND / "index.html")
