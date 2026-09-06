"""Stale-claim detection: "does this claim still hold?"

CLAUDE.md section 6. The mechanism:
  1. At checkpoint time, capture `entire graph impact --symbol NAME --depth 2`
     and store the raw text as a snapshot in the ledger.
  2. On demand, re-run the identical command at current HEAD.
  3. Diff the two blast radii.
  4. green  - nothing material moved, the verdict still holds
     amber  - surrounding graph shifted but the symbol itself didn't -> stale
     red    - the claim is contradicted by the change

`graph impact` returns formatted text, not JSON. Per CLAUDE.md we do not try to
parse it with regex for the *verdict* - that is a semantic judgement handed to
Claude. What we do extract structurally are the cheap, unambiguous signals a
diff needs: identifiers and file paths mentioned in the output. Those give a
deterministic "something moved" trigger; the LLM then says what it means.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .entire_adapter import EntireAdapter, Evidence

# Deterministic surface extraction. Conservative on purpose: we want the set of
# things the output *mentions*, not a claim to have understood its grammar.
_FILE_RE = re.compile(r"\b[\w./\-]+\.(?:py|ts|tsx|js|jsx|go|rs|java|rb|kt|swift|c|cc|cpp|h|hpp|sql|sh)\b")
_SYMBOL_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]{2,}(?:\.[A-Za-z_][A-Za-z0-9_]*)*\b")

_NOISE = {
    "the", "and", "for", "with", "from", "this", "that", "impact", "symbol",
    "depth", "repo", "callers", "callees", "caller", "callee", "file", "files",
    "line", "lines", "found", "none", "graph", "analysis", "blast", "radius",
    "type", "types", "data", "flow", "flows", "consumers", "results", "result",
    "def", "class", "function", "import", "return", "true", "false", "null",
}

STATUS_GREEN = "holds"
STATUS_AMBER = "stale"
STATUS_RED = "contradicted"
STATUS_UNKNOWN = "unverified"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def extract_surface(text: str) -> dict[str, list[str]]:
    """Files and identifiers mentioned in a graph-impact output."""
    if not text:
        return {"files": [], "symbols": []}
    files = sorted(set(_FILE_RE.findall(text)))
    symbols = sorted({
        s for s in _SYMBOL_RE.findall(text)
        if s.lower() not in _NOISE and not s.replace(".", "").isdigit()
    })
    # A path fragment already counted as a file shouldn't double as a symbol.
    # Both the bare segments (`refund`) and dotted tails (`refund.py`) appear
    # in the identifier sweep, so filter on containment in a known path too.
    file_parts = {p for f in files for p in re.split(r"[./]", f) if p}
    symbols = [
        s for s in symbols
        if s not in file_parts and not any(s in f for f in files)
    ]
    return {"files": files, "symbols": symbols}


@dataclass
class GraphSnapshot:
    """Blast radius for one symbol, captured at a point in history."""

    id: str
    symbol: str
    checkpoint_id: str
    depth: int
    captured_at: str
    head_sha: str | None
    evidence_id: str
    evidence_status: str
    raw_output: str
    surface: dict[str, list[str]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "symbol": self.symbol, "checkpoint_id": self.checkpoint_id,
            "depth": self.depth, "captured_at": self.captured_at,
            "head_sha": self.head_sha, "evidence_id": self.evidence_id,
            "evidence_status": self.evidence_status, "raw_output": self.raw_output,
            "surface": self.surface,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "GraphSnapshot":
        return cls(
            id=d["id"], symbol=d["symbol"], checkpoint_id=d["checkpoint_id"],
            depth=d.get("depth", 2), captured_at=d.get("captured_at", ""),
            head_sha=d.get("head_sha"), evidence_id=d.get("evidence_id", ""),
            evidence_status=d.get("evidence_status", "unknown"),
            raw_output=d.get("raw_output", ""), surface=d.get("surface", {}),
        )

    @property
    def usable(self) -> bool:
        return self.evidence_status in ("ok", "replayed") and bool(self.raw_output.strip())


def capture_snapshot(
    adapter: EntireAdapter, symbol: str, checkpoint_id: str, depth: int = 2,
) -> tuple[GraphSnapshot, Evidence]:
    ev = adapter.graph_impact(symbol, depth=depth)
    snap = GraphSnapshot(
        id=f"gs_{uuid.uuid4().hex[:10]}",
        symbol=symbol, checkpoint_id=checkpoint_id, depth=depth,
        captured_at=_now(), head_sha=adapter.head_sha(),
        evidence_id=ev.id, evidence_status=ev.status,
        raw_output=ev.stdout, surface=extract_surface(ev.stdout),
    )
    return snap, ev


def diff_snapshots(before: GraphSnapshot, after: GraphSnapshot) -> dict[str, Any]:
    """Structural delta between two blast radii for the same symbol.

    Returns the material changes plus a deterministic severity. The LLM layer
    can escalate to `contradicted`, but it can never quietly downgrade a
    structural change back to green - the delta is arithmetic, not opinion.
    """
    if before.symbol != after.symbol:
        raise ValueError("cannot diff snapshots of different symbols")

    b_files = set(before.surface.get("files", []))
    a_files = set(after.surface.get("files", []))
    b_syms = set(before.surface.get("symbols", []))
    a_syms = set(after.surface.get("symbols", []))

    delta = {
        "symbol": before.symbol,
        "files_added": sorted(a_files - b_files),
        "files_removed": sorted(b_files - a_files),
        "symbols_added": sorted(a_syms - b_syms),
        "symbols_removed": sorted(b_syms - a_syms),
        "output_changed": before.raw_output.strip() != after.raw_output.strip(),
        "before": {
            "captured_at": before.captured_at, "head_sha": before.head_sha,
            "evidence_id": before.evidence_id, "file_count": len(b_files),
            "symbol_count": len(b_syms),
        },
        "after": {
            "captured_at": after.captured_at, "head_sha": after.head_sha,
            "evidence_id": after.evidence_id, "file_count": len(a_files),
            "symbol_count": len(a_syms),
        },
    }

    # Did the symbol itself survive in its own blast radius?
    symbol_present_before = before.symbol in b_syms or any(
        before.symbol in s for s in b_syms)
    symbol_present_after = after.symbol in a_syms or any(
        after.symbol in s for s in a_syms)
    delta["symbol_vanished"] = symbol_present_before and not symbol_present_after

    if not before.usable or not after.usable:
        delta["status"] = STATUS_UNKNOWN
        delta["reason"] = (
            "graph impact output unavailable on one side of the comparison; "
            "no verdict is asserted"
        )
        return delta

    changed_count = (
        len(delta["files_added"]) + len(delta["files_removed"])
        + len(delta["symbols_added"]) + len(delta["symbols_removed"])
    )

    if delta["symbol_vanished"]:
        delta["status"] = STATUS_RED
        delta["reason"] = (
            f"`{before.symbol}` no longer appears in its own blast radius - the "
            f"claim references a symbol that has moved or been removed"
        )
    elif delta["files_removed"] or delta["symbols_removed"]:
        delta["status"] = STATUS_AMBER
        delta["reason"] = (
            f"{len(delta['files_removed'])} file(s) and "
            f"{len(delta['symbols_removed'])} symbol(s) left the blast radius "
            f"since the checkpoint - callers this claim relied on are gone"
        )
    elif changed_count:
        delta["status"] = STATUS_AMBER
        delta["reason"] = (
            f"blast radius grew by {len(delta['files_added'])} file(s) and "
            f"{len(delta['symbols_added'])} symbol(s) - new dependents exist "
            f"that were never covered by the original verdict"
        )
    elif delta["output_changed"]:
        delta["status"] = STATUS_AMBER
        delta["reason"] = "graph impact output changed with no change to the file/symbol set"
    else:
        delta["status"] = STATUS_GREEN
        delta["reason"] = "blast radius is byte-identical to the checkpoint snapshot"

    delta["changed_count"] = changed_count
    return delta


def recheck(
    adapter: EntireAdapter, snapshot: GraphSnapshot,
) -> tuple[dict[str, Any], GraphSnapshot, Evidence]:
    """Re-run the identical command at HEAD and diff it against the snapshot."""
    current, ev = capture_snapshot(
        adapter, snapshot.symbol, snapshot.checkpoint_id, depth=snapshot.depth,
    )
    return diff_snapshots(snapshot, current), current, ev


def to_subgraph(snapshot: GraphSnapshot, delta: dict[str, Any] | None = None) -> dict[str, Any]:
    """Local subgraph for rendering - the blast radius only, never the whole repo.

    Nodes are coloured by what the diff said about them, so the visualisation
    and the agent cards use one status vocabulary (CLAUDE.md section 3).
    """
    delta = delta or {}
    added = set(delta.get("symbols_added", [])) | set(delta.get("files_added", []))
    removed = set(delta.get("symbols_removed", [])) | set(delta.get("files_removed", []))

    nodes: list[dict[str, Any]] = [{
        "id": snapshot.symbol, "label": snapshot.symbol, "kind": "root",
        "status": delta.get("status", STATUS_UNKNOWN),
    }]
    seen = {snapshot.symbol}

    # Cap what we render - CLAUDE.md: keep it to what fits on one screen.
    for sym in snapshot.surface.get("symbols", [])[:14]:
        if sym in seen:
            continue
        seen.add(sym)
        nodes.append({
            "id": sym, "label": sym, "kind": "symbol",
            "status": STATUS_RED if sym in removed else
                      STATUS_AMBER if sym in added else STATUS_GREEN,
        })
    for f in snapshot.surface.get("files", [])[:10]:
        if f in seen:
            continue
        seen.add(f)
        nodes.append({
            "id": f, "label": f.split("/")[-1], "path": f, "kind": "file",
            "status": STATUS_RED if f in removed else
                      STATUS_AMBER if f in added else STATUS_GREEN,
        })

    for sym in delta.get("symbols_removed", [])[:8]:
        if sym not in seen:
            seen.add(sym)
            nodes.append({"id": sym, "label": sym, "kind": "symbol",
                          "status": STATUS_RED, "gone": True})

    edges = [
        {"source": snapshot.symbol, "target": n["id"], "status": n["status"]}
        for n in nodes[1:]
    ]
    return {"symbol": snapshot.symbol, "nodes": nodes, "edges": edges,
            "status": delta.get("status", STATUS_UNKNOWN)}
