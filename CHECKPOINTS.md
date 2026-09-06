# Checkpoint record

Chronological record of the graded checkpoints for Bengaluru Tech Week
Buildathon 2026. Each entry names the commit that *is* the checkpoint, what was
verified working at that point, and what was deliberately still open.

---

## Pre-curveball — stable state before the noon constraint

- **Code state:** `5706a39` — the last commit changing code; this record
  commit only adds documentation on top of that tree.
- **Branch:** `claude/prototype-entitle-databricks-4x863t`
- **Taken:** 2026-09-06, ahead of the 12:00 curveball (CLAUDE.md §0, the 11:45
  "get back to a runnable, stable state" checkpoint)
- **Tests:** 38 passing (`cd backend && python3 -m pytest tests/ -q`)

### Verified working end to end

| Area | State |
|---|---|
| Ingestion | Checkpoint metadata, transcript and diff captured as Evidence records — argv, exit code, duration, sha256 of raw stdout — and committed to the audit-ledger |
| Auditor / Watchman / Archivist / Ghost / Messenger | Run over that evidence with role-scoped prompts and citation enforcement |
| Graph re-verification | Blast-radius snapshot, re-check at HEAD, and the holds / stale / contradicted verdict |
| Audit ledger | Separate git repo, append-only, `git fsck` as the tamper-evidence check |
| Databricks | Delta tables mirrored from the ledger, four Genie queries, local SQLite mirror as the free-tier fallback |
| Security | Redaction as a hard write gate, token-gated API, redacted/full handoff toggle |
| Demo | Reproducible standalone build via `tools/build_demo.py` |

### The stale claim caught at this checkpoint

Checkpoint `cp_7f3a91` asserted a verdict about `validate_token`. Re-running
the identical `entire graph impact` command at HEAD returned:

```
STATUS : stale
REASON : 1 file(s) and 4 symbol(s) left the blast radius since the checkpoint
         - callers this claim relied on are gone
GONE   : is_revoked, login_handler, refresh_session, require_auth  (api/routes.py)
NEW    : impersonate_user                                          (admin/console.py)
```

`admin/console.py -> impersonate_user` is a new caller of an auth primitive that
the original verdict never covered — the exact class of drift a one-shot "done"
hides.

### Deliberately still open

- **The Referee has not run.** It is the curveball answer mechanism and there is
  no real constraint to compare against yet. Its prompt, comparison logic and
  card are built; the card stays locked until 12:00. The pre/post comparison
  will be generated from the actual constraint received at noon.
- **Reasoning is in verdict-free mode** wherever `ANTHROPIC_API_KEY` is unset.
  That is the designed degradation — personas return structurally valid output
  with no invented verdicts — not an unfinished path.
- **The `entire` binary is absent in the build container.** The adapter probes
  capability at runtime and replays recorded fixtures; on a machine with the
  Buildathon build the same code path runs live.

### Resuming from here

```bash
git checkout claude/prototype-entitle-databricks-4x863t
cd backend && python3 -m pytest tests/ -q      # confirm the state
PYTHONPATH=backend python3 -m witness.cli status
PYTHONPATH=backend python3 -m witness.cli serve --port 8000
```

To reconstruct context from the ledger rather than from memory — which is the
point of the exercise at noon:

```bash
PYTHONPATH=backend python3 -m witness.cli handoff cp_7f3a91   # Messenger packet
PYTHONPATH=backend python3 -m witness.cli verify              # ledger hash chain
```

### Post-curveball

Once the noon constraint lands, run the Auditor twice and let the Referee
compare them:

```bash
python3 -m witness.cli audit cp_7f3a91 --label pre-curveball
# ... implement the constraint ...
python3 -m witness.cli audit cp_7f3a91 --label post-curveball
python3 -m witness.cli referee cp_7f3a91
```
