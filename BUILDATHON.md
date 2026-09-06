# Witness — Buildathon submission

**Track 1: Checkpoint-Native Developer Experience** · Databricks bonus category

> A small team of agents that turns Entire checkpoints and Entire Graph
> evidence into a verified, re-checkable release-readiness record — and catches
> when its own past claims have gone stale.

---

## The problem

An agent finishes a session and says "done". That claim is a paraphrase of its
own transcript, and nothing re-checks it afterwards. Two failures follow:

1. **The claim was never verified.** "Done" is asserted from the agent's own
   confident prose, not from the diff or the tests. A failing test the agent
   *believed* was a fixture issue reads as success.
2. **The claim goes stale silently.** Even a correct verdict decays. The symbol
   it was about acquires new callers; the code around it moves. Nobody re-runs
   the check, so a verdict from 09:12 is still trusted at 14:30.

Witness attacks both. Every verdict is bound to a command you can re-run, and
Witness re-runs its own past checks to find out which of its claims no longer
hold.

## What makes it different

Not a summarizer. Every screen shows a **verdict plus the command output it
came from**. Where evidence is missing, the label is `unverified` — the system
is built to say "I don't know" rather than produce a plausible answer.

---

## Use of Entire checkpoints

`backend/witness/entire_adapter.py` is the only place that shells out, and it
records each invocation as an **Evidence record**: argv, exit code, duration,
sha256 of raw stdout, and the redacted output.

```
entire checkpoint list --json
entire checkpoint explain <id> --json
entire checkpoint explain <id> --transcript
entire checkpoint search "query" --json
```

Capability is **probed, not assumed** — the adapter runs `entire <sub> --help`
and checks the response. A missing subcommand produces an Evidence record with
status `unavailable`, which propagates to `unverified` labels. It never raises,
and it never fabricates.

The checkpoint transcript is what the Auditor reads to recover original intent,
and what the Ghost reads to recover abandoned work — the context that
disappears when a session closes.

## Use of Entire Graph

```
entire graph impact --repo . --symbol NAME --depth 2
entire graph search --repo . --query "task"
entire graph neighbors --repo . --symbol NAME
```

These return formatted text, not JSON. We do **not** regex-parse them for the
verdict — the raw text goes to the Claude call, because "satisfied vs.
contradicted" is a semantic judgement. What we extract structurally is only the
unambiguous surface (file paths, identifiers), which gives a deterministic
"something moved" trigger.

**The stale-claim check** (`graph_check.py`) is the differentiating mechanism:

1. At checkpoint time, capture the blast radius and store it in the ledger.
2. On demand, re-run the identical command at current HEAD.
3. Diff the two radii.
4. `holds` (green) / `stale` (amber) / `contradicted` (red).

The severity floor is arithmetic, not opinion: the structural delta is computed
deterministically, and the model layer can escalate but never quietly downgrade
a real change back to green.

### A real catch from this build

Checkpoint `cp_7f3a91` claimed a verdict about `validate_token`. Its blast
radius at checkpoint time had four callers, all inside `auth/` and `api/`. A
re-check at HEAD returned:

```
STATUS : stale
REASON : 1 file(s) and 4 symbol(s) left the blast radius since the checkpoint
         - callers this claim relied on are gone
GONE   : is_revoked, login_handler, refresh_session, require_auth  (api/routes.py)
NEW    : impersonate_user                                          (admin/console.py)
```

`admin/console.py -> impersonate_user` is a **new caller of an auth primitive
that the original verdict never covered**. That is precisely the class of drift
that a one-shot "done" hides, and it surfaced from re-running a command rather
than from anyone thinking to look.

## Use of checkpoints as a working practice

The audit-ledger (`ledger.py`) is a **second, separate git repo** — never the
app repo, never Entire's own checkpoint branch. Every meaningful update is a
commit, so git's hash chain is the audit trail with no custom crypto.

```
audit-ledger/
  checkpoints/  requirements/  assumptions/  risk-reports/
  graph-snapshots/  evidence/  handoffs/  referee/  CHANGELOG.md
```

`witness verify` runs `git fsck`, which recomputes every object's hash from its
content — so a record edited in place after commit no longer hashes to its
name. The dashboard's CHAIN pill calls the same endpoint live.

## Databricks integration

Structural, not bolted on. The ledger mirrors into Delta tables
(`checkpoints`, `requirements`, `assumptions`, `risk_reports`,
`graph_snapshots`) and **the dashboard reads its analytics from those tables**,
not from the ledger files.

Genie queries shipped, with the SQL pre-written so the demo has a guaranteed
path:

- *Show checkpoints with unvalidated security assumptions*
- *Which verified claims have gone stale since their checkpoint?*
- *What is the release-readiness trend across checkpoints?*
- *Which requirements are contradicted or unverified?*

Designed around Free Edition limits: one 2X-Small serverless warehouse, no GPU,
no provisioned throughput. **Reasoning never routes through Databricks model
serving** — that would be a quota risk mid-demo. A local SQLite mirror with the
identical schema and identical query text runs the same code path, so the
dashboard stays demoable if the warehouse is asleep.

## The agent cast

Six personas with role-scoped prompts over one orchestrator — no multi-process
agent infra, because there is no rubric credit for it.

| Persona | Reads | Outputs |
|---|---|---|
| The Auditor | transcript, diff, tests | per-requirement status + evidence link |
| The Watchman | Auditor, blast radius, secret scan | readiness score, severity-banded risks |
| The Archivist | transcript | assumptions with confidence, owner, expiry, decay |
| The Messenger | all of the above | resume packet, redacted/full toggle |
| The Ghost | transcript | hypothesis → attempt → why rejected |
| The Referee | two Auditor runs | pre/post-curveball goal drift |

Each has a distinct card with its own verdict format. The Ghost's card is Ghost
Cyan. The Referee's card is locked until 12:00 — it is the curveball answer
mechanism and there is nothing real to show before the constraint lands.

## Security

- Redaction is a **write gate**: `ledger.write` calls `assert_clean` on the
  serialised record and raises rather than commit a detectable secret. Named
  provider patterns plus an entropy sweep, with git shas allow-listed so the
  report stays readable.
- One-way: a sha256 fingerprint identifies a secret across records; the value
  is never stored.
- The audit-ledger is gitignored from the app repo — committing it would make
  its history rewritable from here and void the tamper-evidence claim.
- The dashboard is token-gated (`WITNESS_ACCESS_TOKEN`), and `serve` refuses a
  non-loopback bind without it. This data includes reasoning traces.
- Security-relevant risk gets its own band in the Watchman's report, separate
  from generic test-failure risk.

---

## Honest status

- The `entire` binary is **not installed in this build container**, and the
  public npm `entire-cli` (0.0.3) ships `enable/status/rewind/explain` with no
  `checkpoint` or `graph` subcommands. The adapter targets the documented
  Buildathon surface and probes for it at runtime.
- On this machine the system therefore runs in **replay mode** against recorded
  fixtures, and the UI says so rather than implying live capture. On a machine
  with the Buildathon build, the same code path runs live — `--record` captures
  fixtures as it goes.
- Without `ANTHROPIC_API_KEY` the personas return structurally valid but
  **verdict-free** output. That is the designed degradation, not a bug.
- The Referee has not run against a real curveball yet — the constraint arrives
  at 12:00. Its mechanism and prompt are built and its card is wired; the
  comparison will be generated from the actual constraint.

## Demo path

```bash
PYTHONPATH=backend python3 -m witness.cli init
PYTHONPATH=backend python3 -m witness.cli serve --port 8000
```

1. **CHAIN** pill → `git fsck` verifies the ledger live.
2. Select checkpoint `cp_7f3a91` → **RUN FULL AUDIT**.
3. Graph panel: symbol `validate_token` → **CAPTURE SNAPSHOT**.
4. Restart with `WITNESS_FIXTURES=fixtures-head` (the repo as it stands at HEAD)
   → **RE-CHECK AT HEAD** → the claim goes **stale**, with `impersonate_user`
   named as the new caller.
5. **SYNC** disc → ledger mirrors into the tables; run a Genie query.
6. Click any evidence id → the drawer shows the exact command, its sha256, and
   its raw output.

## Tests

`cd backend && python3 -m pytest tests/ -q` — 32 tests. The ones that matter:
`test_no_usable_evidence_means_no_verdict`,
`test_hallucinated_citation_downgrades_auditor_finding`,
`test_ledger_redacts_before_commit`, `test_unusable_evidence_never_yields_a_verdict`.
