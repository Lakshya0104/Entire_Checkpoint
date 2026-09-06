# Witness

**A checkpoint-native release auditor.** It turns Entire checkpoints and Entire
Graph evidence into a verified, re-checkable release-readiness record — and
catches when its own past claims have gone stale.

Built for Bengaluru Tech Week Buildathon 2026, Track 1 (Checkpoint-Native
Developer Experience), with Databricks as the storage and analytics layer.

---

## The two rules

**1. Every verdict traces to a command.** Every requirement, status and risk
shown anywhere traces back to a real `entire checkpoint` or `entire graph`
invocation. If the evidence is missing, the label is `unverified` — never a
guess.

**2. Nothing raw leaves the machine.** The Databricks mirror and the Claude API
are external services. Neither receives a checkpoint transcript or raw prompt
text. Both call through one shared `privacy.redact()`. Intent extraction happens
on-machine, and only structured claims cross the boundary.

This is enforced in three places, not just intended:

| Guard | Where | What it does |
|---|---|---|
| Evidence records | `entire_adapter.py` | Every command run is captured with argv, exit code, duration, and a sha256 of raw stdout |
| Citation enforcement | `agents.py::_enforce_citations` | A model finding citing an evidence id that doesn't exist is downgraded to `unverified` or dropped |
| No-evidence short-circuit | `agents.py::AgentRunner.run` | If no input command succeeded, the persona returns verdict-free output without ever reaching the model |
| Allowlist egress | `privacy.py::redact` | Outbound payloads are *built* from named fields, so an unanticipated field cannot ride along |
| Final egress gate | `privacy.py::assert_no_raw_text` | Raises if raw text survived into a payload about to leave |
| Partial-context downgrade | `agents.py::_apply_partial_context` | Under withheld input, affirming labels become `redacted` and risk scores become `null` — enforced in code, not left to the model |

`test_hallucinated_citation_downgrades_auditor_finding` and
`test_no_usable_evidence_means_no_verdict` keep those honest.

---

## Quick start

```bash
pip install -r backend/requirements.txt

export ANTHROPIC_API_KEY=...            # reasoning; without it, verdict-free mode
PYTHONPATH=backend python3 -m witness.cli init
PYTHONPATH=backend python3 -m witness.cli status      # what this machine actually has

PYTHONPATH=backend python3 -m witness.cli serve --port 8000
```

Then open http://127.0.0.1:8000.

### On a machine with the Buildathon Entire build

```bash
witness checkpoints                              # entire checkpoint list --json
witness run <checkpoint-id> --symbol validate_token --record
witness recheck validate_token <checkpoint-id>   # does the claim still hold?
witness genie unvalidated_security_assumptions
```

`--record` saves live command output into `fixtures/`, so the demo survives a
machine without the CLI (and gives the stale-claim check an old snapshot to
diff against).

---

## Shareable demo build

`tools/build_demo.py` runs the pipeline for real, captures every API response,
and inlines them with the frontend into one self-contained HTML file:

```bash
python3 tools/build_demo.py --out witness-demo.html
```

The result is a *recording*, not a mock — real Entire output, the ledger's real
commit hashes, a real `git fsck`, and a real stale-claim re-check. Its `api()`
replays those responses instead of pretending to execute, and the page says so
in a banner. Useful for sharing a link when the backend isn't reachable.

---

## Architecture

```
Entire CLI ──▶ Ingestion ──▶ audit-ledger (separate git repo)
                   │                    │
                   ▼                    ▼
            Claude API            Databricks Delta tables
        (6 role-scoped                  │
         personas)                      ▼
                   │           Databricks SQL / Genie
                   ▼
          Frontend dashboard
```

Reasoning goes to the Claude API directly, never through Databricks model
serving — Free Edition has no provisioned throughput, which is a quota risk
mid-demo. Databricks is the storage, analytics, and hosting layer.

### Layout

```
backend/witness/
  entire_adapter.py   shells out to Entire; produces Evidence records
  redaction.py        secret-pattern + entropy scan; the write gate
  ledger.py           the separate append-only git repo
  privacy.py          the shared egress boundary + on-machine intent extraction
  agents.py           six personas, role-scoped prompts, citation enforcement
  graph_check.py      blast-radius snapshot, diff, stale detection
  databricks_sync.py  Delta tables + Genie queries (local SQLite mirror fallback)
  pipeline.py         orchestration
  api.py              FastAPI + static hosting (token-gated)
  cli.py              witness <command>
frontend/             dashboard (no build step, no bundler)
fixtures/             recorded command output — checkpoint-time state
fixtures-head/        recorded command output — HEAD state, for the stale demo
fixtures-redacted/    a checkpoint arriving with fields stripped
```

---

## The six agents

| Agent | Prop | What it does |
|---|---|---|
| **Auditor** | magnifying glass | Maps requirements to diff and test evidence, labels each `satisfied` / `partial` / `unverified` / `contradicted` / `redacted` |
| **Riskbot** | shield | Blast radius, test status and stale assumptions into one evidence-linked score |
| **Ledgerkeep** | open ledger | Source, confidence, owner and expiry per assumption |
| **Scribe** | envelope | Handoff / resume packet |
| **Sleuth** | flashlight | Unfinished work and unresolved requirements |
| **Warden** | lock | Enforces redaction, blocks raw transcripts leaving the machine, marks context partial |

One orchestrator with six labelled output sections — not six processes. Each has
an original flat-geometry mascot sharing one silhouette, differentiated by its
prop. The Referee (goal-drift comparator) predates the privacy boundary and
stays available from the CLI and API.

---

## Security

- **Secret redaction is a write gate, not a pass.** `ledger.write` calls
  `assert_clean` on the serialised record and raises rather than committing a
  detectable secret. Redaction is one-way: a sha256 fingerprint is kept so the
  same secret is recognisable across records, the value never is.
- **The audit-ledger's git history is the tamper-evidence chain.** `git fsck`
  recomputes every object's hash from content, so a file edited in place after
  commit no longer hashes to its name. `witness verify` runs that live; the
  dashboard's CHAIN pill calls the same endpoint.
- **The ledger is gitignored from this repo.** Committing it here would make its
  history rewritable from the app repo and void the claim.
- **The API is gated.** Set `WITNESS_ACCESS_TOKEN` and every `/api` route
  requires `X-Witness-Token` (compared with `secrets.compare_digest`). `serve`
  refuses to bind a non-loopback host without it.
- **The handoff packet has a redacted mode**, which withholds decision
  rationale — least-privilege for context sharing, not just readability.
- **Redaction is an egress rule, not a storage rule.** The local ledger keeps
  the full transcript; only what crosses a service boundary is stripped.
  Confusing the two would destroy the audit trail.

---

## Environment

| Variable | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | Persona reasoning. Absent → verdict-free mode. |
| `WITNESS_ACCESS_TOKEN` | Gates the API. Required to bind a non-loopback host. |
| `WITNESS_ENTIRE_BIN` | Path to the Entire binary (default `entire`). |
| `WITNESS_FIXTURES` | Recorded-output directory for replay (default `fixtures`). |
| `WITNESS_RECORD` | `1` to save live output as fixtures. |
| `DATABRICKS_SERVER_HOSTNAME` / `DATABRICKS_HTTP_PATH` / `DATABRICKS_TOKEN` | Delta backend. All three absent → local SQLite mirror. |
| `DATABRICKS_CATALOG` / `DATABRICKS_SCHEMA` | Defaults `workspace` / `witness`. |

## Tests

```bash
cd backend && python3 -m pytest tests/ -q
```
