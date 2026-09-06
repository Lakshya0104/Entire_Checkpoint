"""Adapter over the Entire CLI.

Every fact in Witness has to trace back to a real command. This module is the
only place that shells out, and it records each invocation as an Evidence
record: argv, exit code, duration, a sha256 of stdout, and the (redacted)
output itself. Nothing downstream is allowed to assert anything it cannot
point at an Evidence id for.

Command surface is CLAUDE.md section 7, verified against docs.entire.io:

    entire checkpoint list --json
    entire checkpoint explain <id> --json
    entire checkpoint explain <id> --transcript
    entire checkpoint search "query" --json
    entire graph impact --repo . --symbol NAME --depth 2
    entire graph search --repo . --query "task"
    entire graph neighbors --repo . --symbol NAME

The `graph *` commands return formatted text, not JSON. We deliberately do not
regex-parse them - the raw text is carried through to the Claude call that
produces the verdict, per CLAUDE.md.

Availability is probed at runtime. If a subcommand is missing, the adapter
returns an Evidence record with status "unavailable" rather than raising or
inventing output. Callers turn that into an `unverified` label - never a guess.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .redaction import redact_obj, scan_and_redact

ENTIRE_BIN = os.environ.get("WITNESS_ENTIRE_BIN", "entire")
DEFAULT_TIMEOUT = int(os.environ.get("WITNESS_ENTIRE_TIMEOUT", "60"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Evidence:
    """One command invocation, and what it proved.

    `status` is the load-bearing field:
      ok          - command ran, exit 0, output captured
      failed      - command ran, non-zero exit
      unavailable - binary or subcommand not present on this machine
      replayed    - served from a recorded fixture (see EvidenceStore)
    """

    id: str
    command: list[str]
    status: str
    exit_code: int | None
    stdout: str
    stderr: str
    stdout_sha256: str
    duration_ms: int
    captured_at: str
    cwd: str
    source: str = "live"          # "live" | "fixture"
    secret_findings: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status in ("ok", "replayed")

    def as_json(self) -> str | None:
        """Parse stdout as JSON, or None if it isn't JSON."""
        if not self.ok or not self.stdout.strip():
            return None
        try:
            return json.loads(self.stdout)
        except json.JSONDecodeError:
            return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "command": self.command,
            "command_str": " ".join(self.command),
            "status": self.status,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "stdout_sha256": self.stdout_sha256,
            "duration_ms": self.duration_ms,
            "captured_at": self.captured_at,
            "cwd": self.cwd,
            "source": self.source,
            "secret_findings": self.secret_findings,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Evidence":
        return cls(
            id=d["id"], command=d["command"], status=d["status"],
            exit_code=d.get("exit_code"), stdout=d.get("stdout", ""),
            stderr=d.get("stderr", ""), stdout_sha256=d.get("stdout_sha256", ""),
            duration_ms=d.get("duration_ms", 0), captured_at=d.get("captured_at", ""),
            cwd=d.get("cwd", ""), source=d.get("source", "live"),
            secret_findings=d.get("secret_findings", []),
        )


class EvidenceStore:
    """Records every Evidence produced this run, and can replay fixtures.

    Replay matters for two reasons: the demo must survive a machine without the
    Entire build installed, and the stale-claim check in CLAUDE.md section 6
    needs the *old* command output to diff against.
    """

    def __init__(self, fixture_dir: Path | None = None) -> None:
        self.records: dict[str, Evidence] = {}
        self.fixture_dir = Path(fixture_dir) if fixture_dir else None

    # -- fixture key ------------------------------------------------------
    @staticmethod
    def _key(command: list[str]) -> str:
        joined = " ".join(command)
        safe = "".join(c if c.isalnum() else "_" for c in joined)[:80]
        return f"{safe}__{hashlib.sha256(joined.encode()).hexdigest()[:8]}.json"

    def load_fixture(self, command: list[str]) -> Evidence | None:
        if not self.fixture_dir:
            return None
        path = self.fixture_dir / self._key(command)
        if not path.exists():
            return None
        ev = Evidence.from_dict(json.loads(path.read_text()))
        ev.id = f"ev_{uuid.uuid4().hex[:12]}"   # fresh id, same payload
        ev.status = "replayed"
        ev.source = "fixture"
        self.records[ev.id] = ev
        return ev

    def save_fixture(self, ev: Evidence) -> Path | None:
        if not self.fixture_dir or not ev.ok:
            return None
        self.fixture_dir.mkdir(parents=True, exist_ok=True)
        path = self.fixture_dir / self._key(ev.command)
        path.write_text(json.dumps(ev.to_dict(), indent=2))
        return path

    def add(self, ev: Evidence) -> Evidence:
        self.records[ev.id] = ev
        return ev

    def get(self, evidence_id: str) -> Evidence | None:
        return self.records.get(evidence_id)

    def all(self) -> list[Evidence]:
        return list(self.records.values())


class EntireAdapter:
    def __init__(
        self,
        repo: str | Path = ".",
        store: EvidenceStore | None = None,
        *,
        record: bool = False,
        replay: bool = True,
    ) -> None:
        self.repo = Path(repo).resolve()
        self.store = store or EvidenceStore()
        self.record = record      # write live output to fixtures
        self.replay = replay      # fall back to fixtures when CLI unavailable
        self._capabilities: dict[str, bool] | None = None

    # -- capability probing ----------------------------------------------
    @property
    def binary_present(self) -> bool:
        return shutil.which(ENTIRE_BIN) is not None

    def capabilities(self) -> dict[str, bool]:
        """Which parts of the documented surface this machine actually has.

        The public npm build of entire-cli (0.0.3) ships enable/status/rewind/
        explain but no `checkpoint` or `graph` subcommands; the Buildathon
        build has both. Probing beats assuming.
        """
        if self._capabilities is not None:
            return self._capabilities
        caps = {"binary": self.binary_present, "checkpoint": False, "graph": False}
        if caps["binary"]:
            for sub in ("checkpoint", "graph"):
                try:
                    p = subprocess.run(
                        [ENTIRE_BIN, sub, "--help"],
                        cwd=self.repo, capture_output=True, text=True, timeout=15,
                    )
                    # A missing subcommand falls back to the root help text.
                    caps[sub] = p.returncode == 0 and sub in p.stdout.lower()
                except (OSError, subprocess.SubprocessError):
                    caps[sub] = False
        self._capabilities = caps
        return caps

    # -- core runner -------------------------------------------------------
    def _run(self, args: list[str], *, needs: str | None = None) -> Evidence:
        command = [ENTIRE_BIN, *args]
        caps = self.capabilities()
        available = caps["binary"] and (needs is None or caps.get(needs, False))

        if not available and self.replay:
            replayed = self.store.load_fixture(command)
            if replayed is not None:
                return replayed

        if not available:
            reason = (
                f"`{ENTIRE_BIN}` not found on PATH"
                if not caps["binary"]
                else f"`{ENTIRE_BIN} {needs}` subcommand not available in this build"
            )
            return self.store.add(Evidence(
                id=f"ev_{uuid.uuid4().hex[:12]}", command=command,
                status="unavailable", exit_code=None, stdout="",
                stderr=reason, stdout_sha256="", duration_ms=0,
                captured_at=_now(), cwd=str(self.repo),
            ))

        started = time.time()
        try:
            proc = subprocess.run(
                command, cwd=self.repo, capture_output=True, text=True,
                timeout=DEFAULT_TIMEOUT,
            )
            rc, out, err = proc.returncode, proc.stdout, proc.stderr
        except subprocess.TimeoutExpired:
            rc, out, err = None, "", f"timed out after {DEFAULT_TIMEOUT}s"
        except OSError as exc:
            rc, out, err = None, "", f"failed to execute: {exc}"
        duration_ms = int((time.time() - started) * 1000)

        # Hash the RAW output - the integrity claim is about what the command
        # actually printed, not about our redacted copy of it.
        raw_sha = hashlib.sha256(out.encode("utf-8")).hexdigest()

        clean_out = scan_and_redact(out)
        clean_err = scan_and_redact(err or "")

        ev = Evidence(
            id=f"ev_{uuid.uuid4().hex[:12]}", command=command,
            status="ok" if rc == 0 else "failed", exit_code=rc,
            stdout=clean_out.text, stderr=clean_err.text,
            stdout_sha256=raw_sha, duration_ms=duration_ms,
            captured_at=_now(), cwd=str(self.repo),
            secret_findings=[f.to_dict() for f in clean_out.findings + clean_err.findings],
        )
        self.store.add(ev)
        if self.record:
            self.store.save_fixture(ev)
        return ev

    # -- checkpoint surface -------------------------------------------------
    def checkpoint_list(self) -> Evidence:
        return self._run(["checkpoint", "list", "--json"], needs="checkpoint")

    def checkpoint_explain(self, checkpoint_id: str) -> Evidence:
        return self._run(["checkpoint", "explain", checkpoint_id, "--json"], needs="checkpoint")

    def checkpoint_transcript(self, checkpoint_id: str) -> Evidence:
        return self._run(["checkpoint", "explain", checkpoint_id, "--transcript"], needs="checkpoint")

    def checkpoint_search(self, query: str) -> Evidence:
        return self._run(["checkpoint", "search", query, "--json"], needs="checkpoint")

    # -- graph surface ------------------------------------------------------
    def graph_impact(self, symbol: str, depth: int = 2) -> Evidence:
        return self._run(
            ["graph", "impact", "--repo", ".", "--symbol", symbol, "--depth", str(depth)],
            needs="graph",
        )

    def graph_search(self, query: str) -> Evidence:
        return self._run(["graph", "search", "--repo", ".", "--query", query], needs="graph")

    def graph_neighbors(self, symbol: str) -> Evidence:
        return self._run(["graph", "neighbors", "--repo", ".", "--symbol", symbol], needs="graph")

    # -- git fallbacks ------------------------------------------------------
    def head_sha(self) -> str | None:
        try:
            p = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.repo,
                               capture_output=True, text=True, timeout=10)
            return p.stdout.strip() or None if p.returncode == 0 else None
        except (OSError, subprocess.SubprocessError):
            return None

    def diff_for(self, checkpoint_id: str, base: str | None = None) -> Evidence:
        """Diff backing a checkpoint. Recorded as evidence like any command."""
        args = ["git", "diff", "--stat", f"{base or checkpoint_id + '~1'}", checkpoint_id]
        started = time.time()
        try:
            p = subprocess.run(args, cwd=self.repo, capture_output=True,
                               text=True, timeout=30)
            rc, out, err = p.returncode, p.stdout, p.stderr
        except (OSError, subprocess.SubprocessError) as exc:
            rc, out, err = None, "", str(exc)
        clean = scan_and_redact(out)
        return self.store.add(Evidence(
            id=f"ev_{uuid.uuid4().hex[:12]}", command=args,
            status="ok" if rc == 0 else "failed", exit_code=rc,
            stdout=clean.text, stderr=err[:2000],
            stdout_sha256=hashlib.sha256(out.encode()).hexdigest(),
            duration_ms=int((time.time() - started) * 1000),
            captured_at=_now(), cwd=str(self.repo),
            secret_findings=[f.to_dict() for f in clean.findings],
        ))


def parse_checkpoint_list(ev: Evidence) -> list[dict[str, Any]]:
    """Normalise `checkpoint list --json` into a stable shape.

    Entire's exact envelope may vary (bare list, or wrapped in a key), so we
    accept the shapes we've seen and normalise field names. Anything we can't
    read comes back empty - the caller reports 'no checkpoints', not a guess.
    """
    data = ev.as_json()
    if data is None:
        return []
    rows = data
    if isinstance(data, dict):
        for key in ("checkpoints", "items", "results", "data"):
            if isinstance(data.get(key), list):
                rows = data[key]
                break
        else:
            rows = []
    if not isinstance(rows, list):
        return []

    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        out.append({
            "id": str(row.get("id") or row.get("checkpoint_id") or row.get("sha") or ""),
            "session_id": row.get("session_id") or row.get("session") or "",
            "branch": row.get("branch") or "",
            "author": row.get("author") or row.get("agent") or "",
            "created_at": row.get("created_at") or row.get("timestamp") or row.get("date") or "",
            "summary": row.get("summary") or row.get("message") or row.get("title") or "",
            "files_touched": row.get("files_touched") or row.get("files") or [],
            "_raw": row,
        })
    return [c for c in out if c["id"]]
