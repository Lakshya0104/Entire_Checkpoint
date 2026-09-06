# Witness — Buildathon submission

**Track 1: Checkpoint-Native Developer Experience** · Databricks bonus category

> A small team of agents that turns Entire checkpoints and Entire Graph evidence
> into a verified, re-checkable release-readiness record — and catches when its
> own past claims have gone stale.

---

## The curveball response, in the three terms that matter

### What assumption changed

Before the privacy boundary, the Auditor recovered intent by **shipping the raw
checkpoint transcript to the Claude API** and asking the model to read it. The
Databricks mirror carried the same shape of data: checkpoint summaries,
requirement rationales, evidence quotes — prose lifted from the transcript.

Both rested on one unexamined assumption:

> *The transcript may leave the machine, because the model needs it to
> understand what was asked.*

The curveball says it may not. Two things in this stack are external services —
the Databricks integration and the Claude API call — and neither may receive a
raw transcript or raw prompt text again.

### How the design changed

**Intent extraction moved on-machine.** `privacy.extract_requirements` parses
the transcript locally and produces structured requirement text. The model now
judges *structured claims* against *structured evidence*; it never sees the
prose. On the real fixture this recovers all five requirements the model used to
read directly:

```
- Add refresh-token rotation
- Requirements: every refresh must issue a new token AND revoke the previous one
- rotation must be atomic
- the existing /login contract must not change
- add tests
```

**One shared boundary, not two.** Both external paths call `privacy.redact()`.
There are exactly two egress points, and that is checked rather than assumed —
a blast-radius pass before the edit confirmed `format_evidence` is called only
by `AgentRunner.run`, and `_collect_rows` only by `sync`.

**The allowlist is structural.** `redact()` builds a new object out of named
fields instead of filtering an existing one, so a field nobody anticipated
cannot ride along by default. A second gate, `assert_no_raw_text`, runs on the
finished payload immediately before it leaves — belt and braces, because "that
should be unreachable" is exactly the assumption that breaks quietly.

**Delta tables lost their free-text columns.** `checkpoints.summary`,
`requirements.rationale` and `requirements.evidence_quote` are gone. What
remains is structured: requirement text, status, evidence reference, context
state. The Genie queries were rewritten against the new schema.

### Why the new result is safe

Safety here is not "we tried to strip the sensitive parts". It is three
properties that hold whether or not the model cooperates:

1. **Nothing raw can leave.** The allowlist is a construction rule, not a
   filter, and a second gate re-checks the finished payload. Tests assert that
   a transcript, a rationale and a checkpoint summary all fail to cross —
   including through the `extra` argument callers pass to a persona.

2. **Incomplete context cannot masquerade as complete.** Every persona's output
   carries the redaction report that produced it, and every report screen shows
   a **Full context** / **Partial — N fields redacted** badge at the top. It is
   a badge, not a tooltip: a reader never has to hover to learn they are looking
   at a partial answer.

3. **A partial input cannot yield an authoritative number.** `_apply_partial_context`
   enforces the downgrade **in code**, after the model replies. Under partial
   context an affirming label (`satisfied`, `partial`) becomes `redacted`, and
   the Riskbot's numeric score becomes `null` with verdict `unverified` — the
   original number is preserved as `score_withheld_from` for the audit trail,
   never presented as a measurement. A policy this important cannot depend on
   the model's compliance.

One deliberate asymmetry: `contradicted` **survives** redaction. Proving
something is broken does not require complete context, and suppressing bad news
is the dangerous direction to fail in. `test_partial_context_does_not_suppress_contradiction`
pins that down.

### This reuses the existing evidence-labeling system

`redacted` is a fifth value in the vocabulary the Auditor already used —
`satisfied` / `partial` / `unverified` / `contradicted` — not a parallel privacy
mode bolted alongside it. That is the honest modelling: a claim resting on
withheld input is a claim we are not permitted to verify, which is a statement
the label system already knew how to make. Every consumer that already handled
`unverified` — the risk score, the Databricks status column, the UI tags —
handles `redacted` with no special case.

### Local functionality is untouched

Redaction is an **egress** rule, not a storage rule. The audit-ledger on the
operator's machine still holds the full transcript; confusing the two would
destroy the audit trail the product exists to provide. The ledger, the secret
scan, `git fsck` verification and graph staleness detection all run with no
external call at all. Two tests hold that line:
`test_fully_local_path_needs_no_external_service` and
`test_local_ledger_still_stores_full_context`.

---

## The problem this solves

An agent finishes a session and says "done". That claim is a paraphrase of its
own transcript, and nothing re-checks it afterwards. Two failures follow:

1. **The claim was never verified.** "Done" is asserted from the agent's own
   confident prose, not from the diff or the tests.
2. **The claim goes stale silently.** Even a correct verdict decays. The symbol
   it was about acquires new callers; nobody re-runs the check.

Witness attacks both. Every verdict is bound to a command you can re-run, and
Witness re-runs its own past checks to find which of its claims no longer hold.

---

## Use of Entire checkpoints

`entire_adapter.py` is the only place that shells out, and it records each
invocation as an **Evidence record**: argv, exit code, duration, sha256 of raw
stdout.

```
entire checkpoint list --json
entire checkpoint explain <id> --json
entire checkpoint explain <id> --transcript
entire checkpoint search "query" --json
```

Capability is **probed, not assumed** — a missing subcommand produces status
`unavailable`, which propagates to `unverified`. It never fabricates.

## Use of Entire Graph

```
entire graph impact --repo . --symbol NAME --depth 2
entire graph search --repo . --query "task"
entire graph neighbors --repo . --symbol NAME
```

**The stale-claim check** is the differentiating mechanism: capture the blast
radius at checkpoint time, re-run the identical command at HEAD, diff the two.
The structural delta is computed deterministically, so the model layer can
escalate a verdict but never quietly downgrade a real change back to green.

### A real catch

Checkpoint `cp_7f3a91` asserted a verdict about `validate_token`. Re-running the
identical command at HEAD returned:

```
STATUS : stale
REASON : 1 file(s) and 4 symbol(s) left the blast radius since the checkpoint
         - callers this claim relied on are gone
GONE   : is_revoked, login_handler, refresh_session, require_auth  (api/routes.py)
NEW    : impersonate_user                                          (admin/console.py)
```

`admin/console.py -> impersonate_user` is a **new caller of an auth primitive
that the original verdict never covered** — exactly the drift a one-shot "done"
hides.

The graph view is interactive and fed from this output: nodes are files and
symbols, colour is the Risk Engine's own severity band (there is no second
colour scale), size is impact radius, and clicking a node re-runs impact for
that symbol and isolates its blast radius.

## Use of checkpoints as a practice

The audit-ledger is a **second, separate git repo** — never the app repo. Every
update is a commit, so git's hash chain is the audit trail with no custom
crypto. `witness verify` runs `git fsck`, which recomputes every object's hash
from content, so a record edited after commit no longer hashes to its name.

## Databricks

Delta tables mirrored from the ledger, and the dashboard reads its analytics
from **those tables**, not from the ledger files. Genie queries shipped with
pre-written SQL, including one added by the curveball — *"Which reports were
built on redacted context?"*

Designed around Free Edition limits. **Reasoning never routes through Databricks
model serving** — no provisioned throughput means a quota risk mid-demo. A local
SQLite mirror with identical schema and identical query text runs the same code
path so the dashboard survives a sleeping warehouse.

## The six agents

| Agent | Prop | What it does |
|---|---|---|
| Auditor | magnifying glass | Maps requirements to diff and test evidence, labels each |
| Riskbot | shield | Blast radius + test status + stale assumptions into one evidence-linked score |
| Ledgerkeep | open ledger | Source, confidence, owner, expiry per assumption |
| Scribe | envelope | Handoff / resume packet |
| Sleuth | flashlight | Unfinished work and unresolved requirements |
| Warden | lock | **New from the curveball.** Enforces redaction, blocks raw transcripts leaving the machine, marks context partial |

One orchestrator with six labelled output sections — not six processes. Mascots
are original flat-geometry artwork sharing one silhouette, differentiated only
by each agent's prop.

---

## Honest status

- The `entire` binary is **not installed in this container**, and the public npm
  `entire-cli` (0.0.3) ships `enable/status/rewind/explain` with no `checkpoint`
  or `graph` subcommands. The adapter targets the documented Buildathon surface
  and probes for it at runtime; here it replays recorded fixtures, and the UI
  says so rather than implying live capture.
- Step 1.4 of the spec — fully quit and relaunch the session — **could not be
  performed**. This session runs in a managed remote container and cannot
  restart itself. The intent behind it was honoured differently: context was
  reconstructed from `CHECKPOINTS.md` and the checkpoint transcript rather than
  from memory, and the pre-edit blast-radius pass was run before any file was
  touched.
- Step 1.6 ran, but `entire graph impact` returned `unavailable` for every
  symbol. Rather than skip the step, `tools/blast_radius.py` performed an
  AST-based caller analysis over this repo — narrower than Entire Graph and
  labelled as such, but enough to establish that the redaction boundary has
  exactly two egress points.
- Without `ANTHROPIC_API_KEY` the personas return structurally valid but
  **verdict-free** output. That is the designed degradation.
- The Referee (goal-drift comparator) predates the curveball and is not one of
  the six cast members the spec names. It remains available from the CLI and API.

## Tests

`cd backend && python3 -m pytest tests/ -q` — 70 tests.

The ones that carry the curveball:

| Test | Asserts |
|---|---|
| `test_redact_builds_from_allowlist_not_a_denylist` | An unanticipated field cannot ride along |
| `test_transcript_never_crosses` | Raw transcript is withheld |
| `test_allowlists_exclude_every_forbidden_key` | The two lists cannot contradict each other |
| `test_databricks_rows_carry_no_prose` | Summary and rationale do not reach the warehouse |
| `test_riskbot_score_is_nulled_under_partial_context` | No averaged number over redacted input |
| `test_partial_context_does_not_suppress_contradiction` | Bad news survives redaction |
| `test_redacted_fixture_does_not_crash` | (1) no crash |
| `test_redacted_fixture_report_is_labeled_incomplete` | (2) labelled incomplete |
| `test_redacted_fixture_presents_nothing_derived_as_verified` | (3) nothing derived from missing data is verified |

## Demo path

```bash
PYTHONPATH=backend python3 -m witness.cli init
PYTHONPATH=backend python3 -m witness.cli serve --port 8000
```

1. **Verify ledger** — `git fsck` runs live over the commit chain.
2. Pick `cp_7f3a91`, then **Run risk check**. Watch the context badge.
3. Graph: `validate_token` → **Capture snapshot**. Restart with
   `WITNESS_FIXTURES=fixtures-head` → **Re-check at HEAD** → **stale**, with
   `impersonate_user` named. Click a node to isolate its blast radius.
4. **Sync** disc → tables; ask *"Which reports were built on redacted context?"*
5. Run against `fixtures-redacted/` to see every screen flip to
   **Partial — N fields redacted** and the score refuse to be a number.
