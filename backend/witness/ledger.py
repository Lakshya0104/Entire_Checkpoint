"""The audit-ledger: a second, separate git repo that is append-only evidence.

Per CLAUDE.md section 5 this must NOT live inside the app repo or on Entire's
own checkpoint branch. Its git history is the tamper-evidence chain - every
meaningful update is a commit, so the commit hash chain gives us an audit trail
with no custom crypto.

Layout:
    audit-ledger/
      checkpoints/<checkpoint-id>.json
      requirements/<checkpoint-id>.json
      assumptions/<id>.json
      risk-reports/<timestamp>.json
      graph-snapshots/<symbol>__<checkpoint-id>.json
      evidence/<evidence-id>.json
      CHANGELOG.md

Nothing is written without passing redaction.assert_clean first.
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .redaction import assert_clean, redact_obj, summarise

SUBDIRS = (
    "checkpoints", "requirements", "assumptions",
    "risk-reports", "graph-snapshots", "evidence", "handoffs", "referee",
    "warden",
)


def _slug(value: str) -> str:
    """Filesystem-safe name. Prevents a symbol like `a/../b` escaping the dir."""
    cleaned = re.sub(r"[^A-Za-z0-9_.\-]", "_", value).strip("._-")
    return (cleaned or "unnamed")[:120]


class LedgerError(RuntimeError):
    pass


class AuditLedger:
    def __init__(self, path: str | Path = "audit-ledger", *, author: str = "Witness <witness@local>") -> None:
        self.path = Path(path).resolve()
        self.author = author

    # -- setup --------------------------------------------------------------
    def init(self) -> dict[str, Any]:
        created = not (self.path / ".git").exists()
        self.path.mkdir(parents=True, exist_ok=True)
        for sub in SUBDIRS:
            d = self.path / sub
            d.mkdir(exist_ok=True)
            keep = d / ".gitkeep"
            if not keep.exists():
                keep.write_text("")
        if created:
            self._git("init", "-q")
            self._git("config", "user.name", "Witness")
            self._git("config", "user.email", "witness@local")
            changelog = self.path / "CHANGELOG.md"
            if not changelog.exists():
                changelog.write_text(
                    "# Audit Ledger\n\n"
                    "Append-only evidence record for Witness.\n"
                    "Every entry below corresponds to one commit; the git hash chain "
                    "is the tamper-evidence trail.\n\n"
                )
            self._commit("ledger: initialise audit-ledger")
        return {"path": str(self.path), "created": created}

    @property
    def exists(self) -> bool:
        return (self.path / ".git").exists()

    # -- git ----------------------------------------------------------------
    def _git(self, *args: str) -> subprocess.CompletedProcess[str]:
        proc = subprocess.run(
            ["git", *args], cwd=self.path, capture_output=True, text=True, timeout=30,
        )
        if proc.returncode != 0 and args[0] not in ("diff", "status", "rev-parse", "log"):
            raise LedgerError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
        return proc

    def _commit(self, message: str) -> str | None:
        self._git("add", "-A")
        status = self._git("status", "--porcelain")
        if not status.stdout.strip():
            return None                      # nothing changed; no empty commits
        self._git("commit", "-q", "-m", message)
        return self._git("rev-parse", "HEAD").stdout.strip()

    def head(self) -> str | None:
        if not self.exists:
            return None
        out = self._git("rev-parse", "HEAD").stdout.strip()
        return out or None

    def history(self, limit: int = 50) -> list[dict[str, str]]:
        if not self.exists:
            return []
        proc = self._git("log", f"-{limit}", "--pretty=format:%H%x1f%an%x1f%aI%x1f%s")
        rows = []
        for line in proc.stdout.splitlines():
            if not line.strip():
                continue
            parts = line.split("\x1f")
            if len(parts) == 4:
                rows.append({
                    "sha": parts[0], "short": parts[0][:8],
                    "author": parts[1], "date": parts[2], "subject": parts[3],
                })
        return rows

    def verify_chain(self) -> dict[str, Any]:
        """Ask git to re-verify its own object hashes.

        `git fsck` recomputes every object's sha1 from content. If any ledger
        file were edited in place after commit, the object no longer hashes to
        its name and this reports it. That is the whole tamper-evidence claim,
        and it is checkable live in the demo.
        """
        if not self.exists:
            return {"ok": False, "reason": "ledger not initialised"}
        proc = subprocess.run(
            ["git", "fsck", "--no-progress", "--no-dangling"],
            cwd=self.path, capture_output=True, text=True, timeout=60,
        )
        commits = self.history(limit=1000)
        return {
            "ok": proc.returncode == 0,
            "fsck_output": (proc.stdout + proc.stderr).strip() or "no corruption detected",
            "commit_count": len(commits),
            "head": self.head(),
        }

    # -- writes -------------------------------------------------------------
    def write(self, subdir: str, name: str, payload: dict[str, Any], *, message: str) -> dict[str, Any]:
        """Redact, write, commit. Returns the record's provenance."""
        if subdir not in SUBDIRS:
            raise LedgerError(f"unknown ledger subdir: {subdir}")
        if not self.exists:
            self.init()

        redacted, findings = redact_obj(payload)
        redacted.setdefault("_ledger", {})
        redacted["_ledger"] = {
            "written_at": datetime.now(timezone.utc).isoformat(),
            "secret_scan": {
                "clean": not findings,
                "redacted_count": len(findings),
                "kinds": summarise(findings),
            },
        }

        text = json.dumps(redacted, indent=2, sort_keys=False, default=str)
        assert_clean(text, f"{subdir}/{name}")     # hard gate

        rel = Path(subdir) / f"{_slug(name)}.json"
        target = self.path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

        self._append_changelog(message, rel)
        sha = self._commit(message)
        return {
            "path": str(rel), "commit": sha, "short": sha[:8] if sha else None,
            "redacted_count": len(findings),
        }

    def _append_changelog(self, message: str, rel: Path) -> None:
        changelog = self.path / "CHANGELOG.md"
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
        with changelog.open("a") as fh:
            fh.write(f"- `{stamp}` {message} → `{rel}`\n")

    def read(self, subdir: str, name: str) -> dict[str, Any] | None:
        target = self.path / subdir / f"{_slug(name)}.json"
        if not target.exists():
            return None
        return json.loads(target.read_text())

    def list(self, subdir: str) -> list[str]:
        d = self.path / subdir
        if not d.exists():
            return []
        return sorted(p.stem for p in d.glob("*.json"))

    def read_all(self, subdir: str) -> list[dict[str, Any]]:
        out = []
        for name in self.list(subdir):
            rec = self.read(subdir, name)
            if rec is not None:
                out.append(rec)
        return out

    def stats(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "exists": self.exists,
            "head": self.head(),
            "commits": len(self.history(limit=1000)),
            "counts": {sub: len(self.list(sub)) for sub in SUBDIRS},
        }
