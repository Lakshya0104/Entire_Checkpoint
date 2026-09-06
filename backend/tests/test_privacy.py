"""Tests for the privacy boundary introduced by the Track 1 curveball.

Two things are being defended here:

  1. No raw transcript or prompt text reaches an external service - the
     Databricks mirror or the Claude API. There are exactly two egress points
     and both call through `privacy.redact`.
  2. A report built on withheld context is never presented as authoritative.
     `redacted` extends the existing evidence-labeling vocabulary rather than
     introducing a parallel privacy mode.
"""

import json
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
ROOT = Path(__file__).resolve().parents[2]

from witness.agents import AgentRunner, evidence_to_facts, _apply_partial_context
from witness.databricks_sync import DatabricksSync, TABLES
from witness.entire_adapter import Evidence
from witness.ledger import AuditLedger
from witness.pipeline import Witness
from witness.privacy import (
    ALLOWED_FIELDS, FORBIDDEN_KEYS, RedactionReport, STATUS_REDACTED,
    assert_no_raw_text, downgrade_for_partial, extract_requirements,
    extract_test_results, redact, score_is_presentable, summarise_diff,
)

TRANSCRIPT = """\
[09:12:04] user: Add refresh-token rotation. Requirements: every refresh must
issue a new token AND revoke the previous one; rotation must be atomic; the
existing /login contract must not change; add tests.

[09:44:10] assistant: Tests: 11 pass, 1 fails - test_rotation_revokes_old_token.
"""


# ── the boundary itself ──────────────────────────────────────────────

def test_redact_builds_from_allowlist_not_a_denylist():
    """A field nobody thought about must not ride along by default."""
    payload = {"requirement_text": "must revoke", "some_future_field": "leak me"}
    out, report = redact(payload, destination="claude")
    assert out == {"requirement_text": "must revoke"}
    assert "some_future_field" in report.withheld


def test_transcript_never_crosses():
    out, report = redact({"transcript": TRANSCRIPT}, destination="claude")
    assert out == {}
    assert "transcript" in report.withheld
    assert TRANSCRIPT not in json.dumps(out)


def test_unknown_destination_is_refused():
    with pytest.raises(ValueError, match="undefined boundary"):
        redact({"requirement_text": "x"}, destination="some-new-vendor")


@pytest.mark.parametrize("key", ["transcript", "stdout", "prompt", "rationale"])
def test_assert_no_raw_text_catches_forbidden_keys(key):
    with pytest.raises(ValueError, match="privacy boundary"):
        assert_no_raw_text({"a": {"b": {key: "anything"}}}, where="test")


def test_assert_no_raw_text_passes_structured_payload():
    assert_no_raw_text(
        {"facts": [{"requirement_text": "x", "test_passed": 3}]}, where="test")


def test_allowlists_exclude_every_forbidden_key():
    """The two lists must not contradict each other."""
    for dest, allowed in ALLOWED_FIELDS.items():
        overlap = set(allowed) & set(FORBIDDEN_KEYS)
        assert not overlap, f"{dest} allows forbidden key(s): {overlap}"


# ── on-machine intent extraction ─────────────────────────────────────

def test_requirements_extracted_locally():
    reqs, report = extract_requirements(TRANSCRIPT)
    texts = [r["requirement_text"] for r in reqs]
    assert any("revoke the previous one" in t for t in texts)
    assert any("atomic" in t for t in texts)
    assert not report.partial


def test_wrapped_lines_do_not_sever_a_requirement():
    """Transcripts hard-wrap; a line break is not a clause boundary."""
    reqs, _ = extract_requirements(TRANSCRIPT)
    assert any("issue a new token" in r["requirement_text"] for r in reqs)


def test_missing_transcript_reports_partial_not_empty_success():
    reqs, report = extract_requirements("")
    assert reqs == []
    assert report.partial
    assert "transcript" in report.missing


def test_test_results_extracted_as_structure():
    out, _ = extract_test_results(TRANSCRIPT)
    assert out["test_passed"] == 11
    assert out["test_failed"] == 1
    assert "test_rotation_revokes_old_token" in out["test_names"]


def test_diff_summary_carries_no_diff_body():
    summary, _ = summarise_diff(" auth/session.py | 12 ++++----\n api/routes.py | 3 +-\n")
    assert summary["files_touched"] == ["auth/session.py", "api/routes.py"]
    assert "++++" not in json.dumps(summary)


# ── never present incomplete as authoritative ────────────────────────

def test_partial_context_downgrades_affirming_labels():
    partial = RedactionReport(withheld=["transcript"])
    assert downgrade_for_partial("satisfied", partial) == STATUS_REDACTED
    assert downgrade_for_partial("partial", partial) == STATUS_REDACTED


def test_partial_context_does_not_suppress_contradiction():
    """Failing safe means keeping bad news, not hiding it."""
    partial = RedactionReport(withheld=["transcript"])
    assert downgrade_for_partial("contradicted", partial) == "contradicted"


def test_full_context_leaves_labels_alone():
    full = RedactionReport()
    assert downgrade_for_partial("satisfied", full) == "satisfied"


def test_score_is_withheld_when_any_input_is_non_affirming():
    assert score_is_presentable(["satisfied", "partial"])
    assert not score_is_presentable(["satisfied", "redacted"])
    assert not score_is_presentable(["unverified"])


def test_riskbot_score_is_nulled_under_partial_context():
    """The model is told to do this; the code makes it true regardless."""
    data = _apply_partial_context(
        "riskbot", {"score": 88, "verdict": "ship"},
        RedactionReport(withheld=["transcript"]))
    assert data["score"] is None
    assert data["verdict"] == "unverified"
    assert data["score_withheld_from"] == 88


def test_auditor_findings_downgrade_under_partial_context():
    data = _apply_partial_context(
        "auditor",
        {"requirements": [{"text": "x", "status": "satisfied"}]},
        RedactionReport(missing=["transcript"]))
    req = data["requirements"][0]
    assert req["status"] == STATUS_REDACTED
    assert req["downgraded_from"] == "satisfied"


def test_badge_text():
    assert RedactionReport().badge() == "Full context"
    assert RedactionReport(withheld=["a"], missing=["b"]).badge() == \
        "Partial — 2 fields redacted"
    # singular, because a product about precision should not say "1 fields"
    assert RedactionReport(withheld=["a"]).badge() == "Partial — 1 field redacted"


# ── what actually leaves for the Claude API ──────────────────────────

def _transcript_evidence():
    return Evidence(
        "ev_t", ["entire", "checkpoint", "explain", "cp1", "--transcript"],
        "ok", 0, TRANSCRIPT, "", "sha", 5, "t", ".")


def test_facts_carry_requirements_but_not_the_transcript():
    facts, report = evidence_to_facts([_transcript_evidence()])
    blob = json.dumps(facts)
    assert "requirement_text" in blob
    assert "assistant:" not in blob
    assert "Tests: 11 pass" not in blob
    assert not report.partial


def test_outbound_payload_passes_the_final_gate():
    facts, _ = evidence_to_facts([_transcript_evidence()])
    assert_no_raw_text({"facts": facts}, where="test")


def test_runner_refuses_to_send_raw_extra():
    """`extra` crosses the boundary too, so it is redacted like everything else."""
    runner = AgentRunner(api_key=None)
    res = runner.run("auditor", [_transcript_evidence()],
                     extra={"transcript": TRANSCRIPT, "status": ["satisfied"]})
    assert "transcript" in res.redaction["withheld"]


# ── what actually leaves for Databricks ──────────────────────────────

def test_databricks_schema_declares_no_free_text_columns():
    for table, cols in TABLES.items():
        names = {n for n, _ in cols}
        bad = names & {"summary", "rationale", "evidence_quote", "detail", "text"}
        assert not bad, f"{table} still has free-text column(s): {bad}"


def test_databricks_rows_carry_no_prose(tmp_path):
    led = AuditLedger(tmp_path / "ledger")
    led.init()
    led.write("checkpoints", "cp1",
              {"id": "cp1", "branch": "main", "summary": "SENSITIVE PROSE"},
              message="m")
    led.write("requirements", "cp1", {
        "checkpoint_id": "cp1",
        "requirements": [{"id": "r1", "text": "must revoke", "status": "redacted",
                          "evidence_id": "ev1", "rationale": "SENSITIVE REASONING"}],
    }, message="m")

    sync = DatabricksSync(led, local_db=tmp_path / "m.db")
    assert sync.sync().ok
    dump = json.dumps(sync.query("SELECT * FROM requirements")) \
        + json.dumps(sync.query("SELECT * FROM checkpoints"))
    assert "SENSITIVE PROSE" not in dump
    assert "SENSITIVE REASONING" not in dump
    assert "must revoke" in dump          # structured field still lands


# ── the redacted fixture (spec 2e) ───────────────────────────────────

REDACTED_FIXTURES = ROOT / "fixtures-redacted"


@pytest.fixture()
def redacted_run(tmp_path):
    ledger = tmp_path / "ledger"
    if ledger.exists():
        shutil.rmtree(ledger)
    w = Witness(ROOT, ledger, fixture_dir=REDACTED_FIXTURES)
    return w, w.run_all("cp_redacted")


def test_redacted_fixture_does_not_crash(redacted_run):
    """(1) it doesn't crash."""
    _, out = redacted_run
    assert out["ingest"]["checkpoint_id"] == "cp_redacted"
    assert out["ledger_head"]


def test_redacted_fixture_report_is_labeled_incomplete(redacted_run):
    """(2) the report is labeled incomplete."""
    _, out = redacted_run
    assert out["context"]["context_state"] == "partial"
    assert out["context"]["badge"].startswith("Partial")
    assert out["audit"]["result"]["redaction"]["partial"] is True


def test_redacted_fixture_presents_nothing_derived_as_verified(redacted_run):
    """(3) no field derived from missing data is presented as verified."""
    _, out = redacted_run
    reqs = out["audit"]["result"]["data"].get("requirements", [])
    assert all(r.get("status") not in ("satisfied", "partial") for r in reqs)
    # A risk score over redacted input is never a number.
    assert out["watch"]["result"]["data"].get("score") is None
    assert out["watch"]["result"]["data"].get("verdict") == "unverified"


def test_redacted_fixture_still_syncs_and_marks_context(redacted_run):
    w, out = redacted_run
    assert out["databricks"]["ok"]
    rows = w.databricks.query("SELECT context_state FROM checkpoints")["rows"]
    assert rows and all(r["context_state"] in ("full", "partial") for r in rows)


# ── local functionality is unaffected (spec 2c) ──────────────────────

def test_fully_local_path_needs_no_external_service(tmp_path):
    """Ledger, secret scan and graph staleness run with no egress at all."""
    led = AuditLedger(tmp_path / "ledger")
    led.init()
    led.write("checkpoints", "cp1", {"id": "cp1", "branch": "main"}, message="m")
    assert led.verify_chain()["ok"]
    assert led.read("checkpoints", "cp1")["id"] == "cp1"


def test_local_ledger_still_stores_full_context(tmp_path):
    """Redaction is an egress rule, not a local storage rule.

    The operator's own machine keeps everything; only what crosses the boundary
    is stripped. Confusing the two would make the audit trail useless.
    """
    led = AuditLedger(tmp_path / "ledger")
    led.init()
    led.write("checkpoints", "cp1",
              {"id": "cp1", "transcript": "the full local transcript"}, message="m")
    assert "the full local transcript" in json.dumps(led.read("checkpoints", "cp1"))
