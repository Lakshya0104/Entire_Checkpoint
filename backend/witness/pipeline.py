"""Orchestration: checkpoint in, release-readiness record out.

The flow from CLAUDE.md section 1:
  1. ingest      - checkpoint metadata + transcript into the ledger
  2. audit       - Auditor extracts requirements, maps them to diff/test evidence
  3. watch       - Watchman scores release readiness over Auditor + graph + secrets
  4. archive     - Archivist records assumptions with decay
  5. haunt       - Ghost recovers dead ends
  6. handoff     - Messenger writes the resume packet
  7. recheck     - re-run graph impact at HEAD, flag stale verdicts
  8. referee     - pre/post curveball goal-drift comparison

Each stage commits to the audit-ledger, so the git hash chain records not just
what we concluded but the order in which we concluded it.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .agents import AgentRunner, AgentResult, decay_assumptions
from .databricks_sync import DatabricksSync
from .entire_adapter import (
    EntireAdapter, EvidenceStore, Evidence, parse_checkpoint_list,
)
from .graph_check import (
    GraphSnapshot, capture_snapshot, diff_snapshots, recheck as graph_recheck,
    to_subgraph,
)
from .ledger import AuditLedger
from .redaction import scan_and_redact, summarise


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Witness:
    def __init__(
        self,
        repo: str | Path = ".",
        ledger_path: str | Path = "audit-ledger",
        *,
        fixture_dir: str | Path | None = None,
        record: bool = False,
        api_key: str | None = None,
    ) -> None:
        self.store = EvidenceStore(Path(fixture_dir) if fixture_dir else None)
        self.adapter = EntireAdapter(repo, self.store, record=record)
        self.ledger = AuditLedger(ledger_path)
        self.agents = AgentRunner(api_key=api_key)
        self.databricks = DatabricksSync(
            self.ledger, local_db=Path(ledger_path) / ".mirror.db")

    # -- status --------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        return {
            "repo": str(self.adapter.repo),
            "entire": {
                "binary": self.adapter.binary_present,
                "capabilities": self.adapter.capabilities(),
                "head": self.adapter.head_sha(),
            },
            "ledger": self.ledger.stats(),
            "agents": {
                "reasoning_available": self.agents.available,
                "note": None if self.agents.available else
                        "ANTHROPIC_API_KEY not set - personas return "
                        "structurally valid but verdict-free output",
            },
            "databricks": self.databricks.status(),
            "evidence_count": len(self.store.all()),
            "generated_at": _now(),
        }

    # -- 1. ingest -----------------------------------------------------------
    def list_checkpoints(self) -> dict[str, Any]:
        ev = self.adapter.checkpoint_list()
        checkpoints = parse_checkpoint_list(ev)
        return {
            "checkpoints": checkpoints,
            "count": len(checkpoints),
            "evidence": ev.to_dict(),
            "available": ev.ok,
            "note": None if ev.ok else
                    f"checkpoint list unavailable ({ev.stderr}) - no checkpoints "
                    f"can be asserted",
        }

    def ingest(self, checkpoint_id: str) -> dict[str, Any]:
        """Pull a checkpoint's metadata + transcript into the ledger."""
        meta_ev = self.adapter.checkpoint_explain(checkpoint_id)
        transcript_ev = self.adapter.checkpoint_transcript(checkpoint_id)
        diff_ev = self.adapter.diff_for(checkpoint_id)

        meta = meta_ev.as_json() or {}
        record = {
            "id": checkpoint_id,
            "session_id": meta.get("session_id") or meta.get("session") or "",
            "branch": meta.get("branch") or "",
            "author": meta.get("author") or meta.get("agent") or "",
            "created_at": meta.get("created_at") or meta.get("timestamp") or "",
            "summary": meta.get("summary") or meta.get("message") or "",
            "files_touched": meta.get("files_touched") or meta.get("files") or [],
            "transcript": transcript_ev.stdout,
            "diff_stat": diff_ev.stdout,
            "ingested_at": _now(),
            "evidence": {
                "metadata": meta_ev.to_dict(),
                "transcript": transcript_ev.to_dict(),
                "diff": diff_ev.to_dict(),
            },
        }
        prov = self.ledger.write(
            "checkpoints", checkpoint_id, record,
            message=f"ingest: checkpoint {checkpoint_id}",
        )
        for ev in (meta_ev, transcript_ev, diff_ev):
            self.ledger.write("evidence", ev.id, ev.to_dict(),
                              message=f"evidence: {' '.join(ev.command[:3])}")
        return {
            "checkpoint_id": checkpoint_id, "ledger": prov,
            "evidence_status": {
                "metadata": meta_ev.status, "transcript": transcript_ev.status,
                "diff": diff_ev.status,
            },
            "usable": any(e.ok for e in (meta_ev, transcript_ev, diff_ev)),
        }

    def _checkpoint_evidence(self, checkpoint_id: str) -> list[Evidence]:
        """Rebuild Evidence objects for a checkpoint already in the ledger."""
        record = self.ledger.read("checkpoints", checkpoint_id)
        if not record:
            return []
        out = []
        for ev_dict in (record.get("evidence") or {}).values():
            if isinstance(ev_dict, dict) and ev_dict.get("id"):
                ev = Evidence.from_dict(ev_dict)
                self.store.add(ev)
                out.append(ev)
        return out

    # -- 2. audit ------------------------------------------------------------
    def audit(self, checkpoint_id: str, *, label: str = "primary") -> dict[str, Any]:
        evidence = self._checkpoint_evidence(checkpoint_id)
        result = self.agents.run(
            "auditor", evidence,
            context=f"Auditing checkpoint {checkpoint_id}. Extract every discrete "
                    f"requirement the transcript states or implies, then judge each "
                    f"against the diff and any test output present.",
        )
        payload = {
            "checkpoint_id": checkpoint_id, "label": label,
            "generated_at": _now(), **result.data,
            "agent": result.to_dict(),
        }
        name = checkpoint_id if label == "primary" else f"{checkpoint_id}__{label}"
        prov = self.ledger.write(
            "requirements", name, payload,
            message=f"audit[{label}]: checkpoint {checkpoint_id}",
        )
        return {"result": result.to_dict(), "ledger": prov,
                "counts": _status_counts(result.data.get("requirements", []))}

    # -- 3. watch ------------------------------------------------------------
    def watch(self, checkpoint_id: str, *, symbol: str | None = None) -> dict[str, Any]:
        evidence = self._checkpoint_evidence(checkpoint_id)
        audit_rec = self.ledger.read("requirements", checkpoint_id) or {}

        graph_ev = None
        if symbol:
            graph_ev = self.adapter.graph_impact(symbol)
            evidence = evidence + [graph_ev]

        secret_findings = _collect_secret_findings(
            self.ledger.read("checkpoints", checkpoint_id) or {})

        result = self.agents.run(
            "watchman", evidence,
            context=f"Scoring release readiness for checkpoint {checkpoint_id}.",
            extra={
                "auditor_requirements": audit_rec.get("requirements", []),
                "secret_scan": secret_findings,
                "graph_symbol": symbol,
            },
        )
        report_id = f"risk_{uuid.uuid4().hex[:10]}"
        payload = {
            "id": report_id, "checkpoint_id": checkpoint_id,
            "generated_at": _now(), "symbol": symbol,
            "secret_scan": secret_findings, **result.data,
            "agent": result.to_dict(),
        }
        prov = self.ledger.write(
            "risk-reports", f"{_ts()}__{checkpoint_id}", payload,
            message=f"watchman: risk report for {checkpoint_id}",
        )
        return {"result": result.to_dict(), "ledger": prov, "report_id": report_id}

    # -- 4. archive ----------------------------------------------------------
    def archive(self, checkpoint_id: str) -> dict[str, Any]:
        evidence = self._checkpoint_evidence(checkpoint_id)
        result = self.agents.run(
            "archivist", evidence,
            context=f"Extracting assumptions from checkpoint {checkpoint_id}.",
        )
        assumptions = decay_assumptions(result.data.get("assumptions", []))
        payload = {
            "checkpoint_id": checkpoint_id, "generated_at": _now(),
            "assumptions": assumptions, "agent": result.to_dict(),
        }
        prov = self.ledger.write(
            "assumptions", checkpoint_id, payload,
            message=f"archivist: assumptions for {checkpoint_id}",
        )
        return {"result": result.to_dict(), "assumptions": assumptions, "ledger": prov}

    # -- 5. haunt ------------------------------------------------------------
    def haunt(self, checkpoint_id: str) -> dict[str, Any]:
        evidence = self._checkpoint_evidence(checkpoint_id)
        result = self.agents.run(
            "ghost", evidence,
            context=f"Recovering abandoned work and dead ends from checkpoint "
                    f"{checkpoint_id}. Report only what was rejected or left "
                    f"unfinished, never the successful path.",
        )
        return {"result": result.to_dict()}

    # -- 6. handoff ----------------------------------------------------------
    def handoff(self, checkpoint_id: str, *, redacted: bool = True) -> dict[str, Any]:
        """Resume packet. `redacted` is least-privilege, not just readability.

        CLAUDE.md section 9 requires the toggle: the full packet carries
        transcript spans and reasoning that shouldn't leave the team, the
        redacted one carries the shape of the work without the contents.
        """
        evidence = self._checkpoint_evidence(checkpoint_id)
        audit_rec = self.ledger.read("requirements", checkpoint_id) or {}
        asm_rec = self.ledger.read("assumptions", checkpoint_id) or {}

        result = self.agents.run(
            "messenger", evidence,
            context=f"Writing the handoff packet for checkpoint {checkpoint_id}.",
            extra={
                "requirements": audit_rec.get("requirements", []),
                "assumptions": asm_rec.get("assumptions", []),
            },
        )
        packet = dict(result.data)
        if redacted:
            packet = _redact_packet(packet)
        payload = {
            "checkpoint_id": checkpoint_id, "mode": "redacted" if redacted else "full",
            "generated_at": _now(), "packet": packet, "agent": result.to_dict(),
        }
        prov = self.ledger.write(
            "handoffs", f"{checkpoint_id}__{'redacted' if redacted else 'full'}",
            payload, message=f"messenger: handoff packet for {checkpoint_id}",
        )
        return {"packet": packet, "mode": payload["mode"],
                "result": result.to_dict(), "ledger": prov}

    # -- 7. graph snapshot + recheck ----------------------------------------
    def snapshot_symbol(self, symbol: str, checkpoint_id: str, depth: int = 2) -> dict[str, Any]:
        snap, ev = capture_snapshot(self.adapter, symbol, checkpoint_id, depth)
        prov = self.ledger.write(
            "graph-snapshots", f"{symbol}__{checkpoint_id}", snap.to_dict(),
            message=f"graph: snapshot {symbol} @ {checkpoint_id}",
        )
        return {"snapshot": snap.to_dict(), "ledger": prov,
                "evidence": ev.to_dict(), "usable": snap.usable}

    def recheck_symbol(self, symbol: str, checkpoint_id: str) -> dict[str, Any]:
        """Does this claim still hold? Re-run the same command at HEAD."""
        stored = self.ledger.read("graph-snapshots", f"{symbol}__{checkpoint_id}")
        if not stored:
            return {"ok": False, "error": "no snapshot recorded for this symbol/checkpoint - "
                                          "capture one at checkpoint time first"}
        before = GraphSnapshot.from_dict(stored)
        delta, after, ev = graph_recheck(self.adapter, before)

        updated = before.to_dict()
        updated["recheck"] = {
            "status": delta["status"], "reason": delta["reason"],
            "checked_at": _now(), "current_head": after.head_sha,
            "current_evidence_id": after.evidence_id, "delta": delta,
        }
        prov = self.ledger.write(
            "graph-snapshots", f"{symbol}__{checkpoint_id}", updated,
            message=f"graph: recheck {symbol} @ {checkpoint_id} -> {delta['status']}",
        )
        return {
            "ok": True, "symbol": symbol, "checkpoint_id": checkpoint_id,
            "delta": delta, "status": delta["status"],
            "subgraph_before": to_subgraph(before, delta),
            "subgraph_after": to_subgraph(after, delta),
            "evidence": ev.to_dict(), "ledger": prov,
        }

    # -- 8. referee ----------------------------------------------------------
    def referee(self, checkpoint_id: str, before_label: str = "pre-curveball",
                after_label: str = "post-curveball") -> dict[str, Any]:
        """Goal-drift comparison. The curveball answer mechanism."""
        before = self.ledger.read("requirements", f"{checkpoint_id}__{before_label}")
        after = self.ledger.read("requirements", f"{checkpoint_id}__{after_label}")
        missing = [n for n, v in ((before_label, before), (after_label, after)) if not v]
        if missing:
            return {"ok": False,
                    "error": f"missing audit run(s): {', '.join(missing)}. "
                             f"Run `witness audit <id> --label <name>` for each side first."}

        evidence = self._checkpoint_evidence(checkpoint_id)
        result = self.agents.run(
            "referee", evidence,
            context=f"Comparing two Auditor runs for checkpoint {checkpoint_id}: "
                    f"'{before_label}' against '{after_label}'.",
            extra={
                "before": {"label": before_label, "requirements": before.get("requirements", [])},
                "after": {"label": after_label, "requirements": after.get("requirements", [])},
            },
        )
        payload = {
            "checkpoint_id": checkpoint_id, "before_label": before_label,
            "after_label": after_label, "generated_at": _now(),
            **result.data, "agent": result.to_dict(),
        }
        prov = self.ledger.write(
            "referee", f"{checkpoint_id}__{_ts()}", payload,
            message=f"referee: goal drift for {checkpoint_id}",
        )
        return {"ok": True, "result": result.to_dict(), "ledger": prov,
                "before": before.get("requirements", []),
                "after": after.get("requirements", [])}

    # -- full run ------------------------------------------------------------
    def run_all(self, checkpoint_id: str, *, symbol: str | None = None) -> dict[str, Any]:
        out: dict[str, Any] = {"checkpoint_id": checkpoint_id, "started_at": _now()}
        out["ingest"] = self.ingest(checkpoint_id)
        out["audit"] = self.audit(checkpoint_id)
        out["archive"] = self.archive(checkpoint_id)
        out["haunt"] = self.haunt(checkpoint_id)
        if symbol:
            out["snapshot"] = self.snapshot_symbol(symbol, checkpoint_id)
        out["watch"] = self.watch(checkpoint_id, symbol=symbol)
        out["handoff"] = self.handoff(checkpoint_id)
        out["databricks"] = self.databricks.sync().to_dict()
        out["finished_at"] = _now()
        out["ledger_head"] = self.ledger.head()
        return out


# ---------------------------------------------------------------------------

def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _status_counts(requirements: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"satisfied": 0, "partial": 0, "unverified": 0, "contradicted": 0}
    for r in requirements:
        s = r.get("status")
        if s in counts:
            counts[s] += 1
    return counts


def _collect_secret_findings(checkpoint_record: dict[str, Any]) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    for ev in (checkpoint_record.get("evidence") or {}).values():
        if isinstance(ev, dict):
            findings.extend(ev.get("secret_findings") or [])
    kinds: dict[str, int] = {}
    for f in findings:
        kinds[f.get("kind", "unknown")] = kinds.get(f.get("kind", "unknown"), 0) + 1
    return {"count": len(findings), "kinds": kinds, "findings": findings[:20]}


_PACKET_FREE_TEXT = ("goal", "state", "next_action", "confidence_rationale")


def _redact_packet(packet: dict[str, Any]) -> dict[str, Any]:
    """Least-privilege handoff: keep the shape, drop the quoted reasoning."""
    out = dict(packet)
    for key in _PACKET_FREE_TEXT:
        if isinstance(out.get(key), str):
            out[key] = scan_and_redact(out[key]).text
    # Decision rationale is where transcript verbatim tends to leak.
    out["decisions"] = [
        {"decision": scan_and_redact(str(d.get("decision", ""))).text,
         "why": "[withheld - request full packet]"}
        for d in (packet.get("decisions") or []) if isinstance(d, dict)
    ]
    out["_redaction"] = {
        "mode": "redacted",
        "withheld": ["decisions[].why"],
        "note": "Full packet available to holders of the ledger; this copy is "
                "safe to paste into a shared channel.",
    }
    return out
